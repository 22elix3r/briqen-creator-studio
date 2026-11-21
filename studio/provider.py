"""Deterministic provider with a separate HTTP boundary and durable request keys."""

import asyncio
import json
import logging
import math
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont, ImageOps
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from studio import settings
from studio.db import pool
from studio.media import ffmpeg, media_path, publish_file, signature

logger = logging.getLogger("creator-studio.provider")


class GenerationInput(BaseModel):
    kind: Literal["image", "video"]
    prompt: str
    title: str
    visual_version: int
    scenario: Literal["normal", "late", "expire"]
    intent: Literal["generate", "regenerate"]


class ScriptInput(BaseModel):
    title: str
    brief: str
    previous: str | None = None


def pending_callbacks():
    with pool.connection() as conn:
        return conn.execute(
            """
            SELECT id FROM provider_runs
            WHERE callback_sent_at IS NULL AND available_at <= clock_timestamp()
            ORDER BY available_at LIMIT 20
            """
        ).fetchall()


def mark_delivered(run_id):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE provider_runs SET callback_sent_at = clock_timestamp() WHERE id = %s",
            (run_id,),
        )


async def send_callbacks():
    async with httpx.AsyncClient(timeout=4) as client:
        while True:
            for run in await run_in_threadpool(pending_callbacks):
                raw = json.dumps({"run_id": str(run["id"]), "status": "succeeded"}).encode()
                try:
                    response = await client.post(
                        settings.CALLBACK_URL,
                        content=raw,
                        headers={
                            "Content-Type": "application/json",
                            "X-Provider-Signature": signature(b"provider:" + raw),
                        },
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    logger.warning("Could not deliver callback for %s: %s", run["id"], exc)
                    continue
                await run_in_threadpool(mark_delivered, run["id"])
            await asyncio.sleep(0.25)


@asynccontextmanager
async def lifespan(_app):
    with pool:
        pool.wait()
        task = asyncio.create_task(send_callbacks())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Studio demo provider", lifespan=lifespan)


@app.post("/script")
def draft_script(body: ScriptInput):
    if body.previous:
        first_scene = body.previous.split("\n\n", maxsplit=1)[0][:5500]
        return {
            "content": (
                f"{first_scene}\n\nPOUR / 6 seconds\nLead with a tight motion shot. "
                "Show the product in use, then end with one clear call to action."
            )
        }
    return {
        "content": (
            f"PACKSHOT / 4 seconds\nFrame {body.title} against a quiet background. "
            f"Use the brief as the visual direction: {body.brief[:600]}\n\n"
            "POUR / 6 seconds\nCut to a close-up of the pour. "
            "Stay on the motion for a beat. End on the line: Try a better afternoon brew."
        )
    }


def run_view(run):
    return {"run_id": run["id"], "state": "succeeded" if run["ready"] else "running"}


@app.put("/runs/{run_id}")
def submit_run(run_id: UUID, body: GenerationInput):
    payload = body.model_dump(mode="json")
    delay = {
        "normal": 0.65,
        "late": settings.LATE_SECONDS,
        "expire": settings.MEDIA_DEADLINE_SECONDS + 5,
    }[body.scenario]
    with pool.connection() as conn:
        inserted = conn.execute(
            """
            INSERT INTO provider_runs (id, input, available_at)
            VALUES (%s, %s, clock_timestamp() + %s * interval '1 second')
            ON CONFLICT DO NOTHING RETURNING id
            """,
            (run_id, Jsonb(payload), delay),
        ).fetchone()
        row = lookup(conn, run_id)
        if row["input"] != payload:
            raise HTTPException(409, "Request key already belongs to different inputs")
    if inserted and body.scenario in {"late", "expire"}:
        # The provider commits the run, then the initial acknowledgement times out.
        return JSONResponse({"detail": "Submission acknowledgement timed out"}, status_code=504)
    return run_view(row)


def lookup(conn, run_id):
    run = conn.execute(
        "SELECT *, available_at <= clock_timestamp() AS ready FROM provider_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/runs/{run_id}")
def get_run(run_id: UUID):
    with pool.connection() as conn:
        return run_view(lookup(conn, run_id))


def draw_frame(path, inputs):
    width, height = settings.WIDTH, settings.HEIGHT
    variant = inputs["intent"] == "regenerate"
    video = inputs["kind"] == "video"
    demo_scene = "pour video" if video else "product still"
    if (
        inputs.get("project_title", "").casefold() == "northline cold brew"
        and inputs["title"].casefold() == demo_scene
    ):
        name = "pour" if video else "still"
        with Image.open(Path(__file__).with_name("demo_media") / f"northline-{name}.jpg") as photo:
            frame = ImageOps.fit(
                photo, (width, height), Image.Resampling.LANCZOS, bleed=0.07 if variant else 0
            )
            frame.save(path)
        return
    top = (51, 58, 53) if variant else (56, 67, 56) if video else (93, 52, 33)
    bottom = (14, 25, 23) if video else (39, 22, 17)
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / height
        draw.line(
            (0, y, width, y),
            tuple(round(start + (end - start) * ratio) for start, end in zip(top, bottom)),
        )
    for offset in range(0, 280, 28):
        radius = 420 - offset
        draw.ellipse(
            (270 - radius, 480 - radius, 270 + radius, 480 + radius), outline="#938676", width=1
        )
    draw.ellipse((123, 695, 420, 752), fill="#201b16")
    draw.rounded_rectangle(
        (173, 335, 369, 716), radius=47, fill="#583524", outline="#ccad88", width=3
    )
    draw.rounded_rectangle((211, 286, 331, 355), radius=15, fill="#d2ad79")
    draw.rectangle((173, 455, 369, 625), fill="#e6ceb1")
    for x in range(188, 354, 8):
        y = 560 + int(22 * math.sin(x / 35))
        draw.line((x, y, x, y + 2), fill="#503629", width=2)
    label = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
    draw.text((271, 499), "COLD BREW", anchor="mm", fill="#503629", font=label)
    title = ("ALT • " if variant else "") + inputs["title"]
    draw.text((32, 61), title[:31], fill="#f7e5cf", font=label)
    image.save(path)


@app.get("/runs/{run_id}/media")
def result_media(run_id: UUID):
    with pool.connection() as conn:
        row = lookup(conn, run_id)
    if not row["ready"]:
        raise HTTPException(409, "Media is still rendering")
    inputs = row["input"]
    video = inputs["kind"] == "video"
    key = f"{run_id}.{'mp4' if video else 'png'}"
    if not media_path(key).is_file():
        settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.MEDIA_DIR) as directory:
            frame = Path(directory) / "frame.png"
            draw_frame(frame, inputs)
            if video:
                clip = Path(directory) / "clip.mp4"
                ffmpeg(
                    "-loop",
                    "1",
                    "-i",
                    frame,
                    "-vf",
                    "zoompan=z='1+0.00075*on':x='iw/2-iw/zoom/2':"
                    "y='ih/2-ih/zoom/2':d=1:s=540x960:fps=30",
                    "-t",
                    "8",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-preset",
                    "ultrafast",
                    "-threads",
                    "2",
                    "-movflags",
                    "+faststart",
                    clip,
                )
                publish_file(clip, key)
            else:
                publish_file(frame, key)
    return FileResponse(media_path(key), media_type="video/mp4" if video else "image/png")
