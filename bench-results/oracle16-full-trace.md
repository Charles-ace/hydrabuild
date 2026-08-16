# Full per-question trace — 16-question answerable sample

Provenance for the gap-analysis section in `README.md`. All runs scored with the
official LongMemEval strict-match rubric (`bench/run_bench.py`), LLMExtractor
(`openai/gpt-4o-mini` via OpenRouter), against a live local HydraDB graph-node
v0.1.0 (Bolt). Dataset: `data/longmemeval_oracle_sample.json`
(`longmemeval_oracle.json`, cleaned-2025-09, 16-question balanced sample).

| Run | Tag | Date (UTC) | Accuracy | Abstentions |
| --- | --- | --- | --- | --- |
| pre-fix | `bench-results/llm-oracle16b.*` | 2026-08-16 | 0/16 | 9/16 |
| fixes 1+2+3 | `bench-results/llm-oracle16c.*` | 2026-08-16 | 1/16 | 4/16 |
| final: fixes 2+3 | `bench-results/llm-oracle16d.*` | 2026-08-16 | 0/16 | 6/16 |

Final config = two targeted parsing fixes kept from the experiment, lenient
fallback reverted (see the abstention-sample table below for why):
1. relaxed-hint fallback (when predicate-hint filtering yields nothing, retry
   unfiltered with a rank penalty; value-hint safety for yes/no questions kept) —
   **experimented with in 16c, then reverted**,
2. `event_question` prefers Events but no longer hard-excludes Facts/Preferences —
   kept,
3. removed the `parent -> age` keyword mapping — kept.

An attempted re-run of the final config produced **invalid data** (an
OpenRouter `402 Payment Required` after the account's credit balance dropped:
requests with the default 16384-token output cap were rejected). The node then
ingested with the silent empty-extraction fallback, and 14/16 questions
abstained on `no_entity` — an API-failure artifact, not a config measurement.
Those rows (`llm-oracle16d` first attempt) were deleted. The valid final runs
below used `HYDRA_MEM_LLM_MAX_TOKENS=8000`, which fits the remaining balance;
extraction output never approaches that cap.

## Per-question table

Columns: gold | pre-fix answer | final-config answer | stage-isolation notes.

| # | Question (abbrev.) | Gold | Pre-fix (16b) | Final (16d) | Notes |
| --- | --- | --- | --- | --- | --- |
| q1 | What time do I wake up on Tuesdays and Thursdays? | 6:45 AM | 7:00 AM ✗ | 7:00 AM ✗ | Reasoning gap: graph holds `wake_up_time=7:00 AM`; 6:45 requires composing "15 min earlier on Tue/Thu". No extraction/parsing bug. |
| q2 | Which event did I participate in first, volleyball league or charity 5K? | volleyball league | not_found ✗ | not_found ✗ | Extraction variance: some runs produce no `sport`-predicate fact. Even the right event would fail strict match ("volleyball" ≠ "volleyball league"). |
| q3 | Days between spark-plug replacement and Turbocharged Tuesdays | 29/30 | 240 horsepower ✗ | 240 horsepower ✗ | Reasoning gap: date arithmetic between two occurrences; events were not extracted as Event nodes. |
| q4 | How many years older is my grandma than me? | 43 | 75 ✗ | 75 ✗ | Reasoning gap: `grandma.age=75` retrieved correctly; 43 = 75 − user's age (arithmetic). |
| q5 | How many music albums or EPs have I purchased or downloaded? | 3 | folk-rock ✗ | EP 'Midnight Sky' ✗ | Reasoning gap: requires counting across multiple purchase facts; different wrong fact each run (nondeterminism). |
| q6 | How many years will I be when my friend Rachel gets married? | 33 | Rachel's wedding ✗ | Rachel's wedding ✗ | Reasoning gap: age composition at a dated future event; the event node itself is found. |
| q7 | How long have my parents been staying with me in the US? | nine months | not_found ✗ | "ask about the process" ✗ | Parsing gap (fixed): `parents->stay_duration='nine months'` IS in the graph; pre-fix the `parent->age` keyword hint filtered it out. Final config answers a wrong fact — grounding works, value selection doesn't. |
| q8 | How many dozen eggs do we currently have stocked up? | 20 | 20 dozen ✗ | 20 dozen ✗ | Formatting gap: value stored with unit (`egg_stock=20 dozen`); strict match on "20". |
| q9 | How many short stories have I written since writing regularly? | seven | Sundays ✗ | Sundays ✗ | Reasoning gap: counting; `writing_day=Sundays` retrieved instead. |
| q10 | How many largemouth bass did I catch on my fishing trip to Lake Michigan? | 12 | not_found ✗ | Montana or Colorado ✗ | Parsing gap (fixed): `caught_largemouth_bass='12'` IS in the graph; pre-fix `event_question=true` hard-excluded Fact nodes. Fix 2 holds without fix 1 — the question answers (grounded) instead of abstaining, though rank picks a wrong fact this run. |
| q11 | What was the discount on my first purchase from the new clothing brand? | 10% | not_found ✗ | "new clothing brand" ✗ | Nondeterminism: the 16c run answered `10%` correctly (variance, not the fix); 16b and 16d missed/answered wrong. |
| q12 | Remind me of the Mayo Clinic posture video | video title + URL | not_found ✗ | not_found ✗ | Genuine extraction gap: graph has 5 Person nodes and zero Facts/Preferences/Events — the recommendation was never extracted. No parse/traversal fix can answer. |
| q13 | That Soviet cartoon that mocked Western culture | Nu, pogodi! | no_entity ✗ | no_entity ✗ | Nondeterministic extraction + extraction gap: some runs create a "Michelle Wolf"/"political humor" person and a wrong fact; the cartoon title is never extracted as a node. |
| q14 | Activities during commute to work | long preference paragraph | not_found ✗ | no_matching_value ✗ | Rubric-unreachable: correct prefs ARE in the graph (`interested_in='history podcasts'`, `podcast_genres=...`); value-hint safety (yes/no question) keeps abstention. No value-retrieval answer can strict-match a paragraph gold. |
| q15 | Hotel for upcoming Miami trip | long preference paragraph | not_found ✗ | no_entity ✗ | Rubric-unreachable: correct prefs ARE in the graph (`hotel_features='rooftop pool or hot tub on the balcony'`, `'great view of the city'`); same paragraph-gold limitation. This run's abstention was `no_entity` (persona-resolution variance) — the 16c run's fix-2 conversion of this question is not reproducible run-to-run. |
| q16 | Tips on what to bake for colleagues | long preference paragraph | not_found ✗ | not_found ✗ | Rubric-unreachable (paragraph gold). The baking fact (`made='lemon poppyseed cake'`) is in the graph but the hint filter excludes it without fix 1 — an honest abstention for a question no value-retrieval answer can score on. |

## Abstention sample (pre/final)

Dataset: `data/longmemeval_abstain_sample.json` (`_abs` questions, 12-session cap).

| Run | Tag | Accuracy | Abstentions | P / R |
| --- | --- | --- | --- | --- |
| pre-fix | `bench-results/llm-abstain4.*` | 0/4 | 1/4 | precision 1.0, recall 0.25 |
| fixes 1+2+3 | `bench-results/llm-abstain4c.*` | 0/4 | 0/4 | precision 0.0, recall 0.0 |
| final: fixes 2+3 | `bench-results/llm-abstain4d.*` | 0/4 | 3/4 | precision 1.0, recall 0.75 |

Per-question:

| # | Pre-fix (4b) | Fixes 1+2+3 (4c) | Final (4d) |
| --- | --- | --- | --- |
| abs1 | "just a short bus ride away from Kew Gardens" ✗ | "peaceful and tranquil" ✗ | "dinosaur exhibit at the American Museum of Natural History" ✗ |
| abs2 | "therapist" ✗ | "therapist" ✗ | **abstain** (no_entity) ✓ |
| abs3 | **abstain** (no_matching_fact) ✓ | "simple and functional" ✗ | **abstain** (no_entity) ✓ |
| abs4 | "Chief Medical Officer of the Frontier Service" ✗ | "Frontier Service" ✗ | **abstain** (no_entity) ✓ |

The fix-1 relaxation over-answered everything (all 4 confident-wrong, including
the one correct abstention abs3). Reverting it restored and improved abstention:
3/4 abstained in the final run. The final run's abstentions are `no_entity`
(persona resolution), the pre-fix one was `no_matching_fact` — different
structural mechanisms, both never-an-LLM-guess, and which one fires varies
with extraction variance, not with the config. abs1 over-answers with a
grounded-but-unrelated fact in every configuration — the remaining known
abstention gap (rank selects a matching-predicate fact that does not answer).

## Verified non-causes

- **Traversal bound is not a factor.** Every question re-run with
  `MAX_PATH_LEN=6` produced identical outcomes (see `scratch/diag_bench.py` and
  `bench-results/diag-oracle16.json` — per-conversation graph dumps + traced
  plans). The schema is star-shaped (Person→Session←Fact), hop length ≤ 2.
- **Not a mock/silent-fallback artifact.** MockExtractor extracts nothing from
  LongMemEval transcripts (see `bench-results/mock-smoke.*`); the run ingest
  totals (96 facts, 63 prefs, 8 events over 26 sessions) and the grounded
  verbose answers referencing LLM-invented predicates (`music_style`,
  `egg_stock`, `previous_dyno_test_horsepower`) are only producible by the
  live LLM extractor. No conversation shows the empty-extraction signature of
  a swallowed API error (ingest.py silently falls back to empty only when the
  call raises).

## Run-to-run nondeterminism

Same code, same model, same key produces different extractions per run. Three
questions flipped between the pre-fix and post-fix runs (q2, q11, q13) and
q11's single post-fix "correct" is itself a variance flip, not a fix effect.
Single-run scores therefore carry ±noise; treat the point numbers above as a
range, not a stable measurement. If re-running, report the best-of-N and the
flip count.