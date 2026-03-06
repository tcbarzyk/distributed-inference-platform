from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_SRC = REPO_ROOT / "libs" / "platform_shared" / "src"
API_SRC = REPO_ROOT / "services" / "api" / "src"
PRODUCER_SRC = REPO_ROOT / "services" / "producer" / "src"
WORKER_SRC = REPO_ROOT / "services" / "worker" / "src"

for path in (REPO_ROOT, SHARED_SRC, API_SRC, PRODUCER_SRC, WORKER_SRC):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

# Ensure imports that build DB engine at module import-time have sane defaults.
os.environ.setdefault("POSTGRES_USER", "platform_user")
os.environ.setdefault("POSTGRES_PASSWORD", "platform_password")
os.environ.setdefault("POSTGRES_HOST", "postgres")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "platform")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("QUEUE_NAME", "video_stream")
