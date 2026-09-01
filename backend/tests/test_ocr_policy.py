from backend.prompts.ocr import build_system_ocr, resolve_ocr_task
from backend.inference.loop import resolve_allowed_tools

def test_build_system_ocr_text():
    assert build_system_ocr(task="text") == "Text Recognition:" or "Text Recognition:" in build_system_ocr(task="text")

def test_build_system_ocr_table():
    assert "Table Recognition:" in build_system_ocr(task="table")

def test_build_system_ocr_formula():
    assert "Formula Recognition:" in build_system_ocr(task="formula")

def test_image_description_allows_vision_only():
    tools = resolve_allowed_tools("Décris cette image", ["/tmp/thot/x/img.png"])
    assert tools == ["vision"]

def test_image_text_request_allows_ocr_only():
    tools = resolve_allowed_tools("Lis le texte de cette affiche", ["/tmp/thot/x/img.png"])
    assert tools == ["doc_ocr"]

def test_pdf_allows_ocr_only():
    tools = resolve_allowed_tools("Résume ce PDF", ["/tmp/thot/x/doc.pdf"])
    assert tools == ["doc_ocr"]

def test_no_file_no_tool():
    tools = resolve_allowed_tools("Explique les MoE", [])
    assert tools == []

def test_resolve_ocr_task():
    assert resolve_ocr_task("extrais le tableau") == "table"
    assert resolve_ocr_task("formule math") == "formula"
    assert resolve_ocr_task("bonjour") == "text"
