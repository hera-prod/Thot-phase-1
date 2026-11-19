# tools/generate_image.py — Outil de production de contenu Phase 2 (C13)
# QUI APPELLE : orchestrateur du chat via toolcall structuré (jamais de chemin arbitraire).
# POURQUOI : même service, même registre, même ordonnanceur que le studio direct —
# seul le chat passe par l'orchestrateur. Le générateur texte n'a JAMAIS accès à cet outil.
# Sorties : identifiants + métadonnées bornées, jamais de chemin libre ni de base64 dans le bus.
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.tools.base import BaseTool, ToolContext, ToolResult, ToolStatus


# Palette fermée des codes d'effet rendus par cet outil (revue clôture phase 2) :
# chaque code est un MEMBRE nommé ici, jamais une chaîne fabriquée par une
# f-string depuis un statut futur du ledger. Un statut hors palette est refusé
# (ValueError à la construction du membre) plutôt qu'émis comme
# `[EFFECT_<INCONNU>]` — une palette ouverte signe des codes que personne
# n'a revus et que l'interface ne connaît pas.
class _CodeEffet(StrEnum):
    RUNNING = "EFFECT_RUNNING"  # effet pris par un run, génération en cours
    STAGED = "EFFECT_STAGED"  # effet pris, sortie réservée non encore publiée
    IN_PROGRESS = "EFFECT_IN_PROGRESS"  # effet déjà en cours dans CE run
    KEY_CONFLICT = "EFFECT_KEY_CONFLICT"  # empreinte prise par un autre effet
    UNCERTAIN = "EFFECT_UNCERTAIN"  # état non résolu : rien produit, empreinte bloquée
    # `published` non-rejouable arrive RÉELLEMENT à la branche « effet indisponible »
    # (le service vérifie `status == "published" and is_effect_replayable(...)` ;
    # l'artefact peut avoir disparu). Garder son code actuel, ne pas confondre le
    # motif avec « déjà en cours ».
    PUBLISHED = "EFFECT_PUBLISHED"


# Le statut du ledger choisit un membre de la palette ; un statut inconnu reste
# hors palette, donc refusé ci-dessous — jamais converti en code fabriqué.
_CODE_EFFET_PAR_STATUT: dict[str, _CodeEffet] = {
    "running": _CodeEffet.RUNNING,
    "staged": _CodeEffet.STAGED,
    "published": _CodeEffet.PUBLISHED,
    "uncertain": _CodeEffet.UNCERTAIN,
}


class GenerateImageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["create", "edit", "outpaint"] = Field(
        default="create",
        description=(
            "create : texte seul ou 1-4 références (reference_ids uniquement, "
            "jamais source_image_id) ; edit : édition globale d’une source, "
            "qui conserve son format ; outpaint : changement de format et "
            "extension vers aspect_ratio. Fournir source_image_id implique "
            "edit (sauf outpaint explicite)."
        ),
    )
    prompt: str = Field(
        max_length=2000,
        description=(
            "Description de l'image à créer ou modifier. Décrire uniquement le "
            "contenu visible ; ne pas ajouter de texte, logo ou slogan non "
            "demandé ; le format se règle par aspect_ratio, pas dans le prompt."
        ),
    )
    aspect_ratio: Literal[
        "1:1", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5", "21:9", "9:21"
    ] = Field(
        default="1:1",
        description=(
            "Ratio officiel ~1 MP. Ignoré en edit ; format cible en outpaint. "
            "Un edit avec un ratio cible explicite non-défaut différent de la "
            "source est traité comme outpaint."
        ),
    )
    seed: int | None = Field(
        default=None, ge=0, le=4294967295, description="Seed optionnel dans [0, 2^32-1]"
    )
    source_image_id: str | None = Field(
        default=None,
        description=(
            "Image source de edit/outpaint ; fournir ce champ avec create est "
            "interprété comme edit."
        ),
    )
    reference_ids: list[str] = Field(
        default_factory=list,
        description="0-4 images de référence doc_N du contexte.",
    )

    @field_validator("prompt")
    @classmethod
    def _normalize_prompt(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("aspect_ratio", mode="before")
    @classmethod
    def _check_aspect_ratio(cls, v: str) -> str:
        v = (v or "").strip() or "1:1"
        if v not in (
            "1:1",
            "16:9",
            "9:16",
            "3:2",
            "2:3",
            "4:3",
            "3:4",
            "5:4",
            "4:5",
            "21:9",
            "9:21",
        ):
            raise ValueError(
                "[ENUM_INVALID] aspect_ratio parmi 1:1, 16:9, 9:16, 3:2, 2:3, 4:3, 3:4, 5:4, 4:5, 21:9, 9:21."
            )
        return v

    @field_validator("seed")
    @classmethod
    def _check_seed(cls, v: int | None) -> int | None:
        if v is not None and (
            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 4294967295
        ):
            raise ValueError(
                "[BAD_ARGS] seed doit être un entier entre 0 et 4294967295."
            )
        return v

    @model_validator(mode="before")
    @classmethod
    def _infer_edit_from_source(cls, data: object) -> object:
        """Une source désigne par définition l'image à modifier.

        `create` ne produit une image qu'à partir du texte ou de
        `reference_ids` : il n'a jamais de `source_image_id`. Forcer `edit`
        dès qu'une source est fournie rend cette combinaison impossible plutôt
        que de la refuser — et évite la boucle de re-ask où le modèle répète
        le même `[BAD_ARGS]`. L'inférence vaut pour tout `Mapping` et ne
        s'applique qu'à une opération absente ou explicitement `create` :
        une valeur inconnue (`"CREATE"`, `123`, `None`…) n'est pas absorbée,
        elle poursuit vers le `Literal` et échoue en validation. `outpaint`
        reste un choix explicite : il étend aussi une source, mais vers un
        autre format.
        """
        if not isinstance(data, Mapping):
            return data
        source = data.get("source_image_id")
        if not (isinstance(source, str) and source.strip()):
            return data
        if "operation" in data and data.get("operation") != "create":
            return data
        return {**data, "operation": "edit"}

    @model_validator(mode="after")
    def _check_operation_consistency(self) -> "GenerateImageArgs":
        operation = self.operation
        if not self.prompt and operation != "outpaint":
            raise ValueError(
                "[EMPTY_QUERY] Décris l’image à créer ou modifier (instruction vide)."
            )

        source = _clean_optional_id(self.source_image_id)
        references = _dedupe_ids(self.reference_ids)
        object.__setattr__(self, "source_image_id", source)
        object.__setattr__(self, "reference_ids", references)

        if operation == "create":
            # Inatteignable pour tout `Mapping` : le validateur « before »
            # force edit dès qu'une source est fournie. On garde néanmoins un
            # refus explicite plutôt que d'ignorer silencieusement la source.
            if source:
                raise ValueError(
                    "[BAD_ARGS] create n’accepte pas source_image_id ; "
                    "utilise edit pour modifier une image."
                )
            if len(references) > 4:
                raise ValueError("[TOO_MANY] create accepte au plus 4 références.")
            return self

        if not source:
            raise ValueError(f"[BAD_ARGS] {operation} exige source_image_id.")

        if operation == "edit":
            if len(references) > 3:
                raise ValueError(
                    "[TOO_MANY] edit accepte au plus 3 références "
                    "(4 images au total, source comprise)."
                )
            if source in references:
                raise ValueError(
                    "[BAD_ARGS] source_image_id ne peut pas figurer aussi dans reference_ids."
                )
            return self

        if operation == "outpaint":
            if "aspect_ratio" not in self.model_fields_set:
                raise ValueError(
                    "[BAD_ARGS] outpaint exige aspect_ratio (format cible)."
                )
            if references:
                raise ValueError("[BAD_ARGS] outpaint n’accepte pas de références.")
        return self


def _clean_optional_id(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _dedupe_ids(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for raw in values or []:
        value = (raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


class GenerateImageTool(BaseTool[GenerateImageArgs]):
    name = "generate_image"
    description = (
        "Crée, édite ou étend une image locale. operation=create produit une image "
        "depuis le texte seul ou 1-4 images de référence ; operation=edit modifie "
        "globalement une source (jusqu’à 3 références) ; operation=outpaint étend la "
        "source vers un autre aspect_ratio, sans référence. Changer le format "
        "d’une image existante se fait avec outpaint, pas edit ; outpaint "
        "n’utilise aucune référence. Une image existante à "
        "modifier se passe en source_image_id et devient edit automatiquement ; "
        "create n’utilise que reference_ids. Passe prompt (0-2000 "
        "caractères, vide accepté seulement pour outpaint), aspect_ratio (1:1, 16:9, "
        "9:16, 3:2, 2:3, 4:3, 3:4, 5:4, 4:5, 21:9, 9:21), seed optionnel, "
        "source_image_id et reference_ids (uniquement des doc_N fournis par le "
        "contexte, jamais inventés). N’ajoute aucun texte, logo ou slogan non "
        "demandé ; le format se règle par aspect_ratio. 4 images maximum au total, "
        "source comprise ; une seule image est produite par appel. Retourne "
        "l'artifact_id publié et "
        "l'état d'ingestion, jamais de chemin ni de base64. Le moteur peut être "
        "indisponible (SERVICE_UNAVAILABLE) : transmets alors la limite au brief."
    )
    input_model = GenerateImageArgs
    timeout_s = 600.0  # génération diffusion réelle : minutes, pas secondes
    max_output_chars = 8000

    async def execute(self, args: GenerateImageArgs, ctx: ToolContext) -> ToolResult:
        from backend.services import image_service as svc
        from backend.services.effect_ledger import (
            clear_staged_path,
            EffectConflict,
            EffectInProgress,
            artifact_event_key,
            claim_image_effect,
            image_request_fingerprint,
            is_effect_replayable,
            mark_event_published_by_key,
            mark_published,
            mark_running,
            mark_uncertain,
            set_staged_path,
        )

        from backend.services.chat_image_plan import (
            prepare_chat_image,
            trigger_generated_ingestion,
        )

        available, reason = svc.is_available()
        if not available:
            return ToolResult(
                ok=False,
                display_summary="generate_image: moteur indisponible",
                error=f"[SERVICE_UNAVAILABLE] {reason}",
            )
        try:
            import torch

            if torch.cuda.is_available():
                free, _ = torch.cuda.mem_get_info()
                if free < 8 * 1024**3:
                    return ToolResult(
                        ok=False,
                        display_summary="generate_image: VRAM occupée",
                        error="[OUT_OF_MEMORY] VRAM insuffisante (modèle texte résident ?). Relance via le chat/studio (unload C07).",
                    )
        except Exception:
            pass

        from backend.db.database import SessionLocal
        from backend.db.models import ImageEffect, Run
        from backend.inference.ipc import emit

        effect_run_id = ctx.run_id or ctx.request_id
        db = SessionLocal()
        plan = None
        claim = None
        try:
            try:
                plan = prepare_chat_image(db, ctx.conv_id, args)
            except ValueError as exc:
                return ToolResult(
                    ok=False,
                    display_summary="generate_image: demande invalide",
                    error=str(exc)[:500],
                )
            fingerprint = image_request_fingerprint(
                plan.prompt,
                plan.aspect_ratio,
                plan.reference_ids,
                args.seed,
                extra=plan.fingerprint_extra,
            )
            logical_action_id = f"image:{fingerprint[:32]}"
            claim = claim_image_effect(
                db,
                run_id=effect_run_id,
                logical_action_id=logical_action_id,
                output_index=0,
                fingerprint=fingerprint,
                tool_call_id=ctx.tool_call_id,
            )
            if (
                claim.status == "published"
                and claim.output_artifact_id
                and is_effect_replayable(db, claim.effect_id, ctx.conv_id)
            ):
                run = db.get(Run, effect_run_id)
                if run is not None:
                    run.status = "completed"
                    run.output_artifact_id = claim.output_artifact_id
                    db.commit()
                delivered = emit(
                    "artifact_created",
                    {
                        "request_id": ctx.request_id,
                        "run_id": effect_run_id,
                        "artifact_id": claim.output_artifact_id,
                        "experimental": True,
                        "deduplicated": True,
                    },
                    run_id=effect_run_id,
                    conversation_id=ctx.conv_id,
                )
                if delivered is not False:
                    mark_event_published_by_key(
                        db, artifact_event_key(effect_run_id, claim.output_artifact_id)
                    )
                return ToolResult(
                    ok=True,
                    content=f"[generate_image] {claim.output_artifact_id} (déjà publié)",
                    display_summary=f"generate_image: {claim.output_artifact_id}",
                )
            if claim.status != "claimed":
                # Palette fermée : le statut du ledger choisit un membre nommé.
                # Un statut hors palette est refusé (INTERNAL_ERROR explicite)
                # au lieu de fabriquer `[EFFECT_<STATUT>]` à la volée.
                membre = _CODE_EFFET_PAR_STATUT.get(str(claim.status or ""))
                if membre is None:
                    return ToolResult(
                        status=ToolStatus.FAILED,
                        display_summary="generate_image: état d'effet inconnu",
                        error=f"[INTERNAL_ERROR] statut d'effet hors palette : {claim.status!r}.",
                    )
                return ToolResult(
                    ok=False,
                    display_summary="generate_image: effet indisponible",
                    error=f"[{membre}] Requête déjà en cours ou état incertain.",
                )
            claim = mark_running(db, claim)

            def mark_effect_published(session, artifact_id: str) -> None:
                row = session.get(ImageEffect, claim.effect_id)
                if row is not None:
                    row.status = "published"
                    row.output_artifact_id = artifact_id
                    row.error = None
                run = session.get(Run, effect_run_id)
                if run is not None:
                    run.output_artifact_id = artifact_id

            from backend.config.paths import get_data_dir

            staged_path = str(
                Path(get_data_dir()) / "staged" / f"{claim.effect_id}.png"
            )
            set_staged_path(db, claim.effect_id, staged_path)
            # `reference_ids` voyage en cinquième position ; le retirer du
            # dictionnaire évite un doublon mot-clé/positionnel.
            service_kwargs = dict(plan.generate_kwargs)
            service_kwargs.pop("reference_ids", None)
            pub = svc.generate(
                None,
                effect_run_id,
                plan.prompt,
                plan.aspect_ratio,
                plan.reference_ids,
                claim.effective_seed,
                conv_id=ctx.conv_id,
                idempotency_key=claim.effect_id,
                origin="tool-direct",
                before_commit=mark_effect_published,
                staged_path=staged_path,
                **service_kwargs,
            )
            artifact_id = str(pub["artifact_id"])
            current = db.get(ImageEffect, claim.effect_id)
            if current is None or current.status != "published":
                mark_published(db, claim.effect_id, artifact_id)
            clear_staged_path(db, claim.effect_id)
            delivered = emit(
                "artifact_created",
                {
                    "request_id": ctx.request_id,
                    "run_id": effect_run_id,
                    "artifact_id": artifact_id,
                    "experimental": bool(pub.get("experimental", True)),
                },
                run_id=effect_run_id,
                conversation_id=ctx.conv_id,
            )
            if delivered is not False:
                mark_event_published_by_key(
                    db, artifact_event_key(effect_run_id, artifact_id)
                )
            ingestion = trigger_generated_ingestion(artifact_id)
            stats = pub.get("stats", {})
            notes_suffix = (" ; " + " ".join(plan.notes)) if plan.notes else ""
            return ToolResult(
                ok=True,
                content=f"[generate_image] {artifact_id} ({stats.get('width')}x{stats.get('height')}, seed {stats.get('seed')}, {stats.get('duration_s')}s, backend {stats.get('backend')}, ingestion {ingestion}{notes_suffix})",
                display_summary=f"generate_image: {artifact_id}",
            )
        except EffectConflict as exc:
            return ToolResult(
                ok=False,
                display_summary="generate_image: conflit d’effet",
                error=f"[{_CodeEffet.KEY_CONFLICT}] {exc}",
            )
        except EffectInProgress as exc:
            return ToolResult(
                ok=False,
                display_summary="generate_image: effet en cours",
                error=f"[{_CodeEffet.IN_PROGRESS}] {exc}",
            )
        except Exception as exc:
            if claim is not None:
                try:
                    mark_uncertain(db, claim.effect_id, f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass
            if "[CANCELLED]" in str(exc):
                return ToolResult(
                    status=ToolStatus.CANCELLED,
                    display_summary="generate_image: annulé",
                    error=str(exc)[:500],
                )
            return ToolResult(
                status=ToolStatus.FAILED,
                display_summary="generate_image: échec",
                error=f"[INTERNAL_ERROR] {type(exc).__name__}: {exc}"[:800],
            )
        finally:
            if plan is not None:
                plan.cleanup()
            db.close()
