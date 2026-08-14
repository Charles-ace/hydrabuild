"""FastAPI server: ingestion + query endpoints for the demo web app.

Endpoints:
  GET  /api/health            node connectivity
  GET  /api/sample            bundled sample transcript + questions
  POST /api/ingest            ingest a transcript (list of sessions)
  POST /api/query             ask a question; returns answer or abstention
  POST /api/reset             empty the graph
  GET  /                      single-page demo app
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .bolt import HydraClient
from .extraction import build_extractor
from .ingest import Ingestor
from .query import QueryService

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"

app = FastAPI(title="hydramem", version="0.1.0")

client = HydraClient()
ingestor = Ingestor(client)
query_service = QueryService(client)
extractor = build_extractor()


class IngestRequest(BaseModel):
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class QueryRequest(BaseModel):
    question: str


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        client.verify()
        return {"ok": True, "mode": config.LLM_MODE}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)}


@app.get("/api/sample")
def sample() -> dict[str, Any]:
    sessions = json.loads((DATA_DIR / "sample_sessions.json").read_text(encoding="utf-8"))
    questions = json.loads((DATA_DIR / "sample_questions.json").read_text(encoding="utf-8"))
    return {"persona": sessions.get("persona", ""), "sessions": sessions["sessions"], "questions": questions["questions"]}


@app.post("/api/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    report = ingestor.ingest_sessions(req.sessions, extractor)
    return {"ok": True, "report": report.to_dict(), "details": report.details}


@app.post("/api/query")
def query(req: QueryRequest) -> dict[str, Any]:
    if not req.question.strip():
        raise ValueError("empty question")
    result = query_service.answer(req.question)
    return result.to_dict()


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    client.reset()
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.on_event("shutdown")
def shutdown() -> None:
    client.close()


if os.environ.get("HYDRA_MEM_SERVE_STATIC") != "0":
    if WEB_DIR.exists():
        app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
