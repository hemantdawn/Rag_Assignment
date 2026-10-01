from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader


@dataclass(frozen=True)
class Principal:
    tenant: str
    scopes: frozenset[str]

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(status_code=403, detail="Scope is not allowed")


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def authenticate(request: Request, api_key: str | None = Depends(api_key_header)) -> Principal:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")
    for candidate, value in request.app.state.settings.api_keys.items():
        if hmac.compare_digest(api_key, candidate):
            return Principal(value["tenant"], frozenset(value["scopes"]))
    raise HTTPException(status_code=401, detail="Invalid API key")
