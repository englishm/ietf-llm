# ietf-llm

Maintain a local, queryable corpus of an [IETF](https://www.ietf.org/) effort's public record — a
Working Group, an [IRTF](https://irtf.org/) Research Group, a mailing list, or a set of drafts. It
pulls together charter, drafts, RFCs, meeting agendas, minutes, slides, transcripts, attendance,
mailing list archives, and GitHub issues and pull requests for use with LLM-based tools.

## What it's for

A working group's history is spread across mailing list archives, Datatracker, GitHub, and meeting
materials — too much to hold in your head, and too scattered to search well by hand. With the record
gathered into one queryable corpus, an LLM can help you:

- **Get up to date with the state of discussions** — what's open, what was recently decided, where a
  debate currently stands.
- **Summarise the arguments already made** about an issue — every distinct position on a topic, who
  holds it, and how the chairs ruled.
- **Formulate a new proposal** — surface the objections raised against similar ideas before, so you
  can anticipate them.
- **Fact-check assertions** about what's happened so far — grounded in the actual list traffic and
  chair statements, not someone's recollection.

> **Tip:** for an IRTF Research Group, pass its shortname (e.g. `cfrg`, `hrpc`, `pearg`) wherever
> the docs use `<name>` — it works the same as a WG.

## What the MCP tools cover

The MCP server exposes the record as areas you can ask about, not one tool per source. The RFC and
chartered-group tools work out of the box; the rest read a corpus you've gathered. A few draft and
meeting tools also make live Datatracker lookups, so you get current state rather than the
snapshot.

| Area | You can ask | Main tools | Needs a gather? |
| --- | --- | --- | --- |
| **Orientation** | What's gathered, what a group is doing, who's active, what's open | `overview`, `list_corpora`, `which_corpus` | Yes |
| **Chartered groups** | Which group covers a topic; a group's charter, milestones and history, for every WG and RG, active or concluded | `find_efforts`, `get_group_info`, `search_groups`, `list_milestones`, `group_timeline` | No |
| **Discussion** | What was argued about a topic, as ranked excerpts or a chronological narrative; replies to a message; chair calls and polls | `search_corpus`, `search_corpora`, `read_topic`, `find_related`, `find_replies`, `tally_positions` | Yes |
| **Absence and exact wording** | Whether something was *ever* said, cited or proposed; which revision first used a term | `grep_corpus` | Yes |
| **Issues and pull requests** | Filtered catalogues of issues, PRs, threads and people; why the text says what it does (the PR that changed it, and the issue behind it) | `read_digest`, `get_issue`, `list_labels`, `list_files`, `get_by_url` | Yes |
| **Drafts** | Lifecycle of a group's drafts; verbatim text by section; authors; where a draft is cited in discussion; live state and who reviewed or balloted, on which revision | `list_drafts`, `get_draft`, `draft_authors`, `find_citations`, `find_message_citations`, `draft_status`, `review_record` | Yes, except the live `draft_status` and `review_record` |
| **Meetings** | Minutes with poll tallies, attendance, and the upcoming schedule | `list_meetings`, `read_minutes`, `meeting_schedule` | Yes, except the live `meeting_schedule` |
| **RFCs** | Which RFC covers a topic; what it says, semantically or as an exact string across the whole series; status, what it obsoletes, who cites it | `search_rfc_index`, `search_rfc_text`, `get_rfc_section`, `get_rfc_info`, `grep_corpus` | No |
| **Norms** | How to read the record without overclaiming consensus; how to contribute without wasting the group's time | `read_ietf_interpretation_norms`, `read_ietf_participation_norms` | No |
| **Gathering** | Add or refresh a corpus from inside the client (on by default for a local server) | `start_gather`, `gather_status` | n/a |


## Modes of operation

There are three supported workflows:

1. **Use it as an MCP server** — register it with Claude, Codex, Cursor, Gemini, Zed,
   etc. and ask questions across any corpus you've gathered. Two ways to run it:
   - **[Local MCP](https://github.com/mnot/ietf-llm/blob/main/docs/mcp-local.md)** — one server
     subprocess per client, on your own machine. <-- **Best starting point**
   - **[As a shared HTTP MCP
     server](https://github.com/mnot/ietf-llm/blob/main/docs/mcp-server.md)** — one process serving
     many concurrent clients (hosted).
3. **[Use it from the CLI](https://github.com/mnot/ietf-llm/blob/main/docs/search-cli.md)** — run
   semantic search over the cache directly with `ietf-llm-search`, no LLM client required.

See the workflow documentation linked above for installation and use instructions, or the
[full documentation listing](https://github.com/mnot/ietf-llm/blob/main/docs/README.md).

All three read a corpus you first **gather** with `ietf-llm <corpus>`, except where the table above
says otherwise: the RFC tools cover **every published RFC** and belong to no corpus, and the
chartered-group tools read a mirror of the Datatracker that gather housekeeping (or `ietf-llm
--init`) keeps current. See [gathering](https://github.com/mnot/ietf-llm/blob/main/docs/gathering.md).

> **Heads up — gathering reaches the network by default.** To make a first gather fast, `ietf-llm`
> pulls a prebuilt snapshot of covered corpora from the public
> [seed store](https://github.com/mnot/ietf-llm/blob/main/docs/seed-store.md) at
> `seed-store.mnot.net` (and `list_corpora` looks its catalog up there), then freshens only the
> delta — skipping most of the embedding and download cost. The same store holds the RFC full
> text and the chartered-group mirror that `ietf-llm --init` installs. **To turn this off:** `ietf-llm <corpus>
> --no-seed` (it *persists* across gathers; re-enable with `--seed`), or set `IETF_LLM_SEED_ENABLED=off`
> (or `IETF_LLM_SEED_URL=off`) in your environment. With seeding off, gathers run fully cold and
> offline. It is best-effort either way — an unreachable or unlisted corpus just gathers from scratch.

