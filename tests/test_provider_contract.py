import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from starlette.datastructures import Headers

from studio import replicate, settings, worker


class ProviderContractTests(unittest.TestCase):
    def sample(self):
        return {
            "id": uuid4(),
            "kind": "media",
            "state": "running",
            "poll_count": 0,
            "provider": "replicate",
            "provider_run_id": None,
            "provider_submit_started_at": None,
            "input": {
                "provider_model": "studio/approved-model",
                "prompt_field": "prompt",
                "provider_input": {"duration": 8},
                "kind": "video",
                "prompt": "Product reel",
                "intent": "generate",
            },
        }

    def test_unknown_submission_is_never_reposted_and_later_run_is_polled(self):
        job = self.sample()
        methods = []

        def upstream(request):
            methods.append(request.method)
            if request.method == "POST":
                self.assertEqual(request.url.path, "/v1/models/studio/approved-model/predictions")
                payload = json.loads(request.content)
                self.assertEqual(payload["input"], {"prompt": "Product reel", "duration": 8})
                self.assertEqual(payload["webhook"], replicate.webhook_url(job["id"]))
                raise httpx.ReadTimeout("uncertain acknowledgement", request=request)
            self.assertEqual(request.url.path, "/v1/predictions/known-run")
            return httpx.Response(200, json={"id": "known-run", "status": "processing"})

        with (
            httpx.Client(base_url=replicate.API, transport=httpx.MockTransport(upstream)) as client,
            patch("studio.replicate.reserve_submission", return_value=True) as reserve,
            patch("studio.worker.reschedule") as reschedule,
        ):
            with self.assertRaises(httpx.ReadTimeout):
                worker.run_replicate(client, job)
            worker.run_replicate(client, {**job, "provider_submit_started_at": True})
            reschedule.assert_called_once()
            self.assertEqual(reschedule.call_args.args[1], "confirming")
            worker.run_replicate(client, {**job, "provider_run_id": "known-run"})
            self.assertEqual(reschedule.call_args.args[1], "running")
        reserve.assert_called_once()
        self.assertEqual(methods, ["POST", "GET"])

    def test_signed_events_bind_to_job_and_expire(self):
        job_id = uuid4()
        body = b'{"id":"known-run","status":"succeeded"}'
        secret = b"controlled-provider-test-key"
        timestamp = 90000
        signature = base64.b64encode(
            hmac.digest(secret, f"fixture-event.{timestamp}.".encode() + body, hashlib.sha256)
        ).decode()
        headers = Headers(
            {
                "webhook-id": "fixture-event",
                "webhook-timestamp": str(timestamp),
                "webhook-signature": "v2,unused v1," + signature,
            }
        )
        grant = replicate.webhook_url(job_id).rsplit("/", 1)[1]
        with patch.object(settings, "webhook_key", return_value=secret):
            self.assertTrue(
                replicate.authenticate_webhook(job_id, grant, headers, body, now=timestamp)
            )
            self.assertFalse(
                replicate.authenticate_webhook(uuid4(), grant, headers, body, now=timestamp)
            )
            self.assertFalse(
                replicate.authenticate_webhook(job_id, grant, headers, body, now=timestamp + 301)
            )
            self.assertFalse(
                replicate.authenticate_webhook(job_id, grant, headers, body + b" ", now=timestamp)
            )

    def test_media_download_uses_its_own_client_and_limits_size(self):
        url = "https://fixtures.replicate.delivery/test.mp4"

        def upstream(request):
            self.assertEqual(str(request.url), url)
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=b"verified fixture")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider-result"
            transport = httpx.MockTransport(upstream)
            replicate.download_media(url, target, transport=transport)
            self.assertEqual(target.read_bytes(), b"verified fixture")
            with (
                patch.object(settings, "MAX_MEDIA_BYTES", 4),
                self.assertRaises(replicate.ProviderRejected),
            ):
                replicate.download_media(url, target, transport=transport)

    def test_production_configuration_refuses_demo_provider(self):
        with (
            patch.object(settings, "PRODUCTION", True),
            patch.object(settings, "PUBLIC_ORIGIN", "https://studio.example"),
            patch.object(settings, "SIGNING_KEY", b"x" * 32),
            patch.object(settings, "MEDIA_BACKEND", "s3"),
            patch.object(settings, "S3_BUCKET", "private-test-bucket"),
            patch.object(settings, "S3_ENDPOINT", None),
            patch.object(settings, "PROVIDER", "fixture"),
            self.assertRaisesRegex(RuntimeError, "requires S3 and the Replicate adapter"),
        ):
            settings.validate("api")
