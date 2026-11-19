"""Tests de manifest_valid."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.services.capture_manifest import manifest_valid


def _make(tmp_path: Path):
    data = b"abc"
    h = hashlib.sha256(data).hexdigest()
    f = tmp_path / "work.png"
    f.write_bytes(data)
    manifest = {
        "run_type": "model_inference",
        "origin": "model_inference",
        "capture_status": "complete",
        "files": {"work.png": {"sha256": h, "bytes": len(data)}},
    }
    return manifest, tmp_path


def test_valid_manifest(tmp_path):
    manifest, d = _make(tmp_path)
    ok, reasons = manifest_valid(manifest, d)
    assert ok is True
    assert reasons == []


def test_non_hex_sha256(tmp_path):
    manifest, d = _make(tmp_path)
    manifest["files"]["work.png"]["sha256"] = "sha"
    ok, reasons = manifest_valid(manifest, d)
    assert ok is False
    assert reasons == ["non_hex_sha256:work.png"]


def test_missing_file(tmp_path):
    manifest, d = _make(tmp_path)
    (d / "work.png").unlink()
    ok, reasons = manifest_valid(manifest, d)
    assert ok is False
    assert "missing_file:work.png" in reasons


def test_unknown_run_type(tmp_path):
    manifest, d = _make(tmp_path)
    manifest["run_type"] = "unknown"
    ok, reasons = manifest_valid(manifest, d)
    assert ok is False
    assert "unknown_run_type" in reasons


def test_tool_direct_origin_accepted(tmp_path):
    manifest, d = _make(tmp_path)
    manifest["origin"] = "tool-direct"
    manifest["mask_kind"] = "fill"
    manifest["source"] = {
        "transform": {"x": 1, "y": 2, "width": 3, "height": 4},
        "source_size": {"width": 10, "height": 10},
    }
    ok, reasons = manifest_valid(manifest, d)
    assert ok is True
    assert reasons == []


def test_unknown_origin_rejected(tmp_path):
    manifest, d = _make(tmp_path)
    manifest["origin"] = "inconnu"
    ok, reasons = manifest_valid(manifest, d)
    assert ok is False
    assert "unknown_origin" in reasons
