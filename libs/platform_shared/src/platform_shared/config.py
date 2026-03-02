"""Shared environment/config loading for producer and worker services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class ServiceConfig:
    redis_host: str
    redis_port: int
    queue_name: str
    redis_db: int
    redis_password: str | None
    log_level: str
    producer_source_mode: str
    producer_sample_root: str | None
    producer_camera_dirs: tuple[str, ...]
    producer_file_glob: str
    producer_replay_mode: str
    producer_fixed_fps: int
    producer_frame_encoding: str
    producer_jpeg_quality: int
    producer_frame_ttl_seconds: int
    producer_frame_key_prefix: str
    producer_stream_url: str | None
    producer_stream_source_id: str | None
    producer_summary_log_interval_seconds: int
    worker_summary_log_interval_seconds: int
    worker_results_mode: str
    worker_results_dir: str
    worker_save_annotated: bool
    worker_annotated_every_n: int
    worker_model_device: str


def _repo_root_from(caller_file: str) -> Path:
    # caller file is expected at: services/<service>/src/main.py
    return Path(caller_file).resolve().parents[3]


def _require(name: str, value: str | None) -> str:
    if value is None or value.strip() == "":
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _to_int(name: str, value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def _to_csv_tuple(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    items = [item.strip() for item in value.split(",") if item.strip()]
    return tuple(items) if items else default


def _to_choice(name: str, value: str | None, default: str, choices: set[str]) -> str:
    candidate = (value or default).strip().lower()
    if candidate not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"Environment variable {name} must be one of: {allowed}")
    return candidate


def _to_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_service_config(*, caller_file: str) -> ServiceConfig:
    repo_root = _repo_root_from(caller_file)
    env_path = repo_root / ".env"
    load_dotenv(dotenv_path=env_path)

    redis_host = _require("REDIS_HOST", os.getenv("REDIS_HOST"))
    queue_name = _require("QUEUE_NAME", os.getenv("QUEUE_NAME"))
    redis_port = _to_int("REDIS_PORT", os.getenv("REDIS_PORT"), 6379)
    redis_db = _to_int("REDIS_DB", os.getenv("REDIS_DB"), 0)
    redis_password = os.getenv("REDIS_PASSWORD") or None
    log_level = (os.getenv("LOG_LEVEL") or "INFO").upper()

    producer_source_mode = _to_choice(
        "PRODUCER_SOURCE_MODE",
        os.getenv("PRODUCER_SOURCE_MODE"),
        "sample_files",
        {"sample_files", "livestream"},
    )
    producer_replay_mode = _to_choice(
        "PRODUCER_REPLAY_MODE",
        os.getenv("PRODUCER_REPLAY_MODE"),
        "realtime",
        {"realtime", "fixed", "max"},
    )
    producer_frame_encoding = _to_choice(
        "PRODUCER_FRAME_ENCODING",
        os.getenv("PRODUCER_FRAME_ENCODING"),
        "jpeg",
        {"jpeg", "png", "webp", "bmp"},
    )

    sample_root_raw = os.getenv("PRODUCER_SAMPLE_ROOT")
    producer_sample_root: str | None = None
    if sample_root_raw and sample_root_raw.strip():
        sample_root_path = Path(sample_root_raw.strip())
        if not sample_root_path.is_absolute():
            sample_root_path = repo_root / sample_root_path
        producer_sample_root = str(sample_root_path.resolve())

    producer_camera_dirs = _to_csv_tuple(
        os.getenv("PRODUCER_CAMERA_DIRS"),
        ("KAB_SK_1_undist", "KAB_SK_4_undist"),
    )
    producer_file_glob = (os.getenv("PRODUCER_FILE_GLOB") or "*.bmp").strip()
    producer_fixed_fps = _to_int("PRODUCER_FIXED_FPS", os.getenv("PRODUCER_FIXED_FPS"), 25)
    producer_jpeg_quality = _to_int("PRODUCER_JPEG_QUALITY", os.getenv("PRODUCER_JPEG_QUALITY"), 80)
    producer_frame_ttl_seconds = _to_int(
        "PRODUCER_FRAME_TTL_SECONDS",
        os.getenv("PRODUCER_FRAME_TTL_SECONDS"),
        60,
    )
    producer_frame_key_prefix = (os.getenv("PRODUCER_FRAME_KEY_PREFIX") or "frame").strip()
    producer_stream_url = os.getenv("PRODUCER_STREAM_URL") or None
    producer_stream_source_id = os.getenv("PRODUCER_STREAM_SOURCE_ID") or None
    producer_summary_log_interval_seconds = _to_int(
        "PRODUCER_SUMMARY_LOG_INTERVAL_SECONDS",
        os.getenv("PRODUCER_SUMMARY_LOG_INTERVAL_SECONDS"),
        30,
    )
    worker_summary_log_interval_seconds = _to_int(
        "WORKER_SUMMARY_LOG_INTERVAL_SECONDS",
        os.getenv("WORKER_SUMMARY_LOG_INTERVAL_SECONDS"),
        30,
    )
    worker_results_mode = _to_choice(
      "WORKER_RESULTS_MODE",
      os.getenv("WORKER_RESULTS_MODE"),
      "local_jsonl",
      {"local_jsonl", "postgres"},
    )
    worker_results_dir_raw = (os.getenv("WORKER_RESULTS_DIR") or "services/worker/output").strip()
    worker_results_dir_path = Path(worker_results_dir_raw)
    if not worker_results_dir_path.is_absolute():
        worker_results_dir_path = repo_root / worker_results_dir_path
    worker_results_dir = str(worker_results_dir_path.resolve())
    worker_save_annotated = _to_bool(os.getenv("WORKER_SAVE_ANNOTATED"), False)
    worker_annotated_every_n = _to_int(
        "WORKER_ANNOTATED_EVERY_N",
        os.getenv("WORKER_ANNOTATED_EVERY_N"),
        30,
    )
    worker_model_device = _to_choice(
        "WORKER_MODEL_DEVICE",
        os.getenv("WORKER_MODEL_DEVICE"),
        "auto",
        {"auto", "cpu", "cuda"},
    )

    if producer_source_mode == "sample_files" and not producer_sample_root:
        default_sample_root = repo_root / "services/producer/sample-video/20140618_Sequence1a/Sequence1a"
        producer_sample_root = str(default_sample_root.resolve())

    if producer_source_mode == "livestream" and not producer_stream_url:
        raise ValueError("Missing required environment variable: PRODUCER_STREAM_URL for livestream mode")

    if producer_source_mode == "livestream" and not producer_stream_source_id:
        producer_stream_source_id = "stream-1"

    if producer_fixed_fps <= 0:
        raise ValueError("PRODUCER_FIXED_FPS must be greater than 0")
    if producer_jpeg_quality < 1 or producer_jpeg_quality > 100:
        raise ValueError("PRODUCER_JPEG_QUALITY must be between 1 and 100")
    if producer_frame_ttl_seconds <= 0:
        raise ValueError("PRODUCER_FRAME_TTL_SECONDS must be greater than 0")
    if producer_summary_log_interval_seconds <= 0:
        raise ValueError("PRODUCER_SUMMARY_LOG_INTERVAL_SECONDS must be greater than 0")
    if worker_summary_log_interval_seconds <= 0:
        raise ValueError("WORKER_SUMMARY_LOG_INTERVAL_SECONDS must be greater than 0")
    if worker_annotated_every_n <= 0:
        raise ValueError("WORKER_ANNOTATED_EVERY_N must be greater than 0")
    if producer_frame_key_prefix == "":
        raise ValueError("PRODUCER_FRAME_KEY_PREFIX cannot be empty")
    if producer_file_glob == "":
        raise ValueError("PRODUCER_FILE_GLOB cannot be empty")

    return ServiceConfig(
        redis_host=redis_host,
        redis_port=redis_port,
        queue_name=queue_name,
        redis_db=redis_db,
        redis_password=redis_password,
        log_level=log_level,
        producer_source_mode=producer_source_mode,
        producer_sample_root=producer_sample_root,
        producer_camera_dirs=producer_camera_dirs,
        producer_file_glob=producer_file_glob,
        producer_replay_mode=producer_replay_mode,
        producer_fixed_fps=producer_fixed_fps,
        producer_frame_encoding=producer_frame_encoding,
        producer_jpeg_quality=producer_jpeg_quality,
        producer_frame_ttl_seconds=producer_frame_ttl_seconds,
        producer_frame_key_prefix=producer_frame_key_prefix,
        producer_stream_url=producer_stream_url,
        producer_stream_source_id=producer_stream_source_id,
        producer_summary_log_interval_seconds=producer_summary_log_interval_seconds,
        worker_summary_log_interval_seconds=worker_summary_log_interval_seconds,
        worker_results_mode=worker_results_mode,
        worker_results_dir=worker_results_dir,
        worker_save_annotated=worker_save_annotated,
        worker_annotated_every_n=worker_annotated_every_n,
        worker_model_device=worker_model_device,
    )
