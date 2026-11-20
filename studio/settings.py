import os
from pathlib import Path

DATABASE_URL = os.getenv("STUDIO_DATABASE_URL", "postgresql://studio:studio@127.0.0.1/studio")
PROVIDER_URL = os.getenv("STUDIO_PROVIDER_URL", "http://127.0.0.1:8001")
CALLBACK_URL = os.getenv("STUDIO_CALLBACK_URL", "http://127.0.0.1:8000/internal/provider-events")
MEDIA_DIR = Path(os.getenv("STUDIO_MEDIA_DIR", "media"))
SIGNING_KEY = os.getenv("STUDIO_SIGNING_KEY", "creator-studio-local-demo").encode()
LATE_SECONDS = float(os.getenv("STUDIO_LATE_SECONDS", "14"))
MEDIA_DEADLINE_SECONDS = int(os.getenv("STUDIO_MEDIA_DEADLINE_SECONDS", "60"))
COSTS = {"image": 4, "video": 12}
LEASE_SECONDS = 20
WIDTH, HEIGHT, FPS = 540, 960, 30
