# Pesquisa multifonte

Coleta explícita além do `arxiv_miner.py`, que permanece compatível. O comando do coletor é `python3 -m multisource`. Ele não depende do guardião. O JSON de papers continua com `title`, `published`, `updated`, `authors`, `link`, `pdf_url` e `summary`. Proveniência fica no manifesto, em outro arquivo.

```bash
python3 -m multisource \
  --topic 1 \
  --limit 1 \
  --sources arxiv,semantic_scholar,google_scholar,papers_with_code \
  --export-json results/topic1-papers.json \
  --export-manifest results/topic1-manifest.json
```

O tema `1` é o boilerplate `cat:cs.LG OR cat:cs.AI`. Não é um tema de TCC validado. Google Scholar e Papers with Code respondem `unsupported` e não são substituídos por Hugging Face. GitHub e documentos oficiais exigem um id arXiv/DOI, um `--github-repo`, ou um `--docs-url`. Um registro sem versão não une `v1` com `v2`.

## Fontes

| Fonte | Estado | Credencial | Verificação nesta mudança |
| --- | --- | --- | --- |
| arxiv | funcional, API pública `https://export.arxiv.org/api/query` | nenhuma | testes com fixture; `live_verified=false` |
| semantic_scholar | funcional, `GET /graph/v1/paper/search` | opcional: `SEMANTIC_SCHOLAR_API_KEY` | testes com fixture; `live_verified=false` |
| github | funcional só com repositório explícito ou consulta que é um único id arXiv/DOI | opcional: `GITHUB_TOKEN` | testes com fixture; `live_verified=false` |
| official_docs | funcional para URLs https explícitas e a homepage de um repositório já vinculado | nenhuma | testes com fixture; sem crawl |
| google_scholar | unsupported, sem API oficial | nenhuma | nenhum request |
| papers_with_code | unsupported. A API oficial acabou em julho de 2025. Um request direto pode redirecionar para `https://huggingface.co/papers/trending`. Esse redirect histórico não é um coletor Hugging Face e este código não chama `huggingface.co`. | nenhuma | sem coleta |

Não há verificação ao vivo nesta mudança (`live_verified=false`). Os testes são fixtures offline. Não houve smoke de provedor: o guardião não autoriza execução.

Hugging Face fica fora deste PR, no branch `feat/huggingface-deferred`. Relatos de modelo (`trust_remote_code`, pickle) pertencem a esse escopo e não são evidência de que este coletor de metadados está comprometido.

## Guardião e Reddit

`python3 -m multisource.guardian --check` inspeciona a árvore local (AST, permissões, symlink, encoding) sem DNS. Só depois disso a etapa Reddit é consultada. Nesta versão ela lê notas locais, não fala com o Reddit, e não aceita essas notas como recibo de aprovação. `--run` não inicia o coletor.

Notas em `security/reddit-notes-principal.json`: pesquisa somente leitura em 2026-11-08 22:08 America/Sao_Paulo, corpus incompleto, nenhum post revisado cita este projeto (isso não prova ausência). O post R5 sobre Actions de terceiros e tags mutáveis continua em aberto; os Actions deste repositório estão pinados por commit, e isso não é um atestado de segurança.

O comando direto `python3 -m multisource` não passa pelo guardião. Isso não é controle do sistema operacional.

Checagens de rede do cliente HTTP: literais IPv6 com NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`) e IPv4-compatible (`::/96`) são recusados sem socket. Cada conexão resolve uma vez e abre TCP no IP checado; não há segundo lookup pelo nome. Se qualquer endereço resolvido não for público, não conecta. Limites: um IP, sem tentar outro endereço, no máximo dois redirects, certificado verificado para o hostname. Isso não prova que o destino é benigno.

Identidade: o mesmo arXiv id une registros quando a versão não conflita, inclusive se só um lado tem DOI. `v1` e `v2` não se unem, mesmo com o mesmo DOI. `pdf_version_verified` exige id e versão exatos no URL observado; URL sintetizado pelo parser legado não conta. Bytes de PDF não são baixados. Dois caminhos de saída equivalentes, inclusive symlink, são recusados antes de escrever. Rejeições de homônimo do GitHub entram no manifesto só com `repo` e `reason`.

## Comando depois do merge

Não executar como coleta enquanto `--run` retornar pendente. O caminho documentado, que hoje bloqueia, é:

```bash
python3 -m multisource.guardian \
  --run \
  --root . \
  --policy security/trusted-policy.json \
  --expect-head <SHA_REVISADO> \
  -- \
  python3 -m multisource \
    --topic 1 \
    --limit 1 \
    --sources arxiv,semantic_scholar \
    --export-json results/topic1-limit1-papers.json \
    --export-manifest results/topic1-limit1-manifest.json
```

A política versionada traz `approved_head` nulo de propósito. Ela não se autoaprova com o HEAD atual.

## CI

`.github/workflows/tests.yml` usa `pull_request` e `contents: read`. O preflight `--check` roda antes de:

```bash
MULTISOURCE_OFFLINE=1 PYTHONPATH=. python3 -m unittest discover -s tests
```

O `--check` verde não autoriza execução nem substitui o resultado do Actions.
