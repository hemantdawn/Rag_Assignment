from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import Principal, authenticate
from app.config import Settings, load_settings
from app.embedding import make_embedder
from app.observability import Trace
from app.retrieval import QueryEngine
from app.storage import create_database


SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=1, max_length=100)


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = create_database(settings)
    embedder = make_embedder(settings)
    db.initialize()
    app = FastAPI(title="Citation-aware RAG", version="0.1.0")
    app.state.settings = settings
    app.state.db = db
    app.state.engine = QueryEngine(db, embedder, settings)

    @app.middleware("http")
    async def attach_trace_id(request: Request, call_next):
        request.state.trace_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Trace-ID"] = request.state.trace_id
        return response

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/documents", status_code=202)
    async def upload_document(
        request: Request, file: UploadFile = File(...), scope: str = Form(...),
        principal: Principal = Depends(authenticate),
    ) -> dict:
        principal.require_scope(scope)
        trace = Trace(db, request.state.trace_id, principal.tenant, scope, "ingestion")
        with trace.stage("upload_total") as total:
            with trace.stage("validate_upload"):
                filename = Path(file.filename or "").name
                if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    raise HTTPException(status_code=415, detail="Supported files: .txt, .md, .pdf, .docx")
            fd, temp_name = tempfile.mkstemp(prefix="upload-", dir=settings.objects_dir)
            temp_path = Path(temp_name)
            digest = hashlib.sha256()
            size = 0
            try:
                with trace.stage("store_upload") as details:
                    with os.fdopen(fd, "wb") as destination:
                        while chunk := await file.read(1024 * 1024):
                            size += len(chunk)
                            if size > settings.max_upload_bytes:
                                raise HTTPException(status_code=413, detail="Document exceeds upload limit")
                            digest.update(chunk)
                            destination.write(chunk)
                    if size == 0:
                        raise HTTPException(status_code=400, detail="Document is empty")
                    details["bytes"] = size
                sha256 = digest.hexdigest()
                with trace.stage("deduplicate_upload") as details:
                    existing = db.existing_document(principal.tenant, scope, sha256)
                    details["duplicate"] = bool(existing)
                if existing:
                    trace.document_id = existing["id"]
                    total["deduplicated"] = True
                    return {"id": existing["id"], "version_id": existing.get("version_id"),
                            "status": existing["status"], "deduplicated": True,
                            "trace_id": trace.trace_id}
                document_id = str(uuid4())
                trace.document_id = document_id
                # Docling uses the extension to select the PDF/DOCX converter.
                object_path = settings.objects_dir / f"{document_id}{Path(filename).suffix.lower()}"
                with trace.stage("enqueue_document"):
                    os.replace(temp_path, object_path)
                    try:
                        created = db.create_document(
                            document_id, principal.tenant, scope, filename, sha256,
                            object_path, trace.trace_id,
                        )
                        if created and created["deduplicated"]:
                            object_path.unlink(missing_ok=True)
                            trace.document_id = created["id"]
                            total["deduplicated"] = True
                            return {**created, "trace_id": trace.trace_id}
                    except sqlite3.IntegrityError:
                        object_path.unlink(missing_ok=True)
                        existing = db.existing_document(principal.tenant, scope, sha256)
                        if existing:
                            trace.document_id = existing["id"]
                            total["deduplicated"] = True
                            return {"id": existing["id"], "status": existing["status"],
                                    "deduplicated": True, "trace_id": trace.trace_id}
                        raise
                    except Exception:
                        object_path.unlink(missing_ok=True)
                        raise
                total.update(bytes=size, deduplicated=False)
                return {**(created or {"id": document_id, "status": "queued",
                                       "deduplicated": False}), "trace_id": trace.trace_id}
            finally:
                temp_path.unlink(missing_ok=True)
                await file.close()

    @app.get("/v1/documents/{document_id}")
    def document_status(document_id: str, principal: Principal = Depends(authenticate)) -> dict:
        document = db.get_document(document_id, principal.tenant, principal.scopes)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        return document

    @app.get("/v1/documents/{document_id}/versions")
    def document_versions(document_id: str, principal: Principal = Depends(authenticate)) -> dict:
        if not hasattr(db, "get_version_history"):
            raise HTTPException(status_code=501, detail="Document versions require PostgreSQL mode")
        versions = db.get_version_history(document_id, principal.tenant, principal.scopes)
        if versions is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"id": document_id, "versions": versions}

    @app.post("/v1/documents/{document_id}/versions", status_code=202)
    async def replace_document(
        document_id: str, request: Request, file: UploadFile = File(...),
        scope: str = Form(...), principal: Principal = Depends(authenticate),
    ) -> dict:
        principal.require_scope(scope)
        if not hasattr(db, "create_version"):
            raise HTTPException(status_code=501, detail="Document versions require PostgreSQL mode")
        document = db.get_document(document_id, principal.tenant, principal.scopes)
        if not document or document["scope"] != scope:
            raise HTTPException(status_code=404, detail="Document not found")
        trace = Trace(db, request.state.trace_id, principal.tenant, scope, "ingestion", document_id)
        with trace.stage("upload_version_total") as total:
            filename = Path(file.filename or "").name
            if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise HTTPException(status_code=415, detail="Supported files: .txt, .md, .pdf, .docx")
            fd, temp_name = tempfile.mkstemp(prefix="upload-", dir=settings.objects_dir)
            temp_path = Path(temp_name)
            digest = hashlib.sha256()
            size = 0
            try:
                with trace.stage("store_upload") as details:
                    with os.fdopen(fd, "wb") as destination:
                        while chunk := await file.read(1024 * 1024):
                            size += len(chunk)
                            if size > settings.max_upload_bytes:
                                raise HTTPException(status_code=413, detail="Document exceeds upload limit")
                            digest.update(chunk)
                            destination.write(chunk)
                    if size == 0:
                        raise HTTPException(status_code=400, detail="Document is empty")
                    details["bytes"] = size
                object_path = settings.objects_dir / f"{uuid4().hex}{Path(filename).suffix.lower()}"
                with trace.stage("enqueue_version"):
                    os.replace(temp_path, object_path)
                    try:
                        created = db.create_version(
                            document_id, principal.tenant, scope, filename,
                            digest.hexdigest(), object_path, trace.trace_id,
                        )
                    except Exception:
                        object_path.unlink(missing_ok=True)
                        raise
                    if created is None:
                        object_path.unlink(missing_ok=True)
                        raise HTTPException(status_code=404, detail="Document not found")
                    if created["deduplicated"]:
                        object_path.unlink(missing_ok=True)
                total.update(bytes=size, deduplicated=created["deduplicated"])
                return {**created, "trace_id": trace.trace_id}
            finally:
                temp_path.unlink(missing_ok=True)
                await file.close()

    @app.post("/v1/documents/{document_id}/retry", status_code=202)
    def retry_document(document_id: str, principal: Principal = Depends(authenticate)) -> dict:
        if not db.retry_document(document_id, principal.tenant, principal.scopes):
            raise HTTPException(status_code=404, detail="Failed document not found")
        return {"id": document_id, "status": "queued"}

    @app.post("/v1/query/sync")
    def query_sync(payload: QueryRequest, request: Request,
                   principal: Principal = Depends(authenticate)) -> dict:
        principal.require_scope(payload.scope)
        trace = Trace(db, request.state.trace_id, principal.tenant, payload.scope, "query")
        return app.state.engine.answer(payload.question, principal.tenant, payload.scope, trace=trace)

    @app.post("/v1/query")
    def query_stream(payload: QueryRequest, request: Request,
                     principal: Principal = Depends(authenticate)) -> StreamingResponse:
        principal.require_scope(payload.scope)
        trace = Trace(db, request.state.trace_id, principal.tenant, payload.scope, "query")

        def events():
            yield sse("started", {"scope": payload.scope, "trace_id": trace.trace_id})
            result = app.state.engine.answer(payload.question, principal.tenant, payload.scope, trace=trace)
            yield sse("result", result)
            yield sse("done", {"status": result["status"]})

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/observability/traces/{trace_id}")
    def trace_details(trace_id: str, principal: Principal = Depends(authenticate)) -> dict:
        spans = db.get_trace(trace_id, principal.tenant, principal.scopes)
        if not spans:
            raise HTTPException(status_code=404, detail="Trace not found")
        return {"trace_id": trace_id, "spans": spans}

    @app.get("/v1/observability/stages")
    def stage_metrics(scope: str, hours: int = Query(default=24, ge=1, le=720),
                      principal: Principal = Depends(authenticate)) -> dict:
        principal.require_scope(scope)
        return {"scope": scope, "window_hours": hours,
                "stages": db.stage_summary(principal.tenant, scope, hours)}

    return app


app = create_app()
