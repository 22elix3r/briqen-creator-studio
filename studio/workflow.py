from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from studio import settings

ACTIVE = ("queued", "running", "confirming")
WORKSPACE = "demo"


class StudioError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def project_row(conn, project_id, *, lock=False):
    query = "SELECT * FROM projects WHERE id = %s AND workspace_id = %s"
    row = conn.execute(query + (" FOR UPDATE" if lock else ""), (project_id, WORKSPACE)).fetchone()
    if row is None:
        raise StudioError(404, "Project not found")
    return row


def scene_row(conn, scene_id, *, lock=False):
    query = """
        SELECT s.* FROM scenes s JOIN projects p ON p.id = s.project_id
        WHERE s.id = %s AND p.workspace_id = %s
    """
    row = conn.execute(query + (" FOR UPDATE OF s" if lock else ""), (scene_id, WORKSPACE)).fetchone()
    if row is None:
        raise StudioError(404, "Scene not found")
    return row


def credit_summary(conn):
    return conn.execute(
        """
        SELECT credit_balance AS balance, held_credits AS held,
               credit_balance - held_credits AS available
        FROM workspaces WHERE id = %s
        """,
        (WORKSPACE,),
    ).fetchone()


def log_event(conn, job_id, event, **detail):
    conn.execute(
        "INSERT INTO job_events (job_id, event, detail) VALUES (%s, %s, %s)",
        (job_id, event, Jsonb(detail)),
    )


def save_revision(conn, project, content, parent_id, source):
    revision = conn.execute(
        """
        INSERT INTO script_revisions (id, project_id, parent_id, content, source)
        VALUES (%s, %s, %s, %s, %s) RETURNING *
        """,
        (uuid4(), project["id"], parent_id, content, source),
    ).fetchone()
    if project["current_revision_id"] == parent_id:
        conn.execute(
            "UPDATE projects SET current_revision_id = %s WHERE id = %s",
            (revision["id"], project["id"]),
        )
    return revision


def split_scenes(conn, project, revision_id, specs):
    if project["current_revision_id"] != revision_id:
        raise StudioError(409, "Split the current script revision")
    if conn.execute("SELECT 1 FROM scenes WHERE project_id = %s", (project["id"],)).fetchone():
        raise StudioError(409, "This project already has a scene list")
    for index, spec in enumerate(specs):
        conn.execute(
            """
            INSERT INTO scenes
                (id, project_id, script_revision_id, title, prompt, kind, position, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid4(), project["id"], revision_id, spec.title, spec.prompt, spec.kind,
             index, spec.duration_ms),
        )
    bump_timeline(conn, project["id"])


def bump_timeline(conn, project_id):
    conn.execute(
        "UPDATE projects SET timeline_version = timeline_version + 1 WHERE id = %s",
        (project_id,),
    )


def validate_clip(asset, trim_ms, duration_ms):
    if asset["kind"] == "video" and trim_ms + duration_ms > asset["duration_ms"]:
        raise StudioError(422, "The trim and duration exceed the accepted video length")


def update_timeline(conn, project, edits, expected_version):
    if project["timeline_version"] != expected_version:
        raise StudioError(409, "Timeline changed. Refresh and apply your edits again")
    scenes = conn.execute(
        """
        SELECT s.*, a.duration_ms AS asset_duration_ms
        FROM scenes s LEFT JOIN assets a ON a.id = s.accepted_asset_id
        WHERE s.project_id = %s
        """,
        (project["id"],),
    ).fetchall()
    by_id = {scene["id"]: scene for scene in scenes}
    if len(edits) != len(scenes) or {edit.scene_id for edit in edits} != by_id.keys():
        raise StudioError(422, "Include each scene in this project exactly once")
    for position, edit in enumerate(edits):
        scene = by_id[edit.scene_id]
        if scene["kind"] == "image" and edit.trim_ms:
            raise StudioError(422, "Still images have a zero trim offset")
        if scene["asset_duration_ms"] is not None:
            validate_clip(
                {"kind": scene["kind"], "duration_ms": scene["asset_duration_ms"]},
                edit.trim_ms, edit.duration_ms,
            )
        conn.execute(
            "UPDATE scenes SET position = %s, trim_ms = %s, duration_ms = %s WHERE id = %s",
            (position, edit.trim_ms, edit.duration_ms, edit.scene_id),
        )
    bump_timeline(conn, project["id"])


def accept_asset(conn, scene, asset_id):
    if scene["accepted_asset_id"] == asset_id:
        return
    if scene["candidate_asset_id"] != asset_id:
        raise StudioError(409, "This candidate has been replaced. Refresh to see the current result")
    asset = conn.execute("SELECT * FROM assets WHERE id = %s", (asset_id,)).fetchone()
    validate_clip(asset, scene["trim_ms"], scene["duration_ms"])
    conn.execute("UPDATE scenes SET accepted_asset_id = %s WHERE id = %s", (asset_id, scene["id"]))
    bump_timeline(conn, scene["project_id"])


def existing_action(conn, action_key, project_id, kind, scene_id, request):
    job = conn.execute(
        "SELECT * FROM jobs WHERE workspace_id = %s AND action_key = %s",
        (WORKSPACE, action_key),
    ).fetchone()
    if job and (job["project_id"], job["kind"], job["scene_id"], job["request"]) != (
        project_id, kind, scene_id, request
    ):
        raise StudioError(409, "That idempotency key belongs to a different action")
    return job


def insert_job(conn, project_id, kind, action_key, request, snapshot, *, scene_id=None):
    deadline = settings.MEDIA_DEADLINE_SECONDS if kind == "media" else 180
    row = conn.execute(
        """
        INSERT INTO jobs (id, workspace_id, project_id, kind, scene_id, action_key,
                          request, input, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp() + %s * interval '1 second')
        ON CONFLICT (workspace_id, action_key) DO NOTHING
        RETURNING *
        """,
        (uuid4(), WORKSPACE, project_id, kind, scene_id, action_key,
         Jsonb(request), Jsonb(snapshot), deadline),
    ).fetchone()
    if row:
        log_event(conn, row["id"], "queued")
        return row, True
    return existing_action(conn, action_key, project_id, kind, scene_id, request), False


def start_media(conn, scene, action_key, request):
    project_id, scene_id = scene["project_id"], scene["id"]
    previous = existing_action(conn, action_key, project_id, "media", scene_id, request)
    if previous:
        return previous
    if conn.execute(
        "SELECT 1 FROM jobs WHERE scene_id = %s AND state = ANY(%s)",
        (scene_id, list(ACTIVE)),
    ).fetchone():
        raise StudioError(409, "A generation is already running for this scene")
    if request["intent"] == "regenerate" and not scene["candidate_asset_id"]:
        raise StudioError(409, "Generate an initial candidate before requesting a variant")
    if request["intent"] == "generate" and scene["candidate_asset_id"]:
        raise StudioError(409, "Use Regenerate to request another paid variant")
    if request["scenario"] != "normal" and scene["kind"] != "video":
        raise StudioError(422, "The timeout scenarios apply to video")
    cost = settings.COSTS[scene["kind"]]
    if request["quoted_credits"] != cost:
        raise StudioError(409, f"Generation costs {cost} credits. Confirm the new quote")
    job, created = insert_job(
        conn, project_id, "media", action_key, request,
        {"kind": scene["kind"], "prompt": scene["prompt"], "title": scene["title"],
         "visual_version": scene["visual_version"], "scenario": request["scenario"],
         "intent": request["intent"]},
        scene_id=scene_id,
    )
    if not created:
        return job
    held = conn.execute(
        """
        UPDATE workspaces SET held_credits = held_credits + %s
        WHERE id = %s AND credit_balance - held_credits >= %s RETURNING id
        """,
        (cost, WORKSPACE, cost),
    ).fetchone()
    if held is None:
        raise StudioError(409, "Insufficient available credits")
    conn.execute(
        "INSERT INTO credit_reservations (job_id, workspace_id, amount) VALUES (%s, %s, %s)",
        (job["id"], WORKSPACE, cost),
    )
    conn.execute("UPDATE scenes SET latest_job_id = %s WHERE id = %s", (job["id"], scene_id))
    log_event(conn, job["id"], "credits_held", amount=cost)
    return job


def retry_media(conn, scene, job_id):
    job = conn.execute(
        """
        SELECT *, expires_at > clock_timestamp() AS before_deadline FROM jobs
        WHERE id = %s AND scene_id = %s FOR UPDATE
        """,
        (job_id, scene["id"]),
    ).fetchone()
    if job is None:
        raise StudioError(404, "Job not found for this scene")
    if job["id"] == scene["latest_job_id"] and job["state"] == "confirming" and job["before_deadline"]:
        conn.execute(
            """
            UPDATE jobs SET run_after = least(run_after, clock_timestamp()), wake_seq = wake_seq + 1
            WHERE id = %s
            """,
            (job_id,),
        )
        log_event(conn, job_id, "retry_requested")
    return job


def settle_credits(conn, job, captured):
    state = "captured" if captured else "released"
    reservation = conn.execute(
        """
        UPDATE credit_reservations SET state = %s, updated_at = clock_timestamp()
        WHERE job_id = %s AND state = 'held'
        RETURNING workspace_id, amount
        """,
        (state, job["id"]),
    ).fetchone()
    if reservation is None:
        raise RuntimeError("A live media job must have exactly one outstanding credit hold")
    amount = reservation["amount"]
    conn.execute(
        """
        UPDATE workspaces SET held_credits = held_credits - %s, credit_balance = credit_balance - %s
        WHERE id = %s
        """,
        (amount, amount if captured else 0, reservation["workspace_id"]),
    )
    log_event(conn, job["id"], "credits_" + state, amount=amount)


def export_snapshot(conn, project):
    scenes = conn.execute(
        """
        SELECT s.id AS scene_id, s.trim_ms, s.duration_ms,
               a.id AS asset_id, a.kind, a.storage_key, a.duration_ms AS source_duration
        FROM scenes s LEFT JOIN assets a ON a.id = s.accepted_asset_id
        WHERE s.project_id = %s ORDER BY s.position
        """,
        (project["id"],),
    ).fetchall()
    if not scenes or any(scene["asset_id"] is None for scene in scenes):
        raise StudioError(409, "Accept media for every scene before exporting")
    manifest = []
    for scene in scenes:
        validate_clip(
            {"kind": scene["kind"], "duration_ms": scene["source_duration"]},
            scene["trim_ms"], scene["duration_ms"],
        )
        manifest.append({
            key: str(value) if isinstance(value, UUID) else value
            for key, value in scene.items() if key != "source_duration"
        })
    return {
        "scenes": manifest,
        "timeline_version": project["timeline_version"],
        "width": settings.WIDTH, "height": settings.HEIGHT, "fps": settings.FPS,
    }
