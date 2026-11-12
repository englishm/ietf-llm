"""Tests for the GitHub API fallback's rate-limit handling and reuse
(gather.sources.github): a spent rate limit is waited out rather than ending a
list early, a failure that persists fails the repo (so the previous archive
stays), and unchanged PRs and issues reuse a complete previous entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import ietf_llm.gather.sources.github as github
from ietf_llm.log import Verbosity

BASE = "https://api.github.com/repos/o/r"


class _Res:
    def __init__(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise github.requests.HTTPError(str(self.status_code))


def _pr(number: int, updated: str = "2026-01-02T00:00:00Z") -> Dict[str, Any]:
    return {"number": number, "title": f"PR {number}", "state": "open",
            "user": {"login": "alice"}, "body": "b", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": updated, "closed_at": None, "merged_at": None, "labels": [],
            "html_url": f"https://github.com/o/r/pull/{number}", "head": {}, "base": {}}


class _Fake:
    """governed_get stub serving `routes`; `limited` URLs answer one 403 with
    the rate limit spent before succeeding, `broken` ones always 500."""

    def __init__(self, routes: Dict[str, Any], limited: Optional[List[str]] = None,
                 broken: Optional[List[str]] = None) -> None:
        self.routes, self.limited, self.broken = routes, list(limited or []), broken or []
        self.calls: List[str] = []

    def __call__(self, url: str, headers: Optional[Dict[str, str]] = None,
                 params: Optional[Dict[str, Any]] = None, timeout: int = 0) -> _Res:
        self.calls.append(url)
        if (params or {}).get("page", 1) > 1:
            return _Res(200, [])
        if url in self.limited:
            self.limited.remove(url)
            return _Res(403, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})
        if url in self.broken:
            return _Res(500, {})
        if url in self.routes:
            return _Res(200, self.routes[url])
        return _Res(404, {})


def _routes() -> Dict[str, Any]:
    return {
        f"{BASE}/pulls?state=all": [_pr(1), _pr(2)],
        f"{BASE}/pulls/1/reviews": [{"id": 7, "user": {"login": "bob"}, "state": "APPROVED",
                                     "body": "ok", "submitted_at": "2026-01-02T00:00:00Z"}],
        f"{BASE}/issues/2/comments": [{"user": {"login": "carol"}, "body": "hi",
                                       "created_at": "2026-01-02T00:00:00Z"}],
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: Any) -> List[float]:
    slept: List[float] = []
    monkeypatch.setattr(github.time, "sleep", slept.append)
    return slept


def test_rate_limit_is_waited_out(monkeypatch: Any, _no_sleep: List[float]) -> None:
    fake = _Fake(_routes(), limited=[f"{BASE}/pulls/1/reviews"])
    monkeypatch.setattr(github, "governed_get", fake)
    pulls = github._fetch_all_pulls("o", "r", {})
    assert _no_sleep  # waited once
    assert [r["author"] for r in pulls[0]["reviews"]] == ["bob"]
    assert all(p["complete"] for p in pulls)


def test_persistent_failure_fails_the_repo(monkeypatch: Any, tmp_path: Path) -> None:
    routes = _routes()
    routes[f"{BASE}/issues?state=all&page=1&per_page=100"] = []
    monkeypatch.setattr(github, "governed_get",
                        _Fake(routes, broken=[f"{BASE}/issues/2/comments"]))
    dest = tmp_path / "o-r.json"
    dest.write_text(json.dumps({"repo": "o/r", "issues": [], "pulls": [{"number": 9}]}))
    assert not github.download_github_issues("o/r", str(dest), verbose=Verbosity.QUIET)
    assert json.loads(dest.read_text())["pulls"] == [{"number": 9}]  # kept


def test_unchanged_complete_pulls_are_reused(monkeypatch: Any) -> None:
    fake = _Fake(_routes())
    monkeypatch.setattr(github, "governed_get", fake)
    first = github._fetch_all_pulls("o", "r", {})
    # Entry 2 is incomplete (an older archive): fetched again; entry 1 reused.
    previous = [first[0], {**first[1], "complete": False}]
    fake.calls.clear()
    again = github._fetch_all_pulls("o", "r", {}, previous)
    assert again[0] is previous[0]
    assert not any("/pulls/1/" in c for c in fake.calls)
    assert any("/pulls/2/" in c for c in fake.calls)
    # A changed PR is fetched again.
    fake.routes[f"{BASE}/pulls?state=all"] = [_pr(1, "2026-02-01T00:00:00Z"), _pr(2)]
    fake.calls.clear()
    github._fetch_all_pulls("o", "r", {}, again)
    assert any("/pulls/1/" in c for c in fake.calls)


def test_unchanged_issue_comments_are_reused(monkeypatch: Any) -> None:
    issue = {"number": 5, "title": "t", "state": "open", "user": {"login": "a"},
             "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z",
             "labels": [], "body": "b", "comments": 1, "comments_url": f"{BASE}/issues/5/comments"}
    fake = _Fake({f"{BASE}/issues?state=all&page=1&per_page=100": [issue],
                  f"{BASE}/issues/5/comments": [{"user": {"login": "z"}, "body": "c",
                                                  "created_at": "2026-01-03T00:00:00Z"}]})
    monkeypatch.setattr(github, "governed_get", fake)
    first = github._fetch_all_issues("o", "r", {}, Verbosity.QUIET)
    assert first[0]["comments"][0]["author"] == "z" and first[0]["complete"]
    fake.calls.clear()
    again = github._fetch_all_issues("o", "r", {}, Verbosity.QUIET, first)
    assert again[0]["comments"] == first[0]["comments"]
    assert f"{BASE}/issues/5/comments" not in fake.calls
