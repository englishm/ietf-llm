"""Tests for the chartered-group mirror: the writer
(`gather.sources.groups`), the reader (`singletons.groups`), the charter
chunker, the MCP tools, and the charter-aware `find_efforts`.

The writer is driven with canned Datatracker payloads through its `fetch`
seam, then the bytes it wrote are read back through the reader tools: the
writer->reader round trip is what catches format drift.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings import chunking
from ietf_llm.gather.sources import groups as writer
from ietf_llm.log import Verbosity
from ietf_llm.mcp import groups as tools
from ietf_llm.paths import get_cache_dir
from ietf_llm.singletons import catalog
from ietf_llm.singletons import groups as reader

# --- canned Datatracker payloads --------------------------------------------

_WG = "/api/v1/name/grouptypename/wg/"
_RG = "/api/v1/name/grouptypename/rg/"
_ACTIVE = "/api/v1/name/groupstatename/active/"
_CONCLUDED = "/api/v1/name/groupstatename/conclude/"

_GROUPS = [
    {"acronym": "httpbis", "name": "HTTP", "type": _WG, "state": _ACTIVE,
     "parent": "/api/v1/group/group/100/", "resource_uri": "/api/v1/group/group/1/",
     "description": "HTTP semantics, caching, and the extensions around them, maintained as a family.",
     "list_email": "ietf-http-wg@w3.org"},
    {"acronym": "l3vpn", "name": "Layer 3 VPNs", "type": _WG, "state": _CONCLUDED,
     "parent": "/api/v1/group/group/101/", "resource_uri": "/api/v1/group/group/2/",
     "description": "BGP/MPLS IP VPN solutions for provider-provisioned layer-3 virtual private networks."},
    {"acronym": "iccrg", "name": "Internet Congestion Control", "type": _RG, "state": _ACTIVE,
     "parent": "/api/v1/group/group/102/", "resource_uri": "/api/v1/group/group/3/",
     "description": "Research into congestion control for the Internet and its evolving transports."},
]
_AREAS = [
    {"acronym": "wit", "name": "Web and Internet Transport", "resource_uri": "/api/v1/group/group/100/"},
    {"acronym": "rtg", "name": "Routing", "resource_uri": "/api/v1/group/group/101/"},
    {"acronym": "irtf", "name": "IRTF", "resource_uri": "/api/v1/group/group/102/"},
]
_CHARTER_DOCS = [
    {"name": "charter-ietf-httpbis", "rev": "09", "group": "/api/v1/group/group/1/"},
    {"name": "charter-ietf-l3vpn", "rev": "05", "group": "/api/v1/group/group/2/"},
    {"name": "charter-irtf-iccrg", "rev": "03", "group": "/api/v1/group/group/3/"},
]
_CHARTERS = {
    "charter-ietf-httpbis": "The HTTP Working Group maintains HTTP semantics and caching.",
    "charter-ietf-l3vpn": "The L3VPN Working Group specifies BGP/MPLS VPNs.",
    "charter-irtf-iccrg": "ICCRG studies congestion control.",
}


def _milestone(mid: int, group: int, desc: str, due: str, state: str = "active",
               resolved: str = "", time_: str = "2024-01-01T00:00:00Z") -> Dict[str, Any]:
    return {"id": mid, "group": f"/api/v1/group/group/{group}/", "desc": desc, "due": due,
            "resolved": resolved, "state": f"/api/v1/name/groupmilestonestatename/{state}/",
            "docs": ["/api/v1/doc/document/draft-ietf-httpbis-cache/"] if mid == 10 else [],
            "time": time_}


class _Datatracker:
    """Canned responses keyed on URL fragments; records every URL asked for."""

    def __init__(self) -> None:
        self.urls: List[str] = []
        self.extra_milestones: List[Dict[str, Any]] = []
        self.fail = False

    def __call__(self, url: str, verbosity: Verbosity) -> Optional[List[Dict[str, Any]]]:
        self.urls.append(url)
        if self.fail:
            return None
        if "type__in=area" in url:
            return list(_AREAS)
        if "type__in=wg,rg" in url and "/group/group/" in url:
            return list(_GROUPS)
        if "doc/document/?type=charter" in url:
            return list(_CHARTER_DOCS)
        if "groupevent" in url:
            return [{"id": 1, "group": "/api/v1/group/group/2/", "time": "2014-10-22T22:21:24Z",
                     "desc": "State changed to <b>Concluded</b> from Active"}]
        if "type=new_revision" in url:
            return [{"id": 2, "doc": "/api/v1/doc/document/charter-ietf-httpbis/", "rev": "09",
                     "time": "2025-10-13T21:49:21Z"}]
        if "type=iesg_approved" in url:
            return [{"id": 3, "doc": "/api/v1/doc/document/charter-ietf-httpbis/", "rev": "09",
                     "desc": "IESG has approved the charter", "time": "2025-10-13T21:49:22Z"}]
        if "groupmilestonehistory" in url:
            first = _milestone(10, 1, "Submit cache", "2020-06-30", time_="2020-01-01T00:00:00Z")
            return [dict(first, id=900, milestone="/api/v1/group/groupmilestone/10/")]
        if "groupmilestone" in url:
            rows = [
                _milestone(10, 1, "Submit cache", "2099-01-01", time_="2024-04-26T00:00:00Z"),
                _milestone(11, 1, "Submit semantics", "2022-06-30", resolved="Done",
                           time_="2022-07-01T00:00:00Z"),
                _milestone(12, 3, "Dropped thing", "2023-01-01", state="deleted"),
            ]
            return rows + self.extra_milestones
        if "published_rfc" in url:
            return [{"id": 4, "doc": "/api/v1/doc/document/rfc9110/", "time": "2022-06-06T00:00:00Z"}]
        return []

    def feed_urls(self, fragment: str) -> List[str]:
        return [u for u in self.urls if fragment in u]


def _charter_fetch(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
    return _CHARTERS.get(doc)


def _seed_rfc_mirror(home: Path) -> None:
    rfc = home / ".cache" / "ietf-llm" / "_rfc"
    rfc.mkdir(parents=True)
    (rfc / "rfcs.json").write_text(json.dumps({"RFC9110": {"wg": "httpbis", "title": "HTTP Semantics"}}))
    (rfc / "refs.json").write_text("{}")
    (rfc / "tags.json").write_text("{}")


def _refresh(dt: _Datatracker, **kw: Any) -> None:
    kw.setdefault("embed", False)
    kw.setdefault("force", True)
    writer.ensure_groups_index(Verbosity.QUIET, fetch=dt, charter_fetch=_charter_fetch, **kw)


@pytest.fixture(name="mirror")
def _mirror(isolated_home: Path) -> _Datatracker:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    _refresh(dt)
    return dt


# --- writer -> reader --------------------------------------------------------


def test_group_round_trip(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_group("httpbis")
    assert "# httpbis — HTTP" in out
    assert "Working Group · active" in out
    assert "Web and Internet Transport (wit)" in out
    assert "rev 09" in out
    assert "maintains HTTP semantics and caching" in out
    assert "Submit cache (draft-ietf-httpbis-cache)" in out
    assert "1 open of 2" in out


def test_concluded_group_is_labelled(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_group("l3vpn")
    assert "concluded" in out
    assert "no longer active" in out
    assert "last state change 2014-10-22" in out


def test_research_group_has_irtf_area(mirror: _Datatracker) -> None:  # noqa: ARG001
    group = reader.get_group("iccrg")
    assert group is not None
    assert group["type"] == "rg" and group["area"] == "irtf"


def test_unknown_group(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "No chartered group `nope`" in reader.render_group("nope")


def test_timeline_covers_each_source(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_timeline("httpbis")
    assert "Charter revision 09" in out
    assert "IESG has approved the charter (rev 09)" in out
    assert "RFC 9110 published: HTTP Semantics" in out
    assert "Milestone added: Submit cache (due 2020-06)" in out
    assert "Milestone due 2099-01 (was 2020-06): Submit cache" in out
    assert "Milestone done" not in out  # only one version of that milestone
    assert reader.render_timeline("l3vpn").count("State changed to Concluded from Active") == 1


def test_timeline_year_filter(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_timeline("httpbis", 2022)
    assert "RFC 9110" in out and "Charter revision" not in out
    assert "No timeline events" in reader.render_timeline("httpbis", 1999)


def test_dropped_milestones_never_listed(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "Dropped thing" not in reader.render_milestones(status="all", include_closed_groups=True)


# --- milestone listing -------------------------------------------------------


def test_milestones_default_to_open(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_milestones()
    assert "Submit cache" in out and "Submit semantics" not in out


def test_milestones_done_and_all(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "Submit semantics" in reader.render_milestones(status="done")
    both = reader.render_milestones(status="all")
    assert "Submit cache" in both and "Submit semantics" in both


def test_milestones_overdue_and_date_window(mirror: _Datatracker) -> None:  # noqa: ARG001
    mirror.extra_milestones = [_milestone(13, 1, "Late thing", "2020-01-01")]
    _refresh(mirror)
    assert "Late thing" in reader.render_milestones(status="overdue")
    assert "Submit cache" not in reader.render_milestones(status="overdue")
    assert "Late thing" not in reader.render_milestones(due_after="2021-01-01")


def test_milestones_filter_by_area_and_group(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "Submit cache" in reader.render_milestones(area="wit")
    assert reader.render_milestones(area="rtg") == "No milestones match."
    assert "Submit cache" in reader.render_milestones(group="httpbis")
    assert "No chartered group" in reader.render_milestones(group="nope")


def test_milestones_reject_bad_input(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "must be one of" in reader.render_milestones(status="soon")
    assert "ISO date" in reader.render_milestones(due_before="next week")


# --- incremental refresh -----------------------------------------------------


def test_second_refresh_asks_only_for_what_changed(mirror: _Datatracker) -> None:
    mirror.urls.clear()
    _refresh(mirror)
    feeds = mirror.feed_urls("groupmilestone/?")
    assert feeds and all("time__gte=2024-04-26T00:00:00" in u for u in feeds)
    history = mirror.feed_urls("groupmilestonehistory")
    assert history and all("time__gte=2020-01-01T00:00:00" in u for u in history)


def test_new_rows_merge_into_stored_ones(mirror: _Datatracker) -> None:
    mirror.extra_milestones = [_milestone(14, 1, "Fresh", "2099-02-01", time_="2026-01-01T00:00:00Z")]
    _refresh(mirror)
    out = reader.render_milestones(group="httpbis", status="all")
    assert "Fresh" in out and "Submit semantics" in out


def test_failed_refresh_keeps_the_mirror(mirror: _Datatracker) -> None:
    before = reader.render_group("httpbis")
    mirror.fail = True
    _refresh(mirror)
    assert reader.render_group("httpbis") == before


def test_ttl_skips_the_network(mirror: _Datatracker) -> None:
    mirror.urls.clear()
    _refresh(mirror, force=False)
    assert mirror.urls == []


# --- charter fetching --------------------------------------------------------


def test_charters_trickle_in_active_first(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    _refresh(dt, batch=1)
    held = [a for a in ("httpbis", "iccrg", "l3vpn") if reader.charter_text(a)]
    assert held == ["httpbis"]  # active before concluded, alphabetical within
    _refresh(dt, batch=1)
    assert reader.charter_text("iccrg") and not reader.charter_text("l3vpn")
    _refresh(dt, batch=5)
    assert reader.charter_text("l3vpn")


def test_group_without_charter_text_falls_back_to_description(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    writer.ensure_groups_index(
        Verbosity.QUIET, fetch=dt, charter_fetch=lambda d, r: None, force=True, embed=False
    )
    assert reader.charter_text("httpbis") is None
    assert "No charter text available" in reader.render_group("httpbis")
    assert "## Description" in Path(reader.charter_file("httpbis")).read_text()


def test_failed_charter_is_not_retried_at_once(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    calls: List[str] = []

    def failing(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
        calls.append(doc)
        return None

    writer.ensure_groups_index(Verbosity.QUIET, fetch=dt, charter_fetch=failing, force=True, embed=False)
    first = len(calls)
    writer.ensure_groups_index(Verbosity.QUIET, fetch=dt, charter_fetch=failing, force=True, embed=False)
    assert first == 3 and len(calls) == 3


def test_new_charter_revision_is_refetched(mirror: _Datatracker) -> None:
    _CHARTER_DOCS[0]["rev"] = "10"
    try:
        _CHARTERS["charter-ietf-httpbis"] = "Revised charter text."
        _refresh(mirror)
        assert "Revised charter text." in reader.render_group("httpbis")
    finally:
        _CHARTER_DOCS[0]["rev"] = "09"
        _CHARTERS["charter-ietf-httpbis"] = "The HTTP Working Group maintains HTTP semantics and caching."


def test_milestone_change_rerenders_without_refetching(mirror: _Datatracker) -> None:
    fetched: List[str] = []

    def spy(doc: str, rev: str) -> Optional[str]:
        fetched.append(doc)
        return _charter_fetch(doc, rev)

    mirror.extra_milestones = [_milestone(15, 1, "Brand new", "2099-03-01", time_="2026-02-01T00:00:00Z")]
    writer.ensure_groups_index(Verbosity.QUIET, fetch=mirror, charter_fetch=spy, force=True, embed=False)
    assert fetched == []
    assert "Brand new" in Path(reader.charter_file("httpbis")).read_text()


def test_unmirrored_cache_reports_not_gathered(isolated_home: Path) -> None:  # noqa: ARG001
    assert reader.render_group("httpbis") == reader.NOT_GATHERED
    assert reader.render_milestones() == reader.NOT_GATHERED
    assert reader.render_timeline("httpbis") == reader.NOT_GATHERED


# --- chunker -----------------------------------------------------------------


def test_charter_chunks_carry_state_area_and_type(mirror: _Datatracker, isolated_home: Path) -> None:  # noqa: ARG001
    path = reader.charter_file("l3vpn")
    chunks = chunking._chunk_file(path, "charters/l3vpn.md")  # pylint: disable=protected-access
    assert chunks
    first = chunks[0]
    assert first.state == "conclude"
    assert first.labels == "area:rtg,type:wg"
    assert first.title == "l3vpn — Layer 3 VPNs"
    assert first.url == "https://datatracker.ietf.org/doc/charter-ietf-l3vpn/"


def test_state_dir_is_not_embedded(mirror: _Datatracker) -> None:  # noqa: ARG001
    files = chunking._eligible_files(reader.groups_files_dir(), "_groups")  # pylint: disable=protected-access
    assert files and all("/state/" not in f for f in files)
    assert all(f.endswith(".md") for f in files)


# --- semantic search ---------------------------------------------------------


class _AreaModel:
    """Scores everything alike, so scoping can only come from the filters."""

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


@pytest.fixture(name="indexed")
def _indexed(mirror: _Datatracker) -> _Datatracker:
    embeddings._MODEL_CACHE["stub"] = _AreaModel()  # pylint: disable=protected-access
    _refresh(mirror, embed=True, embed_model="stub", force=False)
    return mirror


def test_index_is_built_and_hidden_from_corpus_listing(indexed: _Datatracker) -> None:  # noqa: ARG001
    from ietf_llm.paths import cached_wg_names  # pylint: disable=import-outside-toplevel

    assert tools.index_installed()
    assert "_groups" not in cached_wg_names()


def test_semantic_ranking_scopes_by_area(indexed: _Datatracker) -> None:  # noqa: ARG001
    assert set(tools.semantic_ranking("anything")) == {"httpbis", "iccrg", "l3vpn"}
    assert set(tools.semantic_ranking("anything", area="wit")) == {"httpbis"}
    assert set(tools.semantic_ranking("anything", area="Web and Internet Transport")) == {"httpbis"}
    assert set(tools.semantic_ranking("anything", kind="rg")) == {"iccrg"}
    assert set(tools.semantic_ranking("anything", state="concluded")) == {"l3vpn"}


def test_search_groups_filters_and_labels(indexed: _Datatracker) -> None:  # noqa: ARG001
    out = tools.tool_search_groups("anything", state="closed")
    assert "l3vpn" in out and "httpbis" not in out
    assert "concluded" in out
    assert "No chartered groups match" in tools.tool_search_groups("anything", area="sec")


def test_search_groups_falls_back_to_keywords_without_an_index(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = tools.tool_search_groups("congestion control")
    assert "iccrg" in out
    assert "charter index is not installed" in out


def test_search_groups_before_gather(isolated_home: Path) -> None:  # noqa: ARG001
    assert tools.tool_search_groups("x") == reader.NOT_GATHERED


def test_search_below_the_floor_is_not_a_match(indexed: _Datatracker, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(tools, "SEMANTIC_FLOOR", 1.5)
    assert tools.semantic_ranking("anything") == {}


# --- find_efforts ------------------------------------------------------------


def test_find_efforts_includes_concluded_groups_below_active(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("VPN virtual private networks", 10)
    assert "l3vpn" in out and "concluded" in out


def test_find_efforts_prefers_active_on_equal_match(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("congestion", 10, semantic=["l3vpn", "iccrg"])
    assert out.index("**iccrg**") < out.index("**l3vpn**")


def test_find_efforts_admits_semantic_only_matches(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("zzzz unmatched words", 10, semantic=["httpbis"])
    assert "httpbis" in out
    assert "No efforts match" in catalog.render_efforts("zzzz unmatched words", 10)


def test_find_efforts_pins_a_named_group(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("httpbis", 10, semantic=["iccrg", "l3vpn"])
    assert out.index("**httpbis**") < out.index("**iccrg**")


def test_find_efforts_word_equal_to_an_acronym_is_not_pinned(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("httpbis congestion", 10, semantic=["iccrg"])
    assert out.index("**iccrg**") < out.index("**httpbis**")


def test_find_efforts_falls_back_to_the_catalog(isolated_home: Path) -> None:
    cat = Path(get_cache_dir()) / "_catalog"
    cat.mkdir(parents=True)
    (cat / "catalog.json").write_text(json.dumps([
        {"acronym": "quic", "name": "QUIC", "type": "wg", "state": "active",
         "area": "wit", "area_name": "", "description": "Transport."}
    ]))
    assert "**quic**" in catalog.render_efforts("quic", 5)


def test_row_summary_replaces_a_stub_description(mirror: _Datatracker) -> None:  # noqa: ARG001
    group = dict(reader.get_group("httpbis") or {}, description="Background")
    assert "maintains HTTP semantics" in reader.summary(group)


def test_mirror_is_stamped_fresh(mirror: _Datatracker) -> None:  # noqa: ARG001
    path = Path(reader.groups_files_dir()) / reader.GROUPS_FILE
    assert time.time() - path.stat().st_mtime < 60
