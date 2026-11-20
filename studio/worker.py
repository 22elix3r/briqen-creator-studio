import logging
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout

from studio import replicate, settings, workflow
from studio.db import pool, schema_ready
from studio.media import normalize_media, render_manifest, stage_bytes, store_file
from studio.observability import configure_logging

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
                run_after = least(expires_at,
                    CASE WHEN wake_seq = %s THEN clock_timestamp() + %s * interval '1 second'
                         ELSE least(run_after, clock_timestamp()) END),
                updated_at = clock_timestamp(), poll_count = poll_count + 1
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

    logger.info("job_transition", extra={"job_id": job["id"], "kind": job["kind"], "state": state})


def finish(claim, *, result=None, error=None):
    with pool.connection() as conn:
        workspace_id = claim["workspace_id"]
        project = workflow.project_row(conn, claim["project_id"], workspace_id, lock=True)
        scene = (
            workflow.scene_row(conn, claim["scene_id"], workspace_id, lock=True)
            if claim["scene_id"]
            else None
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
                if expired and job["provider"]
                else "The job exceeded its deadline."
                if expired
                else "The scene has newer inputs."
                if stale
                else error
            )
            workflow.settle_credits(conn, job, captured=False)
            transition(conn, job, "expired" if expired else "failed", error=message)
            return
        if job["kind"] == "script":
            parent = UUID(job["input"]["parent_id"]) if job["input"]["parent_id"] else None
            revision = workflow.save_revision(conn, project, result["content"], parent, "provider")
            workflow.settle_credits(conn, job, captured=True)
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


def run_fixture_media(client, job):
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


def backoff(job):
    if job["provider"] == "fixture":
        return 0.75
    return min(25, 2 * 1.45 ** min(job["poll_count"], 10))


def run_replicate(client, job):
    if job["provider_run_id"]:
        prediction = replicate.status(client, job["provider_run_id"])
    elif job["provider_submit_started_at"] or not replicate.reserve_submission(job):
        # An unacknowledged POST may already have started a billable run.
        reschedule(job, "confirming", backoff(job))
        return
    else:
        prediction = replicate.submit(client, job)
        if not replicate.remember_prediction(job, prediction):
            return
    if prediction.status in {"failed", "canceled"}:
        finish(job, error="The generation provider ended this run. No credits were charged.")
        return
    if prediction.status != "succeeded":
        reschedule(job, "running", backoff(job))
        return
    if job["kind"] == "script":
        finish(job, result={"content": replicate.script_output(prediction.output)})
        return
    kind = job["input"]["kind"]
    url = replicate.media_output(prediction.output, kind)
    key = f"{job['id']}.{'mp4' if kind == 'video' else 'png'}"
    with tempfile.TemporaryDirectory() as directory:
        source, output = Path(directory) / "provider-media", Path(directory) / key
        replicate.download_media(url, source)
        duration = normalize_media(source, output, kind)
        store_file(output, key)
    finish(job, result={"kind": kind, "storage_key": key, "duration_ms": duration})


def perform(fixture_client, live_client, job):
    with pool.connection() as conn:
        expired = conn.execute(
            "SELECT %s <= clock_timestamp() AS expired", (job["expires_at"],)
        ).fetchone()["expired"]
    if expired:
        finish(job, error="Expired.")
        return
    if job["kind"] == "export":
        key = f"{job['id']}.mp4"
        duration = render_manifest(job["input"], key)
        finish(job, result={"kind": "export", "storage_key": key, "duration_ms": duration})
    elif job["provider"] == "replicate":
        run_replicate(live_client, job)
    elif job["kind"] == "media":
        run_fixture_media(fixture_client, job)
    else:
        response = fixture_client.post("/script", json=job["input"])
        response.raise_for_status()
        finish(job, result=response.json())


def renew_lease(stop, job):
    while not stop.wait(settings.LEASE_SECONDS / 3):
        try:
            with pool.connection() as conn:
                current = conn.execute(
                    """
                    UPDATE jobs SET lease_until = least(expires_at, clock_timestamp() + %s * interval '1 second')
                    WHERE id = %s AND lease_token = %s AND expires_at > clock_timestamp()
                    RETURNING id
                    """,
                    (settings.LEASE_SECONDS, job["id"], job["lease_token"]),
                ).fetchone()
            if not current:
                return
        except (DatabaseError, PoolTimeout) as exc:
            logger.warning(
                "lease_renewal_failed",
                extra={"job_id": job["id"], "error_type": type(exc).__name__},
            )


@contextmanager
def heartbeat(job):
    stop = Event()
    thread = Thread(target=renew_lease, args=(stop, job), daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def process_claim(fixture, live, job):
    def transient():
        reschedule(job, "confirming" if job["provider"] else job["state"], backoff(job))

    with heartbeat(job):
        try:
            perform(fixture, live, job)
        except (httpx.TransportError, replicate.ProviderUnavailable, BotoCoreError):
            transient()
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] in {409, 429, 500, 502, 503, 504}:
                transient()
            else:
                logger.error(
                    "object_store_error",
                    extra={"job_id": job["id"], "error_type": type(exc).__name__},
                )
                finish(job, error="Media storage could not complete this job.")
        except replicate.ProviderRejected as exc:
            finish(job, error=str(exc))
        except (
            subprocess.SubprocessError,
            OSError,
            httpx.HTTPError,
            KeyError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            logger.error(
                "job_processing_error",
                extra={"job_id": job["id"], "error_type": type(exc).__name__},
            )
            finish(job, error="The job could not complete. Try a fresh request.")


def work_forever():
    settings.validate("worker")
    stop = Event()

    def stop_worker(_signal, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    with (
        pool,
        httpx.Client(base_url=settings.PROVIDER_URL, timeout=10, trust_env=False) as fixture,
        httpx.Client(
            base_url=replicate.API,
            headers={"Authorization": f"Bearer {settings.REPLICATE_TOKEN}"},
            timeout=12,
            trust_env=False,
            follow_redirects=False,
        ) as live,
    ):
        pool.wait()
        if not schema_ready():
            raise RuntimeError("Run the matching database migrations before starting workers")
        while not stop.is_set():
            try:
                job = claim_job()
                if job is None:
                    stop.wait(0.20)
                else:
                    process_claim(fixture, live, job)
            except (DatabaseError, PoolTimeout) as exc:
                logger.warning("database_retry", extra={"error_type": type(exc).__name__})
                stop.wait(2)


if __name__ == "__main__":
    configure_logging()
    work_forever()
