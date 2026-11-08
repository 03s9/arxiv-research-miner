import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from arxiv_miner import Paper, parse_arxiv_response, load_topics

SAMPLE_ATOM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2601.0001v1</id>
    <published>2026-01-01T12:00:00Z</published>
    <updated>2026-01-02T12:00:00Z</updated>
    <title>Sample Paper on Distributed AI</title>
    <summary>This paper proposes a new distributed scheduler for large language models.</summary>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Jones</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/2601.0001v1.pdf" type="application/pdf"/>
  </entry>
</feed>
"""

class TestArxivMiner(unittest.TestCase):
    def test_parse_arxiv_response(self):
        papers = parse_arxiv_response(SAMPLE_ATOM_XML)
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p.title, "Sample Paper on Distributed AI")
        self.assertEqual(p.published, "2026-01-01")
        self.assertEqual(p.authors, ["Alice Smith", "Bob Jones"])
        self.assertEqual(p.pdf_url, "http://arxiv.org/pdf/2601.0001v1.pdf")
        self.assertIn("distributed scheduler", p.summary)

    def test_load_topics(self):
        topics_file = Path(__file__).parent.parent / "topics.json"
        topics = load_topics(topics_file)
        self.assertIsInstance(topics, list)
        self.assertGreater(len(topics), 0)
        self.assertIn("query", topics[0])

if __name__ == "__main__":
    unittest.main()
