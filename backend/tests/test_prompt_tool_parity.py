"""Parité prompt orchestrateur <-> outils (non-régression).

Garantit que le LLM reçoit VRAIMENT le mode d'emploi d'appel :
- chaque outil nommé dans SYSTEM_ORCHESTRATOR,
- chaque nom d'argument documenté (artifact_id, source_id, query...),
- la règle anti-hallucination (pas d'id/chemin inventé, pas de [vision(...)] textuel),
- les schémas JSON exposés au LLM contiennent artifact_id/source_id.
"""
from __future__ import annotations

from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR
from backend.tools.document_card import GetDocumentCardArgs, GetDocumentCardTool
from backend.tools.get_source import GetSourceArgs, GetSourceTool
from backend.tools.read_document import ReadDocumentArgs, ReadDocumentTool
from backend.tools.read_text import ReadTextArgs, ReadTextTool
from backend.tools.registry import get_default_registry
from backend.tools.search_documents import SearchDocumentsArgs, SearchDocumentsTool
from backend.tools.vision_general import VisionGeneralArgs, VisionGeneralTool
from backend.tools.vision_ocr import VisionOCRArgs, VisionOCRTool


def test_prompt_nomme_les_4_outils():
    for name in ("vision", "doc_ocr", "get_source", "read_text"):
        assert name in SYSTEM_ORCHESTRATOR, f"outil {name} absent du prompt"


def test_prompt_nomme_outils_documentaires():
    # Annexe 11 Lots A/B : découverte + fiche + lecture localisée dans le mode d'emploi
    for name in ("search_documents", "get_document_card", "read_document"):
        assert name in SYSTEM_ORCHESTRATOR, f"outil {name} absent du prompt"
    assert "evidence_id" in SYSTEM_ORCHESTRATOR
    assert "donnée non fiable" in SYSTEM_ORCHESTRATOR


def test_prompt_documente_les_arguments():
    for arg in ("artifact_id", "source_id", "query", "reason", "purpose"):
        assert arg in SYSTEM_ORCHESTRATOR, f"argument {arg} absent du prompt"


def test_prompt_interdit_hallucination_et_textuel():
    assert "N'écris JAMAIS [vision(...)]" in SYSTEM_ORCHESTRATOR
    assert "n'invente JAMAIS un artifact_id" in SYSTEM_ORCHESTRATOR


def test_prompt_mentionne_list_all_et_registre():
    assert "SOURCES" in SYSTEM_ORCHESTRATOR
    assert "SANS source_id" in SYSTEM_ORCHESTRATOR


def test_schemas_exposent_les_ids():
    assert "artifact_id" in VisionGeneralArgs.model_json_schema()["properties"]
    assert "artifact_id" in VisionOCRArgs.model_json_schema()["properties"]
    assert "source_id" in GetSourceArgs.model_json_schema()["properties"]
    assert "source_id" in ReadTextArgs.model_json_schema()["properties"]
    assert "artifact_id" in GetDocumentCardArgs.model_json_schema()["properties"]
    assert "artifact_id" in ReadDocumentArgs.model_json_schema()["properties"]
    assert ReadDocumentArgs.model_json_schema()["properties"]["max_chars"]["maximum"] == 16000


def test_get_source_id_optionnel_pour_list_all():
    assert not GetSourceArgs.model_json_schema().get("required") or \
        "source_id" not in GetSourceArgs.model_json_schema().get("required", [])


def test_descriptions_outils_mentionnent_ids():
    assert "artifact_id" in VisionGeneralTool().description
    assert "artifact_id" in VisionOCRTool().description
    assert "source_id" in GetSourceTool().description
    assert "source_id" in ReadTextTool().description


def test_registre_4_outils():
    # Annexe 11 Lots A/B : 7 outils (4 socle + search_documents + get_document_card + read_document)
    assert set(get_default_registry().tool_names()) == {"vision", "doc_ocr", "get_source", "read_text", "search_documents", "get_document_card", "read_document"}
