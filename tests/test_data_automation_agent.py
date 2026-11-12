"""
Testes do agente de automação. Usam só unittest (sem dependências extras):

    python -m unittest discover -s tests -v      # ou: pytest -v
"""
import io
import logging
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import data_automation_agent as agent  # noqa: E402

logging.disable(logging.CRITICAL)  # silencia logs nos testes


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.saida = self.dir / "saida.csv"

    def tearDown(self):
        self._tmp.cleanup()

    def csv(self, conteudo, nome="in.csv", encoding="utf-8"):
        p = self.dir / nome
        p.write_bytes(conteudo.encode(encoding))
        return p

    def rodar(self, conteudo, **kw):
        resumo = agent.processar_arquivo(self.csv(conteudo), self.saida, **kw)
        return resumo, pd.read_csv(self.saida)


class TestPipeline(Base):
    def test_exemplo_do_projeto(self):
        resumo, df = self.rodar(agent.ENTRADA_PADRAO.read_text(encoding="utf-8"))
        self.assertEqual(resumo.linhas_lidas, 5)
        self.assertEqual(resumo.duplicadas, 1)
        self.assertEqual(list(df["produto"]), ["Caneta", "Caderno", "Borracha", "Lápis"])
        self.assertEqual(list(df["total"]), [25.0, 75.0, 24.0, 45.0])
        self.assertEqual(resumo.total_geral, 169.0)

    def test_formatos_numericos_brasileiros_e_us(self):
        _, df = self.rodar(
            'produto,valor,quantidade\nA,"2,50",2\nB,"R$ 1.234,56",1\nC,"1,234.56",1\nD,3.5,2\n'
        )
        self.assertEqual(list(df["valor"]), [2.5, 1234.56, 1234.56, 3.5])

    def test_milhar_com_ponto_e_decimal_com_virgula(self):
        """1.234 é milhar (1234.0), enquanto 1,234 é decimal (1.234)."""
        _, df = self.rodar(
            'produto,valor,quantidade\nMilhar,1.234,1\nDecimal,"1,234",1\nMilharGrande,1.234.567,1\n'
        )
        self.assertEqual(list(df["valor"]), [1234.0, 1.234, 1234567.0])

    def test_rejeicao_valores_contaminados(self):
        """Valores com letras misturadas (12abc, 12a34) são rejeitados sem tentar adivinhar."""
        resumo, df = self.rodar(
            "produto,valor,quantidade\nOk,10,1\nContam1,12abc,1\nContam2,12a34,1\nContam3,R$ 12abc,1\n"
        )
        self.assertEqual(list(df["produto"]), ["Ok"])
        self.assertEqual(resumo.descartadas_nulas, 3)

    def test_caso_teste_valores_csv(self):
        """Teste específico reportado: ProdutoA com 1.234 e ProdutoB com 12abc."""
        resumo, df = self.rodar(
            "produto;valor;quantidade\nProdutoA;1.234;1\nProdutoB;12abc;1\n"
        )
        self.assertEqual(resumo.linhas_lidas, 2)
        self.assertEqual(resumo.descartadas_nulas, 1)
        self.assertEqual(resumo.linhas_saida, 1)
        self.assertEqual(list(df["produto"]), ["ProdutoA"])
        self.assertEqual(df["valor"][0], 1234.0)
        self.assertEqual(df["total"][0], 1234.0)

    def test_valores_invalidos_nulos_e_negativos_sao_descartados(self):
        resumo, df = self.rodar(
            "produto,valor,quantidade\nOk,1.0,1\nTexto,abc,1\nVazio,,2\n,3.0,2\nNeg,-5,1\nNegQ,5,-1\n"
        )
        self.assertEqual(list(df["produto"]), ["Ok"])
        self.assertEqual(resumo.descartadas_nulas, 3)
        self.assertEqual(resumo.descartadas_negativas, 2)

    def test_duplicatas_apos_normalizacao(self):
        resumo, df = self.rodar("produto,valor,quantidade\nCaneta,2.5,10\n  Caneta ,2.50,10\n")
        self.assertEqual(len(df), 1)
        self.assertEqual(resumo.duplicadas, 1)

    def test_total_arredondado(self):
        _, df = self.rodar("produto,valor,quantidade\nX,0.1,3\n")
        self.assertEqual(df["total"][0], 0.3)  # sem 0.30000000000000004

    def test_quantidade_inteira_sem_ponto_zero(self):
        self.rodar("produto,valor,quantidade\nX,1,10\n")
        self.assertIn(",10,", self.saida.read_text())

    def test_sem_colunas_valor_quantidade_nao_quebra(self):
        resumo, df = self.rodar("nome,idade\nAna,30\nAna,30\nBia,25\n")
        self.assertNotIn("total", df.columns)
        self.assertEqual(len(df), 2)
        self.assertIsNone(resumo.total_geral)

    def test_entrada_nao_e_modificada(self):
        bruto = pd.DataFrame({"produto": [" A "], "valor": ["1,5"], "quantidade": ["2"]})
        copia = bruto.copy()
        agent.transformar_dados(agent.limpar_dados(bruto))
        pd.testing.assert_frame_equal(bruto, copia)


class TestArquivos(Base):
    def test_separador_ponto_e_virgula_com_virgula_decimal(self):
        _, df = self.rodar("produto;valor;quantidade\nCaneta;2,50;10\nLápis;1,50;30\n")
        self.assertEqual(list(df["total"]), [25.0, 45.0])

    def test_encoding_latin1(self):
        p = self.csv("produto,valor,quantidade\nLápis,1.5,2\n", encoding="latin-1")
        agent.processar_arquivo(p, self.saida)
        self.assertEqual(pd.read_csv(self.saida)["produto"][0], "Lápis")

    def test_bom_utf8_do_excel(self):
        p = self.csv("\ufeffproduto,valor,quantidade\nA,1,1\n")
        agent.processar_arquivo(p, self.saida)
        self.assertIn("produto", pd.read_csv(self.saida).columns)

    def test_saida_em_pasta_inexistente(self):
        destino = self.dir / "novo" / "sub" / "out.csv"
        agent.processar_arquivo(self.csv("produto,valor,quantidade\nA,1,1\n"), destino)
        self.assertTrue(destino.is_file())

    def test_sem_arquivo_temporario_residual(self):
        self.rodar("produto,valor,quantidade\nA,1,1\n")
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])


class TestErros(Base):
    def test_arquivo_inexistente(self):
        with self.assertRaises(FileNotFoundError):
            agent.processar_arquivo(self.dir / "nao_existe.csv", self.saida)

    def test_arquivo_vazio(self):
        with self.assertRaises(ValueError):
            agent.processar_arquivo(self.csv(""), self.saida)

    def test_so_cabecalho_ou_sem_linhas_validas_preserva_saida_anterior(self):
        self.saida.write_text("ANTIGA", encoding="utf-8")
        for conteudo in ("produto,valor,quantidade\n", "produto,valor,quantidade\nX,abc,1\n"):
            with self.assertRaises(ValueError):
                agent.processar_arquivo(self.csv(conteudo), self.saida)
        self.assertEqual(self.saida.read_text(encoding="utf-8"), "ANTIGA")

    def test_main_retorna_codigos_de_saida(self):
        ok = self.csv("produto,valor,quantidade\nA,1,1\n")
        self.assertEqual(agent.main([str(ok), "-o", str(self.saida)]), 0)
        self.assertEqual(agent.main([str(self.dir / "x.csv"), "-o", str(self.saida)]), 1)

    def test_chunksize_invalido(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            agent._argumentos(["--chunksize", "0"])


class TestChunks(Base):
    def test_blocos_equivalem_ao_modo_normal_incluindo_duplicatas_entre_blocos(self):
        linhas = ["produto,valor,quantidade"]
        for i in range(25):
            linhas.append(f"P{i % 7},{i % 5 + 0.5},{i % 3 + 1}")   # muitas repetições
        linhas += ["Ruim,abc,1", "Neg,-1,1", ",1,1"]
        conteudo = "\n".join(linhas) + "\n"

        r1, normal = self.rodar(conteudo)
        saida_blocos = self.dir / "blocos.csv"
        r2 = agent.processar_arquivo(self.csv(conteudo), saida_blocos, chunksize=4)
        blocos = pd.read_csv(saida_blocos)

        pd.testing.assert_frame_equal(normal, blocos)
        for campo in ("linhas_lidas", "descartadas_nulas", "descartadas_negativas", "duplicadas", "linhas_saida"):
            self.assertEqual(getattr(r1, campo), getattr(r2, campo), campo)
        self.assertEqual(r1.total_geral, r2.total_geral)

    def test_bloco_inteiro_invalido_no_meio_nao_quebra(self):
        conteudo = "produto,valor,quantidade\nA,1,1\nB,x,1\nC,y,1\nD,2,2\n"
        r = agent.processar_arquivo(self.csv(conteudo), self.saida, chunksize=2)
        self.assertEqual(r.linhas_saida, 2)
        self.assertEqual(list(pd.read_csv(self.saida)["produto"]), ["A", "D"])


if __name__ == "__main__":
    unittest.main()


# ==========================================================================
# Testes dos novos formatos: XML, SQLite, DOCX, Markdown
# ==========================================================================

class TestXML(Base):
    """Testes para leitura de arquivos XML."""

    def _xml(self, conteudo, nome="dados.xml"):
        p = self.dir / nome
        p.write_text(conteudo, encoding="utf-8")
        return p

    def test_xml_basico(self):
        """Lê um XML com registros tabulares e processa corretamente."""
        xml = (
            '<?xml version="1.0"?>\n<dados>\n'
            "  <registro><produto>Caneta</produto><valor>2.50</valor><quantidade>10</quantidade></registro>\n"
            "  <registro><produto>Caderno</produto><valor>15.00</valor><quantidade>5</quantidade></registro>\n"
            "</dados>"
        )
        arq = self._xml(xml)
        resumo = agent.processar_arquivo(arq, self.saida)
        df = pd.read_csv(self.saida)
        self.assertEqual(resumo.linhas_lidas, 2)
        self.assertEqual(resumo.linhas_saida, 2)
        self.assertEqual(list(df["produto"]), ["Caneta", "Caderno"])
        self.assertEqual(list(df["total"]), [25.0, 75.0])
        self.assertEqual(resumo.total_geral, 100.0)

    def test_xml_com_duplicatas_e_negativos(self):
        """Limpa XML removendo duplicatas e valores negativos."""
        xml = (
            '<?xml version="1.0"?>\n<dados>\n'
            "  <registro><produto>A</produto><valor>1</valor><quantidade>2</quantidade></registro>\n"
            "  <registro><produto>A</produto><valor>1</valor><quantidade>2</quantidade></registro>\n"
            "  <registro><produto>B</produto><valor>-5</valor><quantidade>1</quantidade></registro>\n"
            "</dados>"
        )
        arq = self._xml(xml)
        resumo = agent.processar_arquivo(arq, self.saida)
        df = pd.read_csv(self.saida)
        self.assertEqual(len(df), 1)
        self.assertEqual(resumo.duplicadas, 1)
        self.assertEqual(resumo.descartadas_negativas, 1)

    def test_xml_arquivo_inexistente(self):
        """Erro ao tentar ler XML inexistente."""
        with self.assertRaises(FileNotFoundError):
            agent.processar_arquivo(self.dir / "nao_existe.xml", self.saida)


class TestSQL(Base):
    """Testes para leitura de bancos SQLite."""

    def _db(self, dados: dict, tabela="vendas", nome="dados.db"):
        p = self.dir / nome
        con = sqlite3.connect(str(p))
        df = pd.DataFrame(dados)
        df.to_sql(tabela, con, index=False, if_exists="replace")
        con.close()
        return p

    def test_sqlite_basico(self):
        """Lê tabela SQLite e processa corretamente."""
        arq = self._db({
            "produto": ["Caneta", "Caderno", "Borracha"],
            "valor": ["2.50", "15.00", "1.20"],
            "quantidade": ["10", "5", "20"],
        })
        resumo = agent.processar_arquivo(arq, self.saida)
        df = pd.read_csv(self.saida)
        self.assertEqual(resumo.linhas_lidas, 3)
        self.assertEqual(resumo.linhas_saida, 3)
        self.assertEqual(list(df["produto"]), ["Caneta", "Caderno", "Borracha"])
        self.assertAlmostEqual(resumo.total_geral, 124.0)

    def test_sqlite_com_tabela_especifica(self):
        """Seleciona tabela pelo nome quando há mais de uma."""
        p = self.dir / "multi.db"
        con = sqlite3.connect(str(p))
        pd.DataFrame({"nome": ["ignorar"]}).to_sql("lixo", con, index=False)
        pd.DataFrame({
            "produto": ["X"], "valor": ["5"], "quantidade": ["3"],
        }).to_sql("principal", con, index=False)
        con.close()
        resumo = agent.processar_arquivo(p, self.saida, tabela="principal")
        df = pd.read_csv(self.saida)
        self.assertEqual(list(df["produto"]), ["X"])
        self.assertEqual(df["total"][0], 15.0)

    def test_sqlite_tabela_inexistente_erro(self):
        """Erro ao pedir tabela que não existe."""
        arq = self._db({"a": [1]}, tabela="existe")
        with self.assertRaises(ValueError):
            agent.processar_arquivo(arq, self.saida, tabela="nao_existe")

    def test_sqlite_banco_vazio_erro(self):
        """Erro ao abrir banco sem tabelas."""
        p = self.dir / "vazio.db"
        sqlite3.connect(str(p)).close()
        with self.assertRaises(ValueError):
            agent.processar_arquivo(p, self.saida)


class TestDOCX(Base):
    """Testes para leitura de arquivos DOCX."""

    def _docx(self, linhas, nome="dados.docx"):
        from docx import Document
        doc = Document()
        colunas = linhas[0]
        tabela = doc.add_table(rows=len(linhas), cols=len(colunas))
        for i, row in enumerate(linhas):
            for j, val in enumerate(row):
                tabela.cell(i, j).text = val
        p = self.dir / nome
        doc.save(str(p))
        return p

    def test_docx_basico(self):
        """Lê tabela de um DOCX e processa corretamente."""
        arq = self._docx([
            ["produto", "valor", "quantidade"],
            ["Caneta", "2.50", "10"],
            ["Caderno", "15.00", "5"],
        ])
        resumo = agent.processar_arquivo(arq, self.saida)
        df = pd.read_csv(self.saida)
        self.assertEqual(resumo.linhas_lidas, 2)
        self.assertEqual(resumo.linhas_saida, 2)
        self.assertEqual(list(df["produto"]), ["Caneta", "Caderno"])
        self.assertEqual(resumo.total_geral, 100.0)

    def test_docx_sem_tabela_erro(self):
        """Erro ao ler DOCX sem tabelas."""
        from docx import Document
        doc = Document()
        doc.add_paragraph("Texto sem tabela.")
        p = self.dir / "sem_tabela.docx"
        doc.save(str(p))
        with self.assertRaises(ValueError):
            agent.processar_arquivo(p, self.saida)


class TestMarkdown(Base):
    """Testes para leitura de tabelas Markdown."""

    def _md(self, conteudo, nome="dados.md"):
        p = self.dir / nome
        p.write_text(conteudo, encoding="utf-8")
        return p

    def test_md_basico(self):
        """Lê tabela pipe de um Markdown e processa corretamente."""
        md = (
            "# Relatório\n\nDados do mês:\n\n"
            "| produto | valor | quantidade |\n"
            "|---------|-------|------------|\n"
            "| Caneta  | 2.50  | 10         |\n"
            "| Caderno | 15.00 | 5          |\n"
        )
        arq = self._md(md)
        resumo = agent.processar_arquivo(arq, self.saida)
        df = pd.read_csv(self.saida)
        self.assertEqual(resumo.linhas_lidas, 2)
        self.assertEqual(resumo.linhas_saida, 2)
        self.assertEqual(list(df["produto"]), ["Caneta", "Caderno"])
        self.assertEqual(resumo.total_geral, 100.0)

    def test_md_sem_tabela_erro(self):
        """Erro ao ler Markdown sem tabela."""
        arq = self._md("# Título\n\nApenas texto, sem tabela.\n")
        with self.assertRaises(ValueError):
            agent.processar_arquivo(arq, self.saida)

    def test_md_com_duplicatas(self):
        """Limpa Markdown removendo duplicatas."""
        md = (
            "| produto | valor | quantidade |\n"
            "|---------|-------|------------|\n"
            "| A       | 1     | 2          |\n"
            "| A       | 1     | 2          |\n"
            "| B       | 3     | 4          |\n"
        )
        arq = self._md(md)
        resumo = agent.processar_arquivo(arq, self.saida)
        df = pd.read_csv(self.saida)
        self.assertEqual(len(df), 2)
        self.assertEqual(resumo.duplicadas, 1)


class TestDeteccaoFormato(unittest.TestCase):
    """Testes da detecção de formato por extensão."""

    def test_extensoes_csv(self):
        for ext in (".csv", ".tsv", ".txt"):
            self.assertEqual(agent._detectar_formato(Path(f"f{ext}")), "csv")

    def test_extensoes_xml(self):
        self.assertEqual(agent._detectar_formato(Path("f.xml")), "xml")

    def test_extensoes_sql(self):
        for ext in (".db", ".sqlite", ".sqlite3"):
            self.assertEqual(agent._detectar_formato(Path(f"f{ext}")), "sql")

    def test_extensoes_doc(self):
        for ext in (".doc", ".docx"):
            self.assertEqual(agent._detectar_formato(Path(f"f{ext}")), "doc")

    def test_extensoes_md(self):
        for ext in (".md", ".markdown"):
            self.assertEqual(agent._detectar_formato(Path(f"f{ext}")), "md")

    def test_extensao_desconhecida_vira_csv(self):
        self.assertEqual(agent._detectar_formato(Path("f.xyz")), "csv")


class TestCLIFormatos(Base):
    """Testes do CLI com --tabela."""

    def test_main_com_sqlite_e_tabela(self):
        p = self.dir / "test.db"
        con = sqlite3.connect(str(p))
        pd.DataFrame({"produto": ["A"], "valor": ["1"], "quantidade": ["2"]}).to_sql(
            "minha_tabela", con, index=False
        )
        con.close()
        ret = agent.main([str(p), "-o", str(self.saida), "--tabela", "minha_tabela"])
        self.assertEqual(ret, 0)
        df = pd.read_csv(self.saida)
        self.assertEqual(list(df["produto"]), ["A"])


if __name__ == "__main__":
    unittest.main()
