import os
import unittest
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
from support import db_connection, key, workspace

from studio import settings
from studio.admin import execute

API = os.getenv("STUDIO_TEST_API", "http://127.0.0.1:8000")


@unittest.skipUnless(settings.AUTH_REQUIRED, "Authenticated Compose profile")
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.first = workspace()
        self.other = workspace()
        self.editor = key(self.first)
        self.viewer = key(self.first, "viewer")
        self.outsider = key(self.other)
        self.client = httpx.Client(base_url=API, timeout=10, follow_redirects=True)
        self.addCleanup(self.client.close)

    def bearer(self, issued):
        return {"Authorization": f"Bearer {issued['key']}"}

    def project(self, issued, title="Northline"):
        response = self.client.post(
            "/api/projects",
            json={"title": title, "brief": "Test brief"},
            headers=self.bearer(issued),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_scoped_keys_sessions_and_revocation(self):
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        first = self.project(self.editor)
        second = self.project(self.editor, "Second brief")
        other = self.project(self.outsider, "Other tenant")
        viewer_headers = self.bearer(self.viewer)
        response = self.client.get("/api/projects?limit=1", headers=viewer_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["projects"][0]["id"], second)
        cursor = response.json()["next_cursor"]
        next_page = self.client.get(f"/api/projects?before={cursor}", headers=viewer_headers)
        self.assertEqual([entry["id"] for entry in next_page.json()["projects"]], [first])
        self.assertEqual(
            self.client.get(f"/api/projects?before={other}", headers=viewer_headers).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/api/projects/{first}", headers=self.bearer(self.outsider)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/projects/{first}/revisions",
                json={"parent_id": None, "content": "Read only"},
                headers=viewer_headers,
            ).status_code,
            403,
        )
        draft = self.client.post(
            f"/api/projects/{first}/script-assist",
            json={"parent_id": None},
            headers={**self.bearer(self.editor), "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(draft.status_code, 202, draft.text)
        self.assertEqual(
            self.client.get(
                draft.headers["Location"], headers=self.bearer(self.outsider)
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/api/session", json={"access_key": self.editor["key"]}).status_code,
            403,
        )
        signed_in = self.client.post(
            "/api/session",
            json={"access_key": self.editor["key"]},
            headers={"Origin": settings.PUBLIC_ORIGIN},
        )
        self.assertEqual(signed_in.status_code, 204, signed_in.text)
        self.assertIn("httponly", signed_in.headers["set-cookie"].lower())
        self.assertIn("samesite=strict", signed_in.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/api/session").json()["role"], "editor")
        self.assertEqual(
            self.client.post(
                "/api/projects",
                json={"title": "Denied", "brief": "Test"},
                headers={"Origin": "https://other.example"},
            ).status_code,
            403,
        )
        with db_connection() as conn:
            execute(conn, Namespace(command="key-revoke", id=self.editor["id"]))
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.assertEqual(
            self.client.get("/api/projects", headers=self.bearer(self.editor)).status_code, 401
        )

    def test_workspace_limit_is_atomic_across_projects(self):
        slug = workspace(max_active_jobs=1)
        editor = key(slug)
        ids = [self.project(editor, name) for name in ("First campaign", "Second campaign")]

        def create_scenes(project_id):
            api = f"/api/projects/{project_id}"
            headers = self.bearer(editor)
            draft = self.client.post(
                f"{api}/revisions",
                headers=headers,
                json={"parent_id": None, "content": "Scene 1\n\nScene 2"},
            )
            self.assertEqual(draft.status_code, 201, draft.text)
            response = self.client.post(
                f"{api}/scenes",
                headers=headers,
                json={
                    "revision_id": draft.json()["id"],
                    "scenes": [
                        {
                            "title": "Still",
                            "prompt": "Product",
                            "kind": "image",
                            "duration_ms": 4000,
                        },
                        {
                            "title": "Video",
                            "prompt": "Motion",
                            "kind": "video",
                            "duration_ms": 6000,
                        },
                    ],
                },
            )
            self.assertEqual(response.status_code, 204, response.text)
            return self.client.get(api, headers=headers).json()["scenes"][1]["id"]

        video_ids = list(map(create_scenes, ids))

        def submit(scene):
            return self.client.post(
                f"/api/scenes/{scene}/generations",
                headers={**self.bearer(editor), "Idempotency-Key": str(uuid4())},
                json={"intent": "generate", "scenario": "late", "quoted_credits": 12},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(submit, video_ids))
        self.assertEqual(sorted(r.status_code for r in responses), [202, 429])
        credits = self.client.get("/api/projects", headers=self.bearer(editor)).json()["credits"]
        self.assertEqual(credits, {"balance": 100, "held": 12, "available": 88})
