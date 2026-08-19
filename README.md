# hydramem

Graph-native agent memory layer for **HydraDB**, built for the Hack Hydra — Track 3
(Memory & Context Retrieval) hackathon, Aug 12–20 2026.

## Problem

LLM agents forget. The standard fix — stuffing raw conversation history into a
prompt or a vector store — either blows the context window or retrieves
semantically similar-but-wrong text, and LLMs happily *invent* an answer when
the memory is silent. Agent memory needs three properties a retrieval system
must not fake: (1) exact-value retrieval ("what is the standup format *now*"
vs "*last month*" must not blur), (2) temporal correctness (a superseded fact
must be ranked below the current one), and (3) **structural abstention** —
when the graph provably contains no matching fact, the system must say "not
found" rather than guess. This project treats those as first-class design
constraints, not accidents of prompting.

## What was built

Conversation history is extracted into a property graph of `Person`, `Fact`,
`Event`, `Preference` and `Session` nodes. Queries are answered by bounded graph
traversal (`algo.SSpaths`) with an explicit **abstention protocol**: when the
graph does not contain a fact matching the question, the system says "not found"
instead of inventing an answer. Contradictions are recorded as
`CONTRADICTS` / `SUPERSEDES` edges, so the system can answer "what was X before
the switch?" and "what is X now?" from the same graph. Includes a live
LongMemEval-s benchmark harness (`bench/`) with honest, measured results
(see the gap analysis) and a recorded demo walkthrough — the demo video is
submitted as a link on the hackathon submission form (YouTube link will be
pasted here once the video is published).

## Why HydraDB (and what breaks without it)

The memory layer is a *graph query* end to end, not a document store:

- Retrieval is **bounded path traversal** (`algo.SSpaths` from the persona
  node through `MENTIONED_IN` edges to statement nodes) — the abstention
  protocol depends on the traversal returning *exactly* the statements a
  person is connected to, and abstaining when the set is empty.
- **Temporal correctness** is encoded as `CONTRADICTS`/`SUPERSEDES` edges
  written by the ingest contradiction pass and read back during traversal
  ranking — a table scan or vector similarity cannot express "superseded in
  session 4" as a first-class graph relationship.
- Ingestion is **idempotent by construction**: deterministic 63-bit content
  hashes + `UNWIND ... MERGE` upserts, so re-running a conversation never
  duplicates nodes.

Without HydraDB, the core claims collapse: a vector store loses exact-value
and abstention guarantees, and an LLM-in-the-loop re-reader replaces
structural abstention with statistical guessing — the exact failure mode this
build exists to avoid. The "Verified HydraDB behaviour" section below records
the real engine quirks the implementation had to work around, which is part of
the point: this is a live graph-node integration, not a thin wrapper around a
text database.

## Status

This is a working demo core, **not** a production benchmark submission yet.
Verified end-to-end against a live HydraDB node (v0.1.0, Docker):

- ingestion of 5 sample sessions (deterministic-id MERGE, idempotent),
- 10/10 on the demo question suite (current / history / event / abstain),
- zero hallucinated answers by construction — every answer traces to a real
  graph node, never invented from nothing (the gap analysis below shows cases
  where the *wrong* grounded fact was retrieved; that is a retrieval miss, not
  an invention).

No claim is made about competitive LongMemEval performance — measured
numbers (live LLMExtractor run, strict match) are in `bench-results/` and the
gap-analysis section below. The benchmark harness in `bench/` uses the
official LongMemEval-s rubric (strict string match) with extractor mode and
provenance recorded on every row.

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

## Quickstart (PowerShell; needs Python 3.11+ and Docker)

```powershell
# 1. HydraDB node (docs/hydradb-node.md has the exact verified run config:
#    the current image REQUIRES the full env set below — bare `docker run`
#    exits with "invalid environment variable CLOUD_PROVIDER value `null`")
New-Item -ItemType Directory -Force -Path hydradb-data\store, hydradb-data\cache | Out-Null
'local-development-token-32-bytes' | Set-Content -NoNewline hydradb-data\auth-token
docker run -d --name hydra-node -p 7687:7687 -p 8443:8443 -p 9090:9090 \
  -v "$PWD/hydradb-data:/data" `
  -e CLOUD_PROVIDER=local -e LOCAL_PATH=/data/store `
  -e GRAPH_NAMESPACE=default -e GRAPH_ID=default -e GRAPH_CELL_ID=cell-0 `
  -e GRAPH_CELLS=cell-0 -e GRAPH_NODE_ID=node-0 `
  -e "GRAPH_BOLT_NODE_ADDRESSES=node-0=127.0.0.1:7687" `
  -e GRAPH_ADVERTISED_BOLT_ADDR=127.0.0.1:7687 `
  -e GRAPH_DATA_CACHE_DIR=/data/cache -e GRAPH_AUTH_TOKEN_FILE=/data/auth-token `
  -e GRAPH_ALLOW_PLAINTEXT=true -e RUST_MIN_STACK=33554432 `
  ghcr.io/hydra-db/hydradb:latest

# 2. install
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 3. demo web app (mock extractor — no API key needed)
$env:PYTHONPATH = "src"
.venv/Scripts/uvicorn hydramem.server:app --port 8000
#   open http://localhost:8000/  (serves web/index.html + /api/*)
#   health check: Invoke-RestMethod http://localhost:8000/api/health
```

On Linux/macOS replace steps 1–2 with
`mkdir -p hydradb-data/store hydradb-data/cache && printf '%s\n' 'local-development-token-32-bytes' > hydradb-data/auth-token`,
the same `docker run` (without the backticks), `.venv/bin/pip install -r requirements.txt`,
and `HYDRA_MEM_LLM_MODE` defaults are read from the environment in `src/hydramem/config.py`.

`HYDRA_MEM_LLM_BASE_URL` / `HYDRA_MEM_LLM_API_KEY` / `HYDRA_MEM_LLM_MODEL`
configure the extractor LLM (OpenRouter-compatible, provider-agnostic).
`HYDRA_MEM_LLM_MAX_TOKENS` caps the extraction output budget (default: the
provider's cap; the Aug 16 benchmark runs used `8000`, which also fits small
credit balances that reject the 16384-token default). With no
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

Edges: `MENTIONED_IN` (statements → sessions), `SAME_AS` (alias-list matching and
normalized name-similarity resolution), `CONTRADICTS` / `SUPERSEDES` (stored-value
contradictions with `since`), `CAUSED_BY` / `RELATES_TO` (event links, reserved for
the LLM extractor path). Node ids are 63-bit content hashes (`src/hydramem/ids.py`),
making ingestion idempotent and safe to re-run.

## Entity Resolution

`Ingestor._resolve_person` resolves incoming person mentions against existing
`Person` nodes using a combination of explicit alias lists and normalized string
similarity (`SequenceMatcher` with a 0.80 threshold).

*Note on Scope:* Graph-topology-based resolution (such as multi-hop co-occurrence
scoring and shared-edge graph signals) is identified as a natural extension, not a
current capability of this build.

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

## Honest status & audit

- **Extraction mode**: The 10/10 demo suite runs with `MockExtractor` by default for zero-dependency local testing. When `HYDRA_MEM_LLM_MODE=llm` is configured, `LLMExtractor` sends live HTTP requests to OpenRouter/OpenAI. If credentials are missing or the API fails, the system fails loudly (LLMError raised, ingestion falls back to empty extraction) rather than silently substituting mock data. — *Verify `HYDRA_MEM_LLM_API_KEY` / `OPENROUTER_API_KEY` is set; the active extractor mode is labeled in bench output.*
- **Question parsing**: Uses a pattern/intent resolver (`PREDICATE_KEYWORDS`, `HISTORY_WORDS`, `EVENT_WORDS` regexes). Complex multi-hop temporal reasoning on the full LongMemEval dataset requires an LLM planner pass beyond the current deterministic parser.
- **Entity resolution**: Incoming person mentions are resolved against existing `Person` nodes using explicit alias lists + `SequenceMatcher` name-similarity (0.80 threshold). Graph-topology-based resolution (shared edges, co-occurrence) is identified as a natural extension, not a current capability. — *See `src/hydramem/ingest.py:_resolve_person`; no graph-traversal-based coreference yet.*
- **Contradiction/supersession**: `CONTRADICTS`/`SUPERSEDES` edges are written to HydraDB via OpenCypher and read back during traversal ranking. Correctly resolves "what was X before the switch?" questions.
- **Abstention protocol**: `QueryService.answer()` returns `status="not_found"` (with specific reason) when traversal yields zero results — never an LLM guess. The web demo renders abstentions as a distinct NOT-FOUND state.
- **Benchmark harness**: `bench/run_bench.py` executes full live database ingestion and traversal scoring. Provenance tags and extractor modes are recorded in each JSONL row and summary file. Accuracy and abstention rates depend on the active extractor; the Aug 16 2026 measurements (LLMExtractor) are in `bench-results/llm-oracle16{b,c,d}.*` and `bench-results/llm-abstain4{c,d}.*`.

### Gap analysis (measured Aug 16 2026, LLMExtractor, gpt-4o-mini via OpenRouter)

Three configurations of the same pipeline, live runs against the local HydraDB
node, scored with the official LongMemEval strict-match rubric. Baselines and
experiment records are all kept in `bench-results/` (pre-fix `llm-oracle16b.*`
/ `llm-abstain4.*`; fixes 1+2+3 experiment `llm-oracle16c.*` / `llm-abstain4c.*`;
final config `llm-oracle16d.*` / `llm-abstain4d.*`). Full per-question evidence
(golds, answers, graph dumps, stage isolation) in `bench-results/oracle16-full-trace.md`
and `bench-results/diag-oracle16.json`.

| Config | Questions | Accuracy | Abstentions | Abstention P / R |
| --- | --- | --- | --- | --- |
| Answerable sample, pre-fix | 16 | 0/16 | 9/16 (56%) | — |
| Answerable sample, fixes 1+2+3 | 16 | 1/16 | 4/16 (25%) | — |
| Answerable sample, **final: fixes 2+3** | 16 | 0/16 | 6/16 (38%) | — |
| Abstention sample, pre-fix | 4 | 0/4 | 1/4 (25%) | precision 1.0, recall 0.25 |
| Abstention sample, fixes 1+2+3 | 4 | 0/4 | 0/4 (0%) | precision 0.0, recall 0.0 |
| Abstention sample, **final: fixes 2+3** | 4 | 0/4 | 3/4 (75%) | precision 1.0, recall 0.75 |

**What the numbers actually show:**

- **Retrieval works; exact-value reasoning doesn't.** 10 of 16 answerable
  questions produce a *grounded* graph answer in the final config (7/16
  pre-fix — fixes 2+3 converted 3 of the abstentions into grounded answers).
  But strict-match requires the exact gold value, and the golds need
  multi-fact arithmetic or temporal composition the pipeline does not perform:
  - "What time do I wake up on Tuesdays and Thursdays?" → retrieved
    `wake_up_time: 7:00 AM`; gold `6:45 AM` (requires composing 7:00 with
    "waking up 15 minutes earlier on Tuesdays and Thursdays").
  - "How many years older is my grandma than me?" → retrieved grandma's age
    `75`; gold `43` (requires subtracting the user's age).
  - "How many dozen eggs do we currently have stocked up…?" → retrieved
    `20 dozen`; gold `20` (value right, unit in the answer broke the match).
- **We tried a lenient hint-fallback and reverted it.** Fix 1 (when
  predicate-hint filtering yields nothing, retry unfiltered with a rank
  penalty) improved raw retrieval — 12/16 grounded, one strict "correct"
  (itself extraction variance, not the fix) — but it broke abstention
  integrity on the exact category this system is designed to get right: all
  four unanswerable `_abs` questions answered an unrelated-but-real fact
  (P/R 1.0/0.25 → 0.0/0.0), including abs3 which was the one correct
  abstention pre-fix. That trade is not worth a retrieval point for a
  structural-abstention system, so the fallback was reverted. Final config
  keeps only fixes 2 (event questions prefer Events without hard-excluding
  Facts/Preferences) and 3 (`parent→age` mapping removed) — both pure wins:
  abstention P/R is *better* than pre-fix (0.75 recall vs 0.25) because
  fixes 2+3 stop false grounding, and grounded retrieval still rose 7/16 → 10/16.
- **Abstention is precise but imperfect.** Final config abstains on 3 of 4
  unanswerable questions (abs2/3/4; abs1 still over-answers with a
  grounded-but-unrelated fact). The three abstentions were `no_entity`
  (persona resolution) and the pre-fix one was `no_matching_fact` — different
  mechanisms, both structural (never an LLM guess); which one fires varies
  with extraction, not with the config.
- **Run-to-run nondeterminism.** Same code, same model, same key produces
  different extractions per run (q2, q11, q13 flip across runs; verified with
  repeated runs in `scratch/diag_bench.py`). Treat single-run numbers as a
  range, not a stable measurement.
- **Three questions are structurally unanswerable by this pipeline:**
  - q12 (posture-video follow-up): genuine extraction gap — the graph has 5
    Person nodes and zero facts/prefs/events; the recommendation was never
    extracted. No parse or traversal change can answer.
  - q13 (Soviet cartoon): extraction gap + nondeterminism — the title is
    never extracted as a node.
  - q14/q15/q16 (suggestions): rubric-unreachable — the correct preferences
    ARE in the graph, but the golds are paragraph-length texts that no
    value-retrieval answer can strict-match.
- **Verified non-causes**: traversal bound is not a factor (all questions
  re-run with `MAX_PATH_LEN=6` gave identical outcomes — the schema is
  star-shaped, hops ≤ 2); not a silent-fallback artifact (MockExtractor
  extracts nothing from these transcripts, and every conversation's
  extraction totals are only producible by the live LLM — see
  `bench-results/mock-smoke.*` and the trace file).
- **First-person questions were the initial blocker.** LongMemEval questions
  are written as "What time do **I** wake up…", which the original name-only
  resolver abstained on 12/16 times. `QueryService._user_persona()` now
  resolves first-person pronouns to the most-mentioned persona in the graph
  (`src/hydramem/query.py`); the remaining misses are reasoning/extraction
  gaps, not resolution gaps.
- **Extraction volume**: the answerable run ingested 26 sessions → 123
  people, 104 facts, 60 preferences, 5 events, 2 detected contradictions
  across 16 conversations (volumes vary run to run; see the trace).

The identified roadmap to real LongMemEval-S numbers is unchanged and now
empirically motivated: (1) an LLM reading pass over retrieved facts for
value-format and arithmetic composition, (2) stricter candidate grounding so
abstention fires when the matched fact does not actually answer the question —
the fix-1 experiment shows a relax-if-empty rule over-answers instead, and is
the documented reason the final config keeps tight filtering, (3) per-session
person extraction that preserves the user's name so first-person resolution
does not depend on aggregate statement counts.

**Extractor mode disclaimer**: the bundled demo transcript
(`data/sample_sessions.json`) is tuned for zero-dependency `MockExtractor`
testing; the benchmark numbers above are exclusively from `LLMExtractor` with
a live OpenRouter key. Mock mode is inapplicable to real LongMemEval
conversations (see `bench-results/mock-smoke.*` for the 0-extraction
evidence).

## Attribution & dependencies

- **LongMemEval** — benchmark conversations and questions in `data/`
  (`longmemeval_oracle_sample.json`, `longmemeval_abstain_sample.json`) are
  samples derived from the [LongMemEval](https://github.com/sierra-research/LongMemEval)
  dataset (500-question, cleaned-2025-09 release), used under the LongMemEval
  repository license. The samples were selected to cover the six question
  categories; gold answers are the official ones.
- **HydraDB** — the graph engine (`ghcr.io/hydra-db/hydradb`, AGPL-3.0) is
  used strictly as an external service over Bolt; no HydraDB source code is
  vendored or copied into this repository. The verified local run config in
  `docs/hydradb-node.md` was derived empirically from the published image.
- **Python runtime deps** (`requirements.txt`): `fastapi`, `uvicorn[standard]`,
  `neo4j` (Bolt driver), `httpx`. Third-party datasets/libraries are not
  redistributed inside this repo beyond the derived sample files above.

## Layout

```
src/hydramem/     package (bolt client, ingest, query, extraction, server)
web/index.html    demo UI (force-directed evidence graph, NOT-FOUND state)
data/             sample sessions + questions, LongMemEval oracle/abstain samples
bench/            LongMemEval harness (results land in bench-results/)
bench-results/    measured runs: llm-oracle16{b,c,d}.*, llm-abstain4{c,d}.*, mock-smoke.*,
                  diag-oracle16.json, oracle16-full-trace.md
docs/data-format.md
```

MIT licensed.