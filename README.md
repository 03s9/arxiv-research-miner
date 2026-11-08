# arXiv Research Topic Miner 🚀

A lightweight, zero-dependency Python tool to discover, monitor, and harvest recent academic papers from the **arXiv API** for graduate research, master's theses, and literature reviews.

---

## 🌟 Features

- **Zero External Dependencies**: Runs entirely on the Python Standard Library (`urllib`, `xml.etree`, `argparse`).
- **Configurable Topic Boilerplate**: Customize your research topics and queries via `topics.json` without modifying code.
- **Multiple Sorting Modes**:
  - `submittedDate` (default): Catch brand-new preprints published in the last few days.
  - `relevance`: Focus on the most relevant papers matching your query.
  - `lastUpdatedDate`: Track revised versions of existing papers.
- **Direct PDF Links**: Extracts abstract links and direct `.pdf` URLs.
- **Export Ready**: Save results directly to structured Markdown (`--export-md`) or JSON (`--export-json`).
- **Privacy & Security**: Zero telemetry, no API tokens needed, no personal data tracked.

---

## 📁 Repository Structure

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

## ⚡ Quickstart

### 1. View Available Topics
```bash
python3 arxiv_miner.py
```

### 2. Fetch Papers for a Configured Topic
Fetch the 5 most recent papers for Topic ID `1`:
```bash
python3 arxiv_miner.py --topic 1 --limit 5
```

### 3. Run an Ad-Hoc Custom Query
Query arXiv directly using category and keyword syntax:
```bash
python3 arxiv_miner.py --query 'cat:cs.AI AND ti:"agent"' --limit 5
```

### 4. Export to Markdown or JSON
```bash
# Export to Markdown report
python3 arxiv_miner.py --topic 3 --limit 10 --export-md results/systems_papers.md

# Export to JSON
python3 arxiv_miner.py --query 'cat:cs.CR AND abs:"federated"' --export-json results/federated.json
```

---

## ⚙️ Customizing Topics (`topics.json`)

You can edit `topics.json` to configure your own research domains, keywords, and arXiv subject classifications:

```json
[
  {
    "id": "1",
    "title": "My Custom Research Field",
    "query": "cat:cs.LG AND (abs:\"benchmark\" OR ti:\"efficiency\")",
    "description": "Custom description of the research target"
  }
]
```

### Useful arXiv Subject Categories:
- `cs.LG`: Learning / Machine Learning
- `cs.AI`: Artificial Intelligence
- `cs.DC`: Distributed, Parallel, and Cluster Computing
- `cs.CR`: Cryptography and Security
- `cs.DB`: Databases
- `cs.CV`: Computer Vision and Pattern Recognition
- `cs.SE`: Software Engineering

---

## 🧪 Running Unit Tests

Verify the parser and test suite:
```bash
PYTHONPATH=. python3 -m unittest discover -s tests
```

---

## 🔒 Security & Best Practices

- **Rate Limits**: The arXiv API requests users to avoid submitting automated queries more frequently than once every 3 seconds. The tool is designed for on-demand topic queries.
- **Safe I/O**: Output paths are sanitized using standard `pathlib.Path` structures.
- **License**: MIT.
