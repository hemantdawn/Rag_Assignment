from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.db import Database


logger = logging.getLogger(__name__)


@dataclass
class Trace:
    db: Database
    trace_id: str
    tenant: str
    scope: str
    pipeline: str
    document_id: str | None = None
    _span_stack: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls, db: Database, tenant: str, scope: str, pipeline: str) -> Trace:
        return cls(db, uuid4().hex, tenant, scope, pipeline)

    @contextmanager
    def stage(self, name: str, **details):
        span_id = uuid4().hex
        parent_id = self._span_stack[-1]["id"] if self._span_stack else None
        started_at = datetime.now(timezone.utc).isoformat()
        start = time.perf_counter()
        frame = {"id": span_id, "child_ms": 0.0}
        self._span_stack.append(frame)
        error_type = None
        try:
            yield details
        except BaseException as exc:
            error_type = type(exc).__name__
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            self._span_stack.pop()
            try:
                self.db.record_stage({
                    "span_id": span_id,
                    "trace_id": self.trace_id,
                    "parent_span_id": parent_id,
                    "tenant": self.tenant,
                    "scope": self.scope,
                    "document_id": self.document_id,
                    "pipeline": self.pipeline,
                    "stage": name,
                    "status": "error" if error_type else "ok",
                    "duration_ms": round(duration_ms, 3),
                    "self_ms": round(max(0.0, duration_ms - frame["child_ms"]), 3),
                    "error_type": error_type,
                    "details": details,
                    "started_at": started_at,
                })
            except Exception:
                # Telemetry must not turn a successful request or job into a failure.
                logger.exception("Could not persist trace span %s", span_id)
            finally:
                # Parent time includes the child's telemetry write. Exclude that
                # write from the parent's exclusive application time as well.
                if self._span_stack:
                    self._span_stack[-1]["child_ms"] += (time.perf_counter() - start) * 1000
