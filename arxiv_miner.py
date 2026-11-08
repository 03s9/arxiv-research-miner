#!/usr/bin/env python3
"""
arXiv Research Topic Miner & Paper Harvester.
Zero-dependency CLI tool to query recent papers from arXiv API by customized topic catalogs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional

ARXIV_API_BASE_URL = "http://export.arxiv.org/api/query?"
DEFAULT_CONFIG_PATH = Path(__file__).parent / "topics.json"


@dataclass
class Paper:
    title: str
    published: str
    updated: str
    authors: List[str]
    link: str
    pdf_url: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_topics(config_path: Path) -> list[dict[str, str]]:
    """Loads topic templates from a JSON config file."""
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as exc:
        print(f"Error reading {config_path}: {exc}", file=sys.stderr)
    return []


def parse_arxiv_response(xml_bytes: bytes) -> list[Paper]:
    """Parses Atom XML response from arXiv API into Paper objects."""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_bytes)
    papers: list[Paper] = []

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        pub_el = entry.find("atom:published", ns)
        upd_el = entry.find("atom:updated", ns)
        id_el = entry.find("atom:id", ns)
        sum_el = entry.find("atom:summary", ns)

        title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else "Untitled"
        published = pub_el.text[:10] if pub_el is not None and pub_el.text else ""
        updated = upd_el.text[:10] if upd_el is not None and upd_el.text else ""
        link = id_el.text.strip() if id_el is not None and id_el.text else ""
        summary = sum_el.text.strip().replace("\n", " ") if sum_el is not None and sum_el.text else ""

        pdf_url = ""
        for link_el in entry.findall("atom:link", ns):
            if link_el.attrib.get("title") == "pdf":
                pdf_url = link_el.attrib.get("href", "")
                break
        if not pdf_url and link:
            pdf_url = link.replace("/abs/", "/pdf/") + ".pdf"

        authors: list[str] = []
        for author_el in entry.findall("atom:author", ns):
            name_el = author_el.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        papers.append(
            Paper(
                title=title,
                published=published,
                updated=updated,
                authors=authors,
                link=link,
                pdf_url=pdf_url,
                summary=summary,
            )
        )

    return papers


def fetch_papers(
    query: str,
    max_results: int = 5,
    sort_by: str = "submittedDate",
    sort_order: str = "descending",
    timeout_sec: int = 20,
) -> list[Paper]:
    """Queries arXiv API using GET request with proper headers and timeout."""
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    encoded_url = ARXIV_API_BASE_URL + urllib.parse.urlencode(params)
    headers = {"User-Agent": "ArXivTopicMiner/1.0 (+https://github.com)"}
    req = urllib.request.Request(encoded_url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = resp.read()
            return parse_arxiv_response(data)
    except urllib.error.URLError as err:
        print(f"Network error querying arXiv: {err}", file=sys.stderr)
        return []


def export_markdown(papers: list[Paper], output_path: Path, query_info: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# arXiv Papers Report: {query_info}\n",
        f"*Total results: {len(papers)}*\n",
    ]
    for idx, p in enumerate(papers, 1):
        auth_str = ", ".join(p.authors[:3]) + (" et al." if len(p.authors) > 3 else "")
        lines.append(f"## {idx}. [{p.title}]({p.link})")
        lines.append(f"- **Published**: {p.published} | **PDF**: [Download PDF]({p.pdf_url})")
        lines.append(f"- **Authors**: {auth_str}")
        lines.append(f"- **Abstract**: {p.summary}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Exported markdown to: {output_path}")


def export_json(papers: list[Paper], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in papers], f, indent=2, ensure_ascii=False)
    print(f"Exported JSON to: {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harvest and monitor recent papers from arXiv for academic research."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to topics.json configuration file",
    )
    parser.add_argument(
        "--topic",
        "-t",
        type=str,
        help="Topic ID from topics.json to execute",
    )
    parser.add_argument(
        "--query",
        "-q",
        type=str,
        help="Custom query string (overrides --topic)",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=5,
        help="Max results to fetch (default: 5)",
    )
    parser.add_argument(
        "--sort",
        choices=["submittedDate", "relevance", "lastUpdatedDate"],
        default="submittedDate",
        help="Sorting criteria (default: submittedDate)",
    )
    parser.add_argument(
        "--export-md",
        type=Path,
        help="Export output to a Markdown file",
    )
    parser.add_argument(
        "--export-json",
        type=Path,
        help="Export output to a JSON file",
    )

    args = parser.parse_args()
    topics = load_topics(args.config)

    query = args.query
    query_title = "Custom Query"

    if not query:
        if args.topic:
            matched = next((t for t in topics if str(t.get("id")) == str(args.topic)), None)
            if matched:
                query = matched["query"]
                query_title = matched["title"]
            else:
                print(f"Topic ID '{args.topic}' not found in {args.config}", file=sys.stderr)
                return 1
        else:
            print("=== Available Boilerplate Topics ===")
            for t in topics:
                print(f"  [{t.get('id')}] {t.get('title')}: {t.get('description')}")
            print("\nUsage example:")
            print("  python3 arxiv_miner.py --topic 1")
            print("  python3 arxiv_miner.py --query 'cat:cs.AI AND ti:diffusion' --limit 10")
            return 0

    print(f"Fetching {args.limit} papers for: {query_title} ({query})...")
    papers = fetch_papers(query=query, max_results=args.limit, sort_by=args.sort)

    if not papers:
        print("No papers found or error connecting to arXiv.")
        return 0

    print(f"\n--- Results ({len(papers)} papers) ---")
    for idx, p in enumerate(papers, 1):
        auth_str = ", ".join(p.authors[:3]) + (" et al." if len(p.authors) > 3 else "")
        print(f"\n[{idx}] {p.title}")
        print(f"    Date: {p.published} | Authors: {auth_str}")
        print(f"    Link: {p.link} | PDF: {p.pdf_url}")
        print(f"    Abstract: {p.summary[:200]}...")

    if args.export_md:
        export_markdown(papers, args.export_md, query_title)
    if args.export_json:
        export_json(papers, args.export_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
