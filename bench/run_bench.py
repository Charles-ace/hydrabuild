"""LongMemEval benchmark harness for hydramem.

Usage:
  python bench/run_bench.py [--data data/longmemeval_oracle_sample.json] [--tag myrun] [--limit N]

Ingests each conversation once, asks every question through the live graph
(QueryService over the local HydraDB node), and scores answers with the
official LongMemEval strict-match rubric. Per-question rows are appended to
bench-results/<tag>.jsonl and the aggregate summary is printed and written to
bench-results/<tag>.summary.json.

Honesty rules (see README): numbers reported here use exactly this rubric and
dataset version; abstentions count as wrong unless the gold answer is the
explicit empty/not-found answer.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hydramem.bolt import HydraClient
from hydramem.extraction import build_extractor
from hydramem.ingest import Ingestor
from hydramem.query import QueryService

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "bench-results"


# --------------------------------------------------------------------------
# LongMemEval dataset shapes (adapted from the official LongMemEval repo)
# --------------------------------------------------------------------------

def load_dataset(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if isinstance(data, dict):
        return [{"conversation": turns, "questions": qs}
                for turns, qs in data.items()]
    return data  # list of {conversation, questions}


def turns_to_sessions(conversation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Official format: list of {role, content, has_answer, date?}.

    Group by date boundary when present; otherwise one session with all turns.
    """
    sessions: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_date = ""
    for turn in conversation:
        date = turn.get("date", "")
        if current and date and date != current_date:
            sessions.append({"id": f"s{len(sessions)}", "date": current_date, "turns": current})
            current = []
            current_date = date
        elif not current and date:
            current_date = date
        current.append({"role": turn["role"], "content": turn["content"]})
    if current:
        sessions.append({"id": f"s{len(sessions)}", "date": current_date, "turns": current})
    return sessions


# --------------------------------------------------------------------------
# Official-style strict match (LongMemEval scoring)
# --------------------------------------------------------------------------

_STOP = re.compile(r"[^a-z0-9 ]")


def normalize(text: str) -> str:
    return _STOP.sub(" ", text.lower()).strip()


def answer_is_correct(answer: str, gold: str | list[str]) -> bool:
    """Strict match: exact normalized equality against any gold variant.

    Empty gold (not-found gold) requires an abstention (status == not_found).
    """
    golds = gold if isinstance(gold, list) else [gold]
    if not any(normalize(g) for g in golds):
        return answer == ""
    if answer == "":
        return False
    n = normalize(answer)
    return any(normalize(g) == n for g in golds)


@dataclass
class BenchRow:
    conv_id: str
    question_id: str
    question: str
    gold: str
    status: str
    answer: str
    reason: str
    correct: bool
    latency_ms: int = 0
    plan: dict[str, Any] = field(default_factory=dict)


def run(args: argparse.Namespace) -> None:
    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = ROOT / data_path
    dataset = load_dataset(data_path)

    client = HydraClient()
    ingestor = Ingestor(client)
    qs = QueryService(client)
    extractor = build_extractor()

    RESULTS_DIR.mkdir(exist_ok=True)
    tag = args.tag or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS_DIR / f"{tag}.jsonl"
    seen_ids: set[str] = set()

    rows: list[BenchRow] = []
    correct = 0
    total = 0
    abstained = 0
    last_report: dict[str, Any] = {}

    for conv in dataset:
        conv_id = str(conv.get("id", conv.get("conversation_id", f"c{len(seen_ids)}")))
        if conv_id in seen_ids:
            continue
        seen_ids.add(conv_id)
        client.reset()
        sessions = turns_to_sessions(conv.get("conversation", []))
        report = ingestor.ingest_sessions(sessions, extractor)
        last_report = report.to_dict()

        for question in conv.get("questions", []):
            qid = str(question.get("id", question.get("question_id", f"q{total}")))
            text = question.get("question", "")
            gold = question.get("answer", question.get("ground_truth", ""))
            total += 1
            started = time.perf_counter()
            result = qs.answer(text)
            latency = int((time.perf_counter() - started) * 1000)
            if result.status == "not_found":
                abstained += 1
            ok = answer_is_correct(result.answer, gold)
            correct += int(ok)
            row = BenchRow(
                conv_id=conv_id,
                question_id=qid,
                question=text,
                gold=str(gold),
                status=result.status,
                answer=result.answer,
                reason=result.reason,
                correct=ok,
                latency_ms=latency,
                plan=result.to_dict().get("plan", {}),
            )
            rows.append(row)
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row.__dict__) + "\n")
            print(f"[{row.correct and 'OK ' or 'BAD'}] {qid}: {text[:70]!r} -> {result.answer[:70]!r}")

        if args.limit and len(seen_ids) >= args.limit:
            break
        client.reset()

    client.close()

    summary = {
        "tag": tag,
        "dataset": str(data_path),
        "dataset_version": args.version or "unknown",
        "total_questions": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "abstentions": abstained,
        "abstention_rate": (abstained / total) if total else 0.0,
        "mode": "strict-match (LongMemEval rubric)",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "ingest": last_report,
    }
    summary_path = RESULTS_DIR / f"{tag}.summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data/longmemeval_oracle_sample.json")
    parser.add_argument("--tag", default="")
    parser.add_argument("--limit", type=int, default=0, help="max conversations (0 = all)")
    parser.add_argument("--version", default="", help="dataset version string for provenance")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()