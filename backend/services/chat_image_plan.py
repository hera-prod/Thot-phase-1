# services/chat_image_plan.py — Une seule préparation image pour le chat.
# QUI APPELLE : loop._execute_generate_image ET GenerateImageTool.execute.
# POURQUOI : les deux chemins partagent la résolution doc_N, la scène d’édition,
# le staging work/mask et l’empreinte. Deux préparations divergentes produiraient
# deux contrats pour la même demande.
from __future__ import annotations

import logging
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from backend.services import image_service
from backend.services.image_edit_scene import (
    build_global_scene,
    build_outpaint_scene,
    source_format,
)

if TYPE_CHECKING:
    from backend.tools.generate_image import GenerateImageArgs

logger = logging.getLogger(__name__)


@dataclass
class ChatImagePlan:
    operation: str
    prompt: str
    aspect_ratio: str
    seed: int | None
    mode: str
    reference_ids: list[str]
    source_artifact_id: str | None
    generate_kwargs: dict
    fingerprint_extra: dict
    staged_tokens: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        image_service.discard_staged_tokens(self.staged_tokens)


def _resolve_artifact_id(db, conv_id: str | None, value: str) -> str:
    """``doc_N`` → identifiant interne ; ``art_...`` passe tel quel.

    Le contrat public n'expose que des ``doc_N``. Les ``art_...`` restent
    acceptés pour les appels internes et les tests, et subissent la même
    validation de portée que les identifiants résolus depuis un ``doc_N``.

    Un label introuvable lève ``ID_UNKNOWN`` : jamais de repli silencieux sur
    le label lui-même, qui ferait échouer plus loin avec un message moins clair.
    """
    label = (value or "").strip()
    if not label.startswith("doc_"):
        return label
    from backend.search.docrefs import resolve_doc_label

    resolved = resolve_doc_label(db, conv_id or "", label)
    if resolved is None:
        raise ValueError(f"[ID_UNKNOWN] Référence introuvable : {label}.")
    return resolved


def _fingerprint_extra(
    operation: str,
    source_artifact_id: str | None,
    width: int,
    height: int,
    *,
    work_sha256: str | None = None,
    mask_sha256: str | None = None,
    source_transform: dict | None = None,
) -> dict:
    extra = {
        "operation": operation,
        "source_image_id": source_artifact_id or "",
        "canvas_width": width,
        "canvas_height": height,
    }
    if work_sha256:
        extra["work_sha256"] = work_sha256
    if mask_sha256:
        extra["mask_sha256"] = mask_sha256
    if source_transform:
        extra["source_transform"] = source_transform
    return extra


def _prepare_create(args: "GenerateImageArgs", references: list[str]) -> ChatImagePlan:
    mode = "reference" if references else "create"
    prompt, aspect_ratio, refs, seed = image_service.validate_request(
        args.prompt, args.aspect_ratio, references, args.seed, mode=mode
    )
    kwargs: dict = {"mode": mode, "reference_ids": refs}
    if refs:
        kwargs["strength"] = 0.65
    return ChatImagePlan(
        operation="create",
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        seed=args.seed,
        mode=mode,
        reference_ids=refs,
        source_artifact_id=None,
        generate_kwargs=kwargs,
        # Empreinte T2I/référence inchangée : les références sont déjà dans
        # `reference_ids`, et le canevas est porté par `aspect_ratio`.
        fingerprint_extra={},
        staged_tokens=[],
    )


def _prepare_edit(
    db,
    conv_id: str | None,
    args: "GenerateImageArgs",
    source_id: str,
    references: list[str],
    staged: list[str],
) -> ChatImagePlan:
    source_bytes, _artifact = image_service.load_source_image(db, source_id, conv_id)
    scene = build_global_scene(source_bytes)
    prompt, aspect_ratio, refs, _seed = image_service.validate_request(
        args.prompt, scene.aspect_ratio, references, args.seed, mode="global"
    )
    work_token, work_sha256 = image_service.stage_internal_png(scene.work_png)
    staged.append(work_token)
    kwargs = {
        "mode": "global",
        "reference_ids": refs,
        "work_token": work_token,
        "work_sha256": work_sha256,
        "canvas_width": scene.canvas_width,
        "canvas_height": scene.canvas_height,
        "source_artifact_id": source_id,
        "source_size": scene.source_size,
        "strength": 0.65,
    }
    return ChatImagePlan(
        operation="edit",
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        seed=args.seed,
        mode="global",
        reference_ids=refs,
        source_artifact_id=source_id,
        generate_kwargs=kwargs,
        fingerprint_extra=_fingerprint_extra(
            "edit",
            source_id,
            scene.canvas_width,
            scene.canvas_height,
            work_sha256=work_sha256,
        ),
        staged_tokens=[work_token],
    )


def _prepare_outpaint(
    db,
    conv_id: str | None,
    args: "GenerateImageArgs",
    source_id: str,
    staged: list[str],
    source_bytes: bytes | None = None,
) -> ChatImagePlan:
    if source_bytes is None:
        source_bytes, _artifact = image_service.load_source_image(
            db, source_id, conv_id
        )
    scene = build_outpaint_scene(source_bytes, args.aspect_ratio)
    masked = scene.mask_png
    if masked is None:
        raise ValueError("[BAD_ARGS] outpaint requiert un masque de scène.")
    prompt, aspect_ratio, refs, _seed = image_service.validate_request(
        args.prompt,
        scene.aspect_ratio,
        [],
        args.seed,
        mask_kind="fill",
        mode="outpaint",
    )
    work_token, work_sha256 = image_service.stage_internal_png(scene.work_png)
    staged.append(work_token)
    mask_token, mask_sha256 = image_service.stage_internal_png(masked)
    staged.append(mask_token)
    kwargs = {
        "mode": "outpaint",
        "reference_ids": refs,
        "work_token": work_token,
        "work_sha256": work_sha256,
        "mask_token": mask_token,
        "mask_sha256": mask_sha256,
        "canvas_width": scene.canvas_width,
        "canvas_height": scene.canvas_height,
        "source_artifact_id": source_id,
        "source_size": scene.source_size,
        "source_transform": scene.source_rect,
        "mask_kind": "fill",
        "strength": 1.0,
    }
    return ChatImagePlan(
        operation="outpaint",
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        seed=args.seed,
        mode="outpaint",
        reference_ids=refs,
        source_artifact_id=source_id,
        generate_kwargs=kwargs,
        fingerprint_extra=_fingerprint_extra(
            "outpaint",
            source_id,
            scene.canvas_width,
            scene.canvas_height,
            work_sha256=work_sha256,
            mask_sha256=mask_sha256,
            source_transform=scene.source_rect,
        ),
        staged_tokens=[work_token, mask_token],
    )


def _format_nomme_dans_le_prompt(prompt: str, aspect_ratio: str) -> bool:
    """Le prompt réécrit nomme-t-il explicitement le ratio cible ?

    Ce n'est PAS un détecteur d'intention : l'outil est déjà appelé, et un
    prompt conforme ne doit pas nommer le format. C'est seulement un filet
    best-effort si le modèle a malgré tout écrit un ratio exact. On lit le
    prompt visuel déjà réécrit par l'orchestrateur, jamais la demande
    utilisateur d'origine.

    Aucun mot de contenu (``carré`` / ``square``) n'est interprété : « Times
    Square » ou « une table carrée » décrivent la scène, pas un changement de
    format. Seul le ratio exact borné compte.
    """
    texte = prompt or ""
    cible = (aspect_ratio or "").strip()
    if not cible:
        return False
    # Bornes numériques et « : » : sans elles, « 1:1 » matcherait « 91:16 »
    # ou « 1:10 », et réparerait un edit que l'utilisateur n'a pas demandé.
    motif_ratio = rf"(?<![\d:]){re.escape(cible)}(?![\d:])"
    return re.search(motif_ratio, texte, re.IGNORECASE) is not None


def prepare_chat_image(
    db, conv_id: str | None, args: "GenerateImageArgs"
) -> ChatImagePlan:
    """Prépare l’appel image : identifiants résolus, scène stagée, empreinte.

    L’appelant possède le plan et doit appeler ``cleanup()`` dans son ``finally``.
    Toute erreur pendant la préparation libère les tokens déjà écrits puis relance.
    """
    staged: list[str] = []
    try:
        if args.operation in ("edit", "outpaint") and not conv_id:
            raise ValueError(f"[SCOPE_DENIED] {args.operation} exige une conversation.")
        if args.reference_ids and not conv_id:
            raise ValueError(
                "[SCOPE_DENIED] Les références image exigent une conversation."
            )
        source_id = (
            _resolve_artifact_id(db, conv_id, args.source_image_id)
            if args.source_image_id
            else None
        )
        references: list[str] = []
        for reference in args.reference_ids:
            resolved_reference = _resolve_artifact_id(db, conv_id, reference)
            if resolved_reference not in references:
                references.append(resolved_reference)
        if source_id is not None and source_id in references:
            raise ValueError(
                "[BAD_ARGS] source_image_id ne peut pas figurer aussi dans reference_ids."
            )
        if args.operation == "create" and len(references) > 4:
            raise ValueError("[TOO_MANY] create accepte au plus 4 références.")
        if args.operation == "edit" and len(references) > 3:
            raise ValueError(
                "[TOO_MANY] edit accepte au plus 3 références "
                "(4 images au total, source comprise)."
            )
        if args.operation == "outpaint" and references:
            raise ValueError("[BAD_ARGS] outpaint n’accepte pas de références.")
        if args.operation == "create":
            return _prepare_create(args, references)
        if source_id is None:
            raise ValueError(f"[BAD_ARGS] {args.operation} exige source_image_id.")
        if args.operation == "edit":
            # Un `aspect_ratio` explicite non-défaut qui diffère du format
            # source est une demande de changement de format ; `edit` ne peut
            # pas l'honorer. On répare vers `outpaint` au lieu de produire
            # silencieusement une image au mauvais format. Le défaut 1:1 reste
            # ambigu, sauf si le prompt réécrit le nomme : c'est le seul cas où
            # la valeur cible ne peut pas être distinguée d'un `edit` ordinaire.
            format_demande = args.aspect_ratio != "1:1" or (
                _format_nomme_dans_le_prompt(args.prompt, args.aspect_ratio)
            )
            if format_demande:
                source_bytes, _artifact = image_service.load_source_image(
                    db, source_id, conv_id
                )
                if source_format(source_bytes) != args.aspect_ratio:
                    if references:
                        logger.warning(
                            "edit réparé en outpaint pour %s : références %s "
                            "ignorées (outpaint n’en accepte pas).",
                            source_id,
                            references,
                        )
                    plan = _prepare_outpaint(
                        db,
                        conv_id,
                        args,
                        source_id,
                        staged,
                        source_bytes=source_bytes,
                    )
                    if references:
                        plan.notes.append(
                            "Références ignorées pour le changement de format : "
                            f"{', '.join(references)}."
                        )
                    return plan
            plan = _prepare_edit(db, conv_id, args, source_id, references, staged)
        elif args.operation == "outpaint":
            plan = _prepare_outpaint(db, conv_id, args, source_id, staged)
        else:
            raise ValueError(f"[BAD_ARGS] opération inconnue : {args.operation}.")
        return plan
    except Exception:
        image_service.discard_staged_tokens(staged)
        raise


_PENDING_INGESTION: dict[str, list[str]] = {}
_PENDING_INGESTION_LOCK = threading.Lock()


def defer_generated_ingestion(run_id: str, artifact_id: str) -> None:
    """Planifie l'ingestion d'un artefact à la FIN du run, jamais pendant.

    Le modèle texte (ou le générateur) peut encore tenir un bail quand l'image
    vient d'être publiée : lancer l'ingestion tout de suite la ferait refuser en
    ``ResourceBusy`` et transformerait une image valide en dette terminale.
    """
    if not run_id or not artifact_id:
        return
    with _PENDING_INGESTION_LOCK:
        pending = _PENDING_INGESTION.setdefault(run_id, [])
        if artifact_id not in pending:
            pending.append(artifact_id)


def flush_generated_ingestion(run_id: str) -> None:
    """Lance en UNE fois les ingestions planifiées pour ``run_id``.

    Ne lève jamais : une ingestion manquée est une représentation ultérieure,
    pas une condition de publication de l'image.
    """
    if not run_id:
        return
    with _PENDING_INGESTION_LOCK:
        artifact_ids = _PENDING_INGESTION.pop(run_id, [])
    if not artifact_ids:
        return
    try:
        from backend.ingestion.attachment_ingest import lancer_ingestion

        lancer_ingestion(artifact_ids)
    except Exception as exc:  # noqa: BLE001 — contrat : ne jamais lever
        print(
            f"[ingestion] lancement impossible pour {artifact_ids}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def trigger_generated_ingestion(artifact_id: str, *, run_id: str | None = None) -> str:
    """Planifie ou lance l'ingestion en arrière-plan ; ne lève jamais.

    Avec ``run_id``, l'ingestion est différée à ``flush_generated_ingestion`` et
    l'état rendu est ``"planifiée"``. Sans ``run_id`` (chemin outil direct), elle
    est lancée immédiatement. L'état ``"indisponible"`` dit que le lancement a
    échoué ; le résultat de génération reste valide dans tous les cas.
    """
    if run_id:
        defer_generated_ingestion(run_id, artifact_id)
        return "planifiée"
    try:
        from backend.ingestion.attachment_ingest import lancer_ingestion

        lancer_ingestion([artifact_id])
        return "en cours"
    except Exception as exc:  # noqa: BLE001 — contrat : ne jamais lever
        print(
            f"[ingestion] lancement impossible pour {artifact_id}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return "indisponible"


__all__ = [
    "ChatImagePlan",
    "defer_generated_ingestion",
    "flush_generated_ingestion",
    "prepare_chat_image",
    "trigger_generated_ingestion",
]
