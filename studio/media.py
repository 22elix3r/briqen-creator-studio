import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import tempfile
import time
import warnings
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from PIL import Image, ImageOps

from studio import settings

STORAGE_KEY = re.compile(r"[a-f0-9-]{36}\.(?:png|mp4)", re.ASCII)
Image.MAX_IMAGE_PIXELS = 24_000_000


def signature(value):
    return hmac.new(settings.SIGNING_KEY, value, hashlib.sha256).hexdigest()


def signed_url(asset_id):
    expires = int(time.time()) + 600
    token = signature(f"media:{asset_id}:{expires}".encode())
    return f"/media/{asset_id}?expires={expires}&signature={token}"


def media_path(storage_key):
    if not STORAGE_KEY.fullmatch(storage_key):
        raise ValueError("Invalid media object key")
    return settings.MEDIA_DIR / storage_key


def s3_key(storage_key):
    media_path(storage_key)
    return f"{settings.S3_PREFIX}/{storage_key}" if settings.S3_PREFIX else storage_key


@lru_cache
def s3_client():
    return boto3.session.Session().client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        region_name=settings.AWS_REGION,
        config=Config(
            signature_version="s3v4",
            connect_timeout=4,
            read_timeout=15,
            retries={"mode": "standard", "total_max_attempts": 4},
            s3={"addressing_style": "path" if settings.S3_ENDPOINT else "auto"},
        ),
    )


def publish_file(staged, storage_key):
    # Same filesystem atomic publication for fixture/local storage.
    path = media_path(storage_key)
    try:
        os.link(staged, path)
    except FileExistsError:
        pass
    return path


def content_type(storage_key):
    return "image/png" if storage_key.endswith(".png") else "video/mp4"


def store_file(staged, storage_key):
    size = staged.stat().st_size
    if not 0 < size <= settings.MAX_MEDIA_BYTES:
        raise ValueError("Media exceeds the configured size limit")
    if settings.MEDIA_BACKEND == "s3":
        with staged.open("rb") as data:
            try:
                s3_client().put_object(
                    Bucket=settings.S3_BUCKET,
                    Key=s3_key(storage_key),
                    Body=data,
                    ContentLength=size,
                    ContentType=content_type(storage_key),
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                    raise
        return
    settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    # Render/fetch temp files can live on a different filesystem from the mounted volume.
    with tempfile.NamedTemporaryFile(dir=settings.MEDIA_DIR) as target, staged.open("rb") as source:
        while chunk := source.read(256 * 1024):
            target.write(chunk)
        target.flush()
        publish_file(target.name, storage_key)


def fetch_file(storage_key, directory):
    if settings.MEDIA_BACKEND != "s3":
        return media_path(storage_key)
    response = s3_client().get_object(Bucket=settings.S3_BUCKET, Key=s3_key(storage_key))
    target = directory / storage_key
    with response["Body"] as source, target.open("wb") as output:
        if response["ContentLength"] > settings.MAX_MEDIA_BYTES:
            raise ValueError("Stored media exceeds the configured size limit")
        for chunk in source.iter_chunks(chunk_size=256 * 1024):
            output.write(chunk)
    return target


def media_response(asset, download):
    key = asset["storage_key"]
    filename = "creator-studio.png" if key.endswith(".png") else "creator-studio.mp4"
    if settings.MEDIA_BACKEND == "s3":
        parameters = {
            "Bucket": settings.S3_BUCKET,
            "Key": s3_key(key),
            "ResponseCacheControl": "private, no-store",
        }
        if download:
            parameters["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        url = s3_client().generate_presigned_url("get_object", Params=parameters, ExpiresIn=120)
        return RedirectResponse(url, headers={"Cache-Control": "private, no-store"})
    path = media_path(key)
    if not path.is_file():
        raise HTTPException(404, "Media not found")
    return FileResponse(
        path,
        media_type=content_type(key),
        filename=filename if download else None,
        headers={"Cache-Control": "private, max-age=60"},
    )


def stage_bytes(data, key):
    if len(data) > settings.MAX_MEDIA_BYTES:
        raise ValueError("Provider media exceeds the configured size limit")
    with tempfile.TemporaryDirectory() as directory:
        staged = Path(directory) / key
        staged.write_bytes(data)
        store_file(staged, key)


def ffmpeg(*args):
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-filter_threads",
            "2",
            *map(str, args),
        ],
        check=True,
        timeout=120,
        capture_output=True,
    )


def probe_video(path):
    response = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "mov",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration",
            "-of",
            "json",
            path,
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    video = next(iter(json.loads(response.stdout).get("streams", [])), None)
    if not video:
        raise ValueError("Media contains no video stream")
    duration = float(video["duration"])
    if not math.isfinite(duration) or not 0.1 <= duration <= 120:
        raise ValueError("Video length is outside supported limits")
    width, height = video["width"], video["height"]
    if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= 9_000_000):
        raise ValueError("Video dimensions are outside supported limits")
    return int(duration * 1000)


def scale_filter(width, height, fps):
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    )


def normalize_media(source, target, kind):
    if kind == "image":
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source, formats=("JPEG", "PNG", "WEBP")) as image:
                clean = ImageOps.exif_transpose(image).convert("RGB")
                clean.thumbnail((1800, 1800))
                # Reconstruct the image to drop provider EXIF and other embedded metadata.
                flattened = Image.new("RGB", clean.size)
                flattened.paste(clean)
                flattened.save(target, format="PNG")
        return None
    probe_video(source)
    ffmpeg(
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "mov",
        "-threads",
        "2",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        scale_filter(settings.WIDTH, settings.HEIGHT, settings.FPS),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-threads",
        "2",
        target,
    )
    return probe_video(target)


def render_manifest(manifest, key):
    width, height, fps = (manifest[field] for field in ("width", "height", "fps"))
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        elapsed_ms = 0
        for index, scene in enumerate(manifest["scenes"]):
            image = scene["kind"] == "image"
            position = (
                ["-loop", "1", "-framerate", str(fps)]
                if image
                else ["-ss", f"{scene['trim_ms'] / 1000:.3f}"]
            )
            source = fetch_file(scene["storage_key"], work)
            frames = round((elapsed_ms + scene["duration_ms"]) * fps / 1000) - round(
                elapsed_ms * fps / 1000
            )
            elapsed_ms += scene["duration_ms"]
            filters = scale_filter(width, height, fps)
            if not image:
                filters += ",tpad=stop_mode=clone:stop_duration=0.12"
            ffmpeg(
                *position,
                "-protocol_whitelist",
                "file,pipe",
                "-threads",
                "2",
                "-f",
                "image2" if image else "mov",
                "-i",
                source,
                "-frames:v",
                frames,
                "-vf",
                filters,
                "-an",
                "-map_metadata",
                "-1",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
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
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            concat,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output,
        )
        duration = probe_video(output)
        store_file(output, key)
        return duration
