"""Ingestion pipeline: sessions -> typed nodes/edges in HydraDB.

Per session:
1. LLM (or mock) extraction into people/facts/events/preferences.
2. Session node, then typed nodes with deterministic ids (MERGE-idempotent),
   written in batched UNWIND upserts (the only standalone-node upsert form a
   live graph-node accepts).
3. MENTIONED_IN edges from every extracted node to the session.
4. Entity resolution: new person mentions are linked to a canonical person
   via SAME_AS (name similarity or alias membership), never by embeddings.
5. Contradiction pass: conflicting facts/preferences on the same
   subject+predicate get CONTRADICTS and SUPERSEDES edges; the older node is
   flagged superseded but never deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from . import schema
from .bolt import HydraClient, normalize_name
from .extraction import Extraction
from .ids import session_id

log = logging.getLogger(__name__)

SAME_AS_THRESHOLD = 0.8


@dataclass
class IngestReport:
    sessions_ingested: int = 0
    people: int = 0
    facts: int = 0
    events: int = 0
    preferences: int = 0
    same_as_edges: int = 0
    contradictions: int = 0
    supersessions: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions_ingested": self.sessions_ingested,
            "people": self.people,
            "facts": self.facts,
            "events": self.events,
            "preferences": self.preferences,
            "same_as_edges": self.same_as_edges,
            "contradictions": self.contradictions,
            "supersessions": self.supersessions,
        }


class Ingestor:
    def __init__(self, client: HydraClient) -> None:
        self.client = client

    def ingest_sessions(
        self,
        sessions: list[dict[str, Any]],
        extractor: Any,
        start_index: int = 0,
    ) -> IngestReport:
        report = IngestReport()
        for offset, session in enumerate(sessions):
            index = start_index + offset
            try:
                extraction = extractor.extract_session(index, session)
            except Exception as exc:  # never let one session break the run
                log.warning("extraction failed for session %s: %s", index, exc)
                extraction = Extraction()
            self._ingest_session(index, session, extraction, report)
            report.sessions_ingested += 1
        return report

    # -- internals --------------------------------------------------------

    def _ingest_session(
        self,
        index: int,
        session: dict[str, Any],
        extraction: Extraction,
        report: IngestReport,
    ) -> None:
        sid = session_id(index)
        turns = session.get("turns", [])

        session_rows = [
            {
                "id": sid,
                "session_index": index,
                "date": session.get("date", ""),
                "turn_count": len(turns),
            }
        ]
        person_rows: list[dict[str, Any]] = []
        same_as_rows: list[dict[str, Any]] = []
        fact_rows: list[dict[str, Any]] = []
        event_rows: list[dict[str, Any]] = []
        pref_rows: list[dict[str, Any]] = []
        mention_fact: list[dict[str, Any]] = []
        mention_event: list[dict[str, Any]] = []
        mention_pref: list[dict[str, Any]] = []
        mention_person: list[dict[str, Any]] = []
        contradiction_rows: list[dict[str, Any]] = []

        for person in extraction.people:
            name = person["name"]
            aliases = ",".join(person.get("aliases", []))
            mention_id = self.client.upsert_person_row(person_rows, name, aliases)
            report.people += 1
            mention_person.append({"src": mention_id, "dst": sid})
            canonical = self._resolve_person(name, aliases, mention_id)
            if canonical is not None and canonical != mention_id:
                same_as_rows.append(
                    {
                        "src": mention_id,
                        "dst": canonical,
                        "eid": _edge_id(schema.REL_SAME_AS, mention_id, canonical),
                        "confidence": 0.99,
                    }
                )
                report.same_as_edges += 1

        for fact in extraction.facts:
            nid = self.client.upsert_fact_row(fact_rows, fact, index, turns)
            report.facts += 1
            mention_fact.append({"src": nid, "dst": sid})
            self._contradiction_rows(
                contradiction_rows,
                schema.NODE_FACT,
                nid,
                fact["subject"],
                fact["predicate"],
                str(fact["value"]),
                index,
            )

        for event in extraction.events:
            nid = self.client.upsert_event_row(event_rows, event, index, session)
            report.events += 1
            mention_event.append({"src": nid, "dst": sid})

        for pref in extraction.preferences:
            nid = self.client.upsert_pref_row(pref_rows, pref, index)
            report.preferences += 1
            mention_pref.append({"src": nid, "dst": sid})
            self._contradiction_rows(
                contradiction_rows,
                schema.NODE_PREFERENCE,
                nid,
                pref["subject"],
                pref["predicate"],
                str(pref["value"]),
                index,
            )

        # write everything
        self.client.upsert_nodes(schema.NODE_SESSION, session_rows)
        self.client.upsert_nodes(schema.NODE_PERSON, person_rows)
        self.client.upsert_nodes(schema.NODE_FACT, fact_rows)
        self.client.upsert_nodes(schema.NODE_EVENT, event_rows)
        self.client.upsert_nodes(schema.NODE_PREFERENCE, pref_rows)
        for src_label, rows in (
            (schema.NODE_FACT, mention_fact),
            (schema.NODE_EVENT, mention_event),
            (schema.NODE_PREFERENCE, mention_pref),
            (schema.NODE_PERSON, mention_person),
        ):
            self.client.create_edges(schema.REL_MENTIONED_IN, src_label, schema.NODE_SESSION, rows)
        self.client.create_edges(schema.REL_SAME_AS, schema.NODE_PERSON, schema.NODE_PERSON, same_as_rows)
        self._apply_contradictions(contradiction_rows, report)

        report.details.append(
            {
                "session_index": index,
                "date": session.get("date", ""),
                "people": len(extraction.people),
                "facts": len(extraction.facts),
                "events": len(extraction.events),
                "preferences": len(extraction.preferences),
            }
        )

    def _resolve_person(self, name: str, aliases: str, mention_id: int) -> int | None:
        norm = normalize_name(name)
        alias_set = {normalize_name(a) for a in aliases.split(",") if a}
        best: tuple[float, int] = (0.0, -1)
        rows = self.client.run(
            "MATCH (p:Person) WHERE p.id <> $id RETURN p.id AS id, p.name AS name, p.aliases AS aliases",
            {"id": mention_id},
        )
        for row in rows:
            other_norm = normalize_name(row["name"])
            score = 0.0
            if other_norm == norm:
                score = 1.0
            elif other_norm in alias_set:
                score = 0.99
            else:
                score = SequenceMatcher(None, norm, other_norm).ratio()
            other_aliases = {
                normalize_name(a) for a in (row.get("aliases") or "").split(",") if a
            }
            if norm in other_aliases:
                score = max(score, 0.99)
            if score > best[0]:
                best = (score, row["id"])
        if best[0] >= SAME_AS_THRESHOLD:
            return best[1]
        return None

    def _contradiction_rows(
        self,
        rows: list[dict[str, Any]],
        node_label: str,
        new_id: int,
        subject: str,
        predicate: str,
        value: str,
        new_session_index: int,
    ) -> None:
        existing = self.client.find_facts(subject, predicate, value, node_label)
        for old in existing:
            if old["session_index"] > new_session_index:
                continue
            old_id = old["id"]
            rows.append(
                {
                    "label": node_label,
                    "kind": schema.REL_CONTRADICTS,
                    "src": new_id,
                    "dst": old_id,
                    "eid": _edge_id(schema.REL_CONTRADICTS, new_id, old_id),
                    "since": new_session_index,
                }
            )
            if old["session_index"] < new_session_index:
                rows.append(
                    {
                        "label": node_label,
                        "kind": schema.REL_SUPERSEDES,
                        "src": new_id,
                        "dst": old_id,
                        "eid": _edge_id(schema.REL_SUPERSEDES, new_id, old_id),
                        "since": new_session_index,
                    }
                )

    def _apply_contradictions(
        self, rows: list[dict[str, Any]], report: IngestReport
    ) -> None:
        by_label: dict[str, dict[str, list[dict[str, Any]]]] = {}
        supersede_marks: list[tuple[str, int]] = []
        for row in rows:
            by_label.setdefault(row["label"], {}).setdefault(row["kind"], []).append(row)
            if row["kind"] == schema.REL_SUPERSEDES:
                supersede_marks.append((row["label"], row["dst"]))
                report.supersessions += 1
        for label, kinds in by_label.items():
            for kind, edge_rows in kinds.items():
                self.client.create_edges(kind, label, label, edge_rows)
                report.contradictions += len(edge_rows) if kind == schema.REL_CONTRADICTS else 0
        for label, nid in supersede_marks:
            self.client.mark_superseded(label, nid)


# Row builders as small helpers on the client to keep Ingestor readable.
def _edge_id(kind: str, src: int, dst: int) -> int:
    from .ids import rel_id

    return rel_id(kind, src, dst)
