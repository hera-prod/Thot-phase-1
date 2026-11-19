from backend.image_engine.core import paths


def test_text_encoder_path_uses_vocab_compatible_qwen8b():
    assert paths.TEXT_ENCODER_GGUF.name == "Qwen3-8B-Q4_K_M.gguf"


def test_explicit_models_path_wins_over_discovered_data(monkeypatch, tmp_path):
    data_models = tmp_path / "data" / "models" / "flux2-klein"
    (data_models / "transformer").mkdir(parents=True)
    explicit_models = tmp_path / "portable" / "models" / "flux2-klein"
    monkeypatch.setenv("THOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("THOT_FLUX_MODELS_DIR", str(explicit_models))

    assert paths.resolve_models_dir() == explicit_models.resolve()


def test_relative_models_path_resolves_from_app_root(monkeypatch, tmp_path):
    app_root = tmp_path / "portable"
    app_root.mkdir()
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(app_root))
    monkeypatch.setenv("THOT_FLUX_MODELS_DIR", "models/flux2-klein")

    assert paths.resolve_models_dir() == (app_root / "models" / "flux2-klein").resolve()


def test_data_models_are_discovered_without_source_file_root(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    expected = data_root / "models" / "flux2-klein"
    (expected / "tokenizer").mkdir(parents=True)
    (expected / "tokenizer" / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("THOT_DATA_DIR", str(data_root))
    monkeypatch.delenv("THOT_FLUX_MODELS_DIR", raising=False)

    assert paths.resolve_models_dir() == expected.resolve()
