import hashlib
import json
import math
from pathlib import Path
from unittest.mock import Mock

import pytest
import torch
from PIL import Image

from backend.image_engine.models.text_encoder import (
    Flux2KleinTextEncoder,
    Qwen3TapEncoder,
)
from scripts import check_captures_v4, v4_metrics


def test_encode_requires_tokenizer_before_forward(tmp_path, monkeypatch):
    encoder = Qwen3TapEncoder(None, str(tmp_path / "absent"))
    forward = Mock()
    monkeypatch.setattr(encoder, "forward", forward)

    with pytest.raises(RuntimeError, match="sans tokenizer.*tokenizer_path"):
        encoder.encode("prompt")

    forward.assert_not_called()


def test_encode_appends_eos_for_each_prompt(tmp_path, monkeypatch):
    encoder = Flux2KleinTextEncoder(None, str(tmp_path / "absent"))
    tokenizer = Mock(return_value={"input_ids": torch.tensor([[1, 2], [3, 4]])})
    monkeypatch.setattr(encoder, "tokenizer", tokenizer)
    monkeypatch.setattr(encoder, "forward", lambda input_ids: input_ids)

    encoded = encoder.encode(["un", "deux"])

    tokenizer.assert_called_once_with(
        ["un", "deux"],
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=511,
    )
    assert encoded.tolist() == [[1, 2, 151643], [3, 4, 151643]]
    assert Flux2KleinTextEncoder is Qwen3TapEncoder
    assert encoder.TAPS == (8, 17, 26)


def _write_capture(run_dir: Path) -> None:
    run_dir.mkdir(parents=True)
    work = Image.new("RGB", (2, 1))
    final = work.copy()
    final.putpixel((1, 0), (20, 20, 20))
    mask = Image.new("L", work.size)
    mask.putpixel((1, 0), 255)
    files = {}
    for name, image in (
        ("work.png", work),
        ("mask_backend.png", mask),
        ("candidate_before_composite.png", final),
        ("final.png", final),
    ):
        path = run_dir / name
        image.save(path)
        data = path.read_bytes()
        files[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {
        "run_id": run_dir.name,
        "run_type": "synthetic_sentinel",
        "capture_status": "complete",
        "origin": "synthetic_sentinel",
        "files": files,
        "source": {
            "transform": {"x": 0, "y": 0, "width": 1, "height": 1},
            "source_size": {"width": 1, "height": 1},
        },
        "inference": {"steps_executed": 0},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_capture_sets_are_explicit_and_missing_images_fail(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("THOT_DIAG_ROOT", str(tmp_path))
    monkeypatch.delenv("THOT_IMAGE_CAPTURE_DIR", raising=False)
    baseline_dir = tmp_path / "v4-captures" / "v4-o2-real"
    _write_capture(baseline_dir)
    for folder, run_name in (
        ("v4-captures-noref", "v4-o2-noref"),
        ("v4-captures-o1", "v4-o1-noref"),
        ("v4-captures-i1", "v4-i1-noref"),
    ):
        _write_capture(tmp_path / folder / run_name)

    assert check_captures_v4.main([]) == 1
    assert "v4-o2-noref-src" in capsys.readouterr().err
    assert v4_metrics.main([]) == 1
    assert "v4-o2-noref-src" in capsys.readouterr().err
    assert (tmp_path / "v4_metrics.json").is_file()

    historical = ["--capture-set", "historical"]
    report_path = tmp_path / "reports" / "nested" / "metrics.json"
    assert check_captures_v4.main(historical) == 0
    assert v4_metrics.main([*historical, "--output", str(report_path)]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["o2_before_after"]["after_run"] == "v4-o2-noref"
    assert (
        report["runs"]["v4-o2-noref"]["regions"]["right"]["final_vs_work"][
            "changed_gt8"
        ]
        == 1
    )

    (tmp_path / "v4-captures-noref" / "v4-o2-noref" / "final.png").unlink()
    assert check_captures_v4.main(historical) == 1
    assert v4_metrics.main(historical) == 1
    assert "final.png" in capsys.readouterr().err


def test_producer_and_consumers_share_capture_override(tmp_path, monkeypatch):
    root, capture_root = tmp_path / "diag", tmp_path / "my-captures"
    monkeypatch.setenv("THOT_DIAG_ROOT", str(root))
    monkeypatch.setenv("THOT_IMAGE_CAPTURE_DIR", str(capture_root))
    for name in (
        "THOT_DATA_DIR",
        "THOT_IMAGE_MASK_DIAGNOSTICS",
        "THOT_IMAGE_OPTIN_PATH",
    ):
        monkeypatch.setenv(name, "")

    data, run_dir, inputs = check_captures_v4.prepare_capture_producer(
        "v4-captures-noref", "v4-o2-noref-src"
    )

    assert data == root / "v4-data"
    assert (data / "image_backend.optin").read_text(encoding="utf-8") == "flux2\n"
    assert inputs == root / "v4-inputs" / "v4-o2-noref-src"
    assert inputs.is_dir()
    assert run_dir == capture_root / "v4-o2-noref-src"
    source_runs = check_captures_v4.capture_run_dirs(root, "source")
    assert source_runs[0] == run_dir
    assert all(path.parent == capture_root for path in source_runs)
    with pytest.raises(SystemExit, match="Capture illisible.*manifest.json"):
        check_captures_v4.load_produced_manifest(run_dir)


def test_bord_masque_o2_coherent():
    # Les bornes de la région protégée doivent coïncider avec le masque
    # (x >= rx et x < rx + rw, bornes exclusives).
    rx, rw = 437.5, 405.0
    left_bound = math.ceil(rx)
    right_bound = math.ceil(rx + rw)
    assert left_bound == 438
    assert right_bound == 843

    mask = Image.new("L", (1280, 1))
    for x in range(1280):
        inside = rx <= x < rx + rw
        mask.putpixel((x, 0), 0 if inside else 255)

    # Colonne juste avant la borne : editable (hors masque).
    assert mask.getpixel((437, 0)) >= 128
    # Première colonne protégée.
    assert mask.getpixel((438, 0)) < 128
    # Dernière colonne protégée (842 < 842.5).
    assert mask.getpixel((842, 0)) < 128
    # Colonne juste après la borne exclusive : editable.
    assert mask.getpixel((843, 0)) >= 128
