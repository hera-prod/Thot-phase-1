"""Tests du reporting de ``steps_executed`` (P-13)."""

from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from backend.services import flux_spike as fx


def test_parse_steps_executed_missing():
    log_tail = [f"line {i}" for i in range(10)]
    assert fx._parse_steps_executed(log_tail) is None


def test_parse_steps_executed_found_after_20_lines():
    log_tail = ["STEPS_EXECUTED: 7"] + [f"line {i}" for i in range(20)]
    assert fx._parse_steps_executed(log_tail) == 7


def test_parse_steps_executed_string():
    text = "STEPS_EXECUTED: 3\n" + "".join(f"line {i}\n" for i in range(20))
    assert fx._parse_steps_executed(text) == 3


def _png_512():
    buf = io.BytesIO()
    Image.new("RGB", (512, 512), (12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def _fake_popen_factory(steps_line):
    class P:
        pass

    def factory(command, *args, **kwargs):
        output_path = command[command.index("--output") + 1]
        with open(output_path, "wb") as stream:
            stream.write(_png_512())
        read_fd, write_fd = os.pipe()
        os.write(write_fd, (steps_line + "\n").encode())
        os.close(write_fd)
        p = P()
        p.stdout = os.fdopen(read_fd, "rb")
        p.returncode = 0
        p.pid = None

        def _poll():
            return 0

        def _wait(timeout=None):
            return 0

        p.poll = _poll
        p.wait = _wait
        p.terminate = lambda: None
        p.kill = lambda: None
        return p

    return factory


def test_run_locked_without_steps_raises(monkeypatch):
    monkeypatch.delenv("THOT_USE_OFFICIAL_INPAINT", raising=False)
    monkeypatch.setattr(fx, "_official_inpaint_module", lambda: None)
    monkeypatch.setattr(fx, "_cleanup_blocked", None, raising=False)
    fx._cleanup_blocked = None
    recorded = []
    monkeypatch.setattr(fx.subprocess, "Popen", _fake_popen_factory("no steps here"))

    with pytest.raises(RuntimeError, match=r"\[STEPS_UNREPORTED\]"):
        fx._run_t2i_locked(
            "un prompt",
            7,
            512,
            512,
            30.0,
            None,
            None,
            None,
            init_image="/tmp/init.png",
            mask="/tmp/mask.png",
            mode="inpaint",
            strength=0.8,
        )


def test_run_locked_with_steps_found_late(monkeypatch):
    monkeypatch.delenv("THOT_USE_OFFICIAL_INPAINT", raising=False)
    monkeypatch.setattr(fx, "_official_inpaint_module", lambda: None)
    fx._cleanup_blocked = None
    monkeypatch.setattr(fx.subprocess, "Popen", _fake_popen_factory("STEPS_EXECUTED: 4"))

    png_bytes, stats = fx._run_t2i_locked(
        "un prompt",
        7,
        512,
        512,
        30.0,
        None,
        None,
        None,
        init_image="/tmp/init.png",
        mask="/tmp/mask.png",
        mode="inpaint",
        strength=0.8,
    )
    assert stats["steps_executed"] == 4
