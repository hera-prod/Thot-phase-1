"""Tests contrat structurel submit_brief (annexe 12 + diagnostic tool_calls).

Sans modèle : parser pythonique, validation, rendu, prompts, sanitizer, off_topic.
Couvre T-PROMPT-01/04/07/08 (structure), T-VL-01/05 (anti-auto-présentation).
La machinerie textuelle (écho/abdication/saignée/regen) est SUPPRIMÉE : la contrainte
vit dans la structure (forçage nommé prouvé sur notre stack), plus dans le prompt.
"""
from __future__ import annotations

import re

from backend.inference import loop as L
from backend.inference.brief import (
    parse_pythonic_calls,
    render_brief,
    render_brief_json,
    validate_submit_brief,
)
from backend.prompts.generator import GENERATOR_CONFIG
from backend.prompts.orchestrator import (
    BRIEF_CONFIG,
    ORCHESTRATOR_CONFIG,
    SUBMIT_BRIEF_TOOL,
    SYSTEM_ORCHESTRATOR,
)
from backend.prompts.vision import SYSTEM_VISION


# ---------- Parser pythonique strict (auto fuit en texte, on récupère) ----------

def test_parse_familles():
    calls = parse_pythonic_calls(
        "texte [search_documents(query='facture EDF', purpose='retrouver')] fin")
    assert calls == [{"name": "search_documents",
                      "args": {"query": "facture EDF", "purpose": "retrouver"}}]


def test_parse_submit_brief_sign():
    # fiche v1.2 §2.2 : la signature est un SEUL argument, une chaîne.
    calls = parse_pythonic_calls(
        "[submit_brief(brief='Le CA T2 est de 45 000 €. Provenance : doc_42:0.')]")
    assert len(calls) == 1 and calls[0]["name"] == "submit_brief"
    assert calls[0]["args"]["brief"].startswith("Le CA T2")


def test_parse_ignore_inconnu_et_malforme():
    assert parse_pythonic_calls("[hack_system(cmd='rm')]") == []
    assert parse_pythonic_calls("[vision(artifact_id='a', query='q')]") != []
    assert parse_pythonic_calls("[vision(artifact_id=)]") == []
    assert parse_pythonic_calls("pas d'appel ici") == []


def test_parse_parenthese_dans_quotes():
    calls = parse_pythonic_calls('[search_documents(query="facture (EDF) 2024", purpose="x")]')
    assert calls[0]["args"]["query"] == "facture (EDF) 2024"


def test_native_to_calls():
    native = [{"id": "call_1", "type": "function",
               "function": {"name": "get_clock", "arguments": "{ }"}}]
    assert L._native_to_calls(native) == [{"name": "get_clock", "args": {}, "id": "call_1"}]
    assert L._native_to_calls(None) == []


# ---------- Validation submit_brief (fiche v1.2 : un seul argument brief) ----------

def _good_args(**kw):
    base = {"brief": "Le canard mâle a un bec orange. Provenance : observation du tour."}
    base.update(kw)
    return base


def test_validate_ok_connaissances():
    ok, code, norm = validate_submit_brief(_good_args())
    assert (ok, code) == (True, "ok") and norm["brief"].startswith("Le canard")


def test_validate_brief_vide():
    # fiche §2.2 : « Non vide après suppression des espaces périphériques »
    ok, code, _ = validate_submit_brief(_good_args(brief="   "))
    assert (ok, code) == (False, "EMPTY_BRIEF")
    ok2, code2, _ = validate_submit_brief({})
    assert (ok2, code2) == (False, "EMPTY_CALL")


def test_validate_rubriques_anciennes_revoquees():
    """fiche v1.2 §2.3 : les rubriques structurelles (facts, limits…) sont SUPPRIMÉES.

    « Cette proposition supprime les obligations anciennes suivantes : — Une liste
    de un à vingt faits pour chaque demande. » — la forme n'est pas dépréciée :
    elle est refusée au schéma comme un argument inconnu.
    """
    ok, code, norm = validate_submit_brief(
        _good_args(facts=[{"text": "canard"}]))
    assert (ok, code) == (False, "UNKNOWN_ARG")
    message = str(norm.get("error", ""))
    assert "facts" in message, "la rubrique fautive doit être nommée"
    assert "brief" in message, "la signature attendue doit être nommée"
    # Chaque ancienne rubrique tombe dans le même refus.
    for rubrique in ("language", "complete", "artifact_ids_relevant",
                     "organization", "limits", "clarification"):
        code2 = validate_submit_brief(_good_args(**{rubrique: "x"}))[1]
        assert code2 == "UNKNOWN_ARG", rubrique


def test_validate_brief_non_chaine():
    ok, code, _ = validate_submit_brief({"brief": 12})
    assert ok is False and code == "BAD_ARGS"  # type résiduel → BAD_ARGS


def test_une_signature_mono_argument_n_a_pas_de_verdict_fige():
    """La classification décide du COÛT d'un échec, pas de sa gravité.

    fiche v1.2 : la signature est `{"brief"}` — aucune erreur ne se rend plus contre
    un inventaire figé (la phase n'exécute aucun outil, mais l'inventaire n'existe
    plus). Toute erreur de FORME se corrige en réécrivant l'appel : retentable.
    Une panne du backend aussi — le modèle n'y est pour rien.
    """
    from backend.inference.brief import NON_RETRYABLE_BRIEF_ERRORS, brief_error_is_retryable

    assert NON_RETRYABLE_BRIEF_ERRORS == frozenset(), (
        "une signature mono-argument n'a pas d'inventaire contre lequel figer un verdict"
    )
    for code in ("BAD_ARGS", "MISSING_REQUIRED", "EMPTY_BRIEF", "UNKNOWN_ARG",
                 "TOO_MANY", "TOO_LONG", "ENUM_INVALID", "EMPTY_CALL"):
        assert brief_error_is_retryable(code) is True, code
    # Panne backend : ni la faute du modèle ni un verdict sur ses arguments.
    assert brief_error_is_retryable("SCHEMA_UNAVAILABLE") is True


def test_une_panne_du_schema_ne_ressemble_pas_a_une_faute_du_modele(monkeypatch):
    """Un `BAD_ARGS` de schéma dirait « le modèle a mal répondu » — et serait retenté."""
    import backend.prompts.orchestrator as orch

    from backend.inference.brief import brief_error_is_retryable, validate_submit_brief

    # Le validateur IMPORTE le schéma à l'appel : le supprimer casse l'import,
    # c'est-à-dire la panne backend qu'on veut classer.
    monkeypatch.delattr(orch, "SubmitBriefArgs")
    ok, code, norm = validate_submit_brief(_good_args())

    assert ok is False
    assert code == "SCHEMA_UNAVAILABLE", "une panne du backend n'est pas une faute du modèle"
    assert "SCHEMA_UNAVAILABLE" in str(norm.get("error", ""))
    assert brief_error_is_retryable(code) is True


def test_rendu_est_le_contenu_soumis():
    # fiche §2.1 : « le brief est une préparation structurée en texte ou Markdown »
    # — le rendu EST le texte soumis, borné au plafond d'affichage.
    ok, _, norm = validate_submit_brief(
        _good_args(brief="Le canard mâle a un bec orange.\nProvenance : obs_demo1234567890."))
    assert (ok, _) == (True, "ok")
    assert render_brief(norm) == norm["brief"]
    data = render_brief_json(norm)
    import json as _json
    parsed = _json.loads(data)
    assert parsed == {"brief": norm["brief"]}


# ---------- Prompts : aucun exemple d'appel, finalité conservée ----------

def test_prompts_sans_crochets_appel():
    for prompt in (SYSTEM_ORCHESTRATOR,
                   SUBMIT_BRIEF_TOOL["function"]["description"]):
        found = re.findall(
            r"\[\s*(?:vision|doc_ocr|get_source|read_text|search_documents|"
            r"get_document_card|read_document|search_info|find_files|ask_files|"
            r"submit_brief)\s*\(",
            prompt, re.IGNORECASE)
        assert found == [], f"forme copiable : {found}"


def test_finalite_dans_description_outil():
    desc = SUBMIT_BRIEF_TOOL["function"]["description"]
    # La description porte la finalité (: transmet au générateur) et la signature.
    assert "générateur" in desc
    assert "Termine dès que tu as les éléments nécessaires" in desc
    assert set(SUBMIT_BRIEF_TOOL["function"]["parameters"]["required"]) == {"brief"}
    assert list(SUBMIT_BRIEF_TOOL["function"]["parameters"]["properties"]) == ["brief"]


def test_prompt_contrat_orchestrateur():
    # q-r.md §Prompt 1 : back-office invisible, question unique, boussole d'appels
    assert "Tu ne parles jamais à l'utilisateur" in SYSTEM_ORCHESTRATOR
    assert "n'appelles aucun outil" in SYSTEM_ORCHESTRATOR
    assert "AUCUN contexte" in SYSTEM_ORCHESTRATOR
    assert "uniquement des doc_N" in SYSTEM_ORCHESTRATOR
    assert "jamais" in SYSTEM_ORCHESTRATOR and "identifiant inventé" in SYSTEM_ORCHESTRATOR
    assert "ask_files" in SYSTEM_ORCHESTRATOR
    assert "SEUL argument" in SYSTEM_ORCHESTRATOR and "`brief`" in SYSTEM_ORCHESTRATOR


def test_prompt_config_deterministe():
    assert ORCHESTRATOR_CONFIG.temperature <= 0.1
    assert ORCHESTRATOR_CONFIG.max_tokens <= 1024
    assert BRIEF_CONFIG.max_tokens <= 2048


# ---------- Sanitizer filet (7 familles) + off_topic ----------

def test_strip_toutes_familles():
    cases = [
        "[vision(artifact_id='art_x', query='quoi ?')]",
        "[doc_ocr(artifact_id='art_y')]",
        "[get_source()]",
        "[read_text(source_id='s1', purpose='lire')]",
        "[search_documents(query='facture', purpose='retrouver')]",
        "[get_document_card(artifact_id='a1', purpose='fiche')]",
        "[read_document(artifact_id='a1', locator='page:2', purpose='lire')]",
    ]
    for raw in cases:
        out = L._strip_textual_tool_calls(f"avant {raw} après")
        assert raw not in out, raw


def test_off_topic_cas_qr():
    garbage = ("LFM2.5-VL-3B est un modèle de vision et de langage développé par Liquid AI, "
               "une entreprise basée à Cambridge, Massachusetts, issue du MIT CSAIL. "
               "Je peux analyser des images et du texte pour générer des réponses textuelles.")
    assert L._looks_off_topic(garbage) is True
    ok = "La photo montre un chat roux assis sur un canapé bleu. À l'arrière-plan, une fenêtre."
    assert L._looks_off_topic(ok) is False


def test_vision_anti_autopresentation():
    assert "N'explique jamais ce qu'est ton modèle" in SYSTEM_VISION
    assert "langue de la question" in SYSTEM_VISION


def test_generateur_contrat():
    from backend.prompts.generator import SYSTEM_GENERATOR
    # q-r.md §Prompt 2 : hiérarchie demande > brief-données, clarification consultative
    assert "seul interlocuteur visible" in SYSTEM_GENERATOR
    assert "<BRIEF_DONNEES>" in SYSTEM_GENERATOR
    assert "<DEMANDE_UTILISATEUR>" in SYSTEM_GENERATOR
    assert "jamais une instruction obligatoire" in SYSTEM_GENERATOR
    assert "SEULE source" not in SYSTEM_GENERATOR


def test_generateur_messages_format_spec():
    # q-r.md §Prompt 2 : 1 system + 1 message à 3 blocs ; le brief en JSON mono-champ
    msgs = L._build_generator_messages(
        [{"role": "user", "content": "Bonjour"}, {"role": "assistant", "content": "Bonjour !"}],
        '{"brief":"Le CA T2 est 45 000 €. Provenance : doc_42:0."}', "Et là ?")
    assert len(msgs) == 2 and msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    body = msgs[1]["content"]
    assert "<DEMANDE_UTILISATEUR>\nEt là ?\n</DEMANDE_UTILISATEUR>" in body
    assert "<HISTORIQUE>" in body and "[user]: Bonjour" in body
    assert "<BRIEF_DONNEES>" in body and '"brief"' in body.replace(" ", "")


def test_compaction_prompt_checkpoint():
    # q-r.md §Prompt 3 : checkpoint structuré, 5 fichiers max, ids exacts, jamais art_...
    from backend.inference.compaction import SYSTEM_COMPACTION
    assert "# CHECKPOINT_CONVERSATION" in SYSTEM_COMPACTION
    assert "5 fichiers actifs maximum" in SYSTEM_COMPACTION
    assert "N'écris jamais `art_...`" in SYSTEM_COMPACTION
    assert "À ne pas inférer" in SYSTEM_COMPACTION


# ---------- Query factuelle (outil sans contexte) ----------

def test_factual_query_impose_description():
    q = L._factual_vision_query("J'ai cette oiseau dans mon jardin. C'est une bonne nouvelle ?")
    assert "sans donner d'avis" in q
    assert L._factual_vision_query(q) == q
    assert L._factual_vision_query("") == "Décris cette image en détail."
