from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from backend.inference import ipc
from backend.inference.ipc import _validate_request, is_valid_request_id
from backend.utils import files


@pytest.mark.parametrize(
    "request_id",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "request-123",
        "request_id_42",
    ],
)
def test_ipc_accepts_normal_request_ids(request_id: str) -> None:
    assert is_valid_request_id(request_id)
    valid, error_code, normalized = _validate_request({
        "id": request_id,
        "method": "chat.send",
        "params": {},
    })
    assert valid is True
    assert error_code is None
    assert normalized["request_id"] == request_id


@pytest.mark.parametrize(
    "request_id",
    [
        "",
        "..",
        "../outside",
        "..\\outside",
        "/absolute/path",
        "C:\\absolute\\path",
        "with/slash",
        "with\\backslash",
        "has.dot",
        "has space",
        "line\nbreak",
        None,
        42,
    ],
)
def test_ipc_rejects_path_like_request_ids(request_id: object) -> None:
    valid, error_code, _ = _validate_request({
        "request_id": request_id,
        "method": "chat.send",
        "params": {},
    })
    assert valid is False
    assert error_code in {"missing_request_id", "invalid_request_id"}


def test_explicit_empty_request_id_cannot_fall_back_to_legacy_id() -> None:
    valid, error_code, _ = _validate_request({
        "request_id": "",
        "id": "valid-request",
        "method": "chat.send",
        "params": {},
    })
    assert valid is False
    assert error_code == "missing_request_id"


def test_ipc_does_not_queue_or_correlate_invalid_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict] = []
    emitted: list[tuple[str, dict, dict]] = []
    request = {
        "request_id": "../escape",
        "method": "chat.send",
        "params": {"message": "hello"},
    }

    monkeypatch.setattr(ipc.sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(
        ipc,
        "emit",
        lambda event, payload, **kwargs: emitted.append((event, payload, kwargs)) or True,
    )

    thread = threading.Thread(target=ipc.stdin_loop, args=(received.append,), daemon=True)
    thread.start()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert received == []
    assert len(emitted) == 1
    event, payload, kwargs = emitted[0]
    assert event == "error"
    assert payload["code"] == "invalid_request_id"
    assert payload["request_id"] == "unknown"
    assert kwargs["run_id"] == "unknown"


def test_save_upload_and_cleanup_stay_under_canonical_temp_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "temp"
    monkeypatch.setattr(files, "_get_tmp_root", lambda: root)
    payload = b"\x89PNG\r\n\x1a\npayload"

    saved_path, digest = files.save_upload("request-123", "photo.png", payload)
    saved = Path(saved_path).resolve()
    assert saved.parent.parent == root.resolve()
    assert digest.startswith("sha256:")

    files.cleanup_request("request-123")
    assert not (root / "request-123").exists()


@pytest.mark.parametrize(
    "request_id",
    ["", "..", "../outside", "..\\outside", "/absolute/path", "with/slash", "with.dot"],
)
def test_temp_operations_reject_invalid_request_ids_without_touching_outside(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request_id: str,
) -> None:
    root = tmp_path / "temp"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(files, "_get_tmp_root", lambda: root)

    with pytest.raises(ValueError):
        files.save_upload(request_id, "photo.png", b"\x89PNG\r\n\x1a\npayload")
    with pytest.raises(ValueError):
        files.cleanup_request(request_id)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cleanup_rejects_a_sibling_with_a_matching_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "temp"
    sibling = tmp_path / "temp_evil"
    sibling.mkdir()
    sentinel = sibling / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(files, "_get_tmp_root", lambda: root)

    with pytest.raises(ValueError):
        files.cleanup_request("../temp_evil")

    assert sentinel.exists()


def test_main_handler_revalidates_request_id_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import main

    events: list[tuple[str, dict, dict]] = []
    monkeypatch.setattr(
        main,
        "emit",
        lambda event, payload, **kwargs: events.append((event, payload, kwargs)) or True,
    )

    main.handle({
        "request_id": "../escape",
        "method": "chat.send",
        "params": {"message": "must not run"},
    })

    assert len(events) == 1
    event, payload, kwargs = events[0]
    assert event == "error"
    assert payload["code"] == "invalid_request_id"
    assert payload["request_id"] == "unknown"
    assert kwargs["run_id"] is None
