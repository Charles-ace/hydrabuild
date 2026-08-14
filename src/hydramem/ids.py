"""Deterministic integer id allocation for graph nodes and relationships.

HydraDB node ids are non-negative integers and are the identity that MERGE
matches on. Ids here are content hashes of canonical keys, which makes
ingestion idempotent: re-ingesting a session MERGEs instead of duplicating.
"""

import hashlib

_ID_MASK = 0x7FFFFFFFFFFFFFFF


def node_id(*parts: str) -> int:
    key = ":".join(parts)
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & _ID_MASK


def person_id(normalized_name: str) -> int:
    return node_id("Person", normalized_name)


def session_id(session_index: int) -> int:
    return node_id("Session", str(session_index))


def fact_id(subject: str, predicate: str, value: str, session_index: int, stated_at: int) -> int:
    return node_id(
        "Fact", subject, predicate, value, str(session_index), str(stated_at)
    )


def event_id(session_index: int, stated_at: int, summary: str) -> int:
    return node_id("Event", str(session_index), str(stated_at), summary)


def preference_id(
    subject: str, predicate: str, value: str, session_index: int, stated_at: int
) -> int:
    return node_id(
        "Preference", subject, predicate, value, str(session_index), str(stated_at)
    )


def rel_id(kind: str, src: int, dst: int) -> int:
    return node_id("Rel", kind, str(src), str(dst))
