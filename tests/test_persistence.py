import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from support import db_connection, workspace

from studio import replicate, settings, workflow
from studio.db import pool
from studio.media import fetch_file, s3_client, s3_key, store_file


class ProviderPersistenceTests(unittest.TestCase):
    def test_submission_intent_and_callback_binding_survive_a_restart(self):
        tenant, project, job_id, lease = workspace(), uuid4(), uuid4(), uuid4()
        with db_connection() as conn:
            conn.execute(
                "INSERT INTO projects (id, workspace_id, title, brief) VALUES (%s, %s, 'Adapter test', 'Test')",
                (project, tenant),
            )
            conn.execute(
                """
                INSERT INTO jobs (id, workspace_id, project_id, kind, state, action_key, request, input,
                                  provider, expires_at, run_after, lease_token, lease_until)
                VALUES (%s, %s, %s, 'script', 'running', %s, '{}', '{}', 'replicate',
                        clock_timestamp() + interval '10 minutes', clock_timestamp() + interval '10 minutes',
                        %s, clock_timestamp() + interval '15 minutes')
                """,
                (job_id, tenant, project, uuid4(), lease),
            )
        claim = {"id": job_id, "lease_token": lease}
        prediction = replicate.Prediction(id="verified-" + job_id.hex, status="succeeded")
        event = "delivery-" + job_id.hex

        with pool:
            self.assertTrue(replicate.reserve_submission(claim))
            self.assertFalse(replicate.reserve_submission(claim))
            replicate.record_event(job_id, event, prediction)
            replicate.record_event(job_id, event, prediction)
            with self.assertRaisesRegex(workflow.StudioError, "does not match"):
                replicate.record_event(
                    job_id,
                    event + "-different",
                    replicate.Prediction(
                        id="wrong-" + job_id.hex,
                        status="succeeded",
                    ),
                )
            with db_connection() as conn:
                row = conn.execute(
                    "SELECT provider_run_id, provider_submit_started_at, wake_seq FROM jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()
                self.assertEqual(row["provider_run_id"], prediction.id)
                self.assertIsNotNone(row["provider_submit_started_at"])
                self.assertEqual(row["wake_seq"], 1)
                conn.execute(
                    "UPDATE jobs SET state = 'expired', lease_token = NULL, lease_until = NULL WHERE id = %s",
                    (job_id,),
                )
            replicate.record_event(job_id, event + "-late", prediction)
        with db_connection() as conn:
            row = conn.execute(
                "SELECT state, wake_seq, result FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
            self.assertEqual(row, {"state": "expired", "wake_seq": 1, "result": None})


@unittest.skipUnless(settings.MEDIA_BACKEND == "s3", "S3-compatible Compose profile")
class StorageTests(unittest.TestCase):
    def test_objects_are_private_and_immutable(self):
        key = f"{uuid4()}.png"
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "source.png"
            source.write_bytes(b"first complete object")
            store_file(source, key)
            source.write_bytes(b"another upload")
            store_file(source, key)
            self.assertEqual(fetch_file(key, directory).read_bytes(), b"first complete object")
        request_url = f"{settings.S3_ENDPOINT}/{settings.S3_BUCKET}/{s3_key(key)}"
        self.assertEqual(httpx.get(request_url, timeout=5).status_code, 403)
        url = s3_client().generate_presigned_url(
            "get_object", Params={"Bucket": settings.S3_BUCKET, "Key": s3_key(key)}
        )
        self.assertEqual(httpx.get(url, timeout=5).content, b"first complete object")
