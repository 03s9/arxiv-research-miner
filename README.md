# arXiv Research Topic Miner 🚀

> **Languages**: [English](#-english-documentation) | [Português](#-documentação-em-português)

---

## 🇺🇸 English Documentation

A lightweight, zero-dependency Python tool to discover, monitor, and harvest recent academic papers from the **arXiv API** for graduate research, master's theses, and literature reviews.

### 🌟 Features

- **Zero External Dependencies**: Powered purely by Python's Standard Library (`urllib`, `xml.etree`, `argparse`).
- **Configurable Topic Templates**: Define custom research fields, queries, and keyword combinations via `topics.json`.
- **Multiple Sorting Modes**:
  - `submittedDate` (default): Catch brand-new preprints published in the last few days.
  - `relevance`: Focus on the most relevant papers matching your query.
  - `lastUpdatedDate`: Track revised versions of existing papers.
- **Direct PDF Links**: Extracts abstract links and direct `.pdf` download URLs.
- **Export Ready**: Save results directly to structured Markdown (`--export-md`) or JSON (`--export-json`).
- **Privacy & Security**: Zero telemetry, no API tokens needed, no personal data tracked.

---

### 📁 Repository Structure

```text
arxiv-research-miner/
├── arxiv_miner.py      # Core CLI tool & arXiv API parser
├── topics.json         # Configurable topic templates (boilerplate)
├── tests/
│   └── test_miner.py   # Unit tests
├── .gitignore
├── LICENSE             # MIT License
└── README.md
```

---

### ⚡ Quickstart & Practical Examples

#### 1. View Available Pre-configured Topics
```bash
python3 arxiv_miner.py
```
**Output Example**:
```text
=== Available Boilerplate Topics ===
  [1] Machine Learning & AI: General machine learning, deep learning architectures, and artificial intelligence
  [2] Computer Vision: Image processing, object detection, and generative vision models
  [3] Distributed & Parallel Computing: Cloud systems, cluster orchestration, and high-performance computing
  [4] Cryptography & Security: Information security, cryptography, privacy, and system robustness
  [5] Software Engineering: Software architecture, testing, refactoring, and developer tools
```

#### 2. Fetch Recent Papers by Topic ID
Fetch the 3 most recent papers for Topic `1` (*Machine Learning & AI*):
```bash
python3 arxiv_miner.py --topic 1 --limit 3
```
**Output Example**:
```text
Fetching 3 papers for: Machine Learning & AI (cat:cs.LG OR cat:cs.AI)...

--- Results (3 papers) ---

[1] SwarmEvolve: Latent Agent Swarms for Evolutionary Discovery
    Date: 2026-11-05 | Authors: Qile Jiang, George Em Karniadakis
    Link: http://arxiv.org/abs/2611.08483v1 | PDF: https://arxiv.org/pdf/2611.08483v1
    Abstract: LLM-guided evolutionary search uses AI agents to improve programs for mathematical and computational problems...
```

#### 3. Custom Query (Keyword + Category)
Search papers mentioning *"approximate nearest neighbor"* or *"vector"* in Distributed Computing (`cs.DC`):
```bash
python3 arxiv_miner.py --query 'cat:cs.DC AND (abs:"vector" OR abs:"approximate nearest neighbor")' --limit 5
```

#### 4. Exporting Reports to Markdown or JSON
Generate an academic report file ready for Obsidian, Notion, or your thesis notes:
```bash
# Export to Markdown report
python3 arxiv_miner.py --topic 4 --limit 10 --export-md results/security_papers.md

# Export to JSON for data processing
python3 arxiv_miner.py --query 'cat:cs.CR AND abs:"federated"' --export-json results/federated.json
```

---

### ⚙️ Customizing Topics (`topics.json`)

Edit `topics.json` to define your own categories and search queries:

```json
[
  {
    "id": "1",
    "title": "Edge AI & Quantization",
    "query": "cat:cs.LG AND (abs:\"quantization\" OR abs:\"edge computing\")",
    "description": "Efficient inference and low-bit models on edge devices"
  }
]
```

#### Common arXiv Subject Classes:
| Tag | Field |
| :--- | :--- |
| `cs.LG` | Machine Learning |
| `cs.AI` | Artificial Intelligence |
| `cs.DC` | Distributed, Parallel & Cluster Computing |
| `cs.CR` | Cryptography & Security |
| `cs.DB` | Databases & Data Engineering |
| `cs.CV` | Computer Vision |
| `cs.SE` | Software Engineering |

---

### 🧪 Unit Tests

Run the test suite using standard `unittest`:
```bash
PYTHONPATH=. python3 -m unittest discover -s tests
```

---
---

## 🇧🇷 Documentação em Português

Ferramenta CLI minimalista em Python (sem dependências externas) para minerar, filtrar e monitorar artigos científicos recentes da **API oficial do arXiv** para pesquisa acadêmica, dissertação de mestrado e levantamento de estado da arte.

---

### 🌟 Destaques

- **Zero Dependências**: Executa diretamente com a biblioteca padrão do Python (`urllib`, `xml.etree`, `argparse`). Não precisa de `pip install`.
- **Boilerplate Configurável**: Cadastre temas, palavras-chave e categorias no arquivo `topics.json`.
- **Ordenação por Recência**: Parâmetro `submittedDate` configurado para trazer artigos submetidos nos últimos dias.
- **Links Diretos para PDF**: Extrai tanto o link do resumo (abstract) quanto o link direto para download do `.pdf`.
- **Exportação Estruturada**: Gera relatórios automáticos em Markdown (`--export-md`) ou JSON (`--export-json`).
- **Segurança & Privacidade**: Sem rastreamento de dados pessoais, sem necessidade de tokens/chaves de API.

---

### ⚡ Guia Rápido com Exemplos

#### 1. Listar os Temas Cadastrados
```bash
python3 arxiv_miner.py
```

#### 2. Buscar Papers por ID de Tema
Busca os 3 artigos mais recentes do Tema `3` (*Distributed & Parallel Computing*):
```bash
python3 arxiv_miner.py --topic 3 --limit 3
```

#### 3. Realizar Busca Personalizada via Linha de Comando
Busca artigos recentes sobre *Large Language Models* em Sistemas Distribuídos:
```bash
python3 arxiv_miner.py --query 'cat:cs.DC AND (abs:"LLM" OR abs:"distributed training")' --limit 5
```

#### 4. Exportar Relatório para Markdown
Gera um arquivo `.md` estruturado para leitura no Obsidian ou Notion:
```bash
python3 arxiv_miner.py --topic 1 --limit 10 --export-md relatorio_ia.md
```

#### 5. Exportar Dados em JSON para Scripts
```bash
python3 arxiv_miner.py --topic 4 --limit 20 --export-json dados_seguranca.json
```

---

### ⚙️ Como Customizar Temas (`topics.json`)

Basta abrir o arquivo `topics.json` e adicionar ou alterar os blocos de configuração:

```json
[
  {
    "id": "1",
    "title": "Bancos Vetoriais & SysML",
    "query": "cat:cs.DC AND (abs:\"vector database\" OR abs:\"approximate nearest neighbor\")",
    "description": "Pesquisa em escalabilidade e particionamento de índices vetoriais para IA"
  }
]
```

---

### 🧪 Executar Testes Automatizados

Valide o parser XML e a leitura de configurações:
```bash
PYTHONPATH=. python3 -m unittest discover -s tests
```

---

## 📄 Licença

Distribuído sob licença MIT. Veja [`LICENSE`](LICENSE) para mais informações.
