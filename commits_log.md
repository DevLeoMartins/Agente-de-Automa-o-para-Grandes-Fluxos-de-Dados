# Registro de commits do projeto

## Commit 1: Inicialização do projeto
Criado o arquivo principal `data_automation_agent.py` e dependências.

## Commit 2: Configuração de logging
Adicionado logging avançado para monitoramento.

## Commit 3: Documentação e Exemplo CSV
Adicionado README completo e dados de entrada para testes.

## Commit 4: fix — dados reais não quebram mais o pipeline
- `"2,50"` / `"R$ 1,00"` / texto no lugar de número causavam `TypeError` em `transformar_dados`;
  agora são convertidos (formatos BR/US) ou descartados e contados.
- CSV com `;` era lido como 1 coluna e "dava certo" em silêncio; CSV em Latin-1 do Excel dava
  `UnicodeDecodeError`. Separador e encoding agora são detectados.
- Caminhos fixos relativos à pasta atual: padrões agora são relativos ao projeto e há CLI (argparse).
- Valores negativos agora são descartados; duplicatas comparadas após normalizar o texto.
- `df['total'] = ...` num DataFrame derivado (risco de `SettingWithCopyWarning`): agora trabalha em cópia.
- `total` arredondado (evita `0.30000000000000004`).

## Commit 5: feat — arquivos grandes e saída segura
- `--chunksize`: processamento em blocos com deduplicação entre blocos.
- Gravação atômica (.tmp + replace); sem linhas válidas preserva a saída anterior e retorna exit code 1.
- `logging.basicConfig` saiu do import (só roda em `main()`); logs com formatação lazy.

## Commit 6: test/docs — 20 testes e README reescrito
- `requirements.txt`: `pandas==2.1.0` fixo era incompatível com Python 3.12+ recente e o README
  dizia "Python 3.8+" (pandas 2.1 exige 3.9+). Agora `pandas>=2.1,<4`, Python 3.9+.
