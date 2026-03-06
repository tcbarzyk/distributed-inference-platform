from __future__ import annotations

import importlib
import sys
import types

import numpy as np


def _load_inference_module():
    module_name = "inference"
    sys.modules.pop(module_name, None)

    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))

    class _FakeYOLO:
        def __init__(self, _weights: str):
            self.moved_to = None

        def to(self, device: str):
            self.moved_to = device
            return self

        def __call__(self, *_args, **_kwargs):
            return []

    fake_ultralytics = types.SimpleNamespace(YOLO=_FakeYOLO)
    sys.modules["torch"] = fake_torch
    sys.modules["ultralytics"] = fake_ultralytics
    return importlib.import_module(module_name)


inference = _load_inference_module()


def test_resolve_device_auto_falls_back_to_cpu() -> None:
    assert inference._resolve_device("auto") == "cpu"
    assert inference._resolve_device("cuda") == "cpu"
    assert inference._resolve_device("cpu") == "cpu"


def test_preprocess_image_normalizes_shapes() -> None:
    gray = np.zeros((12, 10), dtype=np.uint8)
    out_gray = inference.preprocess_image(gray, img_size=8)
    assert out_gray.shape == (8, 8, 3)

    bgra = np.zeros((12, 10, 4), dtype=np.uint8)
    out_bgra = inference.preprocess_image(bgra, img_size=8)
    assert out_bgra.shape == (8, 8, 3)


def test_postprocess_detections_filters_by_threshold() -> None:
    class _Tensor:
        def __init__(self, value):
            self._value = value

        def cpu(self):
            return self

        def tolist(self):
            return self._value

    boxes = types.SimpleNamespace(
        xyxy=_Tensor([[1.111, 2.222, 3.333, 4.444], [5, 6, 7, 8]]),
        conf=_Tensor([0.99, 0.1]),
        cls=_Tensor([0.0, 2.0]),
    )
    result = types.SimpleNamespace(boxes=boxes, names={0: "person", 2: "car"})

    detections = inference.postprocess_detections([result], conf_threshold=0.25)
    assert len(detections) == 1
    assert detections[0]["label"] == "person"
    assert detections[0]["class_id"] == 0
    assert detections[0]["bbox_xyxy"] == [1.11, 2.22, 3.33, 4.44]
