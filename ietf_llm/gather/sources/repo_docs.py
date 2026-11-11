"""Markdown documents kept in GitHub repos (meeting notes, proposals, specs).

Some groups keep their record as files in a repo rather than on a mailing list
or in issues: per-meeting notes under `meetings/`, proposals, governance docs.
`--repo-docs OWNER/REPO/PATH` gathers every `.md` file at or under PATH on the
repo's default branch into `docs/OWNER/REPO/PATH...`, where the indexer embeds
it like any other `.md` file. MDX pages (docs sites) are kept too, saved as
`<name>.mdx.md` so the indexer picks them up (as `.pdf.txt` does for PDFs).

Source burden: one GitHub API call per repo (the recursive git tree, shared by
all of that repo's paths), then each file from raw.githubusercontent.com,
which does not count against the API rate limit. A file whose blob SHA matches
the one recorded last time is not re-downloaded.
"""

import json
import os
from typing import Dict, List, Optional, Tuple

import requests

from ...atomicio import atomic_open
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get

DIR_DOCS = "docs"
_MANIFEST = ".repo-docs.json"
_EXTENSIONS = (".md", ".markdown", ".mdx")


def parse_spec(spec: str) -> Tuple[str, str]:
    """`OWNER/REPO[/PATH]` → (`owner/repo`, `path`); an empty path is the
    whole repo. Raises ValueError on anything without an owner and repo."""
    parts = [p for p in spec.strip().strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"--repo-docs wants OWNER/REPO[/PATH], got {spec!r}")
    return "/".join(parts[:2]), "/".join(parts[2:])


def source_url(relpath: str) -> Optional[str]:
    """The github.com URL for a `docs/OWNER/REPO/PATH` cache path."""
    parts = relpath.replace(os.sep, "/").split("/")
    if len(parts) < 4 or parts[0] != DIR_DOCS:
        return None
    owner, repo, rest = parts[1], parts[2], "/".join(parts[3:])
    if rest.endswith(".mdx.md"):
        rest = rest[: -len(".md")]
    return f"https://github.com/{owner}/{repo}/blob/HEAD/{rest}"


def _headers() -> Dict[str, str]:
    headers = {**DEFAULT_HEADERS, "Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _tree(repo: str, verbose: Verbosity) -> Optional[Dict[str, str]]:
    """{path: blob sha} for every markdown file on the default branch, or
    None if the tree can't be read."""
    url = f"https://api.github.com/repos/{repo}/git/trees/HEAD"
    try:
        res = governed_get(
            url, headers=_headers(), params={"recursive": "1"}, timeout=60
        )
    except requests.RequestException as err:
        log(f"{repo}: tree fetch failed: {err}", verbose, level=LogLevel.ERROR)
        return None
    if res.status_code != 200:
        log(
            f"{repo}: tree fetch failed: HTTP {res.status_code}",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    body = res.json()
    if body.get("truncated"):
        log(f"{repo}: tree listing truncated", verbose, level=LogLevel.STATUS)
    return {
        item["path"]: item["sha"]
        for item in body.get("tree", [])
        if item.get("type") == "blob" and item["path"].lower().endswith(_EXTENSIONS)
    }


def _cache_path(docs_dir: str, key: str) -> str:
    """Where the file for `owner/repo/path` lives under docs/."""
    if key.lower().endswith(".mdx"):
        key += ".md"
    return os.path.join(docs_dir, *key.split("/"))


def _selected(tree: Dict[str, str], path: str) -> Dict[str, str]:
    """The tree entries at or under `path` (all of them for an empty path)."""
    if not path:
        return dict(tree)
    prefix = path.rstrip("/") + "/"
    return {p: s for p, s in tree.items() if p == path or p.startswith(prefix)}


def _fetch(repo: str, path: str) -> Optional[str]:
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    try:
        res = governed_get(url, headers=DEFAULT_HEADERS, timeout=60)
    except requests.RequestException:
        return None
    if res.status_code != 200:
        return None
    res.encoding = "utf-8"
    return res.text


def _load_manifest(docs_dir: str) -> Dict[str, str]:
    try:
        with open(os.path.join(docs_dir, _MANIFEST), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def gather_repo_docs(
    specs: "Optional[List[str]]",
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> int:
    """Mirror the markdown files named by `specs` into `docs/`. Files no
    longer named (or no longer in the repo) are removed, so dropping a path
    heals on the next gather. Returns the number of files written or kept.

    A repo whose tree can't be read keeps its existing files rather than
    losing them to a transient failure."""
    docs_dir = os.path.join(cache_dir, DIR_DOCS)
    if not specs and not os.path.isdir(docs_dir):
        return 0
    previous = _load_manifest(docs_dir)
    wanted: Dict[str, List[str]] = {}
    for spec in specs or []:
        repo, path = parse_spec(spec)
        wanted.setdefault(repo, []).append(path)

    current: Dict[str, str] = {}
    fetched = 0
    for repo, repo_paths in wanted.items():
        tree = _tree(repo, verbose)
        if tree is None:
            current.update(
                {k: v for k, v in previous.items() if k.startswith(repo + "/")}
            )
            continue
        selected: Dict[str, str] = {}
        for path in repo_paths:
            selected.update(_selected(tree, path))
        for path, sha in sorted(selected.items()):
            key = f"{repo}/{path}"
            dest = _cache_path(docs_dir, key)
            if previous.get(key) == sha and os.path.exists(dest):
                current[key] = sha
                continue
            text = _fetch(repo, path)
            if text is None:
                log(f"{key}: download failed", verbose, level=LogLevel.STATUS)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with atomic_open(dest) as fh:
                fh.write(text)
            current[key] = sha
            fetched += 1

    for key in set(previous) - set(current):
        try:
            os.remove(_cache_path(docs_dir, key))
        except OSError:
            pass
    if current or os.path.isdir(docs_dir):
        os.makedirs(docs_dir, exist_ok=True)
        with atomic_open(os.path.join(docs_dir, _MANIFEST)) as fh:
            json.dump(current, fh, indent=1, sort_keys=True)
    log(
        f"repo docs: {len(current)} files ({fetched} downloaded)",
        verbose,
        level=LogLevel.STATUS,
    )
    return len(current)
