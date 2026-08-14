"""Turn raw chat sessions into typed extraction records.

Two extractors:
- LLMExtractor: structured-JSON extraction via the configured chat model.
- MockExtractor: deterministic keyword rules, used when no API key is set
  (HYDRA_MEM_LLM_MODE=mock). It is tuned for the bundled sample transcript
  and exists so the demo and tests run offline; the benchmark uses the LLM.

Both produce the same dataclass, so the ingestion pipeline is identical.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import llm

SYSTEM_PROMPT = (
    "You are a fact-extraction engine for a chat memory graph. Given one chat "
    "session between a user and an assistant, extract ONLY what is explicitly "
    "stated. Never infer, guess, or complete from general knowledge.\n"
    "Return a JSON object with these keys:\n"
    "- people: array of {name, aliases} for every distinct person mentioned, "
    "including the user. Aliases are other names the same person uses.\n"
    "- facts: array of {subject, predicate, value, source_turn} for stable "
    "attributes (role, pets, location, works_at, etc.).\n"
    "- events: array of {summary, date, source_turn} for time-stamped "
    "occurrences. date is the date string the session/utterance gives, or the "
    "session date if no date is stated.\n"
    "- preferences: array of {subject, predicate, value, source_turn} for "
    "explicit stated preferences and dislikes (meeting style, tools, "
    "communication channel, schedule, ...).\n"
    "Conventions:\n"
    "- predicate is a short snake_case canonical slug, e.g. standup_style, "
    "contact_channel, works_at, pet, location.\n"
    "- value is the concise stated value, e.g. 'async', 'email', 'Bangalore'.\n"
    "- subject is the person name as stated.\n"
    "- source_turn is the 0-based index of the turn inside the session that "
    "contains the statement.\n"
    "- A later statement that reverses or replaces an earlier one is STILL "
    "extracted normally; contradiction handling is a separate step.\n"
    "If nothing qualifies for a key, use an empty array."
)

USER_TEMPLATE = (
    "Session index: {session_index}\nSession date: {session_date}\n\n"
    "Transcript (each turn is \"<index> <role>: <content>\"):\n\n{turns}"
)


@dataclass
class Extraction:
    people: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    preferences: list[dict[str, Any]] = field(default_factory=list)


class LLMExtractor:
    def extract_session(self, session_index: int, session: dict[str, Any]) -> Extraction:
        turns = []
        for i, turn in enumerate(session["turns"]):
            turns.append(f"{i} {turn['role']}: {turn['content']}")
        data = llm.chat_json(
            SYSTEM_PROMPT,
            USER_TEMPLATE.format(
                session_index=session_index,
                session_date=session.get("date", ""),
                turns="\n".join(turns),
            ),
        )
        return Extraction(
            people=data.get("people", []),
            facts=data.get("facts", []),
            events=data.get("events", []),
            preferences=data.get("preferences", []),
        )


class MockExtractor:
    """Deterministic keyword extractor for the bundled sample transcript.

    The persona identity (name + aliases) is learned from the first session
    and carried across sessions, mirroring how a real memory system tracks a
    returning user across conversations.
    """

    USER_NAME_PATTERNS = [
        re.compile(r"call me (\w[\w.\-@ ]*?)(?:,|\.|!|$)", re.I),
        re.compile(r"i'm ([a-zA-Z][a-zA-Z\-]*)", re.I),
        re.compile(r"my name is ([a-zA-Z][a-zA-Z\-]*)", re.I),
    ]
    ALIAS_PATTERNS = [
        re.compile(
            r"(?:call me|know me as|also known as|goes by)\s+(@?[\w][\w@.\-]*)",
            re.I,
        ),
    ]

    PREF_RULES: list[tuple[str, re.Pattern, str, str]] = [
        ("standup_style", re.compile(r"prefer\w* (async|sync) standups?", re.I), "{v} standups"),
        ("standup_style", re.compile(r"standups?.*(async|sync)\.? (?:didn't|working)", re.I), "{v} standups"),
        ("contact_channel", re.compile(r"reach me on (email|slack)", re.I), "{v}"),
        ("contact_channel", re.compile(r"slack me", re.I), "slack"),
        ("contact_channel", re.compile(r"(email|slack).*i (?:live|gone off|prefer)", re.I), "{v}"),
    ]

    FACT_RULES: list[tuple[str, re.Pattern, str]] = [
        ("works_at", re.compile(r"(?:work|engineering manager) at ([A-Za-z0-9]+)", re.I), "{v}"),
        ("pet", re.compile(r"(?:(?:a |the )?(cat|dog))\b(?: called ([A-Z]\w*))?", re.I), "{v}"),
        ("location", re.compile(r"moved to ([A-Z][A-Za-z ]*?)(?=\s+last\b|,|\.|!|$)", re.I), "{v}"),
        ("language", re.compile(r"(python|rust|javascript|golang)\b", re.I), "{v}"),
    ]

    EVENT_RULES: list[tuple[re.Pattern, str]] = [
        (re.compile(r"(moved to [^.!]*)", re.I), "relocation"),
        (re.compile(r"(getting married [^.!]*)", re.I), "wedding"),
        (re.compile(r"(started running [^.!]*)", re.I), "hobby"),
    ]

    def __init__(self) -> None:
        self.user_name = ""
        self.aliases: set[str] = set()

    def extract_session(self, session_index: int, session: dict[str, Any]) -> Extraction:
        turns = session["turns"]
        session_date = session.get("date", "")
        user_name = self.user_name
        extraction = Extraction()

        for i, turn in enumerate(turns):
            content = turn["content"]
            if turn["role"] == "user" and not user_name:
                for pat in self.USER_NAME_PATTERNS:
                    m = pat.search(content)
                    if m:
                        user_name = m.group(1).strip().lstrip("@")
                        self.user_name = user_name
                        break
            if turn["role"] == "user":
                for pat in self.ALIAS_PATTERNS:
                    for m in pat.finditer(content):
                        for g in m.groups():
                            if g:
                                self.aliases.add(g.strip())

            if not user_name:
                continue
            subject = user_name

            for pred, pat, fmt in self.PREF_RULES:
                m = pat.search(content)
                if m:
                    v = m.group(1) if m.lastindex else m.group(0)
                    extraction.preferences.append(
                        {
                            "subject": subject,
                            "predicate": pred,
                            "value": fmt.format(v=v.lower()),
                            "source_turn": i,
                        }
                    )
            for pred, pat, fmt in self.FACT_RULES:
                m = pat.search(content)
                if m:
                    v = m.group(1) if m.lastindex else m.group(0)
                    extraction.facts.append(
                        {
                            "subject": subject,
                            "predicate": pred,
                            "value": fmt.format(v=v.strip()),
                            "source_turn": i,
                        }
                    )
            for pat, kind in self.EVENT_RULES:
                m = pat.search(content)
                if m:
                    extraction.events.append(
                        {
                            "summary": m.group(1),
                            "date": session_date,
                            "source_turn": i,
                        }
                    )

        if user_name:
            extraction.people.append({"name": user_name, "aliases": sorted(self.aliases)})
        return extraction


def build_extractor() -> LLMExtractor | MockExtractor:
    if llm_is_configured():
        return LLMExtractor()
    return MockExtractor()


def llm_is_configured() -> bool:
    from . import config

    return config.LLM_MODE == "llm" and bool(config.LLM_API_KEY)


def extraction_to_json(extraction: Extraction) -> dict[str, Any]:
    return {
        "people": extraction.people,
        "facts": extraction.facts,
        "events": extraction.events,
        "preferences": extraction.preferences,
    }