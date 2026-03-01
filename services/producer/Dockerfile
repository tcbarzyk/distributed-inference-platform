FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# OpenCV runtime dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy only what producer needs.
COPY services/producer/requirements.txt services/producer/requirements.txt
COPY services/producer/src services/producer/src
COPY libs/platform_shared libs/platform_shared

# Requirements currently include a platform_shared editable Git line that is not
# portable in Linux builds. Filter it out and install shared package locally.
RUN grep -av "platform_shared" services/producer/requirements.txt > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir ./libs/platform_shared

CMD ["python", "services/producer/src/main.py"]

