import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool

from studio import settings, workflow
from studio.db import pool
from studio.media import media_path, signed_url
from studio.media import signature as sign

STATIC = Path(__file__).with_name("static")
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
ActionKey = Annotated[UUID, Header(alias="Idempotency-Key")]
Duration = Annotated[int, Field(ge=1000, le=15000)]


class NewProject(BaseModel):
    title: ShortText
    brief: LongText


class Revision(BaseModel):
    parent_id: UUID | None
    content: LongText


class ScriptRequest(BaseModel):
    parent_id: UUID | None


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
    with pool:
        pool.wait()
        yield


app = FastAPI(title="Creator Studio", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(workflow.StudioError)
async def studio_error(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


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
        jsonable_encoder(data), status_code=202, headers={"Location": data["status_url"]}
    )


def asset_view(asset):
    return {
        "id": asset["id"],
        "kind": asset["kind"],
        "duration_ms": asset["duration_ms"],
        "url": signed_url(asset["id"]),
    }


@app.get("/", include_in_schema=False)
def studio():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/projects")
def list_projects():
    with pool.connection() as conn:
        projects = conn.execute(
            """
            SELECT id, title, created_at FROM projects WHERE workspace_id = %s
            ORDER BY created_at DESC LIMIT 30
            """,
            (workflow.WORKSPACE,),
        ).fetchall()
        return {
            "projects": projects,
            "credits": workflow.credit_summary(conn),
            "costs": settings.COSTS,
        }


@app.post("/api/projects", status_code=201)
def create_project(body: NewProject):
    with pool.connection() as conn:
        return conn.execute(
            """
            INSERT INTO projects (id, workspace_id, title, brief)
            VALUES (%s, %s, %s, %s) RETURNING id, title
            """,
            (uuid4(), workflow.WORKSPACE, body.title, body.brief),
        ).fetchone()


@app.get("/api/projects/{project_id}")
def get_project(project_id: UUID):
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        project = workflow.project_row(conn, project_id)
        revisions = conn.execute(
            "SELECT * FROM script_revisions WHERE project_id = %s ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        scenes = conn.execute(
            "SELECT * FROM scenes WHERE project_id = %s ORDER BY position", (project_id,)
        ).fetchall()
        assets = conn.execute(
            "SELECT * FROM assets WHERE project_id = %s", (project_id,)
        ).fetchall()
        media = {asset["id"]: asset_view(asset) for asset in assets}
        jobs = conn.execute(
            """
            SELECT j.*, c.amount AS credit_amount, c.state AS credit_state
            FROM jobs j LEFT JOIN credit_reservations c ON c.job_id = j.id
            WHERE j.project_id = %s ORDER BY j.created_at DESC
            """,
            (project_id,),
        ).fetchall()
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
            "credits": workflow.credit_summary(conn),
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
def assist_script(project_id: UUID, body: ScriptRequest, action_key: ActionKey):
    request = body.model_dump(mode="json")
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, lock=True)
        old = workflow.existing_action(conn, action_key, project_id, "script", None, request)
        if old:
            return accepted(old)
        if project["current_revision_id"] != body.parent_id:
            raise workflow.StudioError(409, "Refresh to use the current script revision")
        previous = None
        if body.parent_id:
            previous = conn.execute(
                "SELECT content FROM script_revisions WHERE id = %s", (body.parent_id,)
            ).fetchone()["content"]
        job, _ = workflow.insert_job(
            conn,
            project_id,
            "script",
            action_key,
            request,
            {**request, "title": project["title"], "brief": project["brief"], "previous": previous},
        )
        return accepted(job)


@app.post("/api/projects/{project_id}/revisions", status_code=201)
def revise_script(project_id: UUID, body: Revision):
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, lock=True)
        if project["current_revision_id"] != body.parent_id:
            raise workflow.StudioError(409, "Refresh before editing the newer script revision")
        return workflow.save_revision(conn, project, body.content, body.parent_id, "editor")


@app.post("/api/projects/{project_id}/scenes", status_code=204)
def split_script(project_id: UUID, body: SceneSplit):
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, lock=True)
        workflow.split_scenes(conn, project, body.revision_id, body.scenes)
    return Response(status_code=204)


@app.put("/api/projects/{project_id}/timeline", status_code=204)
def edit_timeline(project_id: UUID, body: TimelineEdit):
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, lock=True)
        workflow.update_timeline(conn, project, body.scenes, body.version)
    return Response(status_code=204)


@app.post("/api/scenes/{scene_id}/generations", status_code=202)
def generate(scene_id: UUID, body: MediaRequest, action_key: ActionKey):
    with pool.connection() as conn:
        scene = workflow.scene_row(conn, scene_id)
        workflow.project_row(conn, scene["project_id"], lock=True)
        scene = workflow.scene_row(conn, scene_id, lock=True)
        return accepted(workflow.start_media(conn, scene, action_key, body.model_dump(mode="json")))


@app.post("/api/scenes/{scene_id}/generations/{job_id}/retry", status_code=202)
def retry(scene_id: UUID, job_id: UUID):
    with pool.connection() as conn:
        scene = workflow.scene_row(conn, scene_id)
        workflow.project_row(conn, scene["project_id"], lock=True)
        scene = workflow.scene_row(conn, scene_id, lock=True)
        return accepted(workflow.retry_media(conn, scene, job_id))


@app.put("/api/scenes/{scene_id}/accepted/{asset_id}", status_code=204)
def accept(scene_id: UUID, asset_id: UUID):
    with pool.connection() as conn:
        scene = workflow.scene_row(conn, scene_id)
        workflow.project_row(conn, scene["project_id"], lock=True)
        scene = workflow.scene_row(conn, scene_id, lock=True)
        workflow.accept_asset(conn, scene, asset_id)
    return Response(status_code=204)


@app.post("/api/projects/{project_id}/exports", status_code=202)
def export(project_id: UUID, body: ExportRequest, action_key: ActionKey):
    request = body.model_dump(mode="json")
    with pool.connection() as conn:
        project = workflow.project_row(conn, project_id, lock=True)
        old = workflow.existing_action(conn, action_key, project_id, "export", None, request)
        if old:
            return accepted(old)
        if project["timeline_version"] != body.version:
            raise workflow.StudioError(409, "Refresh before exporting the newer cut")
        snapshot = workflow.export_snapshot(conn, project)
        job, _ = workflow.insert_job(conn, project_id, "export", action_key, request, snapshot)
        return accepted(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: UUID):
    with pool.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = %s AND workspace_id = %s", (job_id, workflow.WORKSPACE)
        ).fetchone()
        if job is None:
            raise workflow.StudioError(404, "Job not found")
        events = conn.execute(
            "SELECT event, detail, created_at FROM job_events WHERE job_id = %s ORDER BY id",
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
            "provider_run_id": job_id if job["kind"] == "media" else None,
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
    raw = await request.body()
    expected = sign(b"provider:" + raw)
    if not hmac.compare_digest(request.headers.get("X-Provider-Signature", ""), expected):
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
            WHERE id = %s AND kind = 'media' AND state IN ('queued', 'running', 'confirming')
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
        signature, sign(f"media:{asset_id}:{expires}".encode())
    ):
        raise HTTPException(403, "Media grant has expired or is invalid")
    with pool.connection() as conn:
        asset = conn.execute(
            "SELECT kind, storage_key FROM assets WHERE id = %s", (asset_id,)
        ).fetchone()
    if asset is None or not media_path(asset["storage_key"]).is_file():
        raise HTTPException(404, "Media not found")
    is_image = asset["kind"] == "image"
    return FileResponse(
        media_path(asset["storage_key"]),
        media_type="image/png" if is_image else "video/mp4",
        filename=("creator-studio.png" if is_image else "creator-studio.mp4") if download else None,
        headers={"Cache-Control": "private, max-age=60"},
    )
