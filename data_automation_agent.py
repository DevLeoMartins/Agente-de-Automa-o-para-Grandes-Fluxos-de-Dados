"""
Agente de Automação para Grandes Fluxos de Dados (Antigravity Stack)

Pipeline: carregar arquivo -> limpar -> transformar -> salvar.

Formatos suportados: CSV, XML, SQL (SQLite), DOC/DOCX, Markdown (.md).

Melhorias em relação à versão inicial (veja commits_log.md):
- aceita números no formato brasileiro ("2,50", "R$ 1.234,56") em vez de quebrar;
- detecta separador (, ; tab |) e encoding (UTF-8 ou Latin-1/Excel) automaticamente;
- processa arquivos grandes em blocos (--chunksize) sem estourar a memória,
  removendo duplicatas também ENTRE blocos;
- escreve o resultado de forma atômica (nunca deixa um arquivo pela metade nem
  sobrescreve uma saída boa com uma saída vazia);
- CLI com argparse, mensagens de erro claras e código de saída útil para agendadores;
- suporte a múltiplos formatos de entrada: CSV, XML, SQLite (.db/.sqlite),
  DOC/DOCX e Markdown (.md).

Uso:
    python data_automation_agent.py                       # usa o CSV de exemplo
    python data_automation_agent.py vendas.csv -o saida.csv --chunksize 100000
    python data_automation_agent.py dados.xml -o saida.csv
    python data_automation_agent.py banco.db -o saida.csv --tabela vendas
    python data_automation_agent.py relatorio.docx -o saida.csv
    python data_automation_agent.py notas.md -o saida.csv
"""
from __future__ import annotations

import argparse
import codecs
import csv
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pandas as pd

# Logger do módulo. A configuração (formato/nível) é feita só em main(), para que
# importar este arquivo como biblioteca não altere o logging de quem o importa.
log = logging.getLogger("data_automation_agent")

BASE_DIR = Path(__file__).resolve().parent
ENTRADA_PADRAO = BASE_DIR / "dados_entrada.csv"
SAIDA_PADRAO = BASE_DIR / "dados_processados.csv"

# Colunas que devem ser numéricas e não-negativas (se existirem no arquivo).
COLUNAS_NUMERICAS = ("valor", "quantidade")

_AMOSTRA_BYTES = 64 * 1024  # trecho lido para detectar encoding e separador


@dataclass
class Resumo:
    """Estatísticas do processamento (devolvidas por processar_arquivo)."""

    linhas_lidas: int = 0
    descartadas_nulas: int = 0       # vazias, ou número inválido (ex.: "abc")
    descartadas_negativas: int = 0   # valor/quantidade < 0
    duplicadas: int = 0
    linhas_saida: int = 0
    total_geral: float | None = None  # soma da coluna 'total', se calculada

    def __str__(self) -> str:
        total = "" if self.total_geral is None else f" | soma de 'total': {self.total_geral:,.2f}"
        return (
            f"lidas={self.linhas_lidas} | nulas/inválidas={self.descartadas_nulas} | "
            f"negativas={self.descartadas_negativas} | duplicadas={self.duplicadas} | "
            f"gravadas={self.linhas_saida}{total}"
        )


# --------------------------------------------------------------------------
# Leitura - novos formatos (XML, SQLite, DOC/DOCX, Markdown)
# --------------------------------------------------------------------------

# Extensões reconhecidas como CSV (incluindo TSV / texto delimitado genérico).
_EXT_CSV = {".csv", ".tsv", ".txt"}
_EXT_XML = {".xml"}
_EXT_SQL = {".db", ".sqlite", ".sqlite3"}
_EXT_DOC = {".doc", ".docx"}
_EXT_MD = {".md", ".markdown"}

FORMATOS_SUPORTADOS = _EXT_CSV | _EXT_XML | _EXT_SQL | _EXT_DOC | _EXT_MD


def _detectar_formato(caminho: Path) -> str:
    """Retorna o tipo do arquivo a partir da extensão."""
    ext = caminho.suffix.lower()
    if ext in _EXT_CSV:
        return "csv"
    if ext in _EXT_XML:
        return "xml"
    if ext in _EXT_SQL:
        return "sql"
    if ext in _EXT_DOC:
        return "doc"
    if ext in _EXT_MD:
        return "md"
    # Extensão desconhecida: tenta como CSV (comportamento anterior).
    return "csv"


def carregar_xml(caminho: Path) -> pd.DataFrame:
    """
    Lê um arquivo XML e devolve um DataFrame.

    Espera-se uma estrutura do tipo:
        <dados>
            <registro> <produto>Caneta</produto> ... </registro>
        </dados>

    :raises ValueError: se o XML não contiver registros tabulares.
    """
    caminho = Path(caminho)
    if not caminho.is_file():
        raise FileNotFoundError(f"Arquivo XML não encontrado: {caminho}")
    log.info("Lendo arquivo XML: %s", caminho)
    try:
        df = pd.read_xml(caminho, dtype=str)
    except Exception as exc:
        raise ValueError(f"Não foi possível ler o XML como tabela: {exc}") from exc
    if df.empty:
        raise ValueError(f"O arquivo XML não contém registros: {caminho}")
    log.info("XML carregado: %d linhas, %d colunas.", len(df), df.shape[1])
    return df


def carregar_sql(caminho: Path, tabela: str | None = None) -> pd.DataFrame:
    """
    Lê uma tabela de um banco SQLite e devolve um DataFrame.

    :param tabela: nome da tabela (se None, usa a primeira tabela encontrada).
    :raises ValueError: banco vazio ou tabela inexistente.
    """
    caminho = Path(caminho)
    if not caminho.is_file():
        raise FileNotFoundError(f"Banco de dados não encontrado: {caminho}")
    log.info("Abrindo banco SQLite: %s", caminho)
    con = sqlite3.connect(str(caminho))
    try:
        tabelas = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        if not tabelas:
            raise ValueError(f"O banco de dados não contém tabelas: {caminho}")
        nome = tabela if tabela else tabelas[0]
        if nome not in tabelas:
            raise ValueError(
                f"Tabela '{nome}' não encontrada. Tabelas disponíveis: {tabelas}"
            )
        log.info("Lendo tabela '%s'...", nome)
        df = pd.read_sql_query(f'SELECT * FROM "{nome}"', con, dtype=str)
    finally:
        con.close()
    if df.empty:
        raise ValueError(f"A tabela '{nome}' está vazia.")
    log.info("SQL carregado: %d linhas, %d colunas.", len(df), df.shape[1])
    return df


def carregar_doc(caminho: Path) -> pd.DataFrame:
    """
    Lê um arquivo DOC/DOCX e extrai a primeira tabela como DataFrame.

    Requer a biblioteca ``python-docx`` (apenas .docx; .doc legado não é suportado
    diretamente - recomenda-se converter para .docx ou .csv).

    :raises ValueError: sem tabelas no documento.
    """
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "Para ler arquivos .docx instale python-docx: pip install python-docx"
        ) from exc

    caminho = Path(caminho)
    if not caminho.is_file():
        raise FileNotFoundError(f"Arquivo DOC/DOCX não encontrado: {caminho}")
    log.info("Lendo documento DOCX: %s", caminho)
    doc = Document(str(caminho))
    if not doc.tables:
        raise ValueError(f"O documento não contém tabelas: {caminho}")

    tabela = doc.tables[0]
    dados = [[cell.text.strip() for cell in row.cells] for row in tabela.rows]
    if len(dados) < 2:
        raise ValueError(f"A tabela do documento não contém dados (só cabeçalho ou vazia): {caminho}")
    df = pd.DataFrame(dados[1:], columns=dados[0], dtype=str)
    log.info("DOCX carregado: %d linhas, %d colunas.", len(df), df.shape[1])
    return df


def _parse_md_tabela(texto: str) -> pd.DataFrame | None:
    """
    Extrai a primeira tabela Markdown (formato pipe) do texto.

    Exemplo de tabela:
        | produto | valor | quantidade |
        |---------|-------|------------|
        | Caneta  | 2.50  | 10         |
    """
    linhas = texto.splitlines()
    inicio = None
    for i, linha in enumerate(linhas):
        # Uma linha de tabela MD tem pelo menos um "|" no meio do texto.
        stripped = linha.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
            if inicio is None:
                inicio = i
        else:
            if inicio is not None:
                break
    if inicio is None:
        return None

    fim = i if inicio is not None and not (linhas[i].strip().startswith("|")) else len(linhas)
    # Pega as linhas da tabela
    tbl_linhas = []
    for j in range(inicio, fim):
        stripped = linhas[j].strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            tbl_linhas.append(stripped)
        else:
            break

    if len(tbl_linhas) < 3:  # cabeçalho + separador + pelo menos 1 linha
        return None

    def _parse_row(row: str) -> list[str]:
        # Remove | das pontas e divide.
        return [c.strip() for c in row.strip("|").split("|")]

    cabecalho = _parse_row(tbl_linhas[0])
    # Pula a linha separadora (---|----|---)
    dados = [_parse_row(r) for r in tbl_linhas[2:]]
    return pd.DataFrame(dados, columns=cabecalho, dtype=str)


def carregar_md(caminho: Path) -> pd.DataFrame:
    """
    Lê um arquivo Markdown e extrai a primeira tabela pipe como DataFrame.

    :raises ValueError: nenhuma tabela encontrada no arquivo.
    """
    caminho = Path(caminho)
    if not caminho.is_file():
        raise FileNotFoundError(f"Arquivo Markdown não encontrado: {caminho}")
    log.info("Lendo arquivo Markdown: %s", caminho)
    texto = caminho.read_text(encoding="utf-8")
    df = _parse_md_tabela(texto)
    if df is None or df.empty:
        raise ValueError(f"Nenhuma tabela encontrada no arquivo Markdown: {caminho}")
    log.info("Markdown carregado: %d linhas, %d colunas.", len(df), df.shape[1])
    return df


def carregar_arquivo(caminho, tabela: str | None = None, sep=None, encoding=None) -> pd.DataFrame:
    """
    Ponto de entrada unificado: detecta o formato pelo sufixo e delega ao
    carregador correto. Para CSV, chama ``carregar_dados`` (detecção automática
    de separador e encoding). Para SQL, ``tabela`` indica qual tabela ler.

    :return: DataFrame com todas as colunas como texto (str).
    """
    caminho = Path(caminho)
    fmt = _detectar_formato(caminho)
    if fmt == "csv":
        return carregar_dados(caminho, sep=sep, encoding=encoding)
    if fmt == "xml":
        return carregar_xml(caminho)
    if fmt == "sql":
        return carregar_sql(caminho, tabela=tabela)
    if fmt == "doc":
        return carregar_doc(caminho)
    if fmt == "md":
        return carregar_md(caminho)
    return carregar_dados(caminho, sep=sep, encoding=encoding)


# --------------------------------------------------------------------------
# Leitura - CSV (original)
# --------------------------------------------------------------------------
def _detectar_encoding_e_separador(caminho: Path, encoding=None, sep=None):
    """Descobre encoding e separador olhando só o começo do arquivo."""
    with open(caminho, "rb") as f:
        amostra = f.read(_AMOSTRA_BYTES)

    if encoding is None:
        try:
            # Decoder incremental: tolera um caractere UTF-8 cortado no fim da amostra.
            codecs.getincrementaldecoder("utf-8-sig")().decode(amostra, final=False)
            encoding = "utf-8-sig"  # também remove o BOM que o Excel grava
        except UnicodeDecodeError:
            encoding = "latin-1"  # Excel/Windows antigo; nunca falha ao decodificar

    if sep is None:
        texto = codecs.getincrementaldecoder(encoding)(errors="replace").decode(amostra)
        try:
            sep = csv.Sniffer().sniff(texto, delimiters=",;\t|").delimiter
        except csv.Error:
            sep = ","
    return encoding, sep


def _abrir_leitor(caminho, sep=None, encoding=None, chunksize=None):
    """Valida o arquivo e devolve (DataFrame | iterador de DataFrames)."""
    caminho = Path(caminho)
    if not caminho.is_file():
        raise FileNotFoundError(f"Arquivo de entrada não encontrado: {caminho}")
    if caminho.stat().st_size == 0:
        raise ValueError(f"O arquivo está vazio: {caminho}")

    encoding, sep = _detectar_encoding_e_separador(caminho, encoding, sep)
    log.info("Lendo %s (encoding=%s, separador=%r)", caminho, encoding, sep)
    # dtype=str: nada é "adivinhado" por bloco (evita tipos diferentes entre
    # blocos); a conversão numérica é feita de forma controlada em limpar_dados.
    return pd.read_csv(caminho, sep=sep, encoding=encoding, dtype=str, chunksize=chunksize)


def carregar_dados(caminho_arquivo, sep=None, encoding=None) -> pd.DataFrame:
    """
    Carrega um CSV inteiro em memória (para arquivos grandes use processar_arquivo
    com chunksize).

    :param caminho_arquivo: str | Path - local do arquivo CSV.
    :param sep: separador; None = detectar automaticamente.
    :param encoding: encoding; None = UTF-8 com fallback para Latin-1.
    :return: DataFrame com todas as colunas como texto (str).
    :raises FileNotFoundError: arquivo inexistente.
    :raises ValueError: arquivo vazio (sem nem o cabeçalho).
    """
    try:
        dados = _abrir_leitor(caminho_arquivo, sep, encoding)
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"O arquivo não contém dados: {caminho_arquivo}") from exc
    log.info("Dados carregados com %d linhas e %d colunas.", len(dados), dados.shape[1])
    return dados


# --------------------------------------------------------------------------
# Limpeza
# --------------------------------------------------------------------------
def _para_numero(serie: pd.Series) -> pd.Series:
    """
    Converte texto para número aceitando formatos BR e US. Inválidos viram NaN.

        "2.50" -> 2.5      "2,50" -> 2.5       "R$ 1.234,56" -> 1234.56
        "1,234.56" -> 1234.56                  "abc" -> NaN

    Regra: com os dois símbolos, o ÚLTIMO é o decimal. Com só vírgula, ela é decimal
    (então "1,234" vale 1.234; milhar sem decimal precisa de ponto: "1.234").
    """
    # Sempre float64: tipo estável entre blocos (senão "10" seria int num bloco e
    # float em outro, e o hash usado na deduplicação entre blocos divergiria).
    if pd.api.types.is_numeric_dtype(serie):
        return pd.to_numeric(serie, errors="coerce").astype("float64")

    s = serie.astype("string").str.strip().str.replace(r"[^\d,.\-]", "", regex=True)
    tem_virgula = s.str.contains(",", regex=False).fillna(False).astype(bool)
    tem_ponto = s.str.contains(".", regex=False).fillna(False).astype(bool)
    virgula_e_decimal = (
        tem_virgula & (~tem_ponto | (s.str.rfind(",") > s.str.rfind("."))).fillna(False).astype(bool)
    )
    virgula_e_milhar = tem_virgula & ~virgula_e_decimal  # formato "1,234.56"

    s = s.mask(virgula_e_decimal, s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False))
    s = s.mask(virgula_e_milhar, s.str.replace(",", "", regex=False))
    return pd.to_numeric(s, errors="coerce").astype("float64")


def _deduplicar(df: pd.DataFrame, vistos: set | None) -> pd.DataFrame:
    """
    Remove linhas repetidas. Com `vistos` (modo em blocos) lembra o hash de cada linha
    já gravada para também eliminar duplicatas entre blocos diferentes; sem ele usa
    o drop_duplicates exato do pandas.
    """
    if vistos is None:
        return df.drop_duplicates()
    hashes = pd.util.hash_pandas_object(df, index=False)
    repetida = hashes.duplicated() | hashes.isin(vistos)
    vistos.update(hashes[~repetida].tolist())
    return df[~repetida]


def limpar_dados(df: pd.DataFrame, _vistos: set | None = None, _resumo: Resumo | None = None) -> pd.DataFrame:
    """
    Limpa os dados:
      1. remove espaços das pontas e trata texto vazio como nulo;
      2. converte 'valor' e 'quantidade' (se existirem) para número;
      3. descarta linhas com qualquer nulo ou número inválido;
      4. descarta valores negativos;
      5. remove duplicatas (depois da normalização: "Caneta " == "Caneta").

    :param df: DataFrame bruto (não é modificado).
    :return: novo DataFrame limpo.
    """
    resumo = _resumo if _resumo is not None else Resumo()
    log.info("Iniciando limpeza dos dados...")
    df = df.copy()

    texto = df.columns[~df.dtypes.map(pd.api.types.is_numeric_dtype).to_numpy()]
    for col in texto:
        df[col] = df[col].astype("string").str.strip().replace("", pd.NA)

    for col in COLUNAS_NUMERICAS:
        if col in df.columns:
            df[col] = _para_numero(df[col])

    antes = len(df)
    df = df.dropna()
    resumo.descartadas_nulas += antes - len(df)

    antes = len(df)
    for col in COLUNAS_NUMERICAS:
        if col in df.columns:
            df = df[df[col] >= 0]
    resumo.descartadas_negativas += antes - len(df)

    antes = len(df)
    df = _deduplicar(df, _vistos)
    resumo.duplicadas += antes - len(df)

    # Quantidades inteiras saem como "10" (e não "10.0"); fracionárias (ex.: 2.5 kg) seguem float.
    if "quantidade" in df.columns and not df.empty and (df["quantidade"] % 1 == 0).all():
        df["quantidade"] = df["quantidade"].astype("int64")

    log.info("Dados limpos, %d linhas restantes.", len(df))
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# Transformação e saída
# --------------------------------------------------------------------------
def transformar_dados(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cria a coluna calculada 'total' = valor * quantidade (arredondada a 2 casas, para
    evitar resíduos como 24.000000000000004). Sem as colunas, apenas avisa e segue.

    :param df: DataFrame limpo (não é modificado).
    """
    log.info("Transformando dados...")
    df = df.copy()
    if {"valor", "quantidade"} <= set(df.columns):
        df["total"] = (df["valor"].astype(float) * df["quantidade"].astype(float)).round(2)
    else:
        log.warning("Colunas 'valor' e 'quantidade' ausentes: coluna 'total' não calculada.")
    log.info("Transformação concluída.")
    return df


def salvar_dados(df: pd.DataFrame, caminho_saida) -> None:
    """Salva em CSV (UTF-8) de forma atômica: grava num .tmp e só então substitui."""
    caminho_saida = Path(caminho_saida)
    caminho_saida.parent.mkdir(parents=True, exist_ok=True)
    log.info("Salvando dados processados em: %s", caminho_saida)
    tmp = caminho_saida.with_name(caminho_saida.name + ".tmp")
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, caminho_saida)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("Arquivo salvo com sucesso.")


def processar_arquivo(
    entrada, saida, chunksize: int | None = None, sep=None, encoding=None,
    tabela: str | None = None,
) -> Resumo:
    """
    Executa o pipeline completo. Detecta o formato pelo sufixo do arquivo
    (CSV, XML, SQLite, DOCX, Markdown) e delega ao carregador correto.

    Com `chunksize` e formato CSV, lê e grava em blocos (memória
    constante em relação ao tamanho do arquivo; só os hashes das linhas gravadas
    ficam em memória, para detectar duplicatas entre blocos).

    :param tabela: para SQLite, nome da tabela a ler (None = primeira).
    :raises FileNotFoundError / ValueError: entrada inexistente, vazia ou sem nenhuma
        linha válida. Nesses casos a saída existente NÃO é alterada.
    """
    resumo = Resumo()
    saida = Path(saida)
    entrada = Path(entrada)
    saida.parent.mkdir(parents=True, exist_ok=True)
    tmp = saida.with_name(saida.name + ".tmp")

    fmt = _detectar_formato(entrada)

    # Formatos não-CSV: carrega de uma vez (não suportam chunksize).
    if fmt != "csv":
        if chunksize:
            log.warning("--chunksize ignorado para formato '%s'.", fmt)
        df = carregar_arquivo(entrada, tabela=tabela, sep=sep, encoding=encoding)
        resumo.linhas_lidas = len(df)
        limpo = transformar_dados(limpar_dados(df, None, resumo))
        if limpo.empty:
            raise ValueError(
                f"Nenhuma linha válida em {entrada} "
                f"({resumo.linhas_lidas} lidas). A saída existente foi preservada."
            )
        resumo.linhas_saida = len(limpo)
        if "total" in limpo.columns:
            resumo.total_geral = round(float(limpo["total"].sum()), 2)
        try:
            limpo.to_csv(tmp, index=False)
            os.replace(tmp, saida)
        finally:
            tmp.unlink(missing_ok=True)
        return resumo

    # Formato CSV: mantém lógica original com suporte a chunksize.
    try:
        leitor = _abrir_leitor(entrada, sep, encoding, chunksize)
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"O arquivo não contém dados: {entrada}") from exc
    blocos = leitor if chunksize else [leitor]  # sem chunksize: um único "bloco" (DataFrame)
    vistos: set | None = set() if chunksize else None
    soma = 0.0
    tem_total = False

    try:
        for bloco in blocos:
            resumo.linhas_lidas += len(bloco)
            limpo = transformar_dados(limpar_dados(bloco, vistos, resumo))
            if limpo.empty:
                continue
            if "total" in limpo.columns:
                tem_total = True
                soma += float(limpo["total"].sum())
            # Primeiro bloco com dados escreve o cabeçalho; os demais anexam.
            limpo.to_csv(tmp, index=False, mode="a" if resumo.linhas_saida else "w",
                         header=not resumo.linhas_saida)
            resumo.linhas_saida += len(limpo)

        if resumo.linhas_saida == 0:
            raise ValueError(
                f"Nenhuma linha válida em {entrada} "
                f"({resumo.linhas_lidas} lidas). A saída existente foi preservada."
            )
        os.replace(tmp, saida)
    finally:
        if chunksize:
            leitor.close()  # libera o arquivo de entrada (importante no Windows)
        tmp.unlink(missing_ok=True)

    resumo.total_geral = round(soma, 2) if tem_total else None
    return resumo


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _argumentos(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Agente de automação de dados. Suporta CSV, XML, SQLite (.db), DOCX e Markdown (.md)."
    )
    p.add_argument("entrada", nargs="?", default=str(ENTRADA_PADRAO),
                    help="Arquivo de entrada: CSV, XML, .db/.sqlite, .docx ou .md (padrão: exemplo CSV do projeto)")
    p.add_argument("-o", "--saida", default=str(SAIDA_PADRAO), help="CSV de saída (padrão: dados_processados.csv)")
    p.add_argument("--chunksize", type=int, default=None, metavar="N",
                   help="processa em blocos de N linhas (use para CSVs grandes)")
    p.add_argument("--sep", default=None, help="separador CSV (padrão: detectar)")
    p.add_argument("--encoding", default=None, help="encoding CSV (padrão: UTF-8, com fallback Latin-1)")
    p.add_argument("--tabela", default=None, help="nome da tabela SQL (padrão: primeira tabela do banco)")
    p.add_argument("-v", "--verbose", action="store_true", help="mostra detalhes e o traceback em caso de erro")
    args = p.parse_args(argv)
    if args.chunksize is not None and args.chunksize < 1:
        p.error("--chunksize deve ser >= 1")
    return args


def main(argv=None) -> int:
    """Ponto de entrada. Retorna 0 em sucesso e 1 em erro (use com cron/agendadores)."""
    args = _argumentos(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    try:
        resumo = processar_arquivo(
            args.entrada, args.saida, args.chunksize, args.sep, args.encoding,
            tabela=args.tabela,
        )
    except (FileNotFoundError, ValueError, pd.errors.ParserError, PermissionError, ImportError) as exc:
        if args.verbose:
            log.exception("Falha no processamento")
        else:
            log.error("%s", exc)
        return 1
    log.info("Concluído: %s", resumo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
