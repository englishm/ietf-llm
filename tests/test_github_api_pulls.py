"""Tests for the GitHub API fallback's pull requests (gather.sources.github):
a repo without a gh-pages archive.json (a fork, say) still yields its PRs, with
conversation comments, reviews and review comments, in archive.json's shape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import ietf_llm.gather.sources.github as github
from ietf_llm.log import Verbosity

BASE = "https://api.github.com/repos/o/r"


class _Res:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise github.requests.HTTPError(str(self.status_code))


def _pr(number: int, merged: bool = False) -> Dict[str, Any]:
    return {
        "number": number, "title": f"PR {number}", "state": "closed" if merged else "open",
        "user": {"login": "alice"}, "body": "b", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z", "closed_at": None,
        "merged_at": "2026-01-03T00:00:00Z" if merged else None,
        "merge_commit_sha": "abc123", "labels": [{"name": "Editorial"}],
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": "topic"}, "base": {"ref": "main"},
    }


def _fake(routes: Dict[str, Any]) -> Any:
    """governed_get stub: routes map 'url' or 'url#page' to a JSON body; anything
    else is a 404 (which, for gh-pages, means "no archive.json")."""

    def get(url: str, headers: Optional[Dict[str, str]] = None,
            params: Optional[Dict[str, Any]] = None, timeout: int = 0) -> _Res:
        page = (params or {}).get("page")
        key = f"{url}#{page}" if page and page > 1 else url
        if key in routes:
            return _Res(200, routes[key])
        if page and page > 1:
            return _Res(200, [])
        return _Res(404, {})

    return get


def _routes() -> Dict[str, Any]:
    first_page: List[Dict[str, Any]] = [_pr(n) for n in range(2, 102)]
    return {
        f"{BASE}/pulls?state=all": [_pr(1, merged=True)] + first_page[:99],
        f"{BASE}/pulls?state=all#2": first_page[99:],
        f"{BASE}/pulls/1": {"merged_by": {"login": "bob"}},
        f"{BASE}/pulls/1/reviews": [
            {"id": 7, "user": {"login": "carol"}, "state": "COMMENTED", "body": "",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"id": 8, "user": {"login": "dave"}, "state": "APPROVED", "body": "lgtm",
             "submitted_at": "2026-01-02T01:00:00Z"},
        ],
        f"{BASE}/pulls/1/comments": [
            {"pull_request_review_id": 7, "path": "draft.md", "original_position": 41,
             "body": "control stream?", "created_at": "2026-01-02T00:00:00Z",
             "updated_at": "2026-01-02T00:00:00Z"},
        ],
        f"{BASE}/issues/1/comments": [
            {"user": {"login": "erin"}, "created_at": "2026-01-02T02:00:00Z", "body": "thanks"},
        ],
    }


def test_pulls_follow_the_archive_shape(monkeypatch: Any) -> None:
    monkeypatch.setattr(github, "governed_get", _fake(_routes()))
    pulls = github._fetch_all_pulls("o", "r", {})
    assert len(pulls) == 101  # paged past the first 100
    pr = pulls[0]
    assert pr["number"] == 1 and pr["state"] == "MERGED"
    assert pr["mergedBy"] == "bob" and pr["mergeCommit"] == {"oid": "abc123"}
    assert pr["author"] == "alice" and pr["labels"] == ["Editorial"]
    assert pr["headRefName"] == "topic" and pr["baseRefName"] == "main"
    assert pr["comments"] == [
        {"author": "erin", "createdAt": "2026-01-02T02:00:00Z", "body": "thanks"}
    ]
    assert [r["author"] for r in pr["reviews"]] == ["carol", "dave"]
    # Review comments land on the review they belong to.
    assert [c["body"] for c in pr["reviews"][0]["comments"]] == ["control stream?"]
    assert pr["reviews"][1]["comments"] == []
    assert pulls[1]["state"] == "OPEN" and pulls[1]["mergedBy"] is None


def test_api_fallback_writes_pulls(monkeypatch: Any, tmp_path: Path) -> None:
    routes = _routes()
    routes[f"{BASE}/issues?state=all&page=1&per_page=100"] = []
    monkeypatch.setattr(github, "governed_get", _fake(routes))
    dest = tmp_path / "o-r.json"
    assert github.download_github_issues("o/r", str(dest), token=None, verbose=Verbosity.QUIET)
    data = json.loads(dest.read_text())
    assert data["repo"] == "o/r" and data["issues"] == []
    assert len(data["pulls"]) == 101 and data["pulls"][0]["state"] == "MERGED"
