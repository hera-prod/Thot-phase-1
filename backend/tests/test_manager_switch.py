def test_switch_vl_then_text_model_succeeds():
    from unittest.mock import MagicMock, patch
    from backend.inference.manager import ModelManager

    manager = ModelManager()
    # Mock Llama pour éviter VRAM réelle
    mock_llm = MagicMock()
    mock_llm.create_chat_completion.return_value = {"choices": [{"message": {"content": "hello"}}]}
    mock_llm.close = MagicMock()

    with patch("backend.inference.manager.Llama", return_value=mock_llm):
        with manager.use_model("LFM2.5-VL-3B") as vlm:
            assert vlm is not None
        # Après sortie du contexte, le modèle reste chaud — on force unload pour tester switch
        manager._unload_current()
        assert manager._current_model is None
        assert manager._current_chat_handler is None
        # Recharge 8B
        with manager.use_model("LFM2.5-8B") as llm:
            assert llm is not None
        assert manager._current_name == "LFM2.5-8B"
