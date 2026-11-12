# Agente de Automação para Grandes Fluxos de Dados 

Agente em Python que lê dados de **múltiplos formatos** (CSV, XML, SQLite, DOCX, Markdown),
limpa, calcula e grava um relatório processado em CSV.
Funciona com dados "do mundo real": números no formato brasileiro, CSV do Excel
(`;` e Latin-1) e arquivos grandes processados em blocos.

## Formatos Suportados

| Formato | Extensões | Detalhes |
|---------|-----------|----------|
| **CSV** | `.csv`, `.tsv`, `.txt` | Detecção automática de separador e encoding |
| **XML** | `.xml` | Registros tabulares (`<dados><registro>...`) |
| **SQLite** | `.db`, `.sqlite`, `.sqlite3` | Lê tabelas de bancos SQLite (`--tabela` para escolher) |
| **DOCX** | `.docx` | Extrai a primeira tabela do documento Word |
| **Markdown** | `.md`, `.markdown` | Extrai a primeira tabela pipe do arquivo |

## Funcionalidades

- **Carga multi-formato** com detecção automática do tipo pelo sufixo do arquivo.
- **CSV**: detecção automática de separador (`, ; tab |`) e encoding (UTF-8/BOM ou Latin-1).
- **XML**: leitura via `pd.read_xml` (requer `lxml`).
- **SQLite**: leitura de qualquer tabela do banco; sem `--tabela`, usa a primeira encontrada.
- **DOCX**: extrai a primeira tabela do documento Word (requer `python-docx`).
- **Markdown**: extrai a primeira tabela no formato pipe (`| col1 | col2 |`).
- **Limpeza**: remove espaços, linhas vazias/nulas, números inválidos, valores negativos e duplicatas
  (a comparação é feita *depois* de normalizar: `"Caneta "` = `"Caneta"`).
- **Números BR e US**: `2,50`, `R$ 1.234,56` e `1,234.56` são entendidos.
- **Transformação**: `total = valor × quantidade`, arredondado a 2 casas.
- **Arquivos grandes**: `--chunksize` processa CSVs em blocos (memória estável), removendo duplicatas
  também entre blocos. Testado com 200 mil linhas: saída idêntica à do modo normal.
- **Saída segura**: grava em arquivo temporário e só então substitui o destino; se não houver
  nenhuma linha válida, a saída anterior é **preservada** e o programa termina com erro.
- **Resumo** no fim (lidas, descartadas por motivo, duplicadas, gravadas, soma dos totais) e
  **código de saída** `0`/`1`, pronto para cron e agendadores.

## Requisitos

Python 3.9+ e dependências listadas em `requirements.txt`:

```bash
pip install -r requirements.txt
```

Dependências principais:
- `pandas` >= 2.1
- `lxml` >= 4.9 (para leitura de XML)
- `python-docx` >= 0.8 (para leitura de DOCX)

> **Nota:** SQLite e Markdown usam apenas a biblioteca padrão do Python + pandas.

## Como usar

```bash
pip install -r requirements.txt

# CSV (comportamento original)
python data_automation_agent.py                      # roda com o CSV de exemplo
python data_automation_agent.py vendas.csv -o saida/relatorio.csv
python data_automation_agent.py grande.csv --chunksize 100000

# XML
python data_automation_agent.py dados.xml -o saida.csv

# SQLite
python data_automation_agent.py banco.db -o saida.csv                    # primeira tabela
python data_automation_agent.py banco.db -o saida.csv --tabela vendas    # tabela específica

# DOCX
python data_automation_agent.py relatorio.docx -o saida.csv

# Markdown
python data_automation_agent.py notas.md -o saida.csv

# Ajuda
python data_automation_agent.py -h
```

| Opção | Descrição |
|-------|-----------|
| `entrada` | Arquivo de entrada: CSV, XML, .db/.sqlite, .docx ou .md (padrão: `dados_entrada.csv`) |
| `-o, --saida` | CSV de saída (padrão: `dados_processados.csv`); pastas são criadas se faltarem |
| `--chunksize N` | processa CSVs em blocos de N linhas |
| `--sep`, `--encoding` | forçam separador/encoding para CSV (padrão: detectar) |
| `--tabela` | nome da tabela SQLite a ler (padrão: primeira tabela do banco) |
| `-v, --verbose` | mais detalhes e traceback completo em caso de erro |

### Como biblioteca

```python
from data_automation_agent import processar_arquivo

# CSV
resumo = processar_arquivo("vendas.csv", "saida.csv", chunksize=50_000)

# XML
resumo = processar_arquivo("dados.xml", "saida.csv")

# SQLite
resumo = processar_arquivo("banco.db", "saida.csv", tabela="vendas")

# DOCX
resumo = processar_arquivo("relatorio.docx", "saida.csv")

# Markdown
resumo = processar_arquivo("notas.md", "saida.csv")

print(resumo)
```

Funções individuais também estão disponíveis:
- `carregar_arquivo(caminho, tabela=None)` — ponto de entrada unificado (detecta formato)
- `carregar_dados`, `carregar_xml`, `carregar_sql`, `carregar_doc`, `carregar_md`
- `limpar_dados`, `transformar_dados`, `salvar_dados`

## Exemplos de formatos de entrada

### XML
```xml
<?xml version="1.0"?>
<dados>
  <registro>
    <produto>Caneta</produto>
    <valor>2.50</valor>
    <quantidade>10</quantidade>
  </registro>
</dados>
```

### SQLite
Qualquer banco `.db` ou `.sqlite` com tabelas contendo colunas tabulares.

### DOCX
Documento Word com pelo menos uma tabela. A primeira tabela é extraída; a primeira
linha é usada como cabeçalho.

### Markdown
```markdown
| produto | valor | quantidade |
|---------|-------|------------|
| Caneta  | 2.50  | 10         |
```

## Regras que valem a pena conhecer

- Colunas esperadas: `produto`, `valor`, `quantidade`. Se `valor`/`quantidade` não existirem, o
  agente apenas limpa e avisa (não calcula `total`).
- Qualquer linha com campo vazio é descartada.
- Com só vírgula, ela é decimal: `1,234` vale `1.234`. Para milhar use ponto (`1.234`) ou o formato
  completo (`1.234,00`).

## Testes

```bash
python -m unittest discover -s tests -v     # sem dependências extras
```

39 testes cobrindo:
- **CSV** (20 testes): formatos numéricos, descartes, duplicatas, encodings, separadores, erros,
  preservação da saída anterior e equivalência entre modo normal e em blocos.
- **XML** (3 testes): leitura básica, limpeza com duplicatas/negativos, arquivo inexistente.
- **SQLite** (4 testes): leitura básica, seleção de tabela específica, tabela inexistente, banco vazio.
- **DOCX** (2 testes): leitura básica com tabela, documento sem tabela.
- **Markdown** (3 testes): leitura básica, remoção de duplicatas, arquivo sem tabela.
- **Detecção de formato** (6 testes): extensões CSV/XML/SQL/DOC/MD e extensão desconhecida.
- **CLI** (1 teste): `--tabela` com SQLite via linha de comando.

## Estrutura

```
data_automation_agent.py    # pipeline multi-formato + CLI
dados_entrada.csv           # exemplo CSV
requirements.txt            # dependências (pandas, lxml, python-docx)
tests/                      # testes unittest
commits_log.md              # histórico das mudanças
LICENSE                     # licença MIT
```

## Licença

Este projeto é distribuído sob a [Licença MIT](LICENSE) — livre para uso pessoal e comercial.
