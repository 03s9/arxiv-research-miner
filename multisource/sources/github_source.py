"""GitHub REST reads. A repository name is not article identity."""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import quote, urlencode

from multisource.http_client import USER_AGENT, HttpClient, HttpResponse
from multisource.identity import query_identifier
from multisource.models import ReleaseNote, RepoSnapshot, SourceOutcome
from multisource.sources.http_status import failure_status, http_failure, json_payload

_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_CODE_EXT = (".py", ".rs", ".go", ".java", ".c", ".cc", ".cpp", ".js", ".ts", ".tsx")
_API = "https://api.github.com"


def parse_repo_spec(spec: str) -> tuple[str, str] | None:
    owner, sep, name = spec.partition("/")
    if not sep or not _SEGMENT.fullmatch(owner) or not _SEGMENT.fullmatch(name):
        return None
    if owner in {".", ".."} or name in {".", ".."}:
        return None
    return owner, name


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class GitHubSource:
    provider = "github"

    def collect(
        self,
        query: str,
        repos: tuple[str, ...],
        limit: int,
        token: str | None,
        client: HttpClient,
        timeout: float,
    ) -> SourceOutcome:
        credentials = "provided" if token else "not_provided"
        headers = _headers(token)
        explicit: list[tuple[str, str]] = []
        for spec in repos:
            parsed = parse_repo_spec(spec)
            if parsed is None:
                return SourceOutcome(self.provider, "upstream_error", "invalid_github_repo", None, credentials)
            explicit.append(parsed)
        identifier = None if explicit else query_identifier(query)
        targets = list(explicit)
        if not targets and identifier is None:
            return SourceOutcome(
                self.provider,
                "skipped",
                "github_requires_exact_identifier_or_explicit_repo",
                None,
                credentials,
            )
        if not targets and identifier is not None:
            found, failure = self._search(identifier, limit, headers, client, timeout, credentials)
            if failure is not None:
                return failure
            targets = found
            if not targets:
                return SourceOutcome(self.provider, "empty", "", 200, credentials)
        cap = min(3, max(limit, 1))
        snapshots: list[RepoSnapshot] = []
        rejected: list[dict[str, str]] = []
        saw_not_found = False
        for owner, name in targets[:cap]:
            snapshot, failure = self._one(owner, name, headers, client, timeout, credentials)
            if failure is not None:
                if failure.status == "not_found":
                    saw_not_found = True
                    rejected.append({"repo": f"{owner}/{name}", "reason": "not_found"})
                    continue
                return failure
            assert snapshot is not None
            if identifier is not None and not _snapshot_has_identifier(snapshot, identifier):
                rejected.append({"repo": f"{owner}/{name}", "reason": "homonym_or_missing_identifier"})
                continue
            snapshots.append(snapshot)
        if not snapshots and saw_not_found and explicit:
            return SourceOutcome(
                self.provider, "not_found", "github_repo_not_found", 404, credentials, rejected=rejected
            )
        status = "ok" if snapshots or rejected else "empty"
        return SourceOutcome(
            self.provider, status, "", 200, credentials, repos=snapshots, rejected=rejected
        )

    def _search(
        self,
        identifier: tuple[str, str, str | None],
        limit: int,
        headers: dict[str, str],
        client: HttpClient,
        timeout: float,
        credentials: str,
    ) -> tuple[list[tuple[str, str]], SourceOutcome | None]:
        _kind, value, version = identifier
        needle = f"{value}{version or ''}"
        url = _API + "/search/repositories?" + urlencode(
            {"q": f"{needle} in:readme", "per_page": min(3, limit)}
        )
        response, failure = self._get(url, headers, client, timeout, credentials)
        if failure is not None:
            return [], failure
        assert response is not None
        try:
            payload = json_payload(response)
        except ValueError:
            return [], SourceOutcome(self.provider, "upstream_error", "invalid_json", response.status, credentials)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return [], SourceOutcome(self.provider, "upstream_error", "invalid_payload", response.status, credentials)
        found: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            login = owner.get("login")
            name = item.get("name")
            if isinstance(login, str) and isinstance(name, str) and parse_repo_spec(f"{login}/{name}"):
                found.append((login, name))
        return found, None

    def _one(
        self,
        owner: str,
        name: str,
        headers: dict[str, str],
        client: HttpClient,
        timeout: float,
        credentials: str,
    ) -> tuple[RepoSnapshot | None, SourceOutcome | None]:
        repo_url = f"{_API}/repos/{quote(owner)}/{quote(name)}"
        response, failure = self._get(repo_url, headers, client, timeout, credentials)
        if failure is not None:
            return None, failure
        assert response is not None
        try:
            payload = json_payload(response)
        except ValueError:
            return None, SourceOutcome(self.provider, "upstream_error", "invalid_json", response.status, credentials)
        if not isinstance(payload, dict):
            return None, SourceOutcome(self.provider, "upstream_error", "invalid_payload", response.status, credentials)
        readme_text, readme_url, readme_failure = self._readme(owner, name, headers, client, timeout, credentials)
        if readme_failure is not None:
            return None, readme_failure
        releases, release_failure = self._releases(owner, name, headers, client, timeout, credentials)
        if release_failure is not None:
            return None, release_failure
        code_file, code_bytes, code_sha, code_failure = self._one_code_file(
            owner, name, headers, client, timeout, credentials
        )
        if code_failure is not None:
            return None, code_failure
        homepage = payload.get("homepage") if isinstance(payload.get("homepage"), str) else ""
        description = payload.get("description") if isinstance(payload.get("description"), str) else ""
        html_url = payload.get("html_url") if isinstance(payload.get("html_url"), str) else ""
        branch = payload.get("default_branch") if isinstance(payload.get("default_branch"), str) else ""
        if not html_url.startswith("https://github.com/"):
            html_url = f"https://github.com/{owner}/{name}"
        return (
            RepoSnapshot(
                owner=owner,
                name=name,
                html_url=html_url,
                description=description,
                default_branch=branch,
                homepage=homepage.strip(),
                readme_text=readme_text,
                readme_url=readme_url,
                releases=tuple(releases),
                code_file=code_file,
                code_bytes=code_bytes,
                code_sha256=code_sha,
            ),
            None,
        )

    def _readme(self, owner, name, headers, client, timeout, credentials):
        url = f"{_API}/repos/{quote(owner)}/{quote(name)}/readme"
        response, failure = self._get(url, headers, client, timeout, credentials)
        if failure is not None:
            if failure.status == "not_found":
                return "", url, None
            return "", url, failure
        assert response is not None
        try:
            payload = json_payload(response)
        except ValueError:
            return "", url, SourceOutcome(self.provider, "upstream_error", "invalid_json", response.status, credentials)
        text = ""
        if isinstance(payload, dict) and payload.get("encoding") == "base64":
            try:
                text = base64.b64decode(payload.get("content") or b"").decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                text = ""
        html = payload.get("html_url") if isinstance(payload, dict) else ""
        return text[:20000], html or url, None

    def _releases(self, owner, name, headers, client, timeout, credentials):
        url = f"{_API}/repos/{quote(owner)}/{quote(name)}/releases?" + urlencode({"per_page": 3})
        response, failure = self._get(url, headers, client, timeout, credentials)
        if failure is not None:
            if failure.status == "not_found":
                return [], None
            return [], failure
        assert response is not None
        try:
            payload = json_payload(response)
        except ValueError:
            return [], SourceOutcome(self.provider, "upstream_error", "invalid_json", response.status, credentials)
        notes: list[ReleaseNote] = []
        if isinstance(payload, list):
            for row in payload[:3]:
                if not isinstance(row, dict):
                    continue
                tag = row.get("tag_name") if isinstance(row.get("tag_name"), str) else ""
                html = row.get("html_url") if isinstance(row.get("html_url"), str) else ""
                title = row.get("name") if isinstance(row.get("name"), str) else tag
                body = row.get("body") if isinstance(row.get("body"), str) else ""
                if tag and html.startswith("https://"):
                    notes.append(ReleaseNote(tag, html, title[:120], " ".join(body.split())[:240]))
        return notes, None

    def _one_code_file(self, owner, name, headers, client, timeout, credentials):
        url = f"{_API}/repos/{quote(owner)}/{quote(name)}/contents/"
        response, failure = self._get(url, headers, client, timeout, credentials)
        if failure is not None:
            if failure.status in {"rate_limited", "timeout", "rejected_credentials", "missing_credentials"}:
                return None, 0, None, failure
            return None, 0, None, None
        if response is None:
            return None, 0, None, None
        try:
            payload = json_payload(response)
        except ValueError:
            return None, 0, None, None
        if not isinstance(payload, list):
            return None, 0, None, None
        chosen = None
        for row in payload:
            if not isinstance(row, dict) or row.get("type") != "file":
                continue
            filename = row.get("name") if isinstance(row.get("name"), str) else ""
            size = row.get("size") if isinstance(row.get("size"), int) else 0
            if filename.lower().endswith(_CODE_EXT) and 0 < size <= 20000:
                chosen = filename
                break
        if not chosen:
            return None, 0, None, None
        file_url = f"{_API}/repos/{quote(owner)}/{quote(name)}/contents/{quote(chosen)}"
        file_response, file_failure = self._get(file_url, headers, client, timeout, credentials)
        if file_failure is not None:
            if file_failure.status in {"rate_limited", "timeout", "rejected_credentials", "missing_credentials"}:
                return None, 0, None, file_failure
            return None, 0, None, None
        if file_response is None:
            return None, 0, None, None
        try:
            file_payload = json_payload(file_response)
        except ValueError:
            return None, 0, None, None
        if not isinstance(file_payload, dict) or file_payload.get("encoding") != "base64":
            return None, 0, None, None
        try:
            raw = base64.b64decode(file_payload.get("content") or b"")
        except (ValueError, TypeError):
            return None, 0, None, None
        if len(raw) > 20000:
            return None, 0, None, None
        return chosen, len(raw), hashlib.sha256(raw).hexdigest(), None

    def _get(self, url, headers, client, timeout, credentials):
        try:
            response = client.get_provider(self.provider, url, headers, timeout)
        except Exception as exc:  # noqa: BLE001
            status, reason = failure_status(exc)
            return None, SourceOutcome(self.provider, status, reason, None, credentials)
        failed = http_failure(response, key_provided=credentials == "provided")
        if failed:
            creds = "rejected" if failed == "rejected_credentials" else credentials
            return None, SourceOutcome(self.provider, failed, failed, response.status, creds)
        return response, None


def _snapshot_has_identifier(snapshot: RepoSnapshot, identifier: tuple[str, str, str | None]) -> bool:
    from multisource.identity import identifiers_in_text

    found = identifiers_in_text("\n".join([snapshot.description, snapshot.readme_text]))
    kind, value, version = identifier
    for found_kind, found_value, found_version in found:
        if found_kind != kind or found_value != value:
            continue
        if version and found_version and found_version != version:
            continue
        if version and found_version == version:
            return True
        if not version:
            return True
    return False
