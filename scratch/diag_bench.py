"""Diagnostic: per-question pipeline trace for the llm-oracle16b bench run.

Reproduces the exact bench conditions (reset per conversation, LLMExtractor,
MAX_PATH_LEN=3) and additionally:
  1. dumps every node the LLM extractor wrote to the graph (raw Cypher),
  2. traces the QueryService parse/resolve/traverse/candidate stages,
  3. re-answers with MAX_PATH_LEN=6 to test the traversal-bound hypothesis,
  4. flags conversations where extraction produced nothing (silent-fallback
     symptom from ingest.py:71-77).

Usage:
  $env:PYTHONPATH="src"; $env:HYDRA_MEM_LLM_MODE="llm"; $env:HYDRA_MEM_LLM_API_KEY="..."
  .venv\Scripts\python scratch\diag_bench.py [--convs q2 q7 q10]  (default: all)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hydramem import config  # noqa: E402
from hydramem.bolt import HydraClient  # noqa: E402
from hydramem.extraction import LLMExtractor  # noqa: E402
from hydramem.ingest import Ingestor  # noqa: E402
from hydramem.query import QueryService  # noqa: E402

DATA = ROOT / "data" / "longmemeval_oracle_sample.json"


def reset_gradual(client: HydraClient, max_attempts: int = 10) -> None:
    """Reset per label with retries; a bare graph-node's DETACH DELETE can
    time out or error once the WAL grows. No WITH/LIMIT/RETURN after the
    delete — the mutation engine rejects those shapes."""
    from hydramem import schema

    for label in schema.ALL_NODE_LABELS:
        for attempt in range(max_attempts):
            try:
                client.run(f"MATCH (n:{label}) DETACH DELETE n")
                break
            except Exception:  # noqa: BLE001
                import time

                time.sleep(3)
        else:
            raise RuntimeError(f"reset failed for {label}")


def dump_graph(client: HydraClient) -> dict:
    out = {}
    out["persons"] = client.run("MATCH (p:Person) RETURN p.name AS name, p.aliases AS aliases ORDER BY p.name")
    out["facts"] = client.run(
        "MATCH (f:Fact) RETURN f.subject AS subject, f.predicate AS predicate, "
        "f.value AS value, f.session_index AS si, f.superseded AS superseded "
        "ORDER BY f.subject, f.predicate"
    )
    out["prefs"] = client.run(
        "MATCH (p:Preference) RETURN p.subject AS subject, p.predicate AS predicate, "
        "p.value AS value, p.session_index AS si, p.superseded AS superseded "
        "ORDER BY p.subject, p.predicate"
    )
    out["events"] = client.run(
        "MATCH (e:Event) RETURN e.summary AS summary, e.date AS date, e.session_index AS si "
        "ORDER BY e.si"
    )
    return out


def trace(client: HydraClient, question: str, max_len: int) -> dict:
    config.MAX_PATH_LEN = max_len
    qs = QueryService(client)
    plan = qs._parse_question(question, client.all_person_names())
    sources = qs._resolve_people(plan.people)
    paths, procedure = qs._traverse(sources)
    candidates = qs._collect_candidates(paths, plan)
    result = qs.answer(question)
    return {
        "max_len": max_len,
        "plan": {
            "people": plan.people,
            "predicate_hints": plan.predicate_hints,
            "history_mode": plan.history_mode,
            "event_question": plan.event_question,
            "value_hints": plan.value_hints,
        },
        "resolved_person_ids": sources,
        "path_count": len(paths),
        "candidate_count": len(candidates),
        "status": result.status,
        "reason": result.reason,
        "answer": result.answer,
        "verbose": result.verbose_answer,
        "procedure": procedure,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--convs", default="", help="space-separated qids to run (default: all)")
    args = ap.parse_args()

    data = json.loads(DATA.read_text(encoding="utf-8"))["data"]
    want = set(args.convs.split()) if args.convs else set()

    client = HydraClient()
    client.verify()
    extractor = LLMExtractor()
    ingestor = Ingestor(client)

    report_path = ROOT / "bench-results" / "diag-oracle16.json"
    done: set[str] = set()
    report: list[dict] = []
    if report_path.exists():
        for entry in json.loads(report_path.read_text(encoding="utf-8")):
            report.append(entry)
            done.add(entry["qid"])

    for conv in data:
        qid = str(conv["questions"][0].get("id", ""))
        if want and qid not in want:
            continue
        if qid in done:
            print(f"[{qid}] already in report, skipping")
            continue
        conv_id = conv["id"]
        gold = conv["questions"][0].get("answer", "")
        text = conv["questions"][0]["question"]

        reset_gradual(client)
        sessions = []
        current, current_date = [], ""
        for turn in conv["conversation"]:
            date = turn.get("date", "")
            if current and date and date != current_date:
                sessions.append({"id": f"s{len(sessions)}", "date": current_date, "turns": current})
                current, current_date = [], date
            elif not current and date:
                current_date = date
            current.append({"role": turn["role"], "content": turn["content"]})
        if current:
            sessions.append({"id": f"s{len(sessions)}", "date": current_date, "turns": current})

        rep = ingestor.ingest_sessions(sessions, extractor)
        graph = dump_graph(client)
        t3 = trace(client, text, 3)
        t6 = trace(client, text, 6)

        report.append(
            {
                "qid": qid,
                "conv_id": conv_id,
                "question": text,
                "gold": str(gold),
                "n_sessions": len(sessions),
                "ingest": rep.to_dict(),
                "graph": graph,
                "trace_len3": t3,
                "trace_len6": t6,
            }
        )
        print(f"[{qid}] sessions={len(sessions)} ingest={rep.to_dict()} "
              f"| len3={t3['status']}({t3['reason']},paths={t3['path_count']},cands={t3['candidate_count']}) "
              f"| len6={t6['status']}({t6['reason']},paths={t6['path_count']},cands={t6['candidate_count']})")

        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\nwrote {report_path} ({len(report)} conversations)")
    client.close()


if __name__ == "__main__":
    main()