"""FastAPI server: ingestion + query endpoints for the demo web app.

Endpoints:
  GET  /api/health            node connectivity and engine status (fails loudly if DB down)
  GET  /api/sample            bundled sample transcript + questions
  POST /api/ingest            ingest a transcript (list of sessions) into HydraDB
  POST /api/query             ask a question; returns answer or abstention from HydraDB
  POST /api/reset             empty the graph in HydraDB
  GET  /                      single-page demo app
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .bolt import HydraClient
from .extraction import build_extractor
from .ingest import Ingestor
from .query import QueryService

log = logging.getLogger(__name__)

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
    history_mode: bool = False


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        client.verify()
        return {
            "ok": True,
            "connected": True,
            "backend": "HydraDB Graph Node (Bolt v0.1.0)",
            "uri": config.BOLT_URI,
            "llm_mode": config.LLM_MODE,
        }
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "ok": False,
                "connected": False,
                "error": f"HydraDB node unreachable at {config.BOLT_URI}: {exc}",
                "backend": "HydraDB Graph Node (Offline)",
            },
        )


@app.get("/api/sample")
def sample() -> dict[str, Any]:
    try:
        sessions = json.loads((DATA_DIR / "sample_sessions.json").read_text(encoding="utf-8"))
        questions = json.loads((DATA_DIR / "sample_questions.json").read_text(encoding="utf-8"))
        return {
            "persona": sessions.get("persona", ""),
            "sessions": sessions.get("sessions", []),
            "questions": questions.get("questions", []),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed loading sample data: {exc}")


@app.post("/api/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    try:
        client.verify()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot ingest: HydraDB is offline at {config.BOLT_URI} ({exc})",
        )

    try:
        if not req.sessions:
            sample_data = sample()
            req.sessions = sample_data["sessions"]
        report = ingestor.ingest_sessions(req.sessions, extractor)
        return {
            "ok": True,
            "report": report.to_dict(),
            "details": report.details,
        }
    except Exception as exc:
        log.exception("Ingest failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/query")
def query(req: QueryRequest) -> dict[str, Any]:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Empty question")
    try:
        client.verify()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot query: HydraDB is offline at {config.BOLT_URI} ({exc})",
        )

    try:
        result = query_service.answer(req.question)
        return result.to_dict()
    except Exception as exc:
        log.exception("Query failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    try:
        client.verify()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot reset: HydraDB is offline at {config.BOLT_URI} ({exc})",
        )

    try:
        client.reset()
        return {"ok": True, "message": "Graph successfully cleared in HydraDB"}
    except Exception as exc:
        log.exception("Reset failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.on_event("shutdown")
def shutdown() -> None:
    client.close()


if os.environ.get("HYDRA_MEM_SERVE_STATIC") != "0":
    if WEB_DIR.exists():
        app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
