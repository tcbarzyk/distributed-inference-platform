"""
Frame discovery for sample-file mode.

Behavior:
- Scans every camera directory listed in `PRODUCER_CAMERA_DIRS`.
- Parses capture timestamps from filenames.
- Merges all cameras into one combined timeline.
- Returns a single manifest (`list[FrameRecord]`) sorted by `capture_ts_us`.
"""

from pathlib import Path
import sys
from dataclasses import dataclass
from typing import Any

# Support both direct-script and module execution.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SHARED_SRC = REPO_ROOT / "libs" / "platform_shared" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from platform_shared.config import load_service_config
from platform_shared.observability.logging import get_logger

CONFIG = load_service_config(caller_file=__file__)
logger = get_logger("Producer.FrameDiscovery")


def _log_extra(event: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


@dataclass(frozen=True)
class FrameRecord:
    frame_id: int
    source_id: str
    capture_ts_us: int
    path: Path


def discover_frames() -> list[FrameRecord]:
    if CONFIG.producer_source_mode == "livestream":
        raise NotImplementedError("Livestream mode is not implemented yet.")
    if CONFIG.producer_source_mode != "sample_files":
        raise ValueError(f"Unsupported PRODUCER_SOURCE_MODE: {CONFIG.producer_source_mode}")

    sample_root = Path(CONFIG.producer_sample_root)
    frame_paths: list[Path] = []

    for camera_dir in CONFIG.producer_camera_dirs:
        camera_path = sample_root / camera_dir
        matched_frames = list(camera_path.glob(CONFIG.producer_file_glob))
        logger.info(
            "Discovered %d candidate frames in %s",
            len(matched_frames),
            camera_path,
            extra=_log_extra(
                "producer.frame_discovery.camera_scanned",
                source_id=camera_dir,
                camera_path=str(camera_path),
                candidates=len(matched_frames),
            ),
        )
        frame_paths.extend(matched_frames)

    if not frame_paths:
        raise ValueError("No frames discovered across configured camera directories.")

    # Keep only files with valid parseable capture timestamps, then sort by timestamp.
    valid_with_ts: list[tuple[int, Path]] = []
    invalid_count = 0
    for frame in frame_paths:
        try:
            ts = parse_capture_timestamp(frame)
            valid_with_ts.append((ts, frame))
        except ValueError as exc:
            invalid_count += 1
            logger.warning(
                "Invalid timestamp for frame: %s",
                frame,
                extra=_log_extra(
                    "producer.frame_discovery.invalid_timestamp",
                    path=str(frame),
                    error_message=str(exc),
                ),
            )

    if not valid_with_ts:
        raise ValueError("No valid frames found after timestamp parsing.")

    valid_with_ts.sort(key=lambda item: item[0])
    sorted_frames = [FrameRecord(frame_id=i, source_id=frame.parent.name, capture_ts_us=ts, path=frame)
                     for i, (ts, frame) in enumerate(valid_with_ts)]

    logger.info(
        "Frame discovery complete: total_candidates=%d, valid=%d, invalid=%d",
        len(frame_paths),
        len(sorted_frames),
        invalid_count,
        extra=_log_extra(
            "producer.frame_discovery.complete",
            total_candidates=len(frame_paths),
            valid=len(sorted_frames),
            invalid=invalid_count,
        ),
    )
    return sorted_frames


def discover_frames_by_source() -> dict[str, list[FrameRecord]]:
    """Discover sample-file frames grouped by source_id and sorted per source timeline."""
    merged_frames = discover_frames()
    grouped: dict[str, list[FrameRecord]] = {}
    for frame in merged_frames:
        grouped.setdefault(frame.source_id, []).append(frame)

    regrouped: dict[str, list[FrameRecord]] = {}
    for source_id, frames in grouped.items():
        frames_sorted = sorted(frames, key=lambda f: f.capture_ts_us)
        # Reindex frame_id per source so sequence semantics remain local to each source.
        regrouped[source_id] = [
            FrameRecord(
                frame_id=index,
                source_id=source_id,
                capture_ts_us=record.capture_ts_us,
                path=record.path,
            )
            for index, record in enumerate(frames_sorted)
        ]
    return regrouped
    

def parse_capture_timestamp(frame_path: Path) -> int:
    """
    Extract capture timestamp from frame filename.

    Assumes filename format: <prefix>_<timestamp>.<ext>
    Example for this dataset:
    KAB_SK_1_undist_1384779301359985.bmp
    """
    timestamp_str = frame_path.stem.split("_")[-1]
    if not timestamp_str.isdigit():
        raise ValueError(f"Failed to parse numeric timestamp from: {frame_path}")
    return int(timestamp_str)
