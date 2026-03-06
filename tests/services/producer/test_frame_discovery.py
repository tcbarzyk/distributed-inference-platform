from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import frame_discovery as fd


def test_parse_capture_timestamp_valid() -> None:
    p = Path("KAB_SK_1_undist_1384779301359985.bmp")
    assert fd.parse_capture_timestamp(p) == 1384779301359985


def test_parse_capture_timestamp_invalid_raises() -> None:
    p = Path("KAB_SK_1_undist_bad.bmp")
    with pytest.raises(ValueError, match="numeric timestamp"):
        fd.parse_capture_timestamp(p)


def test_discover_frames_skips_invalid_and_sorts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "camA"
    source.mkdir(parents=True)
    valid_2 = source / "camA_20.bmp"
    valid_1 = source / "camA_10.bmp"
    invalid = source / "camA_bad.bmp"
    valid_2.write_text("x", encoding="utf-8")
    valid_1.write_text("x", encoding="utf-8")
    invalid.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        fd,
        "CONFIG",
        SimpleNamespace(
            producer_source_mode="sample_files",
            producer_sample_root=str(tmp_path),
            producer_camera_dirs=("camA",),
            producer_file_glob="*.bmp",
        ),
    )

    rows = fd.discover_frames()
    assert [r.capture_ts_us for r in rows] == [10, 20]
    assert [r.frame_id for r in rows] == [0, 1]
    assert all(r.source_id == "camA" for r in rows)


def test_discover_frames_raises_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "camA").mkdir(parents=True)
    monkeypatch.setattr(
        fd,
        "CONFIG",
        SimpleNamespace(
            producer_source_mode="sample_files",
            producer_sample_root=str(tmp_path),
            producer_camera_dirs=("camA",),
            producer_file_glob="*.bmp",
        ),
    )

    with pytest.raises(ValueError, match="No frames discovered"):
        fd.discover_frames()
