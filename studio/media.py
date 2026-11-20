import hashlib
import hmac
import os
import subprocess
import tempfile
import time
from pathlib import Path

from studio import settings


def signature(value):
    return hmac.new(settings.SIGNING_KEY, value, hashlib.sha256).hexdigest()


def signed_url(asset_id):
    expires = int(time.time()) + 900
    token = signature(f"media:{asset_id}:{expires}".encode())
    return f"/media/{asset_id}?expires={expires}&signature={token}"


def media_path(storage_key):
    return settings.MEDIA_DIR / storage_key


def publish_file(staged, storage_key):
    # Linking a complete file makes competing deliveries harmless.
    path = media_path(storage_key)
    try:
        os.link(staged, path)
    except FileExistsError:
        pass
    return path


def stage_bytes(data, key):
    settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=settings.MEDIA_DIR) as staged:
        staged.write(data)
        staged.flush()
        return publish_file(staged.name, key)


def ffmpeg(*args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, args)],
        check=True,
        timeout=120,
        capture_output=True,
    )


def render_manifest(manifest, key):
    settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    width, height, fps = (manifest[field] for field in ("width", "height", "fps"))
    with tempfile.TemporaryDirectory(dir=settings.MEDIA_DIR) as directory:
        work = Path(directory)
        for index, scene in enumerate(manifest["scenes"]):
            position = (
                ["-loop", "1", "-framerate", str(fps)]
                if scene["kind"] == "image"
                else ["-ss", f"{scene['trim_ms'] / 1000:.3f}"]
            )
            ffmpeg(
                *position,
                "-i",
                media_path(scene["storage_key"]),
                "-t",
                f"{scene['duration_ms'] / 1000:.3f}",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "2",
                work / f"scene-{index}.mp4",
            )
        concat = work / "concat.txt"
        concat.write_text(
            "".join(f"file 'scene-{index}.mp4'\n" for index in range(len(manifest["scenes"])))
        )
        output = work / "export.mp4"
        ffmpeg(
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            concat,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output,
        )
        return publish_file(output, key)
