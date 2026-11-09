"""Fail-closed preflight. It does not authorize a harvest by itself.

Trust boundary: this file is the bootstrap a reviewer has to read. It cannot
prove that a modified copy of itself is honest, and static checks are not a
malware detector. Origin and hashes are not proof that code is benign.

Local checks run before any DNS or network hook. Reddit research is a separate
step and, in this build, never contacts Reddit. Execution stays pending until
a path-specific audit exists; a local JSON file is not that audit.

Filesystem limits: symlinks and in-tree world-writable or setuid files are
rejected. Parents above the project root are not fully policed. Re-reading
bytes closes a same-process tamper window only; it does not remove TOCTOU
against another process after this process exits.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

_EVAL = "ev" + "al"
_EXEC = "ex" + "ec"
_NOTES = Path("security/reddit-notes-principal.json")


def maybe_launch(argv: list[str], allowed: bool) -> bool:
    """The only process start. Callers must pass allowed=False until an audit exists."""
    if not allowed:
        return False
    subprocess.run(argv, check=False)
    return True


def _read_nofollow(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        return os.read(fd, 8_000_000)
    finally:
        os.close(fd)


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_python(root: Path, scan_paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in scan_paths:
        candidate = root / raw
        if candidate.is_file():
            found.append(candidate)
            continue
        if not candidate.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(candidate, followlinks=False):
            current = Path(dirpath)
            if current.is_symlink():
                found.append(current)
                dirnames.clear()
                continue
            for name in filenames:
                if name.endswith(".py"):
                    found.append(current / name)
    return found


def _dependency_findings(root: Path, allowed: list[str]) -> list[str]:
    findings: list[str] = []
    requirements = root / "requirements.txt"
    if requirements.is_file() and not requirements.is_symlink():
        for line in _read_nofollow(requirements).decode("utf-8", errors="replace").splitlines():
            item = line.strip()
            if not item or item.startswith("#"):
                continue
            name = item.split("==")[0].split(">=")[0].strip()
            if name not in allowed:
                findings.append(f"unreviewed_dependency:{name}")
    project = root / "pyproject.toml"
    if project.is_file() and not project.is_symlink():
        try:
            import tomllib
        except ModuleNotFoundError:
            return findings + ["dependency_parser_unavailable"]
        try:
            data = tomllib.loads(_read_nofollow(project).decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError):
            return findings + ["dependency_parse_failure"]
        deps = data.get("project", {}).get("dependencies", [])
        if isinstance(deps, list):
            for dep in deps:
                name = str(dep).split()[0].split(">")[0].split("=")[0].split("<")[0]
                if name and name not in allowed:
                    findings.append(f"unreviewed_dependency:{name}")
    return findings


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str, policy: dict[str, Any]) -> None:
        self.rel = rel
        self.policy = policy
        self.findings: list[str] = []

    def _allowed(self, rule: str) -> bool:
        for item in self.policy.get("reviewed_exceptions", []):
            if item.get("path") == self.rel and item.get("rule") == rule:
                return True
        return False

    def _network_ok(self) -> bool:
        return self.rel in set(self.policy.get("reviewed_network_modules", []))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._module(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self._module(node.module)
        self.generic_visit(node)

    def _module(self, name: str) -> None:
        root_name = name.split(".")[0]
        if root_name == "pickle":
            self.findings.append("pickle_import")
        if root_name == "subprocess" and not self._allowed("subprocess_import"):
            self.findings.append("subprocess_import")
        if (name == "urllib.request" or name.startswith("urllib.request.")) and not self._network_ok():
            self.findings.append("network_import")
        if root_name == "socket" and not self._network_ok():
            self.findings.append("network_import")
        if name == "http.client" and not self._network_ok():
            self.findings.append("network_import")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in {_EVAL, _EXEC}:
            self.findings.append(f"call:{func.id}")
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "os" and func.attr == "system":
                self.findings.append("os_system")
            if func.value.id == "pickle" and func.attr in {"load", "loads"}:
                self.findings.append("pickle_call")
            if func.value.id == "torch" and func.attr == "load":
                self.findings.append("torch_load")
            if func.value.id == "subprocess":
                if not self._allowed("subprocess_call"):
                    self.findings.append("subprocess_call")
                for keyword in node.keywords:
                    if (
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        self.findings.append("shell_true")
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg == "trust_remote_code":
            self.findings.append("trust_remote_code")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "trust_remote_code":
            self.findings.append("trust_remote_code")
        self.generic_visit(node)


def _local_import_findings(root: Path, rel: str, tree: ast.AST) -> list[str]:
    findings: list[str] = []
    root_real = root.resolve()
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        if not module:
            continue
        parts = module.split(".")
        if parts[0] not in {"multisource", "arxiv_miner"}:
            continue
        if parts[0] == "arxiv_miner":
            target = root / "arxiv_miner.py"
        else:
            target = root.joinpath(*parts).with_suffix(".py")
            if not target.is_file():
                target = root.joinpath(*parts) / "__init__.py"
        if not target.is_file():
            findings.append(f"missing_local_import:{module}")
            continue
        try:
            resolved = target.resolve()
        except OSError:
            findings.append(f"unresolved_local_import:{module}")
            continue
        if root_real != resolved and root_real not in resolved.parents:
            findings.append(f"escaping_local_import:{module}")
    return findings


def _permission_findings(path: Path, root: Path) -> list[str]:
    findings: list[str] = []
    try:
        info = path.lstat()
    except OSError:
        return ["unreadable"]
    if stat.S_ISLNK(info.st_mode):
        return ["symlink"]
    if info.st_mode & 0o002:
        findings.append("world_writable")
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        findings.append("setuid_or_setgid")
    current = path.parent
    root_real = root.resolve()
    while True:
        try:
            info = current.lstat()
        except OSError:
            findings.append("ancestor_unreadable")
            break
        if stat.S_ISLNK(info.st_mode):
            findings.append("ancestor_symlink")
            break
        if info.st_mode & 0o002 and not info.st_mode & stat.S_ISVTX:
            findings.append("ancestor_world_writable")
        try:
            if current.resolve() == root_real:
                break
        except OSError:
            findings.append("ancestor_unresolved")
            break
        if current.parent == current:
            break
        current = current.parent
    return findings


def scan_tree(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    hashes: dict[str, str] = {}
    for path in _iter_python(root, list(policy.get("scan_paths", []))):
        rel = _rel(root, path)
        perm = _permission_findings(path, root)
        if perm:
            for item in perm:
                findings.append({"path": rel, "rule": item})
            if "symlink" in perm:
                continue
        try:
            data = _read_nofollow(path)
        except OSError:
            findings.append({"path": rel, "rule": "read_failed"})
            continue
        digest = hashlib.sha256(data).hexdigest()
        hashes[rel] = digest
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            findings.append({"path": rel, "rule": "invalid_encoding"})
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError:
            findings.append({"path": rel, "rule": "parse_failure"})
            continue
        visitor = _Scan(rel, policy)
        visitor.visit(tree)
        for item in dict.fromkeys(visitor.findings):
            findings.append({"path": rel, "rule": item})
        for item in _local_import_findings(root, rel, tree):
            findings.append({"path": rel, "rule": item})
    for item in _dependency_findings(root, list(policy.get("allowed_third_party_dependencies", []))):
        findings.append({"path": ".", "rule": item})
    return {"findings": findings, "hashes": hashes, "ok": not findings}


def confirm_hashes(root: Path, hashes: dict[str, str]) -> bool:
    for rel, digest in hashes.items():
        try:
            data = _read_nofollow(root / rel)
        except OSError:
            return False
        if hashlib.sha256(data).hexdigest() != digest:
            return False
    return True


def load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "trusted-execution-policy/v1":
        raise ValueError("invalid_policy")
    if data.get("self_blessing") is True:
        raise ValueError("self_blessing_rejected")
    approval = data.get("approval")
    if not isinstance(approval, dict) or approval.get("method") != "separately_reviewed":
        raise ValueError("policy_not_separately_reviewed")
    return data


def evaluate_reddit(root: Path, audit_path: Path | None = None) -> dict[str, Any]:
    """Read local notes only. Never treats them as a pass, and never contacts Reddit."""
    notes_path = audit_path or (root / _NOTES)
    base = {
        "status": "pending",
        "allows_execution": False,
        "reddit_api_contacted": False,
        "malware_guarantee": False,
        "no_hit_is_not_security": True,
        "reason": "path_specific_audit_unresolved",
    }
    if not notes_path.is_file():
        base["reason"] = "reddit_research_unavailable"
        return base
    try:
        notes = json.loads(notes_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        base["reason"] = "reddit_notes_unreadable"
        return base
    if not isinstance(notes, dict):
        base["reason"] = "reddit_notes_unreadable"
        return base
    # A file that claims to be a pass is still not an authorization.
    base["pass_receipt_ignored"] = bool(notes.get("pass_receipt"))
    base["search_status"] = notes.get("search_status", "incomplete")
    base["relevant_unresolved"] = notes.get("relevant_unresolved", [])
    if notes.get("search_status") != "complete" or notes.get("live_connector") is not True:
        base["reason"] = "reddit_corpus_incomplete_no_connector"
    return base


def decide(
    root: Path,
    policy_path: Path,
    *,
    network: Callable[[], None] | None = None,
    reddit: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    network = network or (lambda: None)
    reddit = reddit or (lambda current: evaluate_reddit(current))
    if not policy_path.is_file():
        return {
            "static_ok": False,
            "allows_execution": False,
            "reason": "missing_policy",
            "reddit_called": False,
            "network_calls": 0,
        }
    try:
        policy = load_policy(policy_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "static_ok": False,
            "allows_execution": False,
            "reason": str(exc),
            "reddit_called": False,
            "network_calls": 0,
        }
    scanned = scan_tree(root, policy)
    if not scanned["ok"] or not confirm_hashes(root, scanned["hashes"]):
        return {
            "static_ok": False,
            "allows_execution": False,
            "reason": "static_rejected" if scanned["ok"] else "static_findings",
            "findings": scanned["findings"],
            "hashes": scanned["hashes"],
            "reddit_called": False,
            "network_calls": 0,
        }
    reddit_result = reddit(root)
    # No network hook runs in this build. Execution is never authorized here.
    return {
        "static_ok": True,
        "allows_execution": False,
        "reason": reddit_result.get("reason", "path_specific_audit_unresolved"),
        "reddit": reddit_result,
        "hashes": scanned["hashes"],
        "reddit_called": True,
        "network_calls": 0,
        "execution_authorized": False,
        "malware_guarantee": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed preflight. Does not launch a harvest.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--expect-head")
    args, extra = parser.parse_known_args(argv)
    result = decide(args.root.resolve(), args.policy)
    result["expect_head"] = args.expect_head
    result["launch_invoked"] = False
    if args.run:
        launched = maybe_launch(extra, False)
        result["launch_invoked"] = launched
    print(json.dumps({key: value for key, value in result.items() if key != "hashes"}, indent=2, default=str))
    if args.check:
        return 0 if result["static_ok"] else 1
    return 0 if result["allows_execution"] else 1


if __name__ == "__main__":
    sys.exit(main())
