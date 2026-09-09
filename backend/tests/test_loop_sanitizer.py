# Test sanitizer backend (ticket llama.cpp #23852) — régression conv.png
# Vérifie que la fuite <think> + [vision(...)] textuel ne pollue pas DB/history
from backend.inference.loop import _sanitize_content, _detect_textual_tool_calls, _strip_textual_tool_calls


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
    assert "The user asks" in thinking
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
