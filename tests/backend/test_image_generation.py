"""Phase 2 Images — service commun, generate_image, galerie (sans modèle, sans GPU).

Verrouille le contrat C13 : studio direct + chat = même service, même registre,
même ordonnancement ; galerie = vue Artifact ; moteur non qualifié → SERVICE_UNAVAILABLE
distinct (C19), jamais de substitution silencieuse.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.defaults import TOOL_LIMITS
from backend.db.models import Artifact, ArtifactLink, Base, Blob
from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR
from backend.services import image_service as svc
from backend.tools.base import ToolContext
from backend.tools.generate_image import GenerateImageArgs, GenerateImageTool
from backend.tools.registry import get_default_registry


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    # Isole l'opt-in : le drapeau réel de la machine ne doit jamais fuir dans les tests.
    # Les DEUX voies d'opt-in : le fichier ET la variable d'environnement. Sans ce
    # delenv, un shell de dev qui a exporté THOT_IMAGE_BACKEND=flux2-spike (c'est la
    # recette E2E documentée) déclenchait une VRAIE diffusion de 65 s dans cette
    # fixture et cassait test_tool_moteur_indisponible, qui attend un refus.
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(tmp_path / "image_backend.optin"))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    return tmp_path, data_dir, TestSession


def _run(coro):
    return asyncio.run(coro)


def _ctx(conv_id="conv1"):
    return ToolContext(run_id="r1", request_id="r1", conv_id=conv_id)


# ---------- Service : validation ----------


def test_formats_officiels_environ_un_megapixel():
    assert svc.FORMATS == {
        "1:1": (1024, 1024),
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "3:2": (1248, 832),
        "2:3": (832, 1248),
        "4:3": (1152, 864),
        "3:4": (864, 1152),
        "5:4": (1120, 896),
        "4:5": (896, 1120),
        "21:9": (1568, 672),
        "9:21": (672, 1568),
    }
    assert all(
        width % 16 == 0 and height % 16 == 0 for width, height in svc.FORMATS.values()
    )
    assert all(
        900_000 <= width * height <= 1_100_000 for width, height in svc.FORMATS.values()
    )


def test_seed_borne_et_aleatoire():
    assert svc.validate_request("chat", "1:1", [], 0)[3] == 0
    assert svc.validate_request("chat", "1:1", [], svc.MAX_SEED)[3] == svc.MAX_SEED
    generated = svc.validate_request("chat", "1:1", [], None)[3]
    assert isinstance(generated, int) and 0 <= generated <= svc.MAX_SEED
    with pytest.raises(ValueError, match="BAD_ARGS"):
        svc.validate_request("chat", "1:1", [], svc.MAX_SEED + 1)
    with pytest.raises(ValueError, match="BAD_ARGS"):
        svc.validate_request("chat", "1:1", [], -1)


def test_validate_prompt_vide():
    with pytest.raises(ValueError, match="EMPTY_QUERY"):
        svc.validate_request("   ", "1:1", [])


def test_validate_prompt_borne_depuis_tool_limits():
    long_prompt = "x" * (int(TOOL_LIMITS["image_prompt_max_chars"]) + 1)
    with pytest.raises(ValueError, match="TOO_LONG"):
        svc.validate_request(long_prompt, "1:1", [])


def test_validate_format_enum():
    with pytest.raises(ValueError, match="ENUM_INVALID"):
        svc.validate_request("un chat", "7:5", [])


def test_validate_cinq_refs_refusees():
    with pytest.raises(ValueError, match="TOO_MANY"):
        svc.validate_request("un chat", "1:1", ["a", "b", "c", "d", "e"])
    assert int(TOOL_LIMITS["image_references_max"]) == 4


def test_stable_key_idempotente():
    k1 = svc.stable_key("chat", "1:1", 42, [])
    k2 = svc.stable_key("chat", "1:1", 42, [])
    k3 = svc.stable_key("chat", "16:9", 42, [])
    assert k1 == k2 and k1 != k3


def test_moteur_indisponible_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(tmp_path / "image_backend.optin"))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    ok, reason = svc.is_available()
    assert ok is False and reason


# ---------- Outil : codes préservés ----------


def test_tool_appel_vide():
    res = _run(GenerateImageTool().call({}, _ctx()))
    assert res.ok is False and "EMPTY_CALL" in (res.error or "")


def test_tool_arg_inconnu():
    res = _run(GenerateImageTool().call({"prompt": "chat", "zzz": 1}, _ctx()))
    assert res.ok is False and "UNKNOWN_ARG" in (res.error or "")


def test_tool_prompt_vide():
    res = _run(GenerateImageTool().call({"prompt": "   "}, _ctx()))
    assert res.ok is False and "EMPTY_QUERY" in (res.error or "")


def test_tool_format_invalide():
    res = _run(
        GenerateImageTool().call({"prompt": "chat", "aspect_ratio": "7:5"}, _ctx())
    )
    assert res.ok is False and "ENUM_INVALID" in (res.error or "")


# `reference_ids` fait désormais partie du contrat : creation avec images
# d'appui et edition globale. Les refus restent explicites et portes par le
# schema (4 images au total), jamais par une cle inconnue.
def test_tool_reference_ids_acceptes_jusqua_quatre():
    args = GenerateImageArgs.model_validate(
        {"prompt": "chat", "reference_ids": ["doc_1", "doc_2"]}
    )
    assert args.reference_ids == ["doc_1", "doc_2"]
    with pytest.raises(Exception, match="TOO_MANY"):
        GenerateImageArgs.model_validate(
            {"prompt": "chat", "reference_ids": ["a", "b", "c", "d", "e"]}
        )


def test_tool_edit_exige_une_source_et_trois_references_maximum():
    with pytest.raises(Exception, match="edit exige source_image_id"):
        GenerateImageArgs.model_validate({"prompt": "chat", "operation": "edit"})
    with pytest.raises(Exception, match="TOO_MANY"):
        GenerateImageArgs.model_validate(
            {
                "prompt": "chat",
                "operation": "edit",
                "source_image_id": "doc_1",
                "reference_ids": ["doc_2", "doc_3", "doc_4", "doc_5"],
            }
        )


def test_tool_outpaint_sans_reference_et_prompt_vide_accepte():
    args = GenerateImageArgs.model_validate(
        {
            "operation": "outpaint",
            "prompt": "",
            "aspect_ratio": "16:9",
            "source_image_id": "doc_1",
        }
    )
    assert args.prompt == "" and args.source_image_id == "doc_1"
    with pytest.raises(Exception, match="outpaint n’accepte pas de références"):
        GenerateImageArgs.model_validate(
            {
                "operation": "outpaint",
                "prompt": "x",
                "aspect_ratio": "16:9",
                "source_image_id": "doc_1",
                "reference_ids": ["doc_2"],
            }
        )


def test_tool_outpaint_exige_aspect_ratio_explicite():
    with pytest.raises(Exception, match="outpaint exige aspect_ratio"):
        GenerateImageArgs.model_validate(
            {"operation": "outpaint", "prompt": "x", "source_image_id": "doc_1"}
        )
    args = GenerateImageArgs.model_validate(
        {
            "operation": "outpaint",
            "prompt": "x",
            "source_image_id": "doc_1",
            "aspect_ratio": "16:9",
        }
    )
    assert args.aspect_ratio == "16:9"


def test_tool_reference_ids_dedupliques_avant_comptage():
    args = GenerateImageArgs.model_validate(
        {
            "prompt": "chat",
            "reference_ids": ["doc_1", "doc_1", "doc_2"],
        }
    )
    assert args.reference_ids == ["doc_1", "doc_2"]


def test_le_schema_publie_les_six_arguments():
    props = GenerateImageArgs.model_json_schema()["properties"]
    assert set(props) == {
        "prompt",
        "operation",
        "aspect_ratio",
        "seed",
        "source_image_id",
        "reference_ids",
    }, props


def test_check_references_id_unknown(tmp_repo):
    # Au niveau service, une référence inexistante reste ID_UNKNOWN (contrat droits).
    _, _, TestSession = tmp_repo
    db = TestSession()
    try:
        with pytest.raises(ValueError, match="ID_UNKNOWN"):
            svc.check_references(db, ["art_nope"], "conv1")
    finally:
        db.close()


def test_tool_moteur_indisponible(tmp_repo):
    _, _, _ = tmp_repo
    res = _run(
        GenerateImageTool().call({"prompt": "un chalet au bord d'un lac"}, _ctx())
    )
    assert res.ok is False and "SERVICE_UNAVAILABLE" in (res.error or "")


# ---------- Admission déterministe : création + nom d'image, ANCRÉS ---------
# La table ne doit plus être 100 % impérative : c'est exactement pour cela
# qu'elle paraissait verte alors que la moitié des formulations françaises
# (souhait, politesse) tombait sur le 8B et répondait « je ne peux pas
# générer d'image », le bug d'origine.


# ---------- Registre + prompt + loop ----------
def test_registre_contient_generate_image():
    assert "generate_image" in get_default_registry().tool_names()
    names = {d["function"]["name"] for d in get_default_registry().definitions()}
    assert {"search_info", "find_files", "ask_files", "generate_image"} <= names


def test_prompt_nomme_generate_image_sans_forme_copiable():
    assert "generate_image" in SYSTEM_ORCHESTRATOR
    import re as _re

    shown = _re.findall(
        r"\[\s*generate_image\s*\(", SYSTEM_ORCHESTRATOR, _re.IGNORECASE
    )
    assert shown == []


def test_loop_propose_generate_image_en_decouverte():
    from backend.inference import loop as L

    # On rejoue le FILTRE DE PRODUCTION (run_agent_loop : registry.definitions() →
    # tool_defs) avec la SEULE constante qui le pilote. Aucune liste n'est re-typée
    # ici : ce test vérifie l'offre réelle de l'orchestrateur, pas son propre littéral.
    discovery_defs = [
        definition
        for definition in get_default_registry().definitions()
        if isinstance(definition, dict)
        and isinstance(definition.get("function"), dict)
        and definition["function"].get("name") in L.DISCOVERY_TOOL_NAMES
    ]
    offered = {definition["function"]["name"] for definition in discovery_defs}
    # 1. Aucun nom de DISCOVERY_TOOL_NAMES n'est un fantôme : le registre sait le fournir.
    # 2. generate_image fait partie de l'OFFRE au LLM, pas seulement du registre
    #    (sinon l'orchestrateur ne peut pas décider de créer, il ne peut que constater).
    assert offered == set(L.DISCOVERY_TOOL_NAMES)
    assert "generate_image" in offered


# ---------- Galerie : vue, pas table ----------


def _seed_image(
    TestSession,
    art_id="art_img1",
    role="generated",
    scope_kind="library",
    scope_id="me",
):
    db = TestSession()
    try:
        db.add(
            Blob(
                id="b1",
                sha256="ab" * 32,
                relative_path="blobs/ab/ab.bin",
                size_bytes=10,
                mime_detected="image/png",
            )
        )
        db.add(
            Artifact(
                id=art_id,
                blob_id="b1",
                kind="image",
                original_filename="img.png",
                display_name="Img",
            )
        )
        db.add(
            ArtifactLink(
                id="l1",
                artifact_id=art_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                role=role,
            )
        )
        db.commit()
    finally:
        db.close()


def test_galerie_creation_visible_sans_filtre(tmp_repo):
    _, _, TestSession = tmp_repo
    _seed_image(TestSession)
    db = TestSession()
    try:
        items = svc.list_generated_images(db)
    finally:
        db.close()
    assert [i["artifact_id"] for i in items] == ["art_img1"]


def test_galerie_import_masque_par_defaut(tmp_repo):
    _, _, TestSession = tmp_repo
    _seed_image(
        TestSession,
        art_id="art_imp",
        role="attachment",
        scope_kind="conversation",
        scope_id="conv1",
    )
    db = TestSession()
    try:
        sans = svc.list_generated_images(db)
        avec = svc.list_generated_images(db, include_imports=True)
    finally:
        db.close()
    assert all(i["artifact_id"] != "art_imp" for i in sans)
    assert any(i["artifact_id"] == "art_imp" for i in avec)


# ---------- Qualité : essais obsolètes distingués (bruit 22/09) ----------


def _seed_card(TestSession, art_id, quality):
    import json as _js
    from backend.db.models import ArtifactCard

    db = TestSession()
    try:
        db.add(
            ArtifactCard(
                id=f"card_{art_id}",
                artifact_id=art_id,
                revision=1,
                is_current=1,
                title="Img",
                summary=_js.dumps({"quality": quality, "prompt": "x"}),
                analysis_status="generated",
                capabilities_json='["image"]',
            )
        )
        db.commit()
    finally:
        db.close()


def test_galerie_qualite_bruit_marquee_deprecated(tmp_repo):
    _, _, TestSession = tmp_repo
    _seed_image(TestSession, art_id="art_old")
    _seed_card(TestSession, "art_old", "UNVALIDATED (bruit au 22/09)")
    db = TestSession()
    try:
        items = svc.list_generated_images(db)
    finally:
        db.close()
    got = {i["artifact_id"]: i for i in items}["art_old"]
    assert got["quality"] == "UNVALIDATED (bruit au 22/09)"
    assert got["deprecated"] is True and got["experimental"] is True


def test_galerie_experimental_non_deprecated(tmp_repo):
    _, _, TestSession = tmp_repo
    _seed_image(TestSession, art_id="art_new")
    _seed_card(TestSession, "art_new", "experimental")
    db = TestSession()
    try:
        items = svc.list_generated_images(db)
    finally:
        db.close()
    got = {i["artifact_id"]: i for i in items}["art_new"]
    assert got["deprecated"] is False and got["experimental"] is True


def test_galerie_sans_carte_qualite_none(tmp_repo):
    _, _, TestSession = tmp_repo
    _seed_image(TestSession, art_id="art_nocard")
    db = TestSession()
    try:
        items = svc.list_generated_images(db)
    finally:
        db.close()
    got = {i["artifact_id"]: i for i in items}["art_nocard"]
    assert got["quality"] is None and got["deprecated"] is False


# ---------- Opt-in : env OU fichier drapeau (interrupteur UI) ----------


def test_optin_fichier_drapeau(tmp_repo, monkeypatch):
    from backend.services import flux_spike as fx

    tmp_path, _, _ = tmp_repo
    flag = tmp_path / "image_backend.optin"
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(flag))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    assert fx.backend_enabled() is False
    fx.set_backend_enabled(True)
    assert fx.backend_enabled() is True
    fx.set_backend_enabled(False)
    assert fx.backend_enabled() is False


def test_optin_env_prioritaire_sur_fichier(tmp_repo, monkeypatch):
    from backend.services import flux_spike as fx

    tmp_path, _, _ = tmp_repo
    flag = tmp_path / "image_backend.optin"
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(flag))
    monkeypatch.setenv("THOT_IMAGE_BACKEND", "flux2-spike")
    assert fx.backend_enabled() is True  # même sans fichier


def test_optin_suit_la_db_active(tmp_path, monkeypatch):
    # RÉGRESSION 23/09 : le flag doit suivre la DB réellement utilisée (THOT_DB_PATH
    # injecté par Rust — portable projet vs XDG), sinon l'app ne voit jamais l'opt-in.
    import backend.db.database as _dbmod
    from backend.services import flux_spike as fx

    fake_db = tmp_path / "database" / "thot-phase-1.db"
    fake_db.parent.mkdir(parents=True)
    monkeypatch.setattr(_dbmod, "DB_PATH", str(fake_db))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    monkeypatch.delenv("THOT_IMAGE_OPTIN_PATH", raising=False)
    assert fx._optin_path() == fake_db.parent / "image_backend.optin"
    assert fx.backend_enabled() is False
    fx.set_backend_enabled(True)
    assert fx.backend_enabled() is True


def test_contrat_list_cles_consommees_par_front(tmp_repo):
    # CONTRAT UI↔Backend : ImagesPage.tsx (timeline, filtre deprecated) et
    # GalleryPage.tsx (badges, qualité) consomment EXACTEMENT ces clés.
    # Si une clé manque (ex. vieux binaire Rust sans quality), le front bascule
    # en silencieux (bruit réaffiché) — ce test casse avant l'UI.
    from backend.services import flux_spike as _fx  # noqa: F401 (contexte pont)

    _, _, TestSession = tmp_repo
    _seed_image(TestSession, art_id="art_contrat")
    _seed_card(TestSession, "art_contrat", "experimental")
    db = TestSession()
    try:
        items = svc.list_generated_images(db)
    finally:
        db.close()
    got = {i["artifact_id"]: i for i in items}["art_contrat"]
    attendues = {
        "artifact_id",
        "display_name",
        "original_filename",
        "kind",
        "created_at",
        "scopes",
        "is_import",
        "quality",
        "experimental",
        "deprecated",
    }
    assert attendues <= set(got.keys()), (
        f"clés manquantes : {attendues - set(got.keys())}"
    )


# ---------- Pont flux_spike : opt-in, sans GPU (faux spike) ----------


def _mini_png(size=(4, 4)) -> bytes:
    from PIL import Image as _PIL
    import io as _io

    buf = _io.BytesIO()
    _PIL.new("RGB", size, (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def test_pont_coupe_par_defaut(tmp_path, monkeypatch):
    from backend.services import flux_spike as fx

    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(tmp_path / "image_backend.optin"))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    assert fx.backend_enabled() is False
    ok, reason = fx.is_available()
    assert ok is False and "non activé" in reason


def test_disponibilite_exige_vram_presets_un_megapixel(monkeypatch):
    from backend.services import flux_spike as fx

    monkeypatch.setattr(fx, "backend_enabled", lambda: True)
    monkeypatch.setattr(
        fx,
        "probe",
        lambda: (
            True,
            {"cuda": True, "free_vram_gb": fx.MIN_FREE_VRAM_GB - 0.1, "files": {}},
        ),
    )
    available, reason = fx.is_available()
    assert available is False
    assert "VRAM libre insuffisante" in reason
    monkeypatch.setattr(
        fx,
        "probe",
        lambda: (
            True,
            {"cuda": True, "free_vram_gb": fx.MIN_FREE_VRAM_GB + 0.1, "files": {}},
        ),
    )
    available, reason = fx.is_available()
    assert available is True and "~1 MP" in reason


def test_generate_indisponible_sans_optin(tmp_path, monkeypatch):
    monkeypatch.setenv("THOT_IMAGE_OPTIN_PATH", str(tmp_path / "image_backend.optin"))
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    with pytest.raises(RuntimeError, match="SERVICE_UNAVAILABLE"):
        svc.generate(None, "run1", "un chat")


def test_generate_refuse_refs_avant_backend(monkeypatch):
    monkeypatch.delenv("THOT_IMAGE_BACKEND", raising=False)
    with pytest.raises(ValueError, match="ID_UNKNOWN"):
        svc.generate(None, "run1", "un chat", reference_ids=["art_x"])


def test_spike_cmd_forwards_edit_args():
    from backend.services import flux_spike as fx

    cmd, _ = fx._spike_cmd(
        "prompt.txt",
        1,
        64,
        64,
        "out.png",
        init_image="init.png",
        mask="mask.png",
        mode="inpaint",
        strength=0.37,
    )
    joined = " ".join(cmd)
    assert "--init-image" in cmd and "init.png" in cmd
    assert "--mask" in cmd and "mask.png" in cmd
    assert "--mode" in cmd and "inpaint" in cmd
    assert "--strength" in cmd and "0.37" in cmd


def test_tail_pour_echec_worker_garde_la_ligne_pic_et_les_huit_dernieres():
    from backend.services import flux_spike as fx

    pic = "PIC SCENARIO: [pic] assemblage du transformer"
    log_tail = ["bruit 0", "bruit 1", pic, *[f"ligne {i}" for i in range(10)]]

    out = fx._tail_pour_echec_worker(log_tail)
    morceaux = out.split(" | ")

    # La ligne PIC ouvre le diagnostic, puis les 8 dernières lignes.
    assert morceaux[0] == pic
    assert morceaux[1:] == [f"ligne {i}" for i in range(2, 10)]
    assert "ligne 0" not in out and "ligne 1" not in out


def test_tail_pour_echec_worker_sans_pic_garde_les_huit_dernieres():
    from backend.services import flux_spike as fx

    log_tail = [f"ligne {i}" for i in range(10)]

    out = fx._tail_pour_echec_worker(log_tail)

    assert out == " | ".join(f"ligne {i}" for i in range(2, 10))


def test_tail_pour_echec_worker_ne_duplique_pas_la_ligne_pic():
    from backend.services import flux_spike as fx

    pic = "PIC SCENARIO: [pic] décodage VAE"
    log_tail = [f"ligne {i}" for i in range(6)] + [pic, "ligne 6", "ligne 7"]

    out = fx._tail_pour_echec_worker(log_tail)

    assert out.count("PIC SCENARIO:") == 1
    assert out.split(" | ")[0] == pic


def test_tail_pour_echec_worker_tronque_a_1600_caracteres():
    from backend.services import flux_spike as fx

    log_tail = ["PIC SCENARIO: " + "x" * 500, "y" * 2000]

    out = fx._tail_pour_echec_worker(log_tail)

    assert len(out) <= 1600


def test_run_edit_rejects_missing_init_image():
    from backend.services import flux_spike as fx

    with pytest.raises(ValueError, match="BAD_ARGS"):
        fx.run_edit("prompt", init_image=None)


def test_create_mode_with_one_reference_promotes_to_reference(tmp_repo, monkeypatch):
    from backend.services import image_service as svc
    from backend.services import flux_spike as fx

    # tmp_repo fixture: repo path, data_dir, TestSession
    _, _, TestSession = tmp_repo

    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    monkeypatch.setattr(
        svc,
        "_resolve_work_token",
        lambda token, supplied_sha256=None: (b"work", 1024, 1024, "worksha"),
    )

    called = {"run_t2i": 0, "run_edit": 0, "mode": None}

    def fake_run_t2i(*a, **k):
        called["run_t2i"] += 1
        called["reference_images"] = k.get("reference_images")
        return _mini_png((1024, 1024)), {
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    def fake_run_edit(*a, **k):
        called["run_edit"] += 1
        called["mode"] = k.get("mode")
        return _mini_png((1024, 1024)), {
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_t2i", fake_run_t2i)
    monkeypatch.setattr(fx, "run_edit", fake_run_edit)
    # avoid ASN.1? provenance publish writes card; real publish is fine.

    png_ = _mini_png((1024, 1024))  # ensure object referenced
    result = svc.generate(
        None,
        "run-ref-promote",
        "prompt exact",
        "1:1",
        reference_tokens=["0" * 32],
        seed=7,
        db_session_factory=TestSession,
        origin="studio",
    )
    assert called["run_t2i"] == 1
    assert called["run_edit"] == 0
    assert len(called.get("reference_images") or []) == 1


def test_sigma_est_converti_en_timestep_modele():
    from backend.image_engine.spike_t2i import _model_timestep
    from backend.image_engine.spike_t2i import _model_timestep

    assert _model_timestep(1.0) == 1000.0
    assert _model_timestep(0.951930) == pytest.approx(951.930)


def test_scheduler_utilise_le_schedule_klein_officiel():
    from backend.image_engine.pipeline.scheduler import Flux2KleinScheduler

    sigmas = Flux2KleinScheduler().set_timesteps(4, image_seq_len=4096)
    assert sigmas.tolist() == pytest.approx(
        [1.0, 0.96738404, 0.90814388, 0.76719993, 0.0], abs=1e-6
    )


def test_defaults_klein_distille_sont_fixes():
    from backend.image_engine.core.config import FLUX2_KLEIN_CONFIG

    assert FLUX2_KLEIN_CONFIG.num_inference_steps == 4
    assert FLUX2_KLEIN_CONFIG.guidance_scale == 1.0


def test_nouveau_worker_refuse_apres_echec_cleanup(monkeypatch):
    from backend.services import flux_spike as fx

    monkeypatch.setattr(fx, "_cleanup_blocked", "groupe image encore actif")
    with pytest.raises(RuntimeError, match=r"\[CLEANUP_FAILED\]"):
        fx.run_t2i("un chat", seed=1, width=512, height=512)


def test_run_t2i_faux_spike(tmp_path, monkeypatch):
    """Le pont sous-processus marche sans GPU : faux script qui écrit un PNG."""
    from backend.services import flux_spike as fx

    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: tmp_path / "data")
    fake = tmp_path / "fake_spike.py"
    fake.write_text(
        "import argparse\n"
        "from PIL import Image\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('--prompt-file', required=True)\n"
        "ap.add_argument('--width', type=int, required=True)\n"
        "ap.add_argument('--height', type=int, required=True)\n"
        "ap.add_argument('--seed', type=int, required=True)\n"
        "ap.add_argument('--output', required=True)\n"
        "a = ap.parse_args()\n"
        "assert open(a.prompt_file, encoding='utf-8').read() == 'un chat test'\n"
        "print('step 0 ok', flush=True)\n"
        "print('STEPS_EXECUTED: 4', flush=True)\n"
        "Image.new('RGB', (a.width, a.height), (9, 9, 9)).save(a.output, format='PNG')\n"
    )
    seen = {}

    def fake_cmd(prompt_file, seed, width, height, out, **_kw):
        command = [
            sys.executable,
            "-u",
            str(fake),
            "--prompt-file",
            prompt_file,
            "--width",
            str(width),
            "--height",
            str(height),
            "--seed",
            str(seed),
            "--output",
            out,
        ]
        seen["prompt_file"] = prompt_file
        seen["command"] = command
        return command, None

    monkeypatch.setattr(fx, "_spike_cmd", fake_cmd)
    staged = tmp_path / "data" / "staged" / "effect.png"
    png, stats = fx.run_t2i(
        "un chat test",
        seed=7,
        width=832,
        height=1248,
        timeout_s=60,
        staged_path=str(staged),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert staged.exists()
    assert stats["staged_path"] == str(staged)
    assert (stats["width"], stats["height"]) == (832, 1248)
    assert stats["backend"] == "flux2"
    assert all("un chat test" not in argument for argument in seen["command"])
    assert Path(seen["prompt_file"]).is_relative_to(
        (tmp_path / "data" / "temp").resolve()
    )


def test_provenance_exacte_et_publication(tmp_repo, monkeypatch):
    from backend.db.models import ArtifactCard
    from backend.services import flux_spike as fx

    _, _, TestSession = tmp_repo
    monkeypatch.setattr(svc, "check_install", lambda: (True, "stub"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "stub"))
    seen = {}

    def fake_run(prompt, seed, **kwargs):
        seen["prompt"] = prompt
        seen["seed"] = seed
        seen.update(kwargs)
        return _mini_png((kwargs["width"], kwargs["height"])), {
            "backend": fx.BACKEND_NAME,
            "width": kwargs["width"],
            "height": kwargs["height"],
            "seed": seed,
            "duration_s": 0.1,
        }

    monkeypatch.setattr(fx, "run_t2i", fake_run)
    pub = svc.generate(
        None,
        "run-provenance",
        "prompt exact",
        "1:1",
        [],
        None,
        idempotency_key="idem-provenance",
        db_session_factory=TestSession,
    )
    assert 0 <= seen["seed"] <= svc.MAX_SEED
    assert seen["width"] == 1024 and seen["height"] == 1024
    db = TestSession()
    try:
        card = (
            db.query(ArtifactCard)
            .filter(ArtifactCard.artifact_id == pub["artifact_id"])
            .first()
        )
        provenance = json.loads(card.summary)
    finally:
        db.close()
    assert provenance["checkpoint"] == fx.CHECKPOINT
    assert provenance["model"] == fx.MODEL_NAME
    assert provenance["prompt"] == "prompt exact"
    assert provenance["aspect_ratio"] == "1:1"
    assert provenance["seed"] == seen["seed"]
    assert provenance["dimensions"] == {"width": 1024, "height": 1024}
    assert provenance["run_id"] == "run-provenance"
    assert provenance["origin"] == "image_create"
    assert provenance["idempotency_key"] == "idem-provenance"
    assert "qualif" not in json.dumps(provenance).lower()


def test_png_dimensions_incoherentes_rejetees(tmp_path, monkeypatch):
    from backend.services import flux_spike as fx

    fake = tmp_path / "wrong_size.py"
    fake.write_text(
        "import argparse\n"
        "from PIL import Image\n"
        "ap=argparse.ArgumentParser()\n"
        "ap.add_argument('--prompt-file')\n"
        "ap.add_argument('--width', type=int)\n"
        "ap.add_argument('--height', type=int)\n"
        "ap.add_argument('--seed', type=int)\n"
        "ap.add_argument('--output')\n"
        "a=ap.parse_args()\n"
        "Image.new('RGB',(8,8),(1,1,1)).save(a.output,format='PNG')\n"
    )
    monkeypatch.setattr(
        fx,
        "_spike_cmd",
        lambda prompt_file, seed, width, height, out, **_kw: (
            [
                sys.executable,
                str(fake),
                "--prompt-file",
                prompt_file,
                "--width",
                str(width),
                "--height",
                str(height),
                "--seed",
                str(seed),
                "--output",
                out,
            ],
            None,
        ),
    )
    with pytest.raises(RuntimeError, match="DIMENSIONS_MISMATCH"):
        fx.run_t2i("prompt", seed=1, width=512, height=512, timeout_s=10)


def test_publish_dedup_par_hash(tmp_repo):
    from backend.services import flux_spike as fx

    _, _, TestSession = tmp_repo
    png = _mini_png()
    meta = {
        "prompt": "chat",
        "display_name": "Chat",
        "aspect_ratio": "1:1",
        "size": 4,
        "seed": 7,
        "origin": "studio",
    }
    db = TestSession()
    try:
        p1 = fx.publish_generated(db, png, meta)
        p2 = fx.publish_generated(db, png, meta)
    finally:
        db.close()
    assert p1["artifact_id"] == p2["artifact_id"]
    db2 = TestSession()
    try:
        items = svc.list_generated_images(db2)
    finally:
        db2.close()
    assert any(i["artifact_id"] == p1["artifact_id"] for i in items)
    assert p1["artifact_id"].startswith("art_")


# ---------- Palette fermée des codes d'effet (revue clôture phase 2) ----------


def test_palette_codes_effet_fermee():
    """Les codes d'effet sont un littéral fermé : chaque code est un membre nommé,
    un code hors palette lève à la construction, et la table statut→code ne connaît
    que les statuts du ledger.

    Avant ce bloc, la branche « effet indisponible » de l'outil fabriquait
    `[EFFECT_{claim.status.upper()}]` — un code inventé depuis n'importe quel
    statut futur, que personne n'a revu et que l'interface ne connaît pas.
    """
    from backend.tools.generate_image import _CODE_EFFET_PAR_STATUT, _CodeEffet

    with pytest.raises(ValueError):
        _CodeEffet(
            "EFFECT_INVENTE_CE_SOIR"
        )  # un statut futur est rejeté, jamais converti

    # La palette complète : les quatre états d'effet nommés + deux motifs d'exception.
    assert {m.value for m in _CodeEffet} == {
        "EFFECT_RUNNING",
        "EFFECT_STAGED",
        "EFFECT_IN_PROGRESS",
        "EFFECT_KEY_CONFLICT",
        "EFFECT_UNCERTAIN",
        "EFFECT_PUBLISHED",
    }, "un code d'effet qui n'est pas dans ce littéral n'est pas au nom de l'outil"

    # La table statut→code ne couvre que les statuts qui atteignent la branche,
    # et tous ses codes viennent de la palette — pas de fabrication latérale.
    assert set(_CODE_EFFET_PAR_STATUT) == {
        "running",
        "staged",
        "published",
        "uncertain",
    }
    for membre in _CODE_EFFET_PAR_STATUT.values():
        assert isinstance(membre, _CodeEffet)


def test_generate_mode_global_requires_work_before_install(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo

    monkeypatch.setattr(
        svc, "check_install", lambda: (_ for _ in ()).throw(AssertionError("called"))
    )
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")

    with pytest.raises(ValueError, match="BAD_ARGS"):
        svc.generate(
            None, "run-global", "prompt", mode="global", db_session_factory=TestSession
        )


def test_generate_mode_local_requires_mask(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo

    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")

    with pytest.raises(ValueError, match="BAD_ARGS"):
        svc.generate(
            None,
            "run-local",
            "prompt",
            mode="local",
            work_token="0" * 32,
            db_session_factory=TestSession,
        )


def test_generate_mode_reference_requires_single_source(tmp_repo, monkeypatch):
    _, _, TestSession = tmp_repo

    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")

    with pytest.raises(ValueError, match="BAD_ARGS"):
        svc.generate(
            None,
            "run-ref-none",
            "prompt",
            mode="reference",
            db_session_factory=TestSession,
        )

    with pytest.raises(ValueError, match="BAD_ARGS"):
        svc.generate(
            None,
            "run-ref-both",
            "prompt",
            mode="reference",
            work_token="0" * 32,
            reference_tokens=["0" * 32],
            db_session_factory=TestSession,
        )


def test_generate_reference_mode_work_token_uses_edit(tmp_repo, monkeypatch):
    from backend.services import image_service as svc
    from backend.services import flux_spike as fx

    _, _, TestSession = tmp_repo

    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    monkeypatch.setattr(
        svc,
        "_resolve_work_token",
        lambda token, supplied_sha256=None: (b"work", 1024, 1024, "worksha"),
    )

    called = {"run_t2i": 0, "run_edit": 0, "mode": None, "init_image": None}

    def fake_run_t2i(*a, **k):
        called["run_t2i"] += 1
        return _mini_png((1024, 1024)), {
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    def fake_run_edit(*a, **k):
        called["run_edit"] += 1
        called["mode"] = k.get("mode")
        called["init_image"] = k.get("init_image")
        return _mini_png((1024, 1024)), {
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_t2i", fake_run_t2i)
    monkeypatch.setattr(fx, "run_edit", fake_run_edit)

    result = svc.generate(
        None,
        "run-ref-work",
        "prompt exact",
        "1:1",
        mode="reference",
        work_token="0" * 32,
        seed=7,
        db_session_factory=TestSession,
        origin="studio",
    )
    assert called["run_t2i"] == 0
    assert called["run_edit"] == 1
    assert called["mode"] == "reference"
    assert called["init_image"] is not None


def test_generate_global_edit_accepts_reference_tokens(tmp_repo, monkeypatch):
    from backend.services import image_service as svc
    from backend.services import flux_spike as fx

    _, _, TestSession = tmp_repo

    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "_rendre_la_vram_du_worker_ocr", lambda: "ok")
    monkeypatch.setattr(
        svc,
        "_resolve_work_token",
        lambda token, supplied_sha256=None: (
            _mini_png((1024, 1024)),
            1024,
            1024,
            "worksha",
        ),
    )
    monkeypatch.setattr(
        svc,
        "_resolve_reference_tokens",
        lambda tokens, shas: [(_mini_png((1024, 1024)), 1024, 1024, "refsha")],
    )

    called = {"run_t2i": 0, "run_edit": 0, "mode": None, "reference_images": None}

    def fake_run_t2i(*a, **k):
        called["run_t2i"] += 1
        return _mini_png((1024, 1024)), {
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    def fake_run_edit(*a, **k):
        called["run_edit"] += 1
        called["mode"] = k.get("mode")
        called["reference_images"] = k.get("reference_images")
        return _mini_png((1024, 1024)), {
            "seed": 7,
            "width": 1024,
            "height": 1024,
            "backend": fx.BACKEND_NAME,
        }

    monkeypatch.setattr(fx, "run_t2i", fake_run_t2i)
    monkeypatch.setattr(fx, "run_edit", fake_run_edit)

    svc.generate(
        None,
        "run-edit-refs",
        "prompt exact",
        "1:1",
        mode="global",
        work_token="0" * 32,
        reference_tokens=["0" * 32],
        reference_sha256="a" * 64,
        seed=7,
        db_session_factory=TestSession,
        origin="studio",
    )

    assert called["run_t2i"] == 0
    assert called["run_edit"] == 1
    assert called["mode"] == "global"
    assert called["reference_images"]
    assert len(called["reference_images"]) == 1
