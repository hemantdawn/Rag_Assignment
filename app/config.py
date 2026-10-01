from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_keys: dict[str, dict]
    database_url: str | None = None
    max_upload_bytes: int = 20 * 1024 * 1024
    embedder: str = "hash"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimensions: int = 256
    llm_model: str = "gpt-4o-mini"
    max_attempts: int = 3

    @property
    def db_path(self) -> Path:
        return self.data_dir / "metadata.sqlite3"

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"


def load_settings() -> Settings:
    raw_keys = os.getenv("RAG_API_KEYS", "{}")
    api_keys = json.loads(raw_keys)
    if not isinstance(api_keys, dict):
        raise ValueError("RAG_API_KEYS must be a JSON object")
    for value in api_keys.values():
        if not isinstance(value, dict) or not isinstance(value.get("tenant"), str):
            raise ValueError("Each API key needs a tenant and scopes")
        if not isinstance(value.get("scopes"), list) or not all(
            isinstance(scope, str) for scope in value["scopes"]
        ):
            raise ValueError("Each API key needs a list of scopes")
    return Settings(
        data_dir=Path(os.getenv("RAG_DATA_DIR", "data")).resolve(),
        api_keys=api_keys,
        database_url=os.getenv("RAG_DATABASE_URL") or None,
        max_upload_bytes=int(os.getenv("RAG_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        embedder=os.getenv("RAG_EMBEDDER", "hash"),
        embedding_model=os.getenv("RAG_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        embedding_dimensions=int(os.getenv("RAG_EMBEDDING_DIMENSIONS", "256" if os.getenv("RAG_EMBEDDER", "hash") == "hash" else "384")),
        llm_model=os.getenv("RAG_LLM_MODEL", "gpt-4o-mini"),
        max_attempts=int(os.getenv("RAG_MAX_ATTEMPTS", "3")),
    )
