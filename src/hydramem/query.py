"""Query pipeline: question -> bounded multi-hop traversal -> answer or abstention.

Flow:
1. Parse the question into person mentions, predicate hints and a history flag
   (LLM-assisted; deterministic fallback in mock mode).
2. Resolve mentions to Person node ids (name/alias match).
3. Traverse with algo.SSpaths per source id (verified on a live graph-node;
   MSpaths needs property indexes built by the graph-indexer role), relDirection
   both, bounded maxLen, over every relationship type in the schema.
4. Score terminal Fact/Preference/Event nodes: prefer non-superseded facts
   unless the question is explicitly about history; prefer later statements.
5. Answer with the evidence path, or return a STRUCTURAL abstention
   ("not found in memory") — never an LLM guess when the traversal is empty.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import config, schema
from .bolt import HydraClient, normalize_name

log = logging.getLogger(__name__)

HISTORY_WORDS = re.compile(
    r"\b(used to|before|previously|originally|at first|earlier|in the past|"
    r"old|first|past|then|before the switch|before the change)\b|"
    r"^(what was|what did|what were|what had|what used)",
    re.I,
)

YES_NO_LEAD = re.compile(
    r"^(does|did|is|are|was|were|has|have|had|can|could|would|will|should)\b",
    re.I,
)

VALUE_STOPWORDS = {
    "the", "and", "for", "with", "his", "her", "their", "your", "about",
    "have", "has", "had", "does", "did", "use", "uses", "used", "own",
    "owns", "live", "lives", "like", "likes", "prefer", "prefers", "a",
    "an", "in", "on", "at", "to", "of", "any", "you", "he", "she", "they",
    "it", "we", "i", "me", "him", "them", "us", "from", "when", "where",
    "what", "how", "why", "who", "which", "not", "no", "now", "then",
    "still", "there", "this", "that", "these", "those", "team", "work",
    "want", "wants", "need", "needs", "organise", "organize", "get", "got",
    "say", "said", "says", "told", "tell", "going", "go", "went", "been",
    "being", "was", "were", "will", "would", "could", "should", "can",
}

PREDICATE_KEYWORDS: list[tuple[str, str]] = [
    ("standup", "standup"),
    ("sync", "standup"),
    ("async", "standup"),
    ("contact", "contact"),
    ("reach", "contact"),
    ("slack", "contact"),
    ("email", "contact"),
    ("live", "location"),
    ("living", "location"),
    ("location", "location"),
    ("city", "location"),
    ("moved", "location"),
    ("move", "location"),
    ("pet", "pet"),
    ("cat", "pet"),
    ("dog", "pet"),
    ("work", "works_at"),
    ("job", "works_at"),
    ("company", "works_at"),
    ("notion", "tool"),
    ("birthday", "birthday"),
]

EVENT_WORDS = re.compile(
    r"\b(happen|happened|weekend|holiday|trip|travel|vacation|"
    r"wedding|married|marriage|birthday|last weekend|party|concert)\b",
    re.I,
)

QUESTION_PARSE_SYSTEM = (
    "You translate a user question about a long-term memory graph into a "
    "structured query plan. The graph knows Person, Fact, Event, Preference "
    "nodes. Return JSON with:\n"
    "- people: list of person names mentioned in the question (as written)\n"
    "- predicate_hints: list of snake_case predicate fragments that the "
    "question is asking about (e.g. 'standup_style', 'contact_channel', "
    "'location', 'pet', 'works_at'); empty if the question targets events "
    "rather than attributes\n"
    "- history_mode: true only if the question explicitly asks about a past "
    "state ('used to', 'before', 'previously', 'earlier')\n"
    "- event_question: true if the question asks what happened / an "
    "occurrence rather than a stable attribute\n"
    "Known people in memory: {people}\n"
    "Existing predicate slugs you may reuse: {predicates}\n"
    "If a question asks about something that cannot be an attribute (e.g. "
    "'does X use Y'), still return predicate_hints matching the tool name."
)


@dataclass
class QueryPlan:
    people: list[str] = field(default_factory=list)
    predicate_hints: list[str] = field(default_factory=list)
    history_mode: bool = False
    event_question: bool = False
    value_hints: list[str] = field(default_factory=list)
    event_keywords: list[str] = field(default_factory=list)


@dataclass
class EvidenceNode:
    id: int
    label: str
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "props": self.props}


@dataclass
class EvidenceEdge:
    type: str
    src: int
    dst: int
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "src": self.src, "dst": self.dst, "props": self.props}


@dataclass
class QueryResult:
    status: str  # "answer" | "not_found"
    answer: str = ""
    reason: str = ""  # for not_found: no_path | no_entity | no_matching_fact
    terminal: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    plan: QueryPlan | None = None
    procedure: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "reason": self.reason,
            "terminal": self.terminal,
            "evidence": self.evidence,
            "plan": {
                "people": self.plan.people if self.plan else [],
                "predicate_hints": self.plan.predicate_hints if self.plan else [],
                "history_mode": self.plan.history_mode if self.plan else False,
                "event_question": self.plan.event_question if self.plan else False,
                "value_hints": self.plan.value_hints if self.plan else [],
            },
            "procedure": self.procedure,
        }


class QueryService:
    def __init__(self, client: HydraClient) -> None:
        self.client = client

    def answer(self, question: str) -> QueryResult:
        people_names = self.client.all_person_names()
        plan = self._parse_question(question, people_names)
        if not plan.people:
            result = QueryResult(status="not_found", reason="no_entity", plan=plan)
            result.answer = "not found in memory: the question does not reference anyone in memory"
            return result

        sources = self._resolve_people(plan.people)
        if not sources:
            result = QueryResult(status="not_found", reason="no_entity", plan=plan)
            result.answer = "not found in memory: no person in memory matches the mention"
            return result

        paths, procedure = self._traverse(sources)
        if not paths:
            result = QueryResult(status="not_found", reason="no_path", plan=plan)
            result.answer = "not found in memory: no graph path within the traversal bound"
            return result

        candidates = self._collect_candidates(paths, plan)
        if not candidates:
            reason = "no_matching_fact"
            if plan.value_hints and self._collect_candidates(paths, plan, skip_value=True):
                reason = "no_matching_value"
            result = QueryResult(status="not_found", reason=reason, plan=plan)
            result.answer = "not found in memory: no stored fact matches this question"
            return result

        best, best_path = self._rank(candidates, plan)
        terminal = best["node"]
        evidence = self._build_evidence(best_path, terminal)
        if terminal["label"] == schema.NODE_EVENT:
            answer = f"{terminal['summary']} ({terminal.get('date', '')})"
        elif plan.history_mode:
            answer = (
                f"{terminal['subject']}'s {terminal['predicate']} was "
                f"'{terminal['value']}' (stated in session {terminal['session_index']}"
                + (", later superseded)" if terminal.get("superseded") else ")")
            )
        else:
            answer = (
                f"{terminal['subject']}'s {terminal['predicate']} is "
                f"'{terminal['value']}' (stated in session {terminal['session_index']}"
                + (", superseded later)" if terminal.get("superseded") else ")")
            )
        result = QueryResult(
            status="answer",
            answer=answer,
            terminal=self._terminal_to_dict(terminal),
            evidence=evidence,
            plan=plan,
            procedure=procedure,
        )
        return result

    # -- parsing ----------------------------------------------------------

    def _parse_question(self, question: str, known_people: list[str]) -> QueryPlan:
        history_mode = bool(HISTORY_WORDS.search(question))
        plan = QueryPlan(history_mode=history_mode)
        qnorm = normalize_name(question)
        matched = []
        for name in known_people:
            if normalize_name(name) and normalize_name(name) in qnorm:
                matched.append(name)
        for alias in self._all_aliases(known_people):
            if alias and alias in qnorm:
                matched.append(alias)
        plan.people = sorted(set(matched))
        hints: set[str] = set()
        keywords: set[str] = set()
        for word, hint in PREDICATE_KEYWORDS:
            if re.search(rf"\b{word}s?\b", question.lower()):
                hints.add(hint)
                keywords.add(word)
        plan.predicate_hints = sorted(hints)
        plan.event_keywords = sorted(keywords)
        plan.event_question = bool(EVENT_WORDS.search(question))
        if YES_NO_LEAD.search(question):
            words = re.findall(r"[a-z@]{3,}", question.lower())
            skip = VALUE_STOPWORDS | {normalize_name(p) for p in known_people}
            plan.value_hints = [w for w in words if w not in skip][:6]
        return plan

    def _all_aliases(self, people: list[str]) -> list[str]:
        out: list[str] = []
        for row in self.client.run(
            "MATCH (p:Person) RETURN p.aliases AS aliases"
        ):
            for a in (row.get("aliases") or "").split(","):
                a = a.strip()
                if a:
                    out.append(normalize_name(a))
        return out

    # -- resolution & traversal ------------------------------------------

    def _resolve_people(self, mentions: list[str]) -> list[int]:
        ids: list[int] = []
        rows = self.client.run(
            "MATCH (p:Person) RETURN p.id AS id, p.name AS name, p.aliases AS aliases"
        )
        for mention in mentions:
            target_norm = normalize_name(mention)
            best_id, best_score = None, 0.0
            for row in rows:
                score = 0.0
                if normalize_name(row["name"]) == target_norm:
                    score = 1.0
                else:
                    aliases = (row.get("aliases") or "").split(",")
                    if any(normalize_name(a) == target_norm for a in aliases if a):
                        score = 0.99
                if score > best_score:
                    best_score, best_id = score, row["id"]
            if best_id is not None:
                ids.append(best_id)
        return ids

    def _traverse(self, sources: list[int]) -> tuple[list[dict[str, Any]], str]:
        rows = self.client.paths_from_sources(
            sources,
            schema.ALL_REL_TYPES,
            config.MAX_PATH_LEN,
            config.PATH_COUNT,
            config.RESULT_LIMIT,
        )
        return rows, "algo.SSpaths"

    # -- candidate assembly ----------------------------------------------

    def _collect_candidates(
        self, paths: list[dict[str, Any]], plan: QueryPlan, skip_value: bool = False
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[int] = set()
        for path_row in paths:
            path = _extract_path(path_row)
            for entry in path.get("nodes", []):
                label = entry["label"]
                props = entry["props"]
                if label not in (schema.NODE_FACT, schema.NODE_PREFERENCE, schema.NODE_EVENT):
                    continue
                if label == schema.NODE_EVENT and not plan.event_question:
                    continue
                if label != schema.NODE_EVENT and plan.event_question:
                    continue
                nid = props.get("id")
                if nid in seen:
                    continue
                if plan.predicate_hints and label in (schema.NODE_FACT, schema.NODE_PREFERENCE):
                    predicate = str(props.get("predicate", ""))
                    if not any(h in predicate for h in plan.predicate_hints):
                        continue
                if plan.value_hints and not skip_value:
                    value = str(props.get("value", "")).lower()
                    if not any(h in value for h in plan.value_hints):
                        continue
                seen.add(nid)
                candidates.append(
                    {
                        "node": {**props, "label": label},
                        "path": path,
                    }
                )
        return candidates

    def _rank(self, candidates: list[dict[str, Any]], plan: QueryPlan) -> tuple[dict[str, Any], dict[str, Any]]:
        def score(item: dict[str, Any]) -> float:
            node = item["node"]
            if plan.event_question and node["label"] == schema.NODE_EVENT:
                hint_bonus = 0.0
                if plan.predicate_hints or plan.event_keywords:
                    summary = str(node.get("summary", "")).lower()
                    if any(h in summary for h in plan.predicate_hints) or any(
                        kw in summary for kw in plan.event_keywords
                    ):
                        hint_bonus = 1e8
                return 1e9 + hint_bonus + node["session_index"]
            freshness = 2.0 if not node.get("superseded") else 1.0
            if plan.history_mode:
                freshness = 2.0 if node.get("superseded") else 1.0
            return (
                freshness * 1e6
                + node["session_index"] * 1e3
                + node.get("stated_at", 0)
            )

        return max(candidates, key=score), max(candidates, key=score)["path"]

    # -- evidence ---------------------------------------------------------

    def _build_evidence(self, path: dict[str, Any], terminal: dict[str, Any]) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for entry in path.get("nodes", []):
            nodes.append({"id": entry["props"].get("id"), "label": entry["label"], "props": entry["props"]})
        for rel in path.get("relationships", []):
            src = rel.get("start_node", {}).get("props", {}).get("id")
            dst = rel.get("end_node", {}).get("props", {}).get("id")
            if src is None or dst is None:
                continue
            edges.append({"type": rel.get("type", ""), "src": src, "dst": dst})
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _terminal_to_dict(terminal: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "id",
            "label",
            "subject",
            "predicate",
            "value",
            "text",
            "superseded",
            "session_index",
            "stated_at",
            "summary",
            "date",
        ]
        return {k: terminal.get(k) for k in keys if k in terminal}


def _extract_path(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a path value into {nodes, relationships}.

    This neo4j driver version flattens HydraDB paths in record.data() to an
    alternating list [node_props, rel_type, node_props, ...]. Older driver
    versions may hand back Path objects or {nodes, relationships} dicts; all
    three shapes are handled. Node ids are reconstructed deterministically
    from props because the flat form drops element ids.
    """
    value = row.get("path", row)
    if isinstance(value, dict):
        if "nodes" in value:
            return {
                "nodes": [_node_entry(n) for n in value["nodes"]],
                "relationships": [
                    {
                        "type": r.get("type", ""),
                        "start_node": _node_entry(r.get("start_node")),
                        "end_node": _node_entry(r.get("end_node")),
                    }
                    for r in value.get("relationships", [])
                ],
            }
        return {"nodes": [], "relationships": []}
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
        items = list(value)
        nodes: list[dict[str, Any]] = []
        rels: list[dict[str, Any]] = []
        for i in range(0, len(items)):
            if i % 2 == 0:
                nodes.append(_node_entry(items[i]))
            else:
                rels.append({"type": items[i]})
        for i, rel in enumerate(rels):
            rel["start_node"] = nodes[i]
            rel["end_node"] = nodes[i + 1]
        return {"nodes": nodes, "relationships": rels}
    return {"nodes": [], "relationships": []}


def _node_entry(node: Any) -> dict[str, Any]:
    if isinstance(node, dict):
        labels = node.get("labels") or []
        props = dict(node.get("properties") or node)
        props.pop("labels", None)
        label = labels[0] if labels else _label_from_props(props)
        props.setdefault("id", _reconstruct_id(label, props))
        return {"label": label, "props": props}
    return {"label": "", "props": {}}


def _reconstruct_id(label: str, props: dict[str, Any]) -> int | None:
    from .ids import event_id, fact_id, person_id, preference_id, session_id

    try:
        if label == schema.NODE_PERSON:
            return person_id(normalize_name(str(props["name"])))
        if label == schema.NODE_SESSION:
            return session_id(int(props["session_index"]))
        if label == schema.NODE_FACT:
            return fact_id(
                str(props["subject"]),
                str(props["predicate"]),
                str(props["value"]),
                int(props["session_index"]),
                int(props["stated_at"]),
            )
        if label == schema.NODE_PREFERENCE:
            return preference_id(
                str(props["subject"]),
                str(props["predicate"]),
                str(props["value"]),
                int(props["session_index"]),
                int(props["stated_at"]),
            )
        if label == schema.NODE_EVENT:
            return event_id(
                int(props["session_index"]), int(props["stated_at"]), str(props["summary"])
            )
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _label_from_props(props: dict[str, Any]) -> str:
    if "subject" in props:
        return schema.NODE_FACT if "confidence" in props else schema.NODE_PREFERENCE
    if "summary" in props:
        return schema.NODE_EVENT
    if "name" in props:
        return schema.NODE_PERSON
    if "session_index" in props:
        return schema.NODE_SESSION
    return ""
