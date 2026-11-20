"""Replicate transport and callback correlation. Create is deliberately one-shot."""

import base64
import hashlib
import hmac
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from studio import settings, workflow
from studio.db import pool
from studio.media import signature

API = "https://api.replicate.com"
PREDICTION_ID = re.compile(r"[a-zA-Z0-9_-]{1,100}", re.ASCII)
TERMINAL = {"succeeded", "failed", "canceled"}
STATES = TERMINAL | {"starting", "processing"}


class ProviderUnavailable(Exception):
    """The provider outcome remains unknown."""


class ProviderRejected(Exception):
    """The provider reported a terminal rejection or unusable result."""


class Prediction(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=PREDICTION_ID.pattern)
    status: str
    output: object = None


def webhook_url(job_id):
    token = signature(f"replicate-callback:{job_id}".encode())
    return f"{settings.PUBLIC_ORIGIN}/webhooks/replicate/{job_id}/{token}"


def authenticate_webhook(job_id, token, headers, body, *, now=None):
    expected_token = signature(f"replicate-callback:{job_id}".encode()).encode()
    if not hmac.compare_digest(expected_token, token.encode()):
        return False
    event_id = headers.get("webhook-id", "")
    stamp = headers.get("webhook-timestamp", "")
    received = headers.get("webhook-signature", "")
    if not (
        event_id
        and len(event_id) <= 200
        and stamp.isascii()
        and stamp.isdecimal()
        and len(stamp) <= 12
    ):
        return False
    now = time.time() if now is None else now
    key = settings.webhook_key()
    if abs(now - int(stamp)) > 300 or not key:
        return False
    payload = f"{event_id}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.digest(key, payload, hashlib.sha256))
    return any(
        part.startswith("v1,") and hmac.compare_digest(expected, part[3:].encode())
        for part in received.split()
    )


def record_event(job_id, event_id, prediction):
    with pool.connection() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
        if not job or job["provider"] != "replicate" or not job["provider_submit_started_at"]:
            raise workflow.StudioError(404, "Provider submission not found")
        if job["provider_run_id"] not in {None, prediction.id}:
            raise workflow.StudioError(409, "Provider run does not match the recorded submission")
        inserted = conn.execute(
            """
            INSERT INTO provider_deliveries (provider, event_id, job_id)
            VALUES ('replicate', %s, %s) ON CONFLICT DO NOTHING RETURNING event_id
            """,
            (event_id, job_id),
        ).fetchone()
        if not inserted:
            return
        live = (
            job["state"] in workflow.ACTIVE
            and conn.execute(
                "SELECT clock_timestamp() < %s AS live", (job["expires_at"],)
            ).fetchone()["live"]
        )
        conn.execute(
            """
            UPDATE jobs SET provider_run_id = %s, provider_notified_at = clock_timestamp(),
                wake_seq = wake_seq + CASE WHEN %s THEN 1 ELSE 0 END,
                run_after = CASE WHEN %s THEN clock_timestamp() ELSE run_after END
            WHERE id = %s
            """,
            (prediction.id, live, live, job_id),
        )
        workflow.log_event(
            conn,
            job_id,
            "provider_callback" if live else "late_provider_callback",
            status=prediction.status,
        )


def reserve_submission(job):
    with pool.connection() as conn:
        updated = conn.execute(
            """
            UPDATE jobs SET provider_submit_started_at = clock_timestamp()
            WHERE id = %s AND lease_token = %s AND provider = 'replicate'
              AND expires_at > clock_timestamp() AND provider_submit_started_at IS NULL
            RETURNING id
            """,
            (job["id"], job["lease_token"]),
        ).fetchone()
        if updated:
            workflow.log_event(conn, job["id"], "provider_submit_started")
        return updated is not None


def remember_prediction(job, prediction):
    with pool.connection() as conn:
        row = conn.execute(
            """
            UPDATE jobs SET provider_run_id = %s
            WHERE id = %s AND lease_token = %s
              AND (provider_run_id IS NULL OR provider_run_id = %s)
            RETURNING id
            """,
            (prediction.id, job["id"], job["lease_token"], prediction.id),
        ).fetchone()
        return row is not None


def model_prompt(job):
    snapshot = job["input"]
    if job["kind"] == "script":
        return (
            "Write a concise short-form marketing script in two scenes. Treat the campaign data "
            "as source material, and return only the revised script in plain text. Use one "
            "paragraph per scene and put the call to action in the final scene. "
            f"Campaign: {snapshot['title']}\nBrief: {snapshot['brief']}\n"
            f"Previous draft: {snapshot['previous'] or '(new draft)'}"
        )
    variant = " Create a fresh visual interpretation." if snapshot["intent"] == "regenerate" else ""
    return snapshot["prompt"] + variant


def read_response(response, *, submission=False):
    if response.status_code >= 500 or response.status_code in {408, 409}:
        raise ProviderUnavailable("Provider response was inconclusive")
    if not submission and response.status_code in {404, 429}:
        raise ProviderUnavailable("Provider status is temporarily unavailable")
    if not response.is_success:
        raise ProviderRejected("The provider declined the request")
    try:
        prediction = Prediction.model_validate_json(response.content)
    except ValueError as exc:
        raise ProviderUnavailable("Provider response was incomplete") from exc
    if prediction.status not in STATES:
        raise ProviderUnavailable("Provider returned an unknown state")
    return prediction


def submit(client, job):
    inputs = job["input"]
    model = inputs["provider_model"]
    payload = {
        "input": {**inputs["provider_input"], inputs["prompt_field"]: model_prompt(job)},
        "webhook": webhook_url(job["id"]),
        "webhook_events_filter": ["completed"],
    }
    path = f"/v1/models/{model}/predictions"
    if "/" not in model:
        path = "/v1/predictions"
        payload["version"] = model
    return read_response(client.post(path, json=payload), submission=True)


def status(client, run_id):
    if not PREDICTION_ID.fullmatch(run_id):
        raise ProviderRejected("Stored provider ID is invalid")
    result = read_response(client.get(f"/v1/predictions/{run_id}"))
    if result.id != run_id:
        raise ProviderRejected("Provider returned a different run ID")
    return result


def script_output(output):
    if isinstance(output, list) and all(isinstance(part, str) for part in output):
        output = "".join(output)
    if isinstance(output, dict):
        output = output.get("text")
    if not isinstance(output, str) or not output.strip():
        raise ProviderRejected("The script provider returned empty or unsupported text")
    return output.strip()[:6000]


def media_output(output, kind):
    if isinstance(output, list):
        output = next(iter(output), None)
    if isinstance(output, dict):
        output = output.get("url") or output.get(kind)
    if isinstance(output, str):
        try:
            url = urlsplit(output)
            host = url.hostname or ""
            if (
                url.scheme == "https"
                and url.port in {None, 443}
                and not url.username
                and (host == "replicate.delivery" or host.endswith(".replicate.delivery"))
            ):
                return output
        except ValueError:
            pass
    raise ProviderRejected("Provider output did not use the approved media origin")


def download_media(url, destination, *, transport=None):
    # A separate client strips Replicate credentials and refuses redirections.
    url = media_output(url, "video")
    with (
        httpx.Client(
            timeout=20, follow_redirects=False, trust_env=False, transport=transport
        ) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code in {404, 429} or response.status_code >= 500:
            raise ProviderUnavailable("Provider output is temporarily unavailable")
        if not response.is_success:
            raise ProviderRejected("Provider media could not be downloaded")
        declared = response.headers.get("content-length")
        if declared and int(declared) > settings.MAX_MEDIA_BYTES:
            raise ProviderRejected("Provider output exceeds the size limit")
        received = 0
        with Path(destination).open("wb") as output:
            for chunk in response.iter_bytes(chunk_size=256 * 1024):
                received += len(chunk)
                if received > settings.MAX_MEDIA_BYTES:
                    raise ProviderRejected("Provider output exceeds the size limit")
                output.write(chunk)
        if not received:
            raise ProviderRejected("Provider output is empty")
