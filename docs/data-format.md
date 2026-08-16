# Data format

## Input: conversation sessions

`data/sample_sessions.json` is a JSON array of sessions:

```json
[
  {
    "id": "s0",
    "date": "2024-05-02",
    "turns": [
      {"role": "user", "content": "hey! i'm sam —"},
      {"role": "assistant", "content": "hey sam, good to see you"}
    ]
  }
]
```

- `id` — unique session id (must match the pattern used by
  `data/sample_questions.json` references; only used for reporting).
- `date` — ISO date of the session. Drives `session_index` ordering (sessions
  are sorted by date before ingestion) and `Event.date`.
- `turns` — ordered `user`/`assistant` messages. Extraction considers user
  turns; `stated_at` is the turn index within the session.

The `LLMExtractor` accepts arbitrary conversations. The `MockExtractor`
(deterministic, no API key needed) fires on a curated pattern set:
names/aliases, standup style, contact channel, pet, location moves, wedding
events, and workspace. Mock output is stable across runs and across sessions
(person identity persists once introduced).

## Question format

`data/sample_questions.json`:

```json
[
  {
    "id": "q1",
    "question": "How does Sam prefer to run standups now?",
    "expect": "answer"
  }
]
```

`expect` ∈ `answer` | `history` | `event` | `abstain`. Used by the smoke suite
to assert the answer/abstention contract, and by the web demo for question
chips.

## Graph schema

| Node label | Properties |
| --- | --- |
| `Person` | `id`, `name`, `aliases` |
| `Fact` | `id`, `subject`, `predicate`, `value`, `text`, `confidence`, `superseded`, `session_index`, `stated_at` |
| `Event` | `id`, `summary`, `date`, `session_index`, `stated_at` |
| `Preference` | `id`, `subject`, `predicate`, `value`, `text`, `confidence`, `superseded`, `session_index`, `stated_at` |
| `Session` | `id`, `session_index`, `date`, `turn_count` |

### Edge types

| Type | Meaning | Properties |
| --- | --- | --- |
| `MENTIONED_IN` | statement node → session it was stated in | `id`, `session_index` |
| `SAME_AS` | two `Person` nodes denote the same identity (alias lists & normalized string similarity; graph-topology is a future extension) | `id`, `confidence` |
| `CONTRADICTS` | newer statement contradicts an older one (different value, same subject/predicate) | `id`, `since` |
| `SUPERSEDES` | newer statement replaces the older one (chronology) | `id`, `since` |
| `CAUSED_BY` | event → triggering event (LLM extractor) | `id` |
| `RELATES_TO` | event ↔ fact/preference (LLM extractor) | `id` |

### Identity and idempotency

Every node id is a deterministic 63-bit content hash
(`src/hydramem/ids.py`):

- `person_id(name)` — canonicalized person name
- `fact_id(subject, predicate, value, session_index, stated_at)` — the
  *content* of the statement, so re-ingesting the same conversation produces
  the same ids and MERGE is a no-op
- `event_id(summary, date, session_index, stated_at)`
- `preference_id(...)` — same shape as `fact_id`
- `session_id(session_index)` — sequential, since sessions are ordered by date
- `edge_id(kind, src, dst)` — stable edge identity

`superseded` is a boolean property on `Fact`/`Preference` nodes, flipped by the
contradiction pass (`SUPERSEDES` edge from the newer node to the older one,
`CONTRADICTS` both ways where the values differ). History is never deleted —
answering "what was X before?" is a traversal over `SUPERSEDES`/`superseded`.

### Predicate vocabulary (canonical)

`works_at`, `location`, `pet`, `language` (facts);
`standup_style`, `contact_channel` (preferences). Other predicates are
permitted; the keyword planner (`src/hydramem/query.py`, `PREDICATE_KEYWORDS`)
maps question words to this vocabulary.

## Query/answer envelope

`POST /api/query {"question": ...}` returns

```json
{
  "answer": "...",          // absent on abstention
  "not_found": true,        // set on abstention
  "reason": "no_matching_fact", // one of the abstention codes
  "plan": {"people": ["sam"], "predicate_hints": ["standup"], ...},
  "evidence": [{"id": ..., "label": "Person", "props": {...}}, ...]
}
```

`evidence` is the winning traversal path (Person → Session → statement) and is
rendered by the web demo as a force-directed graph. Abstention reasons:
`no_entity`, `no_path`, `no_matching_fact`, `no_matching_value`.

## Benchmark inputs (LongMemEval)

`bench/` consumes the official LongMemEval-1023 dataset
(`data/longmemeval_*.json`; the oracle-grounded variant is
`data/longmemeval_oracle_sample.json`, kept in git as an example). The harness
ingests each conversation once, then asks each question through
`/api/query`-equivalent logic and scores answers with the official
strict-match rubric. Raw per-question rows land in `bench-results/` as JSONL;
aggregate numbers are reported there with the exact prompt/version used.