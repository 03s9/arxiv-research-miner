# Hugging Face adiado

Este branch não faz parte do PR principal. Não baixa modelos, não usa `trust_remote_code` e não executa código de repositório. `live_verified=false`: o adaptador só tem testes com fixture. Não houve coleta ao vivo neste branch.

Endpoints oficiais usados no código, lidos em `https://huggingface.co/.well-known/openapi.md`:

- `GET /api/papers/search?q=&limit=`
- `GET /api/papers/{paperId}` com id `YYMM.NNNNN`

Uma sonda anterior, fora deste commit, fez `GET /api/papers/search?q=attention&limit=1` e recebeu HTTP 200 com uma lista JSON. O objeto aninhava `paper.id`. Esse corpo não foi salvo e não valida este adaptador.

Consulta no formato `cat:` do arXiv não é enviada. Sem id estável o registro é descartado. Um `githubRepo` sem revisão não vira identidade de código. Metadado não autoriza execução.

## Relatos de modelo

Pesquisa somente leitura em 2026-11-08 22:08 America/Sao_Paulo. Corpus incompleto, sem conector Reddit, sem corroboração primária (um fetch de notícia retornou 403). Não são prova de que o coletor principal está comprometido. Não são um recibo de aprovação.

- R1 https://www.reddit.com/r/LocalLLaMA/comments/13t2b67/security_psa_huggingface_models_are_code_not_just/ — `trust_remote_code` executa scripts do repositório; safetensors não fecha esse caminho.
- R2 https://www.reddit.com/r/KoboldAI/comments/1b3236a/security_statement_regarding_malicious_ai_models/ — comunicado da comunidade sobre modelos maliciosos, pickle e runpy.
- R3 https://www.reddit.com/r/netsec/comments/1b1fe3y/data_scientists_targeted_by_malicious_hugging/ — discussão de incidente com modelo backdoor; só título e corpo curto foram lidos.

Nenhum post revisado cita este projeto. Isso não prova ausência.
