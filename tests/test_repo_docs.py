"""Tests for gather.sources.repo_docs: markdown files mirrored from GitHub repo
paths into docs/, re-downloaded only when their blob changes, pruned when
dropped, and kept when a repo's tree can't be read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import ietf_llm.gather.sources.repo_docs as repo_docs
from ietf_llm.embeddings.chunking import _windowed_citation_url
from ietf_llm.log import Verbosity

TREE = "https://api.github.com/repos/o/r/git/trees/HEAD"
RAW = "https://raw.githubusercontent.com/o/r/HEAD/"


class _Res:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.encoding: Optional[str] = None

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:
        return str(self._body)


def _install(monkeypatch: Any, tree: Optional[Dict[str, str]], fetched: List[str]) -> None:
    def get(url: str, headers: Optional[Dict[str, str]] = None,
            params: Optional[Dict[str, Any]] = None, timeout: int = 0) -> _Res:
        if url == TREE:
            if tree is None:
                return _Res(500, {})
            items = [{"path": p, "sha": s, "type": "blob"} for p, s in tree.items()]
            items.append({"path": "meetings", "sha": "t", "type": "tree"})
            return _Res(200, {"tree": items})
        if url.startswith(RAW):
            fetched.append(url[len(RAW):])
            return _Res(200, f"# {url[len(RAW):]}\n")
        return _Res(404, {})

    monkeypatch.setattr(repo_docs, "governed_get", get)


def _files(root: Path) -> List[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.md"))


def test_parse_spec() -> None:
    assert repo_docs.parse_spec("o/r") == ("o/r", "")
    assert repo_docs.parse_spec("o/r/meetings/") == ("o/r", "meetings")
    assert repo_docs.parse_spec("o/r/a/B.md") == ("o/r", "a/B.md")
    with pytest.raises(ValueError):
        repo_docs.parse_spec("o")


def test_gather_select_skip_and_prune(tmp_path: Path, monkeypatch: Any) -> None:
    tree = {"meetings/2026-01.md": "a", "meetings/2026-02.md": "b",
            "GOVERNANCE.md": "c", "README.md": "d", "src/x.py": "e",
            "meetings/index.mdx": "f"}
    fetched: List[str] = []
    _install(monkeypatch, tree, fetched)
    specs = ["o/r/meetings", "o/r/GOVERNANCE.md"]
    assert repo_docs.gather_repo_docs(specs, str(tmp_path), Verbosity.QUIET) == 4
    assert _files(tmp_path) == ["docs/o/r/GOVERNANCE.md", "docs/o/r/meetings/2026-01.md",
                                "docs/o/r/meetings/2026-02.md", "docs/o/r/meetings/index.mdx.md"]
    assert (tmp_path / "docs/o/r/GOVERNANCE.md").read_text() == "# GOVERNANCE.md\n"

    # Unchanged blobs are not downloaded again; a changed one is.
    fetched.clear()
    tree["meetings/2026-02.md"] = "b2"
    repo_docs.gather_repo_docs(specs, str(tmp_path), Verbosity.QUIET)
    assert fetched == ["meetings/2026-02.md"]

    # Dropping a path removes its files.
    repo_docs.gather_repo_docs(["o/r/meetings"], str(tmp_path), Verbosity.QUIET)
    assert "docs/o/r/GOVERNANCE.md" not in _files(tmp_path)
    manifest = json.loads((tmp_path / "docs/.repo-docs.json").read_text())
    assert sorted(manifest) == ["o/r/meetings/2026-01.md", "o/r/meetings/2026-02.md",
                                "o/r/meetings/index.mdx"]


def test_unreadable_tree_keeps_files(tmp_path: Path, monkeypatch: Any) -> None:
    _install(monkeypatch, {"GOVERNANCE.md": "c"}, [])
    repo_docs.gather_repo_docs(["o/r"], str(tmp_path), Verbosity.QUIET)
    _install(monkeypatch, None, [])
    assert repo_docs.gather_repo_docs(["o/r"], str(tmp_path), Verbosity.QUIET) == 1
    assert _files(tmp_path) == ["docs/o/r/GOVERNANCE.md"]


def test_no_specs_is_a_no_op(tmp_path: Path) -> None:
    assert repo_docs.gather_repo_docs(None, str(tmp_path), Verbosity.QUIET) == 0
    assert not (tmp_path / "docs").exists()


def test_citation_url() -> None:
    assert (_windowed_citation_url("docs/o/r/meetings/2026-01.md", "")
            == "https://github.com/o/r/blob/HEAD/meetings/2026-01.md")
    assert (_windowed_citation_url("docs/o/r/a/b.mdx.md", "")
            == "https://github.com/o/r/blob/HEAD/a/b.mdx")
