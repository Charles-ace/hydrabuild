"""Thin wrapper around the Neo4j Bolt driver for a local HydraDB graph-node.

Verified against a live `graph-node` v0.1.0 (Aug 2026):

- The query engine accepts only ONE edge pattern per CREATE/MERGE statement,
  and node-only CREATE/MERGE is rejected ("only one-hop edge patterns are
  executable"). Standalone node upserts therefore go through the batched
  `UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Label, ...` form, where
  every SET value MUST read a field from the row map (literals are rejected).
- Edge batches use `UNWIND $rows AS row MATCH (a:LabelA {id: row.src}),
  (b:LabelB {id: row.dst}) MERGE (a)-[r:TYPE {id: row.eid}]->(b) SET ...`.
  Endpoints must carry exactly one label each.
- `algo.SSpaths({sourceNode: int, ...})` resolves by vertex id and works on a
  bare graph-node. `algo.MSpaths` resolves many source/target *property
  values* and needs property indexes, which are built by the separate
  `graph-indexer` role; on a bare node it returns nothing. Traversal in this
  repo therefore uses SSpaths per resolved source id (see query.py).
- Paths come back as driver Path objects: node.element_id is the node id,
  node.labels and node._properties carry the rest.

STRICT MODE: Every query and write executes directly over Bolt against HydraDB.
No stubs, no in-memory fallbacks. If HydraDB is down, operations fail loudly.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Sequence

from neo4j import GraphDatabase

from . import config, ids, schema

log = logging.getLogger(__name__)

BATCH_SIZE = 100


class HydraClient:
    def __init__(
        self,
        uri: str = config.BOLT_URI,
        token: str = config.BOLT_AUTH_TOKEN,
        database: str = config.DATABASE,
    ) -> None:
        self._uri = uri
        self._token = token
        self._database = database
        self._driver = GraphDatabase.driver(uri, auth=("neo4j", token))

    def verify(self) -> None:
        """Verify connectivity to HydraDB; raises loudly if unreachable."""
        self._driver.verify_connectivity()

    def close(self) -> None:
        self._driver.close()

    def run(self, query: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        """Execute a Cypher query on HydraDB via Bolt session."""
        with self._driver.session(database=self._database) as session:
            records = list(session.run(query, params or {}))
            return [_record_to_plain(r) for r in records]

    def run_single(self, query: str, params: Mapping[str, Any] | None = None) -> dict | None:
        rows = self.run(query, params)
        return rows[0] if rows else None

    # -- writes -----------------------------------------------------------

    def reset(self) -> None:
        """Delete all graph nodes and relationships in HydraDB."""
        for label in schema.ALL_NODE_LABELS:
            self.run(f"MATCH (n:{label}) DETACH DELETE n")

    def upsert_nodes(self, label: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """UNWIND upsert: MERGE by id, SET the label and props from the row map."""
        if not rows:
            return
        fields = {k for row in rows for k in row.keys()}
        fields.discard("id")
        for chunk in _chunks(rows, BATCH_SIZE):
            set_clause = ""
            if fields:
                set_clause = (
                    f" SET n:{label}, "
                    + ", ".join(f"n.{f} = row.{f}" for f in sorted(fields))
                )
            self.run(
                f"UNWIND $rows AS row MERGE (n {{id: row.id}}){set_clause}",
                {"rows": [dict(row) for row in chunk]},
            )

    def create_edges(
        self,
        rel_type: str,
        src_label: str,
        dst_label: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """UNWIND batch: MATCH labeled endpoints by id, MERGE the edge by its id."""
        if not rows:
            return
        for chunk in _chunks(rows, BATCH_SIZE):
            chunk_rows: list[dict[str, Any]] = []
            for row in chunk:
                r = dict(row)
                if not r.get("eid"):
                    r["eid"] = ids.rel_id(rel_type, r["src"], r["dst"])
                chunk_rows.append(r)
            fields = {k for r in chunk_rows for k in r.keys()}
            fields.discard("id")
            fields.discard("src")
            fields.discard("dst")
            set_clause = ""
            if fields:
                set_clause = " SET " + ", ".join(f"r.{f} = row.{f}" for f in sorted(fields))
            self.run(
                f"UNWIND $rows AS row "
                f"MATCH (a:{src_label} {{id: row.src}}), (b:{dst_label} {{id: row.dst}}) "
                f"MERGE (a)-[r:{rel_type} {{id: row.eid}}]->(b){set_clause}",
                {"rows": chunk_rows},
            )

    def mark_superseded(self, node_label: str, nid: int) -> None:
        self.upsert_nodes(node_label, [{"id": nid, "superseded": True}])

    # -- row builders (batch ingest support) -----------------------------

    def upsert_person_row(
        self, rows: list[dict[str, Any]], name: str, aliases: str
    ) -> int:
        nid = ids.person_id(normalize_name(name))
        rows.append({"id": nid, "name": name, "aliases": aliases})
        return nid

    def upsert_fact_row(
        self,
        rows: list[dict[str, Any]],
        fact: Mapping[str, Any],
        session_index: int,
        turns: Sequence[Mapping[str, Any]],
    ) -> int:
        subject = str(fact["subject"])
        predicate = str(fact["predicate"])
        value = str(fact["value"])
        stated_at = int(fact.get("source_turn", 0))
        nid = ids.fact_id(subject, predicate, value, session_index, stated_at)
        text = ""
        if 0 <= stated_at < len(turns):
            text = str(turns[stated_at].get("content", ""))
        rows.append(
            {
                "id": nid,
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "text": text,
                "confidence": float(fact.get("confidence", 0.9)),
                "session_index": session_index,
                "stated_at": stated_at,
                "superseded": False,
            }
        )
        return nid

    def upsert_event_row(
        self,
        rows: list[dict[str, Any]],
        event: Mapping[str, Any],
        session_index: int,
        session: Mapping[str, Any],
    ) -> int:
        summary = str(event["summary"])
        stated_at = int(event.get("source_turn", 0))
        nid = ids.event_id(session_index, stated_at, summary)
        rows.append(
            {
                "id": nid,
                "summary": summary,
                "date": str(event.get("date", session.get("date", ""))),
                "session_index": session_index,
                "stated_at": stated_at,
            }
        )
        return nid

    def upsert_pref_row(
        self, rows: list[dict[str, Any]], pref: Mapping[str, Any], session_index: int
    ) -> int:
        subject = str(pref["subject"])
        predicate = str(pref["predicate"])
        value = str(pref["value"])
        stated_at = int(pref.get("source_turn", 0))
        nid = ids.preference_id(subject, predicate, value, session_index, stated_at)
        rows.append(
            {
                "id": nid,
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "session_index": session_index,
                "stated_at": stated_at,
                "superseded": False,
            }
        )
        return nid

    # -- reads ------------------------------------------------------------

    def all_person_names(self) -> list[str]:
        return [r["name"] for r in self.run("MATCH (p:Person) RETURN p.name AS name")]

    def all_fact_ids(self, label: str = schema.NODE_FACT) -> list[int]:
        return [r["id"] for r in self.run(f"MATCH (n:{label}) RETURN n.id AS id")]

    def find_facts(
        self,
        subject: str,
        predicate: str,
        value: str | None = None,
        node_label: str = schema.NODE_FACT,
    ) -> list[dict]:
        where = "n.subject = $subject AND n.predicate = $predicate"
        params: dict[str, Any] = {"subject": subject, "predicate": predicate}
        if value is not None:
            where += " AND n.value <> $value"
            params["value"] = value
        return self.run(
            f"MATCH (n:{node_label}) WHERE {where} "
            f"RETURN n.id AS id, n.value AS value, n.session_index AS session_index, "
            f"n.stated_at AS stated_at, n.superseded AS superseded",
            params,
        )

    def paths_from_sources(
        self,
        source_values: list[int],
        rel_types: list[str],
        max_len: int,
        path_count: int = 10,
        result_limit: int = 200,
    ) -> list[dict]:
        """SSpaths per source id (verified on a bare graph-node; see module doc).

        relTypes must be a literal list: HydraDB rejects composite parameters
        outside UNWIND ("only scalar parameters are supported").
        """
        rel_literal = ", ".join(f"'{t}'" for t in rel_types)
        paths: list[dict] = []
        for src in source_values:
            rows = self.run(
                "CALL algo.SSpaths({sourceNode: $src, relTypes: [" + rel_literal + "], "
                "relDirection: 'both', maxLen: $maxLen, pathCount: $pathCount}) "
                "YIELD path RETURN path",
                {
                    "src": src,
                    "maxLen": max_len,
                    "pathCount": path_count,
                },
            )
            paths.extend(rows[:result_limit])
        return paths


# -- plain-value conversion -------------------------------------------------


def _record_to_plain(record: Any) -> dict:
    if hasattr(record, "data"):
        return {k: _plain(v) for k, v in record.data().items()}
    return _plain(record)


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {
            "nodes": [_plain(n) for n in value.nodes],
            "relationships": [
                {
                    "type": rel.type,
                    "start_node": _plain(rel.start_node),
                    "end_node": _plain(rel.end_node),
                    "properties": _plain(getattr(rel, "_properties", {})),
                }
                for rel in value.relationships
            ],
        }
    props = getattr(value, "_properties", None)
    if props:
        return {**{str(k): _plain(v) for k, v in props.items()}, "id": _id_of(value)}
    element_id = getattr(value, "element_id", None)
    if element_id is not None:
        return {"id": _id_from_element_id(element_id)}
    return str(value)


def _id_of(node: Any) -> int | None:
    return _id_from_element_id(getattr(node, "element_id", None))


def _id_from_element_id(element_id: Any) -> int | None:
    text = str(element_id)
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _chunks(seq: Sequence[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def normalize_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())
