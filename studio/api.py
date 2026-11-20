import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg import Error as DatabaseError
from psycopg.errors import UniqueViolation
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool

from studio import auth, replicate, settings, workflow
from studio.db import pool, schema_ready
from studio.http import RequestGuards
from studio.media import media_response, signed_url
from studio.media import signature as sign
from studio.observability import configure_logging, queue_metrics

STATIC = Path(__file__).with_name("static")
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
ActionKey = Annotated[UUID, Header(alias="Idempotency-Key")]
Duration = Annotated[int, Field(ge=1000, le=15000)]
Principal = Annotated[auth.Actor, Depends(auth.require_actor)]


def require_editor(actor: Principal):
    return auth.authorize_editor(actor)


Editor = Annotated[auth.Actor, Depends(require_editor)]


class LoginRequest(BaseModel):
    access_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class NewProject(BaseModel):
    title: ShortText
    brief: LongText


class Revision(BaseModel):
    parent_id: UUID | None
    content: LongText


class ScriptRequest(BaseModel):
    parent_id: UUID | None
    quoted_credits: Annotated[int, Field(ge=0)] = 0


class SceneSpec(BaseModel):
    title: ShortText
    prompt: LongText
    kind: Literal["image", "video"]
    duration_ms: Duration


class SceneSplit(BaseModel):
    revision_id: UUID
    scenes: Annotated[list[SceneSpec], Field(min_length=2, max_length=6)]


class SceneTiming(BaseModel):
    scene_id: UUID
    trim_ms: Annotated[int, Field(ge=0, le=15000)]
    duration_ms: Duration


class TimelineEdit(BaseModel):
    version: Annotated[int, Field(ge=0)]
    scenes: Annotated[list[SceneTiming], Field(min_length=2, max_length=6)]


class MediaRequest(BaseModel):
    intent: Literal["generate", "regenerate"]
    scenario: Literal["normal", "late", "expire"] = "normal"
    quoted_credits: Annotated[int, Field(gt=0)]


class ExportRequest(BaseModel):
    version: Annotated[int, Field(ge=0)]


class ProviderEvent(BaseModel):
    run_id: UUID
    status: Literal["succeeded"]


@asynccontextmanager
async def lifespan(_app):
    settings.validate("api")
    configure_logging()
    with pool:
        pool.wait()
        if not schema_ready():
            raise RuntimeError("Run the matching database migrations before starting the API")
        yield


app = FastAPI(
    title="Creator Studio",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if settings.PRODUCTION else "/docs",
    redoc_url=None if settings.PRODUCTION else "/redoc",
)
app.add_middleware(RequestGuards)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(workflow.StudioError)
async def studio_error(_request, exc):
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse({"detail": str(exc)}, status_code=exc.status, headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc):
    return JSONResponse(
        {"detail": [{"loc": err["loc"], "msg": err["msg"]} for err in exc.errors()]},
        status_code=422,
    )


@app.exception_handler(PoolTimeout)
async def unavailable(_request, _exc):
    return JSONResponse({"detail": "Database temporarily unavailable"}, status_code=503)


def job_view(job):
    return {
        "id": job["id"],
        "kind": job["kind"],
        "state": job["state"],
        "scene_id": job["scene_id"],
        "error": job["error"],
        "result": job["result"],
        "status_url": f"/api/jobs/{job['id']}",
    }


def accepted(job):
    data = job_view(job)
    return JSONResponse(
        jsonable_encoder(data),
        status_code=202,
        headers={"Location": data["status_url"], "Retry-After": "2"},
    )


def asset_view(asset):
    return {
        "id": asset["id"],
        "kind": asset["kind"],
        "duration_ms": asset["duration_ms"],
        "url": signed_url(asset["id"]),
        "content_url": f"/api/assets/{asset['id']}/content",
    }


@app.get("/", include_in_schema=False)
def studio():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def health():
    try:
        ready = schema_ready()
    except (DatabaseError, PoolTimeout):
        ready = False
    if not ready:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.get("/livez", include_in_schema=False)
def live():
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    if not settings.METRICS_TOKEN:
        raise HTTPException(404)
    header = request.headers.get("Authorization", "").encode()
    if not hmac.compare_digest(header, ("Bearer " + settings.METRICS_TOKEN).encode()):
        raise HTTPException(401)
    return PlainTextResponse(queue_metrics(), media_type="text/plain; version=0.0.4")


@app.get("/api/session")
def get_session(actor: Principal):
    return {
        "workspace": actor.workspace_name,
        "role": actor.role,
        "demo_provider": settings.PROVIDER == "fixture",
        "auth_required": settings.AUTH_REQUIRED,
        "api_docs": not settings.PRODUCTION,
    }


@app.post("/api/session", status_code=204)
def login(body: LoginRequest, request: Request):
    if not settings.AUTH_REQUIRED:
        raise workflow.StudioError(404, "Sign-in is disabled for the demo")
    auth.require_origin(request)
    with pool.connection() as conn:
        value = auth.browser_session(conn, body.access_key)
        if value is None:
            raise workflow.StudioError(401, "Access key is invalid, expired, or revoked")
        if previous := request.cookies.get(auth.COOKIE):
            conn.execute("DELETE FROM browser_sessions WHERE digest = %s", (auth.digest(previous),))
    response = Response(status_code=204)
    response.set_cookie(
        auth.COOKIE,
        value,
        max_age=settings.SESSION_SECONDS,
        secure=settings.PRODUCTION,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@app.delete("/api/session", status_code=204)
def logout(request: Request):
    auth.require_origin(request)
    if cookie := request.cookies.get(auth.COOKIE):
        with pool.connection() as conn:
            conn.execute("DELETE FROM browser_sessions WHERE digest = %s", (auth.digest(cookie),))
    response = Response(status_code=204)
    response.delete_cookie(
        auth.COOKIE, secure=settings.PRODUCTION, httponly=True, samesite="strict"
    )
    return response


@app.get("/api/projects")
def list_projects(
    actor: Principal, limit: Annotated[int, Query(ge=1, le=50)] = 30, before: UUID | None = None
):
    with pool.connection() as conn:
        cursor = workflow.project_row(conn, before, actor.workspace_id) if before else None
        projects = conn.execute(
            """
            SELECT id, title, created_at FROM projects WHERE workspace_id = %s
              AND (%s::timestamptz IS NULL OR (created_at, id) < (%s, %s))
            ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (
                actor.workspace_id,
                cursor and cursor["created_at"],
                cursor and cursor["created_at"],
                before,
                limit + 1,
            ),
        ).fetchall()
        return {
            "projects": projects[:limit],
            "next_cursor": projects[limit - 1]["id"] if len(projects) > limit else None,
            "credits": workflow.credit_summary(conn, actor.workspace_id),
            "costs": settings.COSTS,
        }


@app.post("/api/projects", status_code=201)
def create_project(body: NewProject, actor: Editor):
    with pool.connection() as conn:
        conn.execute(
            "SELECT id FROM workspaces WHERE id = %s FOR NO KEY UPDATE", (actor.workspace_id,)
        )
        count = conn.execute(
            "SELECT count(*) AS total FROM projects WHERE workspace_id = %s", (actor.workspace_id,)
        ).fetchone()["total"]
        if count >= settings.MAX_PROJECTS:
            raise workflow.StudioError(409, "This workspace has reached its project limit")
        return conn.execute(
            """
            INSERT INTO projects (id, workspace_id, title, brief)
            VALUES (%s, %s, %s, %s) RETURNING id, title
            """,
            (uuid4(), actor.workspace_id, body.title, body.brief),
        ).fetchone()


@app.get("/api/projects/{project_id}")
def get_project(project_id: UUID, actor: Principal):
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        project = workflow.project_row(conn, project_id, actor.workspace_id)
        revisions = conn.execute(
            """
            WITH recent AS (
                SELECT id FROM script_revisions WHERE project_id = %s
                ORDER BY created_at DESC LIMIT 80
            )
            SELECT * FROM script_revisions WHERE id IN (SELECT id FROM recent) OR id = %s
            ORDER BY created_at DESC
            """,
            (project_id, project["current_revision_id"]),
        ).fetchall()
        scenes = conn.execute(
            "SELECT * FROM scenes WHERE project_id = %s ORDER BY position", (project_id,)
        ).fetchall()
        latest_ids = [scene["latest_job_id"] for scene in scenes if scene["latest_job_id"]]
        jobs = conn.execute(
            """
            WITH recent AS (
                SELECT id FROM jobs WHERE project_id = %s ORDER BY created_at DESC, id DESC LIMIT 50
            )
            SELECT j.id, j.kind, j.state, j.scene_id, j.error, j.result, j.created_at,
                   c.amount AS credit_amount, c.state AS credit_state
            FROM jobs j LEFT JOIN credit_reservations c ON c.job_id = j.id
            WHERE j.project_id = %s AND (
                j.id IN (SELECT id FROM recent) OR j.id = ANY(%s) OR
                j.state IN ('queued', 'running', 'confirming')
            )
            ORDER BY j.created_at DESC, j.id DESC
            """,
            (project_id, project_id, latest_ids),
        ).fetchall()
        ids = {
            UUID(job["result"]["asset_id"])
            for job in jobs
            if job["result"] and "asset_id" in job["result"]
        }
        ids.update(
            asset_id
            for scene in scenes
            for asset_id in (scene["accepted_asset_id"], scene["candidate_asset_id"])
            if asset_id
        )
        assets = conn.execute(
            "SELECT * FROM assets WHERE project_id = %s AND id = ANY(%s)", (project_id, list(ids))
        ).fetchall()
        media = {asset["id"]: asset_view(asset) for asset in assets}
        job_views = {
            job["id"]: {
                **job_view(job),
                "created_at": job["created_at"],
                "credit_amount": job["credit_amount"],
                "credit_state": job["credit_state"],
                "asset": media.get(UUID(job["result"]["asset_id"]))
                if job["result"] and "asset_id" in job["result"]
                else None,
            }
            for job in jobs
        }
        return {
            "project": project,
            "revisions": revisions,
            "credits": workflow.credit_summary(conn, actor.workspace_id),
            "costs": settings.COSTS,
            "jobs": list(job_views.values()),
            "scenes": [
                {
                    **scene,
                    "accepted": media.get(scene["accepted_asset_id"]),
                    "candidate": media.get(scene["candidate_asset_id"]),
                    "job": job_views.get(scene["latest_job_id"]),
                }
                for scene in scenes
            ],
        }


@app.post("/api/projects/{project_id}/script-assist", status_code=202)
def assist_script(project_id: UUID, body: ScriptRequest, action_key: ActionKey, actor: Editor):
    request = body.model_dump(mode="json", exclude_defaults=True)
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, actor.workspace_id, lock=True)
        old = workflow.existing_action(conn, project, action_key, "script", None, request)
        if old:
            return accepted(old)
        if project["current_revision_id"] != body.parent_id:
            raise workflow.StudioError(409, "Refresh to use the current script revision")
        if body.quoted_credits != settings.COSTS["script"]:
            raise workflow.StudioError(409, "Confirm the current script credit quote")
        previous = None
        if body.parent_id:
            previous = conn.execute(
                "SELECT content FROM script_revisions WHERE id = %s", (body.parent_id,)
            ).fetchone()["content"]
        job, _ = workflow.insert_job(
            conn,
            project,
            "script",
            action_key,
            request,
            {
                "parent_id": request["parent_id"],
                "title": project["title"],
                "brief": project["brief"],
                "previous": previous,
            },
            actor_id=actor.key_id,
        )
        return accepted(job)


@app.post("/api/projects/{project_id}/revisions", status_code=201)
def revise_script(project_id: UUID, body: Revision, actor: Editor):
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, actor.workspace_id, lock=True)
        if project["current_revision_id"] != body.parent_id:
            raise workflow.StudioError(409, "Refresh before editing the newer script revision")
        return workflow.save_revision(conn, project, body.content, body.parent_id, "editor")


@app.post("/api/projects/{project_id}/scenes", status_code=204)
def split_script(project_id: UUID, body: SceneSplit, actor: Editor):
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, actor.workspace_id, lock=True)
        workflow.split_scenes(conn, project, body.revision_id, body.scenes)
    return Response(status_code=204)


@app.put("/api/projects/{project_id}/timeline", status_code=204)
def edit_timeline(project_id: UUID, body: TimelineEdit, actor: Editor):
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, actor.workspace_id, lock=True)
        workflow.update_timeline(conn, project, body.scenes, body.version)
    return Response(status_code=204)


@app.post("/api/scenes/{scene_id}/generations", status_code=202)
def generate(scene_id: UUID, body: MediaRequest, action_key: ActionKey, actor: Editor):
    with pool.connection() as conn:
        scene = workflow.scene_row(conn, scene_id, actor.workspace_id)
        project = workflow.project_row(conn, scene["project_id"], actor.workspace_id, lock=True)
        scene = workflow.scene_row(conn, scene_id, actor.workspace_id, lock=True)
        return accepted(
            workflow.start_media(
                conn,
                project,
                scene,
                action_key,
                body.model_dump(mode="json"),
                actor.key_id,
            )
        )


@app.post("/api/scenes/{scene_id}/generations/{job_id}/retry", status_code=202)
def retry(scene_id: UUID, job_id: UUID, actor: Editor):
    with pool.connection() as conn:
        scene = workflow.scene_row(conn, scene_id, actor.workspace_id)
        workflow.project_row(conn, scene["project_id"], actor.workspace_id, lock=True)
        scene = workflow.scene_row(conn, scene_id, actor.workspace_id, lock=True)
        return accepted(workflow.retry_media(conn, scene, job_id))


@app.put("/api/scenes/{scene_id}/accepted/{asset_id}", status_code=204)
def accept(scene_id: UUID, asset_id: UUID, actor: Editor):
    with pool.connection() as conn:
        scene = workflow.scene_row(conn, scene_id, actor.workspace_id)
        workflow.project_row(conn, scene["project_id"], actor.workspace_id, lock=True)
        scene = workflow.scene_row(conn, scene_id, actor.workspace_id, lock=True)
        workflow.accept_asset(conn, scene, asset_id)
    return Response(status_code=204)


@app.post("/api/projects/{project_id}/exports", status_code=202)
def export(project_id: UUID, body: ExportRequest, action_key: ActionKey, actor: Editor):
    request = body.model_dump(mode="json")
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, actor.workspace_id, lock=True)
        old = workflow.existing_action(conn, project, action_key, "export", None, request)
        if old:
            return accepted(old)
        if project["timeline_version"] != body.version:
            raise workflow.StudioError(409, "Refresh before exporting the newer cut")
        snapshot = workflow.export_snapshot(conn, project)
        job, _ = workflow.insert_job(
            conn, project, "export", action_key, request, snapshot, actor_id=actor.key_id
        )
        return accepted(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: UUID, actor: Principal):
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = %s AND workspace_id = %s", (job_id, actor.workspace_id)
        ).fetchone()
        if job is None:
            raise workflow.StudioError(404, "Job not found")
        events = conn.execute(
            """
            SELECT event, detail, created_at FROM (
                SELECT id, event, detail, created_at FROM job_events
                WHERE job_id = %s ORDER BY id DESC LIMIT 150
            ) recent ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        credit = conn.execute(
            "SELECT amount, state FROM credit_reservations WHERE job_id = %s", (job_id,)
        ).fetchone()
        asset = conn.execute("SELECT * FROM assets WHERE job_id = %s", (job_id,)).fetchone()
        return {
            **job_view(job),
            "events": events,
            "credit": credit,
            "provider_run_id": job["provider_run_id"],
            "manifest": {
                **job["input"],
                "scenes": [
                    {key: value for key, value in scene.items() if key != "storage_key"}
                    for scene in job["input"]["scenes"]
                ],
            }
            if job["kind"] == "export"
            else None,
            "asset": asset_view(asset) if asset else None,
        }


@app.post("/internal/provider-events", status_code=204, include_in_schema=False)
async def provider_event(request: Request):
    if settings.PROVIDER != "fixture":
        raise HTTPException(404)
    raw = await request.body()
    expected = sign(b"provider:" + raw)
    if not hmac.compare_digest(
        request.headers.get("X-Provider-Signature", "").encode(), expected.encode()
    ):
        raise HTTPException(401, "Invalid provider signature")
    try:
        event = ProviderEvent.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid provider event") from exc
    # Run synchronous database work in the threadpool after reading the async request body.
    await run_in_threadpool(record_provider_event, event)
    return Response(status_code=204)


def record_provider_event(event):
    with pool.connection() as conn:
        row = conn.execute(
            """
            UPDATE jobs SET provider_notified_at = clock_timestamp(),
                            wake_seq = wake_seq + 1, run_after = clock_timestamp()
            WHERE id = %s AND provider = 'fixture' AND kind = 'media' AND state IN ('queued', 'running', 'confirming')
                  AND provider_notified_at IS NULL
            RETURNING id
            """,
            (event.run_id,),
        ).fetchone()
        if row:
            workflow.log_event(conn, row["id"], "provider_callback")


@app.get("/media/{asset_id}", include_in_schema=False)
def get_media(asset_id: UUID, expires: int, signature: str, download: bool = False):
    if expires <= int(time.time()) or not hmac.compare_digest(
        signature.encode(), sign(f"media:{asset_id}:{expires}".encode()).encode()
    ):
        raise HTTPException(403, "Media grant has expired or is invalid")
    with pool.connection() as conn:
        asset = conn.execute(
            "SELECT kind, storage_key FROM assets WHERE id = %s", (asset_id,)
        ).fetchone()
    if asset is None:
        raise HTTPException(404, "Media not found")
    return media_response(asset, download)


@app.get("/api/assets/{asset_id}/content")
def workspace_media(asset_id: UUID, actor: Principal, download: bool = False):
    with pool.connection() as conn:
        asset = conn.execute(
            """
            SELECT a.kind, a.storage_key FROM assets a JOIN projects p ON p.id = a.project_id
            WHERE a.id = %s AND p.workspace_id = %s
            """,
            (asset_id, actor.workspace_id),
        ).fetchone()
    if not asset:
        raise workflow.StudioError(404, "Media not found")
    return media_response(asset, download)


@app.post("/webhooks/replicate/{job_id}/{token}", status_code=204, include_in_schema=False)
async def replicate_webhook(job_id: UUID, token: str, request: Request):
    if settings.PROVIDER != "replicate":
        raise HTTPException(404)
    raw = await request.body()
    if not replicate.authenticate_webhook(job_id, token, request.headers, raw):
        raise HTTPException(401, "Invalid provider signature")
    try:
        prediction = replicate.Prediction.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid provider event") from exc
    if prediction.status not in replicate.TERMINAL:
        return Response(status_code=204)
    try:
        await run_in_threadpool(
            replicate.record_event, job_id, request.headers["webhook-id"], prediction
        )
    except UniqueViolation as exc:
        raise HTTPException(409, "Provider run belongs to another action") from exc
    return Response(status_code=204)
