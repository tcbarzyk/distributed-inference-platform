FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# OpenCV runtime dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy only what worker needs.
COPY services/worker/requirements.txt services/worker/requirements.txt
COPY services/worker/src services/worker/src
COPY libs/platform_shared libs/platform_shared

# Requirements files may be UTF-16 on Windows and may include local platform_shared
# path/editable entries that are not portable in Linux image builds.
RUN python - <<'PY'
from pathlib import Path

p = Path("services/worker/requirements.txt")
raw = p.read_bytes()
text = None

for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
    try:
        text = raw.decode(encoding)
        break
    except UnicodeDecodeError:
        pass

if text is None:
    text = raw.decode("latin-1")

lines: list[str] = []
for source_line in text.replace("\r\n", "\n").split("\n"):
    line = source_line.strip()
    if not line or line.startswith("#"):
        continue

    lowered = line.lower()
    if "platform-shared @ file://" in lowered or "platform_shared" in lowered:
        continue

    lines.append(line)

Path("/tmp/requirements.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir ./libs/platform_shared

CMD ["python", "services/worker/src/main.py"]
