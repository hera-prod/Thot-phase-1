"""Opérations image du chat : create, edit, outpaint, refus et ingestion.

Ces tests traversent `_execute_generate_image` — la vraie décision, la vraie
base d'effets, le vrai staging — avec une diffusion simulée. Ils verrouillent
le contrat partagé par le loop et l'outil : une seule préparation
(`prepare_chat_image`) décide des identifiants, des kwargs moteur et de
l'empreinte.
"""

from __future__ import annotations

import io
from collections import UserDict
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Artifact, ArtifactLink, Base, Blob, DocRef
from backend.inference import loop as loop_mod
from backend.services import image_service as svc
from backend.services.chat_image_plan import prepare_chat_image
from backend.services.effect_ledger import image_request_fingerprint
from backend.tools.generate_image import GenerateImageArgs


def _png(width: int = 512, height: int = 512) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'chat_image.db'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", sessions)

    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    temp_dir = tmp_path / "temp"
    monkeypatch.setattr("backend.config.paths.get_temp_dir", lambda: temp_dir)
    monkeypatch.setattr(loop_mod, "emit", lambda *args, **kwargs: True)

    from backend.ingestion import attachment_ingest

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", lambda ids: None)
    return SimpleNamespace(
        sessions=sessions, data_dir=data_dir, temp_dir=temp_dir, tmp_path=tmp_path
    )


def _add_image(
    env,
    artifact_id: str,
    *,
    conv_id: str = "conv1",
    label: str | None = None,
    size: tuple[int, int] = (512, 512),
    kind: str = "image",
    scope_kind: str = "conversation",
    removed: bool = False,
    deleted: bool = False,
) -> None:
    data = _png(*size)
    relative_path = f"blobs/{artifact_id}.png"
    (env.data_dir / relative_path).write_bytes(data)
    session = env.sessions()
    try:
        session.add(
            Blob(
                id=f"blob_{artifact_id}",
                sha256=f"sha_{artifact_id}",
                relative_path=relative_path,
                size_bytes=len(data),
                mime_detected="image/png",
                state="available",
            )
        )
        session.add(
            Artifact(
                id=artifact_id,
                blob_id=f"blob_{artifact_id}",
                kind=kind,
                original_filename=f"{artifact_id}.png",
                display_name=artifact_id,
                deleted_at=datetime.now() if deleted else None,
            )
        )
        session.add(
            ArtifactLink(
                id=f"lnk_{artifact_id}_{conv_id}",
                artifact_id=artifact_id,
                scope_kind=scope_kind,
                scope_id=conv_id,
                role="attachment",
                removed_at=datetime.now() if removed else None,
            )
        )
        if label:
            session.add(
                DocRef(
                    id=f"dref_{artifact_id}",
                    conv_id=conv_id,
                    doc_label=label,
                    doc_index=int(label.split("_")[1]),
                    artifact_id=artifact_id,
                )
            )
        session.commit()
    finally:
        session.close()


def _execute(
    env,
    *,
    prompt: str = "un phare au crépuscule",
    operation: str = "create",
    aspect_ratio: str = "1:1",
    source_image_id: str | None = None,
    reference_ids: list[str] | None = None,
    conv_id: str | None = "conv1",
    run_id: str = "req_1",
) -> dict:
    args: dict = {
        "operation": operation,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
    }
    if source_image_id is not None:
        args["source_image_id"] = source_image_id
    if reference_ids is not None:
        args["reference_ids"] = reference_ids
    return loop_mod._execute_generate_image(
        {"id": f"call_{run_id}", "args": args},
        manager=None,
        conv_id=conv_id,
        request_id=run_id,
        run_id=run_id,
    )


class Capture:
    def __init__(self, env, artifact_id: str = "art_generated") -> None:
        self.env = env
        self.artifact_id = artifact_id
        self.calls = 0
        self.prompt = None
        self.aspect_ratio = None
        self.reference_ids = None
        self.seed = None
        self.kwargs: dict = {}
        self.work_readable = False
        self.mask_readable = False

    def __call__(
        self, manager, run_id, prompt, aspect_ratio, reference_ids, seed, **kwargs
    ):
        self.calls += 1
        self.prompt = prompt
        self.aspect_ratio = aspect_ratio
        self.reference_ids = reference_ids
        self.seed = seed
        self.kwargs = kwargs
        token_root = Path(self.env.temp_dir) / "image-studio"
        work_token = kwargs.get("work_token")
        mask_token = kwargs.get("mask_token")
        if work_token:
            path = token_root / f"{work_token}.png"
            self.work_readable = path.is_file() and path.read_bytes().startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        if mask_token:
            path = token_root / f"{mask_token}.png"
            self.mask_readable = path.is_file() and path.read_bytes().startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        return {
            "artifact_id": self.artifact_id,
            "stats": {
                "width": 1024,
                "height": 1024,
                "seed": seed or 0,
                "duration_s": 1.0,
                "backend": "stub",
            },
        }


def _install(monkeypatch, capture: Capture) -> Capture:
    monkeypatch.setattr("backend.services.image_service.generate", capture)
    return capture


# ---------- 1. create sans référence ----------


def test_create_sans_reference_envoie_le_mode_create(env, monkeypatch):
    capture = _install(monkeypatch, Capture(env))
    result = _execute(env, prompt="un phare", run_id="req_create")

    assert result["ok"] is True
    assert capture.calls == 1
    assert capture.kwargs["mode"] == "create"
    assert capture.reference_ids == []
    assert "work_token" not in capture.kwargs
    assert "mask_token" not in capture.kwargs


# ---------- 2. create avec deux doc_N ----------


def test_create_avec_deux_doc_resout_les_artefacts(env, monkeypatch):
    _add_image(env, "art_ref1", label="doc_1")
    _add_image(env, "art_ref2", label="doc_2")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="deux references",
        reference_ids=["doc_1", "doc_2"],
        run_id="req_refs",
    )

    assert result["ok"] is True
    assert capture.kwargs["mode"] == "reference"
    assert capture.reference_ids == ["art_ref1", "art_ref2"]
    assert len(capture.reference_ids) == 2
    assert capture.kwargs["strength"] == 0.65


# ---------- 3. edit conserve le format de la source ----------


def test_edit_stage_un_work_lisible_puis_le_supprime(env, monkeypatch):
    _add_image(env, "art_source", label="doc_3")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="rends la mer plus calme",
        operation="edit",
        aspect_ratio="1:1",  # même format que la source : ce test vise le staging
        source_image_id="doc_3",
        run_id="req_edit",
    )

    assert result["ok"] is True
    assert capture.kwargs["mode"] == "global"
    assert capture.kwargs["source_artifact_id"] == "art_source"
    assert capture.kwargs["canvas_width"] == 1024
    assert capture.kwargs["canvas_height"] == 1024
    assert capture.kwargs["work_sha256"]
    assert capture.aspect_ratio == "1:1"
    assert capture.work_readable is True, "le token doit être lisible pendant l'appel"

    token = capture.kwargs["work_token"]
    assert not (Path(env.temp_dir) / "image-studio" / f"{token}.png").exists()


def test_edit_avec_format_cible_non_defaut_devient_outpaint(env, monkeypatch):
    """q-r.md : « Nickel. Maintenant met le en 9:16. » sur une image 1:1.

    Le modèle a envoyé un `edit` avec `aspect_ratio="9:16"` et une référence.
    `edit` conserverait 1:1 : la préparation répare en `outpaint`, cible 720×1280,
    et abandonne la référence (outpaint n’en accepte pas).
    """
    _add_image(env, "art_carre", label="doc_3", size=(512, 512))
    _add_image(env, "art_reference", label="doc_4")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="étends la scène en portrait",
        operation="edit",
        aspect_ratio="9:16",
        source_image_id="doc_3",
        reference_ids=["doc_4"],
        run_id="req_edit_9_16",
    )

    assert result["ok"] is True
    assert capture.kwargs["mode"] == "outpaint"
    assert capture.aspect_ratio == "9:16"
    assert (capture.kwargs["canvas_width"], capture.kwargs["canvas_height"]) == (
        720,
        1280,
    )
    assert capture.reference_ids == []
    assert capture.kwargs["mask_token"]
    assert capture.mask_readable is True
    assert "Références ignorées" in result["result"]
    # La note porte l'identifiant résolu, celui que le plan manipule.
    assert "art_reference" in result["result"]

    root = Path(env.temp_dir) / "image-studio"
    assert not (root / f"{capture.kwargs['work_token']}.png").exists()
    assert not (root / f"{capture.kwargs['mask_token']}.png").exists()


def test_edit_avec_format_1_1_sur_source_16_9_reste_un_edit(env, monkeypatch):
    """Le défaut 1:1 est ambigu : il n’est jamais inféré comme changement de format."""
    _add_image(env, "art_paysage", label="doc_5", size=(1280, 720))
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="rends le ciel plus doux",
        operation="edit",
        aspect_ratio="1:1",
        source_image_id="doc_5",
        run_id="req_edit_1_1",
    )

    assert result["ok"] is True
    assert capture.kwargs["mode"] == "global"
    assert capture.aspect_ratio == "16:9"
    assert "mask_token" not in capture.kwargs


def test_edit_avec_1_1_nomme_dans_le_prompt_devient_outpaint(env, monkeypatch):
    """Le prompt réécrit lève l'ambiguïté du défaut 1:1 sur une source 16:9."""
    _add_image(env, "art_paysage_nomme", label="doc_6", size=(1280, 720))
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="recadre le chat en carré, format 1:1",
        operation="edit",
        aspect_ratio="1:1",
        source_image_id="doc_6",
        run_id="req_edit_1_1_nomme",
    )

    assert result["ok"] is True
    assert capture.kwargs["mode"] == "outpaint"
    assert capture.aspect_ratio == "1:1"
    assert (capture.kwargs["canvas_width"], capture.kwargs["canvas_height"]) == (
        1024,
        1024,
    )
    assert capture.reference_ids == []
    assert capture.kwargs["mask_token"]
    assert capture.mask_readable is True


@pytest.mark.parametrize(
    ("prompt", "aspect_ratio", "attendu"),
    [
        ("format 1:1", "1:1", True),
        ("style 9:16", "9:16", True),
        ("Times Square", "1:1", False),
        ("une table carrée", "1:1", False),
        ("rends le ciel plus doux", "1:1", False),
        ("ancien format 91:16", "1:1", False),
        ("zoom 1:10", "1:1", False),
    ],
)
def test_format_nomme_dans_le_prompt(prompt, aspect_ratio, attendu):
    from backend.services.chat_image_plan import _format_nomme_dans_le_prompt

    assert _format_nomme_dans_le_prompt(prompt, aspect_ratio) is attendu


@pytest.mark.parametrize(
    ("prompt", "run_id"),
    [
        ("Times Square la nuit", "req_edit_times_square"),
        ("une table carrée en bois", "req_edit_table_carree"),
    ],
)
def test_mot_de_contenu_ne_declenche_pas_outpaint_sur_source_16_9(
    env, monkeypatch, prompt, run_id
):
    """« carré » / « square » décrivent la scène, pas un format demandé."""
    _add_image(env, "art_paysage_contenu", label="doc_7", size=(1280, 720))
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt=prompt,
        operation="edit",
        aspect_ratio="1:1",
        source_image_id="doc_7",
        run_id=run_id,
    )

    assert result["ok"] is True
    assert capture.kwargs["mode"] == "global"
    assert capture.aspect_ratio == "16:9"
    assert "mask_token" not in capture.kwargs


# ---------- 4. outpaint vers 16:9 ----------


def test_outpaint_stage_work_et_mask_et_les_supprime(env, monkeypatch):
    _add_image(env, "art_square", label="doc_4", size=(512, 512))
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="",
        operation="outpaint",
        aspect_ratio="16:9",
        source_image_id="doc_4",
        run_id="req_outpaint",
    )

    assert result["ok"] is True
    assert capture.kwargs["mode"] == "outpaint"
    assert capture.kwargs["mask_kind"] == "fill"
    assert capture.kwargs["strength"] == 1.0
    assert capture.kwargs["canvas_width"] == 1280
    assert capture.kwargs["canvas_height"] == 720
    transform = capture.kwargs["source_transform"]
    assert transform["width"] > 0 and transform["height"] > 0
    assert capture.kwargs["mask_token"]
    assert capture.work_readable is True and capture.mask_readable is True
    assert capture.prompt.startswith("Reconstruct and extend")

    root = Path(env.temp_dir) / "image-studio"
    assert not (root / f"{capture.kwargs['work_token']}.png").exists()
    assert not (root / f"{capture.kwargs['mask_token']}.png").exists()


# ---------- 5. refus : aucune diffusion ----------


@pytest.mark.parametrize(
    "payload",
    [
        {
            "prompt": "trop de refs",
            "reference_ids": ["doc_1", "doc_2", "doc_3", "doc_4", "doc_5"],
        },
        {
            "prompt": "edit trop de refs",
            "operation": "edit",
            "source_image_id": "doc_1",
            "reference_ids": ["doc_2", "doc_3", "doc_4", "doc_5"],
        },
        {
            "prompt": "outpaint avec ref",
            "operation": "outpaint",
            "aspect_ratio": "16:9",
            "source_image_id": "doc_1",
            "reference_ids": ["doc_2"],
        },
        {
            "prompt": "source dupliquee",
            "operation": "edit",
            "source_image_id": "doc_1",
            "reference_ids": ["doc_1"],
        },
    ],
)
def test_refus_de_validation_sans_diffusion(env, monkeypatch, payload):
    for index in range(1, 6):
        _add_image(env, f"art_{index}", label=f"doc_{index}")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(env, run_id="req_refus", **payload)

    assert result["ok"] is False
    assert capture.calls == 0
    assert result["result"]


def test_source_hors_conversation_scope_denied(env, monkeypatch):
    _add_image(env, "art_other", conv_id="conv_other", label="doc_1")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="edite",
        operation="edit",
        source_image_id="art_other",
        run_id="req_scope",
    )

    assert result["ok"] is False
    assert "SCOPE_DENIED" in result["result"]
    assert capture.calls == 0


def test_doc_inconnu_id_unknown(env, monkeypatch):
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="edite",
        operation="edit",
        source_image_id="doc_9",
        run_id="req_unknown",
    )

    assert result["ok"] is False
    assert "ID_UNKNOWN" in result["result"]
    assert capture.calls == 0


def test_source_pdf_refusee(env, monkeypatch):
    _add_image(env, "art_pdf", label="doc_1", kind="pdf")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="edite",
        operation="edit",
        source_image_id="doc_1",
        run_id="req_source_pdf",
    )

    assert result["ok"] is False
    assert "BAD_ARGS" in result["result"]
    assert capture.calls == 0


def test_source_supprimee_est_introuvable(env, monkeypatch):
    _add_image(env, "art_deleted", label="doc_1", deleted=True)
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="edite",
        operation="edit",
        source_image_id="doc_1",
        run_id="req_source_deleted",
    )

    assert result["ok"] is False
    assert "ID_UNKNOWN" in result["result"]
    assert capture.calls == 0


def test_lien_source_retire_scope_denied(env, monkeypatch):
    _add_image(env, "art_removed", label="doc_1", removed=True)
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="edite",
        operation="edit",
        source_image_id="art_removed",
        run_id="req_source_removed",
    )

    assert result["ok"] is False
    assert "SCOPE_DENIED" in result["result"]
    assert capture.calls == 0


def test_source_scope_workspace_scope_denied(env, monkeypatch):
    _add_image(env, "art_workspace", label="doc_1", scope_kind="workspace")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="edite",
        operation="edit",
        source_image_id="art_workspace",
        run_id="req_source_workspace",
    )

    assert result["ok"] is False
    assert "SCOPE_DENIED" in result["result"]
    assert capture.calls == 0


def test_source_dupliquee_via_doc_et_art_est_refusee(env, monkeypatch):
    _add_image(env, "art_same", label="doc_1")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="edite",
        operation="edit",
        source_image_id="art_same",
        reference_ids=["doc_1"],
        run_id="req_source_dupe",
    )

    assert result["ok"] is False
    assert "BAD_ARGS" in result["result"]
    assert capture.calls == 0


def test_references_sans_conversation_scope_denied(env, monkeypatch):
    _add_image(env, "art_ref", label="doc_1")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="une reference",
        reference_ids=["doc_1"],
        conv_id=None,
        run_id="req_refs_no_conv",
    )

    assert result["ok"] is False
    assert "SCOPE_DENIED" in result["result"]
    assert capture.calls == 0


def test_references_resolues_dedupliquees(env, monkeypatch):
    _add_image(env, "art_dup", label="doc_1")
    capture = _install(monkeypatch, Capture(env))
    result = _execute(
        env,
        prompt="une reference en double",
        reference_ids=["doc_1", "art_dup"],
        run_id="req_refs_dupe",
    )

    assert result["ok"] is True
    assert capture.reference_ids == ["art_dup"]


# ---------- 6. empreintes distinctes par opération ----------


def test_empreintes_create_edit_outpaint_distinctes(env):
    _add_image(env, "art_fp", label="doc_1")
    prompt = "le meme prompt"
    session = env.sessions()
    plans = []
    try:
        for args in (
            GenerateImageArgs.model_validate({"prompt": prompt, "operation": "create"}),
            GenerateImageArgs.model_validate(
                {
                    "prompt": prompt,
                    "operation": "edit",
                    "source_image_id": "doc_1",
                }
            ),
            GenerateImageArgs.model_validate(
                {
                    "prompt": prompt,
                    "operation": "outpaint",
                    "aspect_ratio": "16:9",
                    "source_image_id": "doc_1",
                }
            ),
        ):
            plan = prepare_chat_image(session, "conv1", args)
            plans.append(plan)
    finally:
        session.close()

    fingerprints = {
        image_request_fingerprint(
            plan.prompt,
            plan.aspect_ratio,
            plan.reference_ids,
            None,
            extra=plan.fingerprint_extra,
        )
        for plan in plans
    }
    assert len(fingerprints) == 3
    for plan in plans:
        plan.cleanup()


def test_empreinte_create_reste_celle_du_T2I(env):
    _add_image(env, "art_fp_create", label="doc_1")
    session = env.sessions()
    try:
        plan = prepare_chat_image(
            session,
            "conv1",
            GenerateImageArgs.model_validate(
                {"prompt": "un phare", "operation": "create"}
            ),
        )
    finally:
        session.close()

    assert plan.fingerprint_extra == {}
    assert image_request_fingerprint(
        plan.prompt, plan.aspect_ratio, plan.reference_ids, None
    ) == image_request_fingerprint(
        plan.prompt,
        plan.aspect_ratio,
        plan.reference_ids,
        None,
        extra=plan.fingerprint_extra,
    )
    plan.cleanup()


# ---------- 7. ingestion différée à la fin du tour ----------


def test_ingestion_planifiee_puis_lancee_au_flush(env, monkeypatch):
    from backend.ingestion import attachment_ingest
    from backend.services.chat_image_plan import flush_generated_ingestion

    calls: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda ids: calls.append(list(ids))
    )
    _install(monkeypatch, Capture(env, artifact_id="art_ingested"))
    result = _execute(env, prompt="image ingeree", run_id="req_ingest")

    assert result["ok"] is True
    assert calls == [], "la génération ne doit pas lancer l'ingestion pendant le tour"
    assert "ingestion planifiée" in result["result"]

    flush_generated_ingestion("req_ingest")
    assert calls == [["art_ingested"]]


def test_ingestion_en_echec_ne_casse_pas_la_generation(env, monkeypatch):
    from backend.ingestion import attachment_ingest
    from backend.services.chat_image_plan import flush_generated_ingestion

    def boom(ids):
        raise RuntimeError("worker indisponible")

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", boom)
    _install(monkeypatch, Capture(env, artifact_id="art_no_ingest"))
    result = _execute(env, prompt="image sans ingestion", run_id="req_no_ingest")

    assert result["ok"] is True
    assert result["image_published"] is True
    assert "ingestion planifiée" in result["result"]
    flush_generated_ingestion("req_no_ingest")  # ne lève jamais


def test_flush_generated_ingestion_est_idempotent(env, monkeypatch):
    from backend.ingestion import attachment_ingest
    from backend.services.chat_image_plan import (
        flush_generated_ingestion,
        trigger_generated_ingestion,
    )

    calls: list[list[str]] = []
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda ids: calls.append(list(ids))
    )

    assert trigger_generated_ingestion("art_once", run_id="req_idem") == "planifiée"
    flush_generated_ingestion("req_idem")
    flush_generated_ingestion("req_idem")

    assert calls == [["art_once"]], "un seul lancement pour deux flushs"


def test_finalize_run_lance_l_ingestion_avant_le_cleanup(monkeypatch):
    """`finalize_run` lance l'ingestion différée puis nettoie la requête.

    L'ordre vis-à-vis de ``message_completed`` n'est PAS prouvé ici : il est
    garanti par l'appel de ``finalize_run`` dans le ``finally`` de
    ``run_agent_loop``, lui-même placé après l'émission de ``message_completed``.
    Ce test prouve seulement l'ordre interne ingestion → cleanup.
    """
    from backend.services import chat_image_plan

    order: list[str] = []
    monkeypatch.setattr(
        chat_image_plan,
        "flush_generated_ingestion",
        lambda run_id: order.append(f"flush:{run_id}"),
    )
    monkeypatch.setattr(
        loop_mod,
        "cleanup_request",
        lambda request_id: order.append(f"cleanup:{request_id}"),
    )

    loop_mod.finalize_run("req_finalize", "req_finalize")

    assert order == ["flush:req_finalize", "cleanup:req_finalize"]


# ---------- 8. source_image_id implique edit ----------


def test_source_sans_operation_est_inferee_en_edit():
    args = GenerateImageArgs.model_validate(
        {"prompt": "un chapeau", "source_image_id": "doc_1"}
    )
    assert args.operation == "edit"


def test_create_avec_source_est_reinterprete_en_edit():
    args = GenerateImageArgs.model_validate(
        {"prompt": "un chapeau", "operation": "create", "source_image_id": "doc_1"}
    )
    assert args.operation == "edit"


@pytest.mark.parametrize(
    "data",
    [
        MappingProxyType({"prompt": "un chapeau", "source_image_id": "doc_1"}),
        UserDict({"prompt": "un chapeau", "source_image_id": "doc_1"}),
    ],
)
def test_mapping_avec_source_est_infere_en_edit(data):
    args = GenerateImageArgs.model_validate(data)
    assert args.operation == "edit"


@pytest.mark.parametrize("operation", ["CREATE", "bogus", "", None, 123])
def test_operation_inconnue_avec_source_est_refusee(operation):
    with pytest.raises(ValidationError):
        GenerateImageArgs.model_validate(
            {
                "prompt": "un chapeau",
                "operation": operation,
                "source_image_id": "doc_1",
            }
        )


def test_outpaint_explicite_reste_outpaint_avec_source():
    args = GenerateImageArgs.model_validate(
        {
            "prompt": "étends le ciel",
            "operation": "outpaint",
            "source_image_id": "doc_1",
            "aspect_ratio": "16:9",
        }
    )
    assert args.operation == "outpaint"


def test_sans_source_l_operation_par_defaut_reste_create():
    args = GenerateImageArgs.model_validate({"prompt": "un chat"})
    assert args.operation == "create"


@pytest.mark.parametrize(
    "args",
    [
        {"prompt": "un chapeau", "source_image_id": "doc_1"},
        {"prompt": "un chapeau", "operation": "create", "source_image_id": "doc_1"},
    ],
    ids=["sans-operation", "create-explicite"],
)
def test_edit_infere_passe_la_validation_complete_et_genere(env, monkeypatch, args):
    _add_image(env, "art_source", label="doc_1")
    capture = _install(monkeypatch, Capture(env))
    result = loop_mod._execute_generate_image(
        {
            "id": "call_req_inferred",
            "args": args,
        },
        manager=None,
        conv_id="conv1",
        request_id="req_inferred",
        run_id="req_inferred",
    )

    assert result["ok"] is True
    assert "BAD_ARGS" not in result["result"]
    assert capture.kwargs["mode"] == "global"
    assert capture.kwargs["source_artifact_id"] == "art_source"
