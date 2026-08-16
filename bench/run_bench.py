"""LongMemEval benchmark harness for hydramem.

Usage:
  python bench/run_bench.py [--data data/longmemeval_oracle_sample.json] [--tag myrun] [--limit N] [--extractor llm|mock|auto]

Ingests each conversation once, asks every question through the live graph
(QueryService over the local HydraDB node), and scores answers with the
official LongMemEval strict-match rubric. Per-question rows are appended to
bench-results/<tag>.jsonl and the aggregate summary is printed and written to
bench-results/<tag>.summary.json.

Honesty rules (see README): numbers reported here use exactly this rubric and
dataset version; abstentions count as wrong unless the gold answer is the
explicit empty/not-found answer. Extractor mode is explicitly recorded in every output.
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

from hydramem import config
from hydramem.bolt import HydraClient
from hydramem.extraction import LLMExtractor, MockExtractor, build_extractor
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
    return _STOP.sub(" ", str(text).lower()).strip()


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


def gold_is_abstention(question_id: str, gold: str | list[str]) -> bool:
    """True when the gold answer is the 'unanswerable' class.

    Official LongMemEval marks abstention questions with an '_abs' suffix in
    the question id; their gold answers are explanations of why the question
    is unanswerable rather than empty strings.
    """
    if str(question_id).endswith("_abs"):
        return True
    text = " ".join(str(g) for g in (gold if isinstance(gold, list) else [gold]))
    n = normalize(text)
    return any(
        phrase in n
        for phrase in ("did not mention", "not mention this", "not in the history", "unanswerable")
    )


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
    extractor: str = ""
    extractor_mode: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    verbose_answer: str = ""


def run(args: argparse.Namespace) -> None:
    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = ROOT / data_path
    dataset = load_dataset(data_path)

    # Fail loudly if DB is down
    client = HydraClient()
    client.verify()

    ingestor = Ingestor(client)
    qs = QueryService(client)

    extractor_mode = args.extractor
    if extractor_mode == "auto":
        extractor_mode = config.LLM_MODE

    extractor = build_extractor(mode=extractor_mode)
    extractor_name = extractor.__class__.__name__
    is_llm = isinstance(extractor, LLMExtractor)

    print(f"=== Running benchmark: Extractor = {extractor_name} (mode: {extractor_mode}) ===")
    if is_llm:
        print(f"    Model: {config.LLM_MODEL} | Base URL: {config.LLM_BASE_URL}")

    RESULTS_DIR.mkdir(exist_ok=True)
    tag = args.tag or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = RESULTS_DIR / f"{tag}.jsonl"
    seen_ids: set[str] = set()

    rows: list[BenchRow] = []
    correct = 0
    total = 0
    abstained = 0
    gold_empty_total = 0
    gold_empty_abstained = 0
    ingest_totals: dict[str, int] = {
        "sessions_ingested": 0, "people": 0, "facts": 0, "events": 0,
        "preferences": 0, "same_as_edges": 0, "contradictions": 0,
        "supersessions": 0,
    }

    for conv in dataset:
        conv_id = str(conv.get("id", conv.get("conversation_id", f"c{len(seen_ids)}")))
        if conv_id in seen_ids:
            continue
        seen_ids.add(conv_id)
        client.reset()
        sessions = turns_to_sessions(conv.get("conversation", []))
        report = ingestor.ingest_sessions(sessions, extractor)
        for key in ingest_totals:
            ingest_totals[key] += report.to_dict().get(key, 0)

        for question in conv.get("questions", []):
            qid = str(question.get("id", question.get("question_id", f"q{total}")))
            text = question.get("question", "")
            gold = question.get("answer", question.get("ground_truth", ""))
            golds = gold if isinstance(gold, list) else [gold]
            gold_empty = not any(normalize(g) for g in golds)
            is_abstention = gold_is_abstention(qid, gold)
            total += 1
            started = time.perf_counter()
            result = qs.answer(text)
            latency = int((time.perf_counter() - started) * 1000)
            if result.status == "not_found":
                abstained += 1
                if gold_empty or is_abstention:
                    gold_empty_abstained += 1
            if gold_empty or is_abstention:
                gold_empty_total += 1
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
                extractor=extractor_name,
                extractor_mode=extractor_mode,
                plan=result.to_dict().get("plan", {}),
                verbose_answer=result.verbose_answer,
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
        "extractor": extractor_name,
        "extractor_mode": extractor_mode,
        "llm_model": config.LLM_MODEL if is_llm else "none",
        "total_questions": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "abstentions": abstained,
        "abstention_rate": (abstained / total) if total else 0.0,
        "abstention_precision": (gold_empty_abstained / abstained) if abstained else 0.0,
        "abstention_recall": (gold_empty_abstained / gold_empty_total) if gold_empty_total else 0.0,
        "gold_empty_questions": gold_empty_total,
        "mode": "strict-match (LongMemEval rubric)",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "ingest": ingest_totals,
    }
    summary_path = RESULTS_DIR / f"{tag}.summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("\nBenchmark Summary:")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data/longmemeval_oracle_sample.json")
    parser.add_argument("--tag", default="")
    parser.add_argument("--limit", type=int, default=0, help="max conversations (0 = all)")
    parser.add_argument("--version", default="", help="dataset version string for provenance")
    parser.add_argument(
        "--extractor",
        choices=["auto", "llm", "mock"],
        default="auto",
        help="Extraction engine to use ('llm' fails loudly if API key is missing)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()