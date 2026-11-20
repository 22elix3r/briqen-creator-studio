import logging
import subprocess
import time
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb

from studio import settings, workflow
from studio.db import pool
from studio.media import render_manifest, stage_bytes

logger = logging.getLogger("creator-studio.worker")


def claim_job():
    with pool.connection() as conn:
        job = conn.execute(
            """
            SELECT * FROM jobs
            WHERE state IN ('queued', 'running', 'confirming')
              AND (run_after <= clock_timestamp() OR expires_at <= clock_timestamp())
              AND (lease_until IS NULL OR lease_until <= clock_timestamp())
            ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED
            """
        ).fetchone()
        if job is None:
            return None
        token = uuid4()
        state = "running" if job["state"] == "queued" else job["state"]
        conn.execute(
            """
            UPDATE jobs SET lease_token = %s, lease_until = clock_timestamp() + %s * interval '1 second',
                            state = %s, updated_at = clock_timestamp()
            WHERE id = %s
            """,
            (token, settings.LEASE_SECONDS, state, job["id"]),
        )
        if job["lease_token"] is not None:
            workflow.log_event(conn, job["id"], "lease_recovered")
        if job["state"] == "queued":
            workflow.log_event(conn, job["id"], "running")
        return {**job, "state": state, "lease_token": token}


def reschedule(job, state, delay):
    with pool.connection() as conn:
        updated = conn.execute(
            """
            UPDATE jobs SET state = %s, lease_token = NULL, lease_until = NULL,
                run_after = CASE WHEN wake_seq = %s THEN clock_timestamp() + %s * interval '1 second'
                                 ELSE least(run_after, clock_timestamp()) END,
                updated_at = clock_timestamp()
            WHERE id = %s AND lease_token = %s AND state = %s
            RETURNING id
            """,
            (state, job["wake_seq"], delay, job["id"], job["lease_token"], job["state"]),
        ).fetchone()
        if updated and job["state"] != state:
            workflow.log_event(conn, job["id"], state)


def transition(conn, job, state, *, result=None, error=None):
    conn.execute(
        """
        UPDATE jobs SET state = %s, result = %s, error = %s, lease_token = NULL,
                        lease_until = NULL, updated_at = clock_timestamp() WHERE id = %s
        """,
        (state, Jsonb(result) if result is not None else None, error, job["id"]),
    )
    workflow.log_event(conn, job["id"], state, **({"reason": error} if error else {}))
    logger.info("job=%s kind=%s %s", job["id"], job["kind"], state)


def finish(claim, *, result=None, error=None):
    with pool.connection() as conn:
        project = workflow.project_row(conn, claim["project_id"], lock=True)
        scene = (
            workflow.scene_row(conn, claim["scene_id"], lock=True) if claim["scene_id"] else None
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (claim["id"],)).fetchone()
        if job["state"] not in workflow.ACTIVE or job["lease_token"] != claim["lease_token"]:
            return
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        expired = now >= job["expires_at"]
        stale = scene and (
            scene["latest_job_id"] != job["id"]
            or scene["visual_version"] != job["input"]["visual_version"]
        )
        if expired or stale or error:
            message = (
                "The recovery window expired. No credits were charged."
                if expired and scene
                else "The job exceeded its deadline."
                if expired
                else "The scene has newer inputs."
                if stale
                else error
            )
            if scene:
                workflow.settle_credits(conn, job, captured=False)
            transition(conn, job, "expired" if expired else "failed", error=message)
            return
        if job["kind"] == "script":
            parent = UUID(job["input"]["parent_id"]) if job["input"]["parent_id"] else None
            revision = workflow.save_revision(conn, project, result["content"], parent, "provider")
            transition(conn, job, "succeeded", result={"revision_id": str(revision["id"])})
            return
        asset_id = uuid4()
        conn.execute(
            """
            INSERT INTO assets (id, job_id, project_id, kind, storage_key, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                job["id"],
                project["id"],
                result["kind"],
                result["storage_key"],
                result["duration_ms"],
            ),
        )
        if scene:
            workflow.settle_credits(conn, job, captured=True)
            conn.execute(
                "UPDATE scenes SET candidate_asset_id = %s WHERE id = %s", (asset_id, scene["id"])
            )
        transition(conn, job, "succeeded", result={"asset_id": str(asset_id)})


def run_media(client, job):
    run_path = f"/runs/{job['id']}"
    inputs = job["input"]
    # A worker can die after the provider accepted the previous submission.
    response = client.get(run_path)
    if response.status_code == 404:
        response = client.put(run_path, json=inputs)
    if response.status_code >= 500 or response.status_code in (408, 429):
        reschedule(job, "confirming", 0.75)
        return
    if response.status_code >= 400:
        finish(job, error="The media provider rejected the generation.")
        return
    status = response.json()
    if status["run_id"] != str(job["id"]):
        finish(job, error="The provider returned a mismatched run ID.")
        return
    if status["state"] != "succeeded":
        reschedule(job, job["state"], 0.75)
        return
    response = client.get(f"{run_path}/media")
    if response.status_code in (404, 409, 429) or response.status_code >= 500:
        reschedule(job, "confirming", 0.75)
        return
    response.raise_for_status()
    video = inputs["kind"] == "video"
    key = f"{job['id']}.{'mp4' if video else 'png'}"
    stage_bytes(response.content, key)
    finish(
        job,
        result={
            "kind": inputs["kind"],
            "storage_key": key,
            "duration_ms": 8000 if video else None,
        },
    )


def perform(client, job):
    with pool.connection() as conn:
        expired = conn.execute(
            "SELECT %s <= clock_timestamp() AS expired", (job["expires_at"],)
        ).fetchone()["expired"]
    if expired:
        finish(job, error="Expired.")
        return
    if job["kind"] == "media":
        run_media(client, job)
    elif job["kind"] == "script":
        response = client.post("/script", json=job["input"])
        response.raise_for_status()
        finish(job, result=response.json())
    else:
        key = f"{job['id']}.mp4"
        render_manifest(job["input"], key)
        finish(
            job,
            result={
                "kind": "export",
                "storage_key": key,
                "duration_ms": sum(scene["duration_ms"] for scene in job["input"]["scenes"]),
            },
        )


def work_forever():
    with pool, httpx.Client(base_url=settings.PROVIDER_URL, timeout=10) as client:
        pool.wait()
        while True:
            job = claim_job()
            if job is None:
                time.sleep(0.15)
                continue
            try:
                perform(client, job)
            except (httpx.TimeoutException, httpx.NetworkError):
                reschedule(job, "confirming" if job["kind"] == "media" else job["state"], 1)
            except (subprocess.SubprocessError, OSError, httpx.HTTPError, KeyError, ValueError):
                logger.exception("Job %s could not complete", job["id"])
                finish(job, error="Job could not complete. Try a fresh request.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    work_forever()
