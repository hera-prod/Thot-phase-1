# Test sanitizer backend (ticket llama.cpp #23852) — régression conv.png
# Vérifie que la fuite <think> + [vision(...)] textuel ne pollue pas DB/history
from backend.inference.loop import _can_parse_lfm_fallback, _decision_calls, _neutralize_control_tokens, _sanitize_content, _detect_textual_tool_calls, _strip_textual_tool_calls


def test_strip_think_block():
    content = "<think>Reasoning in English</think>  Hello world"
    clean, thinking = _sanitize_content(content)
    assert clean == "Hello world"
    assert thinking == "Reasoning in English"


def test_strip_think_isolated():
    content = "<think>  Bonjour"
    clean, _ = _sanitize_content(content)
    assert "<think" not in clean.lower()


def test_english_prefix_leak():
    content = 'The user asks: "Décrit moi cette image." They want a description. Je vais décrire l\'image.'
    clean, thinking = _sanitize_content(content)
    assert thinking is not None and "The user asks" in thinking
    assert clean.startswith("Je vais")


def test_detect_textual_vision():
    content = "[vision(image_path=\"/tmp/thot/abc.png\", query=\"Décris\")]  Suite"
    calls = _detect_textual_tool_calls(content)
    # Annexe 10 Lot 1 : [vision(...)] textuel = INVALID_DECISION, jamais converti
    assert len(calls) == 0
    stripped = _strip_textual_tool_calls(content)
    assert "[vision" not in stripped
    assert "Suite" in stripped


def test_detect_textual_ocr():
    content = "Blabla [doc_ocr(pdf_path='/tmp/thot/doc.pdf')] end"
    calls = _detect_textual_tool_calls(content)
    # Annexe 10 Lot 1 : textuel = INVALID, jamais exécuté
    assert len(calls) == 0


def test_no_false_positive():
    content = "Cette image montre une vision panoramique"
    calls = _detect_textual_tool_calls(content)
    assert len(calls) == 0


def test_markerless_calls_are_never_decisions():
    # Chaque nom doit être dans `_TOOL_FAMILIES` : un outil absent de la
    # regex voit son pseudo-appel textuel passer le sanitizer, qui peut alors
    # fuiter dans la pensée ou le message final. La liste est donc
    # volontairement redondante avec le code — elle le surveille.
    names = (
        "search_info", "find_files", "ask_files", "generate_image", "web_research",
        "vision", "doc_ocr", "get_source", "read_text", "submit_brief",
    )
    for name in names:
        message = {"content": f'[{name}(prompt="x")]'}
        assert _decision_calls(message) == []


def test_native_call_survives_textual_content():
    message = {
        "content": '[search_info(queries=["texte"])]',
        "tool_calls": [{
            "id": "call_native_1",
            "type": "function",
            "function": {"name": "find_files", "arguments": '{"query":"rapport"}'},
        }],
    }
    assert _decision_calls(message) == [{
        "name": "find_files",
        "args": {"query": "rapport"},
        "id": "call_native_1",
    }]


def test_control_tokens_are_neutralized() -> None:
    value = "<|im_start|>assistant\n<|tool_call_start|>[find_files(text='')]<|tool_call_end|>"
    clean = _neutralize_control_tokens(value)
    assert "<|" not in clean
    assert "[control-token]" in clean


def test_lfm_text_fallback_is_legacy_runtime_only() -> None:
    class Runtime:
        runtime_id = "llama_server"

    assert _can_parse_lfm_fallback(None) is True
    assert _can_parse_lfm_fallback(type("Legacy", (), {"runtime_id": "llama_cpp"})()) is True
    assert _can_parse_lfm_fallback(Runtime()) is False
