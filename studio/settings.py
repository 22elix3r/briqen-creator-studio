import base64
import binascii
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


def secret(name, default=""):
    value, filename = os.getenv(name), os.getenv(f"{name}_FILE")
    if value is not None and filename:
        raise RuntimeError(f"Set exactly one of {name} and {name}_FILE")
    return Path(filename).read_text().strip() if filename else (value or default)


def number(name, default, low, high):
    value = int(os.getenv(name, str(default)))
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


MODE = os.getenv("STUDIO_MODE", "demo")
if MODE not in {"demo", "production"}:
    raise RuntimeError("STUDIO_MODE must be demo or production")

PRODUCTION = MODE == "production"
AUTH_REQUIRED = PRODUCTION or os.getenv("STUDIO_AUTH_REQUIRED") == "1"
PUBLIC_ORIGIN = os.getenv("STUDIO_PUBLIC_ORIGIN", "http://127.0.0.1:8765").rstrip("/")
DATABASE_URL = secret(
    "STUDIO_DATABASE_URL", "" if PRODUCTION else "postgresql://studio:studio@127.0.0.1/studio"
)
PROVIDER = os.getenv("STUDIO_PROVIDER", "replicate" if PRODUCTION else "fixture")
PROVIDER_URL = os.getenv("STUDIO_PROVIDER_URL", "http://127.0.0.1:8001")
CALLBACK_URL = os.getenv("STUDIO_CALLBACK_URL", "http://127.0.0.1:8000/internal/provider-events")
MEDIA_DIR = Path(os.getenv("STUDIO_MEDIA_DIR", "media"))
SIGNING_KEY = secret(
    "STUDIO_SIGNING_KEY", "" if PRODUCTION else "creator-studio-local-demo"
).encode()
METRICS_TOKEN = secret("STUDIO_METRICS_TOKEN")

MEDIA_BACKEND = os.getenv("STUDIO_MEDIA_BACKEND", "s3" if PRODUCTION else "local")
S3_BUCKET = os.getenv("STUDIO_S3_BUCKET", "")
S3_PREFIX = os.getenv("STUDIO_S3_PREFIX", "studio/").strip("/")
S3_ENDPOINT = os.getenv("STUDIO_S3_ENDPOINT")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
REPLICATE_TOKEN = secret("STUDIO_REPLICATE_TOKEN")


def model_config(kind):
    name = f"STUDIO_REPLICATE_{kind.upper()}"
    options = json.loads(os.getenv(f"{name}_INPUT", "{}"))
    if not isinstance(options, dict):
        raise TypeError(f"{name}_INPUT must be a JSON object")
    return (os.getenv(f"{name}_MODEL", ""), os.getenv(f"{name}_PROMPT_FIELD", "prompt"), options)


def webhook_key():
    value = secret("STUDIO_REPLICATE_WEBHOOK_SECRET")
    try:
        if value.startswith("whsec_"):
            return base64.b64decode(value[6:], validate=True)
    except binascii.Error:
        pass
    return b""


LATE_SECONDS = float(os.getenv("STUDIO_LATE_SECONDS", "14"))
MEDIA_DEADLINE_SECONDS = number(
    "STUDIO_MEDIA_DEADLINE_SECONDS", 900 if PRODUCTION else 60, 10, 2700
)
SCRIPT_DEADLINE_SECONDS = number("STUDIO_SCRIPT_DEADLINE_SECONDS", 180, 10, 900)
EXPORT_DEADLINE_SECONDS = number("STUDIO_EXPORT_DEADLINE_SECONDS", 300, 10, 1800)
LEASE_SECONDS = number("STUDIO_LEASE_SECONDS", 45 if PRODUCTION else 20, 8, 120)
COSTS = {
    "script": number("STUDIO_SCRIPT_CREDITS", 1 if PRODUCTION else 0, 0, 1000),
    "image": number("STUDIO_IMAGE_CREDITS", 4, 1, 1000),
    "video": number("STUDIO_VIDEO_CREDITS", 12, 1, 10000),
}
SESSION_SECONDS = number("STUDIO_SESSION_SECONDS", 8 * 3600, 900, 86400)
MAX_MEDIA_BYTES = number("STUDIO_MAX_MEDIA_MB", 48, 1, 128) * 1024 * 1024
MAX_BODY_BYTES = 128 * 1024
MAX_PROJECTS = number("STUDIO_MAX_PROJECTS", 250, 1, 10000)
WIDTH, HEIGHT, FPS = 540, 960, 30
MODEL_ID = re.compile(r"(?:[a-f0-9]{64}|[\w.-]+/[\w.-]+)", re.ASCII)


def validate(component):
    if not DATABASE_URL:
        raise RuntimeError("STUDIO_DATABASE_URL is required")
    if component in {"migrate", "admin"}:
        return
    if MEDIA_BACKEND not in {"local", "s3"} or (MEDIA_BACKEND == "s3" and not S3_BUCKET):
        raise RuntimeError("Configure a valid media backend and bucket")
    if PROVIDER not in {"fixture", "replicate"}:
        raise RuntimeError("STUDIO_PROVIDER must be fixture or replicate")
    if not PRODUCTION:
        return
    origin = urlsplit(PUBLIC_ORIGIN)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.path
        or origin.query
        or origin.fragment
        or origin.username
    ):
        raise RuntimeError("STUDIO_PUBLIC_ORIGIN must be one HTTPS origin")
    if len(SIGNING_KEY) < 32 or SIGNING_KEY == b"creator-studio-local-demo":
        raise RuntimeError("Production requires a private STUDIO_SIGNING_KEY of at least 32 bytes")
    if METRICS_TOKEN and len(METRICS_TOKEN) < 32:
        raise RuntimeError("The production metrics token must be at least 32 characters")
    if MEDIA_BACKEND != "s3" or PROVIDER != "replicate":
        raise RuntimeError("Production requires S3 and the Replicate adapter")
    if S3_ENDPOINT and urlsplit(S3_ENDPOINT).scheme != "https":
        raise RuntimeError("A production S3 endpoint must use HTTPS")
    if component == "api" and len(webhook_key()) < 16:
        raise RuntimeError("Set the Replicate webhook signing secret for the API")
    if component == "worker":
        if not REPLICATE_TOKEN:
            raise RuntimeError("Set STUDIO_REPLICATE_TOKEN for the worker")
        for kind in ("script", "image", "video"):
            model, field, _ = model_config(kind)
            if not MODEL_ID.fullmatch(model) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
                raise RuntimeError(f"Configure a valid Replicate {kind} model and prompt field")
