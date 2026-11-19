"""Référence specs 01-02-03 — un seul fichier qui vérifie toutes les pipelines.

Sources :
- docs/contrats/01_architecture_registre_index.md (codes, constantes, index, gate, boucle)
- docs/contrats/02_outils.md (4 outils)
- docs/contrats/03_pipelines.md (ingestion)

Purs, sans modèle/GPU/DB (sauf scope, sans IO). Voir tests/backend/README.md.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from typing import cast


# ---------- 01 : table maîtresse de la grille (22 codes) ----------

def test_01_grille_codes_et_actions():
    """contrat commun §3.5 : la grille FERMÉE des codes d'erreur outils."""
    from backend.tools.errors import ERROR_ACTIONS, ToolErrorCode, error_text
    codes = {c.value for c in ToolErrorCode}
    assert codes == {
        # Forme de l'appel
        "EMPTY_CALL", "EMPTY_QUERY", "EMPTY_BRIEF", "MISSING_REQUIRED",
        "UNKNOWN_ARG", "ENUM_INVALID", "TOO_LONG", "TOO_MANY",
        # Références et volumes
        "ID_UNKNOWN", "ID_OUT_OF_SCOPE", "EVIDENCE_UNKNOWN",
        "TIMEOUT", "TOO_LARGE", "BAD_ARGS",
        # Disponibilité et ressources
        "RESOURCE_UNAVAILABLE", "UNSUPPORTED_CAPABILITY", "SERVICE_UNAVAILABLE",
        "MODEL_LOAD_FAILED", "OUT_OF_MEMORY", "BUDGET_EXHAUSTED",
        "CANCELLED", "INTERNAL_ERROR",
    }
    assert set(ERROR_ACTIONS.keys()) == set(ToolErrorCode)
    assert error_text(ToolErrorCode.EMPTY_CALL).startswith("[EMPTY_CALL]")
    assert "Reformule" in error_text(ToolErrorCode.EMPTY_CALL, "x")


# ---------- 01/03 : constantes centralisées (point unique) ----------

def test_01_constantes_spec():
    from backend.config.defaults import PDF_LAYOUT_MAX_PAGES_CONFIG, TOOL_LIMITS
    L = TOOL_LIMITS
    assert L["query_max_chars"] == 150  # dure ColBERT 32 tokens
    assert L["chunk_tokens"] == 500  # marge sous 511
    assert 10 <= L["chunk_overlap_pct"] <= 20
    assert L["queries_max"] == 3
    assert L["results_limit"] == 5
    assert L["questions_max"] == 4 and L["question_max_chars"] == 300
    assert L["artifact_ids_max"] == 3 and L["pairs_max"] == 6
    assert L["render_limit"] == 5
    assert L["ambiguity_gap"] == pytest.approx(0.05)
    assert (L["recent_days"], L["today_hours"]) == (7, 24)
    assert L["sheet_small_max_lines"] == 500 and L["sheet_small_max_sheets"] == 5
    assert 50 <= L["sheet_sample_chunks"] <= 100
    assert float(L["layout_confidence_threshold"]) == pytest.approx(0.3)
    assert (L["layout_input_size"], L["layout_batch_size"]) == (800, 8)
    assert 800 <= L["render_budget_tokens"] <= 1000
    assert PDF_LAYOUT_MAX_PAGES_CONFIG.get("max_num_input_imgs") is None


def test_01_boucle_valeurs():
    from backend.config import defaults as D
    assert (D.MAX_TOOL_CALLS, D.WARN_TOOL_CALLS) == (16, 14)
    assert (D.MAX_DUPLICATE_CALLS, D.DUPLICATE_WINDOW) == (4, 8)
    assert D.MAX_CONSECUTIVE_ERRORS == 4
    assert "3 appels" in D.WARN_MESSAGE


# ---------- 01 : boucle LoopGuard ----------

def test_01_loop_guard_warn_puis_plafond():
    from backend.inference.loop_guards import LoopGuard
    g = LoopGuard()
    for i in range(14):
        assert g.record(f"tool_{i}", {"q": str(i)}, ok=(i % 2 == 1)) is None
    first = g.warn_message()
    assert first is not None and "3 appels" in first
    assert g.warn_message() is None  # une seule fois
    for i in range(14, 16):
        g.record(f"tool_x{i}", {"q": "x"}, ok=(i % 2 == 1))
    assert g.should_force_brief() and g.metrics()["forced"] == "max_calls"


def test_01_loop_guard_duplicata_exact():
    from backend.inference.loop_guards import LoopGuard
    g = LoopGuard()
    last = None
    for _ in range(4):
        last = g.record("search_info", {"queries": ["budget"]}, ok=True)
    assert last == "duplicate"


def test_01_loop_guard_erreurs_consecutives_submit_exclu():
    from backend.inference.loop_guards import LoopGuard
    g = LoopGuard()
    # 3 échecs sur des outils DISTINCTS → force errors au 3e
    assert g.record("search_info", {"x": 1}, ok=False) is None
    assert g.record("find_files", {"x": 2}, ok=False) is None
    assert g.record("web_research", {"x": 3}, ok=False) == "errors"
    # submit_brief ne compte jamais (c'est la sortie)
    g2 = LoopGuard()
    g2.record("search_info", {"x": 1}, ok=False)
    g2.record("search_info", {"x": 2}, ok=False)
    g2.record("search_info", {"x": 3}, ok=False)
    g2.record("submit_brief", {}, ok=False)
    assert g2.consecutive_errors == 3
    g3 = LoopGuard()
    g3.record("search_info", {"x": 1}, ok=False)
    g3.record("search_info", {"x": 2}, ok=True)
    assert g3.consecutive_errors == 0


# ---------- 01 : gate à confiance (pas surface) ----------

def test_01_gate_confiance_pas_surface():
    from backend.ingestion.gate import check_pdf_config, filter_regions_by_confidence, gate_from_regions
    regions = [{"category": "table", "score": 0.9},
               {"category": "text", "score": 0.1},   # sous seuil → ignorée
               {"category": "figure", "score": 0.95}]
    kept = filter_regions_by_confidence(regions, 0.3)
    assert {r["category"] for r in kept} == {"table", "figure"}
    assert gate_from_regions(regions) == "table"
    assert gate_from_regions([{"category": "text", "score": 0.8}]) == "document"
    assert gate_from_regions([{"category": "figure", "score": 0.9}]) == "scene"
    assert gate_from_regions([{"category": "text", "score": 0.1}]) == "scene"
    check_pdf_config()  # lève si max_num_input_imgs != null


def test_03_gate_heuristiques_mots_cles():
    from backend.ingestion.gate import is_table_question, looks_tabular
    assert is_table_question("lis le chiffre dans le tableau") is True
    assert is_table_question("décris les couleurs de cette photo") is False
    assert looks_tabular("Date | Client | Montant\n2026 | Dubois | 45000\n2025 | Martin | 9000") is True
    assert looks_tabular("Le chiffre d'affaires du T2 s'élève à 45 000 euros.") is False


# ---------- 01 : périmètre ID_UNKNOWN vs ID_OUT_OF_SCOPE ----------

def test_01_perimetre_distinct():
    from backend.search.scope import check_artifact_scope
    assert check_artifact_scope("doc_1", {"doc_1"}) is None
    res_unknown = check_artifact_scope("doc_99", set(), exists=False)
    assert res_unknown is not None and res_unknown["code"] == "ID_UNKNOWN"
    res_scope = check_artifact_scope("doc_1", set(), exists=True)
    assert res_scope is not None and res_scope["code"] == "ID_OUT_OF_SCOPE"


# ---------- 03 : texte 500 tokens, phrases ----------

def test_03_chunk_500_tokens_phrases():
    from backend.search import chunks as C
    assert C.CHUNK_SIZE == 500
    assert C.estimate_tokens("a" * 400) == 100
    assert C.chunk_text("") == []
    # Fichier court → 1 chunk ; jamais coupé au milieu d'un mot
    one = C.chunk_text("Bonjour le monde.")
    assert len(one) == 1 and one[0] == "Bonjour le monde."
    long = C.chunk_text(("Ceci est une phrase complète. " * 400).strip())
    assert len(long) > 1
    for ch in long:
        assert C.estimate_tokens(ch) <= 500


# ---------- 03 : tableur, convergence unique ----------

def _tables() -> dict[str, list[list[object]]]:
    # cast : figures de test (str) vers le type cellule générique (object) des callees ;
    # l'invariance list[] interdit l'assignation implicite.
    return cast("dict[str, list[list[object]]]",
               {"Factures": [["Date", "Client", "Montant"],
                             ["2026-03-15", "Dubois SARL", "45000"],
                             ["2026-03-16", "Martin", "9000"]]})


def test_03_sheet_schema_et_caption():
    from backend.ingestion.sheets import extract_schema
    sch = extract_schema(_tables(), origin="natif")
    assert sch.sheets == ["Factures"]
    assert sch.headers["Factures"] == ["Date", "Client", "Montant"]
    assert sch.n_rows["Factures"] == 2 and sch.n_cols["Factures"] == 3
    assert "Date" in sch.caption and "Client" in sch.caption


def test_03_sheet_affiche_vs_embedde():
    from backend.ingestion.sheets import naturalize_row
    aff, emb = naturalize_row("Factures", ["Date", "Client"], ["2026-03-15", "Dubois SARL"])
    assert "=" in aff and "Feuille: Factures" in aff  # brut, rendu snippet
    assert emb.startswith("Ligne de la feuille Factures :")  # naturalisé, encodé ColBERT
    assert "=" not in emb


def test_03_sheet_petit_complet_grand_echantillonne():
    from backend.ingestion.sheets import build_chunks, extract_schema
    small = build_chunks(_tables(), extract_schema(_tables()))
    assert len(small) == 2
    big_rows = [["H1", "H2"]] + [[f"v{i}", str(i)] for i in range(600)]
    big = build_chunks(cast("dict[str, list[list[object]]]", {"S": big_rows}))
    assert 50 <= len(big) <= 100  # échantillon, jamais tout


def test_03_sheet_xlsx_bout_en_bout(tmp_path):
    """XLSX réel : openpyxl lit 2 feuilles → schéma → chunks (régression : moteur manquant = {} silencieux)."""
    _oxl = pytest.importorskip("openpyxl")
    from backend.ingestion.sheets import build_chunks, extract_schema, read_workbook_tables
    path = str(tmp_path / "factures.xlsx")
    wb = _oxl.Workbook()
    ws = wb.active
    ws.title = "Factures"
    ws.append(["Date", "Client", "Montant"])
    ws.append(["2026-03-15", "Dubois SARL", 45000])
    ws2 = wb.create_sheet("Notes")
    ws2.append(["A"])
    wb.save(path)
    tables = read_workbook_tables(path)
    assert sorted(tables) == ["Factures", "Notes"]
    assert tables["Factures"][1] == ["2026-03-15", "Dubois SARL", 45000]
    sch = extract_schema(tables)
    assert sch.n_rows == {"Factures": 1, "Notes": 0}
    assert len(build_chunks(tables, sch)) == 1


def test_03_sheet_types_csv_validation():
    import csv as _csv
    from backend.ingestion.sheets import infer_col_types, read_csv_tables, validate_table
    assert infer_col_types([["1"], ["2"], ["3"]]) == ["nombre"]
    assert infer_col_types([["a"], ["b"]]) == ["texte"]
    assert validate_table([["A", "B"], ["1", "2"], ["3", "4"]]) is True
    assert validate_table([["A"], ["1", "2", "3"]]) is False
    assert validate_table([["seul"]]) is False
    import tempfile as _tf, os as _os
    with _tf.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        _w = _csv.writer(f, delimiter=";")
        _w.writerow(["Date", "Montant"])
        _w.writerow(["2026-03-15", "45000"])
        path = f.name
    try:
        assert read_csv_tables(path)["Feuille 1"][0] == ["Date", "Montant"]
    finally:
        _os.unlink(path)


# ---------- 02 : search_info ----------

def test_02_search_info_validation_spec():
    from backend.tools.search_info import SearchInfoArgs
    with pytest.raises(ValidationError) as e:
        SearchInfoArgs(queries=[])
    assert "[EMPTY_QUERY]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        SearchInfoArgs(queries=["a", "b", "c", "d"])
    assert "[TOO_MANY]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        SearchInfoArgs(queries=["x" * 151])
    assert "[TOO_LONG]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        SearchInfoArgs(queries=["ok"], types=["video"])
    assert "[ENUM_INVALID]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        SearchInfoArgs(queries=["ok"], recency="hier")
    assert "[ENUM_INVALID]" in str(e.value)


def test_02_search_info_degenere_jamais_erreur():
    from backend.tools.search_info import SearchInfoArgs
    assert SearchInfoArgs(queries=["budget", "budget"]).queries == ["budget"]  # dédupliqué
    assert SearchInfoArgs(queries=["budget", "   "]).queries == ["budget"]  # vide retiré


def test_02_search_info_rendus():
    from backend.tools.search_info import render_search_info
    assert "aucun document" in render_search_info([], 0)
    assert "aucune correspondance" in render_search_info(
        [{"query": "x", "candidates": 0, "results": []}], 5)
    near = render_search_info(
        [{"query": "x", "candidates": 0, "results": [],
          "near_miss": "aucune image ne correspond. Par contre : 3 pdf."}], 5)
    assert "aucune image" in near
    txt = render_search_info(
        [{"query": "ca", "candidates": 2, "returned": 2, "results": [
            {"rank": 1, "artifact_id": "doc_42", "evidence_id": "doc_42:1",
             "name": "f.xlsx", "type": "sheet", "date": "2026-09-08",
             "score": 0.8, "snippet": "CA 45 000"},
            {"rank": 2, "artifact_id": "doc_38", "evidence_id": "doc_38:2",
             "name": "r.pdf", "type": "pdf", "date": "2026-09-05",
             "score": 0.7, "snippet": "CA 45k", "duplicate_of_query": 0}]}], 34)
    assert "doc_42:1" in txt and "déjà en recherche 1" in txt


# ---------- 02 : find_files ----------

def test_02_find_files_validation_spec():
    from backend.tools.find_files import FindFilesArgs
    with pytest.raises(ValidationError) as e:
        FindFilesArgs()
    assert "[EMPTY_QUERY]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        FindFilesArgs(query="x" * 151)
    assert "[TOO_LONG]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        FindFilesArgs(query="ok", types=["video"])
    assert "[ENUM_INVALID]" in str(e.value)
    assert FindFilesArgs(types=["pdf"], recency="recent").query is None  # tri date, sans texte


def test_02_find_files_rendus():
    from backend.tools.find_files import render_find_files
    assert "aucun fichier" in render_find_files([], 0, False, None, None)
    assert "aucun fichier ne correspond" in render_find_files([], 4, False, "facture mars", None)
    amb = render_find_files(
        [{"rank": 1, "artifact_id": "doc_42", "name": "f.xlsx", "type": "sheet",
          "date": "2026-09-08", "title": "Facture", "caption": "Dubois 45k"}],
        12, True, "facture", None)
    assert "Dubois 45k" in amb  # captions quand ambigu


def test_02_find_files_norme_nom_exact():
    from backend.tools.find_files import _norm_name
    assert _norm_name("Facture_Mars") == _norm_name("facture mars")
    # L'extension est ignorée au moment du boost (voir FindFilesTool : rsplit(".", 1)[0])
    assert _norm_name("Facture_Mars.pdf".rsplit(".", 1)[0]) == _norm_name("facture mars")


# ---------- 02 : ask_files ----------

def test_02_ask_files_validation_spec():
    from backend.tools.ask_files import AskFilesArgs
    with pytest.raises(ValidationError) as e:
        AskFilesArgs(questions=["   "], resource_ids=["doc_1"])
    assert "[EMPTY_QUERY]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        AskFilesArgs(questions=["a", "b", "c", "d", "e"], resource_ids=["doc_1"])
    assert "[TOO_MANY]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        AskFilesArgs(questions=["x" * 301], resource_ids=["doc_1"])
    assert "[TOO_LONG]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        AskFilesArgs(questions=["q"], resource_ids=[])
    assert "[MISSING_REQUIRED]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        AskFilesArgs(questions=["q"], resource_ids=["doc_1", "doc_2", "doc_3", "doc_4"])
    assert "[TOO_MANY]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        AskFilesArgs(questions=["a", "b", "c"], resource_ids=["doc_1", "doc_2", "doc_3"])  # 9 paires
    assert "[TOO_MANY]" in str(e.value)
    with pytest.raises(ValidationError) as e:
        AskFilesArgs(questions=["q"], resource_ids=["nope"])
    assert "[ID_UNKNOWN]" in str(e.value)
    # Déduplication silencieuse
    a = AskFilesArgs(questions=["q", "q"], resource_ids=["doc_1", "doc_1"])
    assert a.questions == ["q"] and a.resource_ids == ["doc_1"]


def test_02_ask_files_reponses_et_modes():
    from backend.tools.ask_files import _answer_one, render_ask_files

    class _Art:
        original_filename = "f.xlsx"
        display_name = "f.xlsx"

    assert _answer_one("couleurs ?", "doc_1", _Art(), "lignes...", "sheet")["answer"].startswith(
        "la question ne s'applique pas")
    assert _answer_one("ca ?", "doc_1", _Art(), "", "text")["answer"] == "information absente du document"
    got = _answer_one("chiffre affaires ?", "doc_42", _Art(),
                      "Le chiffre d'affaires du T2 est de 45 000 €. Autre phrase.", "text")
    assert "45 000" in got["answer"]
    # Pas de fragment persisté => pas de preuve : jamais un rang de phrase inventé.
    assert got["evidence_id"] is None
    proven = _answer_one("chiffre affaires ?", "doc_42", _Art(),
                         "Le chiffre d'affaires du T2 est de 45 000 €. Autre phrase.", "text",
                         evidence_ordinal=2)
    assert proven["evidence_id"] == "doc_42:2"
    # Matrice 2x2 + transversal 1x2
    mat = render_ask_files("matrix", [
        {"question": "q1", "by_file": [
            {"artifact_id": "doc_1", "name": "a", "answer": "45k", "evidence_id": "doc_1:0", "limits": ""}]},
        {"question": "q2", "by_file": [
            {"artifact_id": "doc_1", "name": "a", "answer": "information absente du document",
             "evidence_id": None, "limits": ""}]}])
    assert "mode matrice" in mat and "information absente" in mat
    tra = render_ask_files("transversal", [
        {"question": "lequel est max ?", "by_file": [
            {"artifact_id": "doc_1", "name": "a", "answer": "45k", "evidence_id": "doc_1:0", "limits": ""},
            {"artifact_id": "doc_2", "name": "b", "answer": "38k", "evidence_id": "doc_2:1", "limits": ""}]}])
    assert "mode transversal" in tra and "doc_1:0" in tra


# ---------- 02 : submit_brief ----------

def _good(**kw):
    base = {"brief": "Le CA T2 est de 45 000 €. Provenance : doc_42:0."}
    base.update(kw)
    return base


def test_02_brief_toute_la_signature_spec():
    """fiche submit_brief v1.2 : signature `{"brief"}`, obligatoire, non vide après
    trim (§2.2) ; les rubriques révoquées tombent en UNKNOWN_ARG (§2.3) ; un repli
    déterministe garantit la sortie (§4.1)."""
    from backend.inference.brief import build_fallback_brief, validate_submit_brief
    ok, code, _ = validate_submit_brief({})
    assert (ok, code) == (False, "EMPTY_CALL")
    # La seule clé admise est brief : toute autre clé (rubrique ancienne ou hubris)
    # est inconnue de la signature.
    ok, code, _ = validate_submit_brief({**_good(), "language": "fr"})
    assert (ok, code) == (False, "UNKNOWN_ARG")
    ok, code, _ = validate_submit_brief({**_good(), "facts": []})
    assert (ok, code) == (False, "UNKNOWN_ARG")
    ok, code, _ = validate_submit_brief(_good(brief="   "))
    assert (ok, code) == (False, "EMPTY_BRIEF")
    ok, code, norm = validate_submit_brief(_good(brief="  Le CA T2 est de 45 000 €.  "))
    assert (ok, code) == (True, "ok") and norm["brief"] == "Le CA T2 est de 45 000 €."
    ok2, code2, _ = validate_submit_brief(_good(brief=12))
    assert (ok2, code2) == (False, "BAD_ARGS")
    fb = build_fallback_brief()
    # fiche §4.2 : « Lorsque aucun résultat n'est utilisable, le brief le dit
    # explicitement » — jamais un repli muet.
    assert fb["brief"] and "n'a pas pu être préparé" in fb["brief"]
