# Pesquisa multifonte

Coleta explícita além do `arxiv_miner.py`, que permanece compatível. O JSON de papers continua com `title`, `published`, `updated`, `authors`, `link`, `pdf_url` e `summary`. Proveniência, código e evidência ficam no manifesto.

## Fontes

| Fonte | Estado | Credencial | Verificação nesta mudança |
| --- | --- | --- | --- |
| arxiv | funcional, API pública `https://export.arxiv.org/api/query` | nenhuma | testes com fixture; `live_verified=false` |
| semantic_scholar | funcional, `GET /graph/v1/paper/search` | opcional: `SEMANTIC_SCHOLAR_API_KEY` | testes com fixture; `live_verified=false` |
| github | funcional só com repositório explícito ou consulta que é um único id arXiv/DOI | opcional: `GITHUB_TOKEN` | testes com fixture; `live_verified=false` |
| official_docs | funcional para URLs https explícitas e a homepage de um repositório já vinculado | nenhuma | testes com fixture; sem crawl |
| google_scholar | unsupported, sem API oficial | nenhuma | nenhum request |
| papers_with_code | unsupported; a API oficial em `paperswithcode.com/api/v1` foi descontinuada em julho de 2025 e um request direto responde 302 para `https://huggingface.co/papers/trending` | nenhuma | checagem limitada do host, sem coleta de papers |

`429`, credencial ausente, credencial rejeitada, timeout e resultado vazio são estados diferentes. Link, heurística, texto de autor e reprodução independente não recebem `production_verified`. Este coletor não certifica execução (`certifies_running=false`) e não decide elegibilidade de TCC nem ativa produção.

## Comando único depois do merge

Não faz parte do CI. Quem executa é o Analyst, uma vez, no SHA mergeado, com os caminhos ainda ausentes:

```bash
python3 -m multisource \
  --topic 1 \
  --limit 1 \
  --sources arxiv,semantic_scholar \
  --export-json results/topic1-limit1-papers.json \
  --export-manifest results/topic1-limit1-manifest.json
```

O assunto `1` usa a consulta já definida em `topics.json`: `cat:cs.LG OR cat:cs.AI`. Saída de sucesso: exit 0, os dois status em `ok` ou `empty`, arquivo de papers criado nessa execução e `papers_sha256` impresso e gravado no manifesto. Exit diferente de 0 não grava o JSON de papers. PDF não é baixado. O briefing, se pedido com `--export-briefing`, não contém o abstract.

## CI

O workflow `.github/workflows/tests.yml` dispara em `pull_request` com `contents: read` e roda:

```bash
MULTISOURCE_OFFLINE=1 PYTHONPATH=. python3 -m unittest discover -s tests
```

Um teste local verde não é o resultado do GitHub Actions.
