from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from app.config import load_settings
from app.embedding import make_embedder
from app.observability import Trace
from app.processing import make_chunks, parse_document
from app.storage import create_database


logger = logging.getLogger(__name__)


def run_once(db, embedder) -> bool:
    job = db.claim_job()
    if not job:
        return False
    work_id = job.get("version_id", job["document_id"])
    trace = Trace(
        db=db, trace_id=job["trace_id"], tenant=job["tenant"],
        scope=job["scope"], pipeline="ingestion", document_id=job["document_id"],
    )
    try:
        with trace.stage("ingestion_total") as total:
            with trace.stage("parse_document") as details:
                markdown = parse_document(Path(job["object_path"]), job["filename"])
                details["characters"] = len(markdown)
                details["file_type"] = Path(job["filename"]).suffix.lower()
            with trace.stage("chunk_document") as details:
                parents, children = make_chunks(markdown, job["filename"])
                if not children:
                    raise ValueError("Document has no extractable text")
                details.update(parent_count=len(parents), child_count=len(children))
            with trace.stage("embed_children") as details:
                vectors = embedder.embed([child["contextual_text"] for child in children])
                for child, vector in zip(children, vectors, strict=True):
                    child["embedding"] = vector
                details["vector_count"] = len(vectors)
            with trace.stage("index_document"):
                db.complete_job(work_id, parents, children)
            total.update(parent_count=len(parents), child_count=len(children))
        logger.info("Indexed document %s with %s children", job["document_id"], len(children))
    except Exception as exc:
        logger.exception("Ingestion failed for document %s", job["document_id"])
        with trace.stage("mark_job_failed"):
            db.fail_job(work_id, str(exc))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued RAG ingestion jobs")
    parser.add_argument("--once", action="store_true", help="Claim one job and exit")
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    db = create_database(settings)
    embedder = make_embedder(settings)
    db.initialize()
    while True:
        worked = run_once(db, embedder)
        if args.once:
            return
        if not worked:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
