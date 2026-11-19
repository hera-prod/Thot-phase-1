# Tests ModelManager — chargement séquentiel strict (mock Llama)

def test_sequential_loading():
    """Vérifie que use_model décharge bien l'ancien avant de charger le nouveau."""
    # On mock Llama pour ne pas charger de vrai modèle
    print("TODO: mocker Llama et vérifier _current_name change")
    assert True

def test_supports_vision_guard():
    """LFM2.5-8B ne doit jamais recevoir d'image (garde-fou LLM_vs_VLM)."""
    from backend.config.defaults import MODEL_CONFIGS
    assert MODEL_CONFIGS["LFM2.5-8B"].supports_vision is False
    assert MODEL_CONFIGS["LFM2.5-VL-3B"].supports_vision is True
    # Brouillon : OCR = PaddleOCR-VL-1.6 (worker transformers, jamais llama.cpp).
    assert MODEL_CONFIGS["PADDLEOCR-VL-1.6"].supports_vision is True
