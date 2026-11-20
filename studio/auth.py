import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Request

from studio import settings
from studio.db import pool
from studio.workflow import StudioError

COOKIE = "__Host-studio_session" if settings.PRODUCTION else "studio_session"


@dataclass(frozen=True)
class Actor:
    workspace_id: str
    workspace_name: str
    role: str
    key_id: UUID | None = None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def require_origin(request):
    if request.headers.get("Origin") != settings.PUBLIC_ORIGIN:
        raise StudioError(403, "Request origin is not authorized")


def browser_session(conn, token):
    if not token.startswith("st_") or len(token) > 128:
        return None
    key = conn.execute(
        """
        SELECT id, expires_at FROM workspace_keys
        WHERE digest = %s AND revoked_at IS NULL AND expires_at > clock_timestamp()
        """,
        (digest(token),),
    ).fetchone()
    if key is None:
        return None
    value = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO browser_sessions (digest, key_id, expires_at)
        VALUES (%s, %s, least(%s, clock_timestamp() + %s * interval '1 second'))
        """,
        (digest(value), key["id"], key["expires_at"], settings.SESSION_SECONDS),
    )
    return value


def credential(conn, token=None, session=None):
    if token is not None:
        if not token.startswith("st_") or len(token) > 128:
            return None
        condition = "k.digest = %s"
        from_clause = "workspace_keys k"
        value = token
    elif session and len(session) <= 128:
        condition = "s.digest = %s AND s.expires_at > clock_timestamp()"
        from_clause = "browser_sessions s JOIN workspace_keys k ON k.id = s.key_id"
        value = session
    else:
        return None
    return conn.execute(
        f"""
        SELECT k.id AS key_id, k.workspace_id, k.role, w.name AS workspace_name
        FROM {from_clause} JOIN workspaces w ON w.id = k.workspace_id
        WHERE {condition} AND k.revoked_at IS NULL AND k.expires_at > clock_timestamp()
        """,
        (digest(value),),
    ).fetchone()


def require_actor(request: Request):
    if not settings.AUTH_REQUIRED:
        return Actor("demo", "Local demo", "editor")
    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise StudioError(401, "A workspace access key is required")
        session = None
    else:
        token = None
        session = request.cookies.get(COOKIE)
        if session and request.method not in ("GET", "HEAD", "OPTIONS"):
            require_origin(request)
    with pool.connection() as conn:
        row = credential(conn, token=token, session=session)
    if row is None:
        raise StudioError(401, "Sign in with a workspace access key")
    return Actor(**row)


def authorize_editor(actor):
    if actor.role != "editor":
        raise StudioError(403, "This action requires editor access")
    return actor
