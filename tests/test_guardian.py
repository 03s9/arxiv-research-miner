import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from multisource.guardian import confirm_hashes, decide, evaluate_reddit, maybe_launch, scan_tree


def _policy(root: Path) -> Path:
    path = root / "policy.json"
    path.write_text(
        json.dumps(
            {
                "schema": "trusted-execution-policy/v1",
                "self_blessing": False,
                "approval": {"method": "separately_reviewed", "approved_head": None},
                "scan_paths": ["sample.py"],
                "reviewed_network_modules": [],
                "reviewed_exceptions": [],
                "allowed_third_party_dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    return path


class GuardianTests(unittest.TestCase):
    def test_missing_policy_blocks_before_reddit_or_network(self):
        calls = {"reddit": 0, "network": 0}
        with TemporaryDirectory() as tmp:
            result = decide(
                Path(tmp),
                Path(tmp) / "missing.json",
                network=lambda: calls.__setitem__("network", calls["network"] + 1),
                reddit=lambda _root: calls.__setitem__("reddit", calls["reddit"] + 1),
            )
        self.assertFalse(result["static_ok"])
        self.assertEqual(calls, {"reddit": 0, "network": 0})
        self.assertFalse(result["allows_execution"])

    def test_local_finding_runs_before_reddit(self):
        calls = {"reddit": 0, "network": 0}

        def reddit(_root):
            calls["reddit"] += 1
            return {"allows_execution": False}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample.py"
            sample.write_text("import pickle\n", encoding="utf-8")
            result = decide(
                root,
                _policy(root),
                network=lambda: calls.__setitem__("network", calls["network"] + 1),
                reddit=reddit,
            )
        self.assertGreater(calls["network"], -1)
        self.assertEqual(calls["reddit"], 0)
        self.assertEqual(calls["network"], 0)
        self.assertFalse(result["allows_execution"])

    def test_static_pass_may_consult_reddit_and_still_not_execute(self):
        calls = {"reddit": 0, "network": 0}
        marker = []

        def reddit(root):
            calls["reddit"] += 1
            return evaluate_reddit(root)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text("def placeholder():\n    return 1\n", encoding="utf-8")
            notes = root / "security"
            notes.mkdir()
            (notes / "reddit-notes-principal.json").write_text(
                json.dumps({"schema": "reddit-threat-notes/v1", "pass_receipt": True, "search_status": "incomplete"}),
                encoding="utf-8",
            )
            result = decide(
                root,
                _policy(root),
                network=lambda: calls.__setitem__("network", calls["network"] + 1),
                reddit=reddit,
            )
            launched = maybe_launch(["python3", "-c", "raise SystemExit('should-not-run')"], False)
            marker.append(launched)
        self.assertEqual(calls["reddit"], 1)
        self.assertEqual(calls["network"], 0)
        self.assertFalse(result["allows_execution"])
        self.assertTrue(result["reddit"]["pass_receipt_ignored"])
        self.assertEqual(marker, [False])

    def test_invalid_encoding_and_postscan_tamper(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample.py"
            sample.write_bytes(b"\xff")
            policy = json.loads(_policy(root).read_text(encoding="utf-8"))
            scanned = scan_tree(root, policy)
            self.assertFalse(scanned["ok"])
            self.assertIn("invalid_encoding", [item["rule"] for item in scanned["findings"]])
            sample.write_text("value = 1\n", encoding="utf-8")
            scanned = scan_tree(root, policy)
            self.assertTrue(scanned["ok"])
            digest = scanned["hashes"]["sample.py"]
            self.assertEqual(digest, __import__("hashlib").sha256(sample.read_bytes()).hexdigest())
            sample.write_text("value = 2\n", encoding="utf-8")
            self.assertFalse(confirm_hashes(root, scanned["hashes"]))

    def test_unsafe_permissions_and_symlink(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample.py"
            sample.write_text("value = 1\n", encoding="utf-8")
            os.chmod(sample, 0o666)
            policy = json.loads(_policy(root).read_text(encoding="utf-8"))
            self.assertFalse(scan_tree(root, policy)["ok"])
            os.chmod(sample, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            link = root / "linked.py"
            link.symlink_to("sample.py")
            policy["scan_paths"] = ["linked.py"]
            self.assertIn("symlink", [item["rule"] for item in scan_tree(root, policy)["findings"]])

    def test_repo_check_stays_unauthorized(self):
        root = Path(".").resolve()
        result = decide(root, root / "security" / "trusted-policy.json")
        self.assertTrue(result["static_ok"])
        self.assertFalse(result["allows_execution"])
        self.assertFalse(result["malware_guarantee"])
        policy = json.loads((root / "security" / "trusted-policy.json").read_text(encoding="utf-8"))
        self.assertIsNone(policy["approval"]["approved_head"])
        self.assertFalse(policy["self_blessing"])


if __name__ == "__main__":
    unittest.main()
