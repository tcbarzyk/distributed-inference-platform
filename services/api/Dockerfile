FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/api/requirements.txt services/api/requirements.txt
COPY services/api/src services/api/src
COPY services/api/alembic services/api/alembic
COPY services/api/alembic.ini services/api/alembic.ini
COPY libs/platform_shared libs/platform_shared

RUN python - <<'PY'
from pathlib import Path

p = Path("services/api/requirements.txt")
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
    # requirements may include local editable/path refs from dev machines.
    if "platform-shared @ file://" in lowered or "platform_shared" in lowered:
        continue

    lines.append(line)

Path("/tmp/requirements.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir ./libs/platform_shared

EXPOSE 8000

CMD ["uvicorn", "services.api.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
