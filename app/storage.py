from __future__ import annotations

from app.config import Settings
from app.db import Database


def create_database(settings: Settings):
    if settings.database_url:
        from app.postgres_db import PostgresDatabase
        return PostgresDatabase(settings)
    return Database(settings)
