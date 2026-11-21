"""Checks against a disposable Compose stack with the real API, database and worker."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from support import db_connection, key, workspace

from studio import settings, workflow
from studio.media import signature

API = os.getenv("STUDIO_TEST_API", "http://127.0.0.1:8000")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workspace_id = workspace() if settings.AUTH_REQUIRED else "demo"
        headers = (
            {"Authorization": f"Bearer {key(self.workspace_id)['key']}"}
            if settings.AUTH_REQUIRED
            else {}
        )
        self.client = httpx.Client(base_url=API, timeout=10, headers=headers, follow_redirects=True)
        self.addCleanup(self.client.close)

    def assert_balanced(self):
        with db_connection() as conn:
            self.assertTrue(workflow.audit_credits(conn, self.workspace_id)["consistent"])

    def request(self, method, path, *, expected=200, **kwargs):
        response = self.client.request(method, path, **kwargs)
        self.assertEqual(response.status_code, expected, response.text[:1000])
        return response

    def wait_for(self, check, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(0.10)
        self.fail("Timed out waiting for the expected transition")

    def get_job(self, job_id):
        return self.request("GET", f"/api/jobs/{job_id}").json()

    def wait_state(self, job_id, wanted):
        end_states = {"succeeded", "failed", "expired"}

        def matching():
            job = self.get_job(job_id)
            if job["state"] == wanted:
                return job
            if job["state"] in end_states:
                self.fail(f"Expected {wanted}, got {job}")
            return None

        return self.wait_for(matching)

    def queue(self, path, payload, key=None, expected=202):
        response = self.request(
            "POST",
            path,
            json=payload,
            expected=expected,
            headers={"Idempotency-Key": key or str(uuid4())},
        )
        if expected == 202:
            self.assertEqual(response.headers["location"], response.json()["status_url"])
        return response.json()

    def project(self, project_id):
        return self.request("GET", f"/api/projects/{project_id}").json()

    def seed(self):
        project_id = self.request(
            "POST",
            "/api/projects",
            expected=201,
            json={
                "title": "Northline workflow check",
                "brief": "Use motion to introduce cold brew.",
            },
        ).json()["id"]
        base = f"/api/projects/{project_id}"
        job = self.queue(f"{base}/script-assist", {"parent_id": None})
        script = self.wait_state(job["id"], "succeeded")["result"]["revision_id"]
        refined = self.request(
            "POST",
            f"{base}/revisions",
            expected=201,
            json={
                "parent_id": script,
                "content": "Product close-up\n\nPour and invite the viewer to try it.",
            },
        ).json()["id"]
        self.request(
            "POST",
            f"{base}/scenes",
            expected=204,
            json={
                "revision_id": refined,
                "scenes": [
                    {
                        "title": "Product",
                        "prompt": "Warm still",
                        "kind": "image",
                        "duration_ms": 4000,
                    },
                    {
                        "title": "Pour",
                        "prompt": "Show motion",
                        "kind": "video",
                        "duration_ms": 6000,
                    },
                ],
            },
        )
        project = self.project(project_id)
        self.assertEqual(project["revisions"][0]["parent_id"], script)
        self.assertEqual({s["script_revision_id"] for s in project["scenes"]}, {refined})
        return project_id, *(scene["id"] for scene in project["scenes"])

    def accept(self, scene_id, asset_id, expected=204):
        self.request("PUT", f"/api/scenes/{scene_id}/accepted/{asset_id}", expected=expected)

    def timeline(self, project_id, image, video, video_first, video_ms):
        scenes = [
            {"scene_id": image, "trim_ms": 0, "duration_ms": 4000},
            {"scene_id": video, "trim_ms": 1000, "duration_ms": video_ms},
        ]
        if video_first:
            scenes.reverse()
        version = self.project(project_id)["project"]["timeline_version"]
        self.request(
            "PUT",
            f"/api/projects/{project_id}/timeline",
            expected=204,
            json={
                "version": version,
                "scenes": scenes,
            },
        )

    def export(self, project_id):
        version = self.project(project_id)["project"]["timeline_version"]
        return self.queue(f"/api/projects/{project_id}/exports", {"version": version})["id"]

    def callback(self, run_id, valid=True):
        raw = json.dumps({"run_id": run_id, "status": "succeeded"}).encode()
        response = self.request(
            "POST",
            "/internal/provider-events",
            expected=204 if valid else 401,
            content=raw,
            headers={"X-Provider-Signature": signature(b"provider:" + raw) if valid else "invalid"},
        )
        self.assertEqual(
            response.content, b"" if valid else b'{"detail":"Invalid provider signature"}'
        )

    def sql(self, query, parameters=()):
        with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
            return conn.execute(query, parameters).fetchall()

    def test_timeout_retries_charge_once_and_exports_keep_their_snapshot(self):
        project_id, image, video = self.seed()
        initial = self.project(project_id)["credits"]
        image_path = f"/api/scenes/{image}/generations"
        video_path = f"/api/scenes/{video}/generations"
        still_request = {"intent": "generate", "quoted_credits": 4}
        action_key = str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as executor:
            duplicate_jobs = list(
                executor.map(
                    lambda _: self.queue(image_path, still_request, action_key)["id"], range(2)
                )
            )
        self.assertEqual(len(set(duplicate_jobs)), 1)
        self.queue(image_path, {**still_request, "quoted_credits": 5}, action_key, expected=409)
        still = self.wait_state(duplicate_jobs[0], "succeeded")["asset"]["id"]
        self.accept(image, still)

        late = self.queue(
            video_path,
            {
                "intent": "generate",
                "scenario": "late",
                "quoted_credits": 12,
            },
        )["id"]
        self.wait_state(late, "confirming")
        path = f"{video_path}/{late}/retry"
        with ThreadPoolExecutor(max_workers=2) as executor:
            retry_ids = list(
                executor.map(
                    lambda _: self.request("POST", path, expected=202).json()["id"], range(2)
                )
            )
        self.assertEqual(retry_ids, [late, late])
        # Model a dead worker's expired lease and postpone polling after the reclaim.
        abandoned = str(uuid4())
        self.wait_for(
            lambda: self.sql(
                """UPDATE jobs SET lease_token = %s, lease_until = clock_timestamp() - interval '1 second',
                              run_after = clock_timestamp()
               WHERE id = %s AND state = 'confirming' AND lease_token IS NULL RETURNING id""",
                (abandoned, late),
            )
        )
        self.wait_for(
            lambda: any(
                event["event"] == "lease_recovered" for event in self.get_job(late)["events"]
            )
        )
        self.sql(
            """UPDATE jobs SET run_after = clock_timestamp() + interval '20 seconds'
               WHERE id = %s AND state = 'confirming' RETURNING id""",
            (late,),
        )
        recovered = self.wait_state(late, "succeeded")
        original = recovered["asset"]["id"]
        self.assertEqual(recovered["credit"], {"amount": 12, "state": "captured"})
        self.assertIn("provider_callback", [event["event"] for event in recovered["events"]])
        self.assertEqual(
            self.sql(
                "SELECT input->>'project_title' AS title FROM provider_runs WHERE id = %s", (late,)
            ),
            [{"title": "Northline workflow check"}],
        )
        self.assertEqual(self.project(project_id)["scenes"][0]["accepted"]["id"], still)
        self.accept(video, original)

        regenerate = self.queue(video_path, {"intent": "regenerate", "quoted_credits": 12})["id"]
        variant = self.wait_state(regenerate, "succeeded")["asset"]["id"]
        self.callback(late, valid=False)
        self.callback(late)
        self.callback(late)
        scene = self.project(project_id)["scenes"][1]
        self.assertEqual((scene["candidate"]["id"], scene["accepted"]["id"]), (variant, original))
        self.assertEqual(
            sum(e["event"] == "credits_captured" for e in self.get_job(late)["events"]), 1
        )
        credits = self.project(project_id)["credits"]
        self.assertEqual(credits["balance"], initial["balance"] - 28)
        self.assertEqual(credits["held"], initial["held"])

        self.timeline(project_id, image, video, video_first=True, video_ms=6000)
        first_export = self.export(project_id)
        self.accept(video, variant)
        self.timeline(project_id, image, video, video_first=False, video_ms=5000)
        second_export = self.export(project_id)
        first = self.wait_state(first_export, "succeeded")
        second = self.wait_state(second_export, "succeeded")
        project = self.project(project_id)
        exports = {job["id"]: job for job in project["jobs"] if job["kind"] == "export"}
        self.assertLess(
            exports[first_export]["timeline_version"], project["project"]["timeline_version"]
        )
        self.assertEqual(
            exports[second_export]["timeline_version"], project["project"]["timeline_version"]
        )
        self.assertEqual([s["asset_id"] for s in first["manifest"]["scenes"]], [original, still])
        self.assertEqual([s["asset_id"] for s in second["manifest"]["scenes"]], [still, variant])
        first_frame = self.verify_video(first, expected_duration=10)
        second_frame = self.verify_video(second, expected_duration=9)
        self.assertLess(first_frame[0], second_frame[0])
        self.request(
            "GET", first["asset"]["url"].split("?")[0] + "?expires=1&signature=x", expected=403
        )
        self.request("GET", first["asset"]["url"], headers={"Range": "bytes=0-15"}, expected=206)
        self.request("GET", first["asset"]["content_url"], expected=200)
        self.assert_balanced()

    def verify_video(self, job, expected_duration):
        media = self.request("GET", job["asset"]["url"]).content
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / "export.mp4"
            filename.write_bytes(media)
            probe = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_streams",
                        "-show_format",
                        "-of",
                        "json",
                        filename,
                    ]
                )
            )
            self.assertAlmostEqual(
                float(probe["format"]["duration"]), expected_duration, delta=1 / 30
            )
            stream = probe["streams"][0]
            self.assertEqual(
                (stream["codec_name"], stream["width"], stream["height"]), ("h264", 540, 960)
            )
            return subprocess.check_output(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    filename,
                    "-vf",
                    "crop=2:2:10:10",
                    "-frames:v",
                    "1",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "rawvideo",
                    "-",
                ]
            )

    def test_expiry_releases_hold_and_later_success_cannot_replace_accepted_media(self):
        project_id, _, video = self.seed()
        path = f"/api/scenes/{video}/generations"
        original = self.queue(path, {"intent": "generate", "quoted_credits": 12})["id"]
        accepted = self.wait_state(original, "succeeded")["asset"]["id"]
        self.accept(video, accepted)
        credits = self.project(project_id)["credits"]
        delayed = self.queue(
            path,
            {
                "intent": "regenerate",
                "scenario": "expire",
                "quoted_credits": 12,
            },
        )["id"]
        self.wait_state(delayed, "confirming")
        self.assertEqual(self.project(project_id)["credits"]["held"], credits["held"] + 12)
        self.sql(
            "UPDATE jobs SET expires_at = clock_timestamp() WHERE id = %s RETURNING id", (delayed,)
        )
        self.wait_state(delayed, "expired")
        self.callback(delayed)
        self.assertEqual(self.get_job(delayed)["credit"], {"amount": 12, "state": "released"})
        self.assertIsNone(self.get_job(delayed)["asset"])
        scene = self.project(project_id)["scenes"][1]
        self.assertEqual(scene["accepted"]["id"], accepted)
        self.assertEqual(scene["candidate"]["id"], accepted)
        self.assertEqual(self.project(project_id)["credits"], credits)
        self.assert_balanced()


if __name__ == "__main__":
    unittest.main()
