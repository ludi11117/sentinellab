import hashlib
import hmac
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import select

from .config import ADMIN_TOKEN, ALLOW_LOCAL


def key_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    project_id: str
    role: str
    key_id: str = "local"


def principal(request: Request):
    # Memoized per request, never accepts a caller supplied project id.
    if hasattr(request.state, "principal"):
        return request.state.principal
    from .db import SessionLocal
    from .models import AccessKey
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise HTTPException(401, "Bearer project key required")
        with SessionLocal() as db:
            key = db.scalar(select(AccessKey).where(AccessKey.digest == key_digest(token), AccessKey.revoked == 0))
            if key is None:
                raise HTTPException(401, "invalid or revoked project key")
            identity = Principal(key.project_id, key.role, key.id)
    elif ALLOW_LOCAL and request.client and request.client.host in ("127.0.0.1", "::1", "testclient"):
        identity = Principal("local", "writer")
    else:
        raise HTTPException(401, "project key required")
    path = request.url.path
    review_path = bool(re.fullmatch(r"/api/public/reviews(?:/[a-f0-9-]{36}(?:/tasks|/votes)?)?", path))
    context_path = path == "/api/public/context" and request.method == "GET"
    if identity.role == "reviewer" and not context_path and (not review_path or (request.method not in ("GET", "HEAD", "OPTIONS") and not path.endswith("/votes"))):
        raise HTTPException(403, "reviewer key is restricted to blinded review tasks and votes")
    if request.method not in ("GET", "HEAD", "OPTIONS") and identity.role != "writer" and not (identity.role == "reviewer" and path.endswith("/votes")):
        raise HTTPException(403, "reader key cannot modify project data")
    request.state.principal = identity
    return identity


def administrator(request: Request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if not ADMIN_TOKEN or scheme.casefold() != "bearer" or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "administrator token required")
