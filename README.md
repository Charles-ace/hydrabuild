# hydramem

Graph-native agent memory layer for **HydraDB**, built for the Hack Hydra — Track 3
(Memory & Context Retrieval) hackathon, Aug 12–20 2026.

Conversation history is extracted into a property graph of `Person`, `Fact`,
`Event`, `Preference` and `Session` nodes. Queries are answered by bounded graph
traversal (`algo.SSpaths`) with an explicit **abstention protocol**: when the
graph does not contain a fact matching the question, the system says "not found"
instead of inventing an answer. Contradictions are recorded as
`CONTRADICTS` / `SUPERSEDES` edges, so the system can answer "what was X before
the switch?" and "what is X now?" from the same graph.

## Status

This is a working demo core, **not** a production benchmark submission yet.
Verified end-to-end against a live HydraDB node (v0.1.0, Docker):

- ingestion of 5 sample sessions (deterministic-id MERGE, idempotent),
- 10/10 on the demo question suite (current / history / event / abstain),
- zero hallucinated answers by construction (structural abstention).

No claim is made about LongMemEval performance. The benchmark harness exists in
`bench/`; its numbers — once produced — will be reported honestly in
`bench-results/`, with the same methodology HydraDB's published figures use
(`LongMemEval-s`, strict string match).

## Architecture

```
conversation sessions
        |
        v
  [LLMExtractor | MockExtractor]      structured fact extraction
        |
        v
       Ingestor                       deterministic 63-bit ids -> idempotent MERGE
        |
        v
   HydraClient                       UNWIND batch upserts, contradiction pass
        |
        v
   HydraDB graph                     Person/Fact/Event/Preference/Session
        ^
        |
  QueryService                       parse -> resolve -> SSpaths -> rank -> answer/abstain
```

## Quickstart

```bash
# 1. HydraDB node (see docs/hydradb-node.md for the verified run config)
docker run -d --name hydra-node -p 7687:7687 -p 8443:8443 -p 9090:9090 \
  ghcr.io/hydra-db/hydradb:latest

# 2. install
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 3. ingest the sample conversations and ask questions
$env:PYTHONPATH = "src"
.venv/Scripts/python -c "from hydramem.server import *"   # or run the demo UI:

# 4. demo web app
$env:PYTHONPATH = "src"; .venv/Scripts/uvicorn hydramem.server:app --port 8000
#   open http://localhost:8000/  (serves web/index.html + /api/*)
```

`HYDRA_MEM_LLM_BASE_URL` / `HYDRA_MEM_LLM_API_KEY` / `HYDRA_MEM_LLM_MODEL`
configure the extractor LLM (OpenRouter-compatible, provider-agnostic). With no
key set, the deterministic `MockExtractor` is used.

## Verified HydraDB behaviour (differs from published cypher-compat notes)

Empirically confirmed against v0.1.0 — worth knowing before you write Cypher:

- **Standalone node `CREATE`/`MERGE` is rejected** ("only one-hop edge patterns
  are executable in Query engine CREATE/MERGE"). Node upserts must use
  `UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Label, n.prop = row.prop`,
  and every SET value must come from the row map (literals rejected).
- **`MERGE` with following clauses is rejected**; one-hop `MATCH ... MERGE` edge
  batches work, but endpoints need exactly one label and `None` values in row
  maps are rejected.
- **Composite parameters are only supported as UNWIND inputs** — edge type
  lists for path procedures must be inlined literals (`relTypes: ['F','G']`).
- **`algo.MSpaths` returns empty** on a bare graph node (it expects property
  indexes built by the `graph-indexer` role). `algo.SSpaths` works and is the
  traversal primitive used here.
- Bare `MATCH (n) DETACH DELETE n` is rejected — reset iterates over the known
  node labels.
- Bolt path values are flattened by `record.data()` into alternating
  `[props, rel_type, props, ...]` lists; node ids/labels are reconstructed from
  properties (ids are deterministic content hashes, so this is lossless).

## Schema

| Node label | Properties |
| --- | --- |
| `Person` | `id`, `name`, `aliases` |
| `Fact` | `id`, `subject`, `predicate`, `value`, `text`, `confidence`, `superseded`, `session_index`, `stated_at` |
| `Event` | `id`, `summary`, `date`, `session_index`, `stated_at` |
| `Preference` | `id`, `subject`, `predicate`, `value`, `text`, `confidence`, `superseded`, `session_index`, `stated_at` |
| `Session` | `id`, `session_index`, `date`, `turn_count` |

Edges: `MENTIONED_IN` (statements → sessions), `SAME_AS` (person identity
resolution), `CONTRADICTS` / `SUPERSEDES` (stored-value contradictions with
`since`), `CAUSED_BY` / `RELATES_TO` (event links, reserved for the LLM
extractor path). Node ids are 63-bit content hashes
(`src/hydramem/ids.py`), making ingestion idempotent and safe to re-run.

## Abstention protocol

`QueryService.answer()` returns one of:

- `answer` — best non-superseded (or, for history questions, superseded) node
  found by traversal, with its evidence path,
- `no_entity` — the person could not be resolved,
- `no_path` — the person exists but traversal found no relevant statements,
- `no_matching_fact` — statements exist but none match the question,
- `no_matching_value` — the fact exists but its value contradicts the question
  (e.g. "does Sam have a dog?" when the stored pet is a cat).

The system **never** answers from LLM priors: if traversal comes up empty, it
abstains. The web demo renders abstentions as a distinct NOT-FOUND state.

## Honest gap analysis (vs HydraDB's published numbers)

HydraDB reports 90.79% on LongMemEval-s (the track brief). This repo does not
yet claim any LongMemEval score. Known gaps to close before claiming parity:

1. The extractor here is the deterministic `MockExtractor`; the
   `LLMExtractor` (OpenRouter) is written but its extraction quality on real
   LongMemEval conversations is unmeasured.
2. Question parsing uses a keyword/pattern matcher, not an LLM planner; it
   answers the demo suite 10/10 but will miss LongMemEval's harder
   temporal/reasoning questions (e.g. cross-event ordering).
3. The benchmark harness (`bench/`) runs the official LongMemEval questions
   against the graph and scores with the official strict-match rubric — it has
   not yet been run at scale, so **no number is published here**.

## Layout

```
src/hydramem/     package (bolt client, ingest, query, extraction, server)
web/index.html    demo UI (force-directed evidence graph, NOT-FOUND state)
data/             sample sessions + questions
bench/            LongMemEval harness (results land in bench-results/)
docs/data-format.md
```

MIT licensed.