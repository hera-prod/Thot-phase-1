# tools/vision_general.py — Tool vision Phase 1 (LFM2.5-VL-3B, 32K, dense)
# Notes FR : hash+path jamais base64, SYSTEM_VISION 3-5 phrases, temp0.2, via manager séquentiel
# Fusion v0.8 : file_id→image_path simplifié Phase 1
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.base import BaseTool, ToolContext, ToolResult


class VisionGeneralArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="Décris cette image en détail.", max_length=2000, description="Question sur l'image")
    image_path: str = Field(min_length=1, description="Chemin /tmp/thot/{request_id}/{hash}.png")
    image_hash: str | None = Field(default=None, description="sha256:hash pour dedup")


class VisionGeneralTool(BaseTool):
    name = "vision"
    description = (
        "Décris une image généraliste via LFM2.5-VL-3B (32K, dense). "
        "À utiliser pour photos, screenshots, scènes, objets, couleurs."
    )
    input_model = VisionGeneralArgs
    timeout_s = 15.0
    max_output_chars = 8000

    async def execute(self, args: VisionGeneralArgs, ctx: ToolContext) -> ToolResult:
        # Early exit — garde-fou fichier
        if not args.image_path:
            return ToolResult(ok=False, display_summary="vision: image_path manquant", error="image_path vide")
        path = Path(args.image_path)
        # Traversal guard
        from backend.config.paths import THOT_TMP

        try:
            if not str(path.resolve()).startswith(str(THOT_TMP.resolve())):
                return ToolResult(ok=False, display_summary="vision: chemin hors /tmp/thot", error="traversal")
        except Exception:
            pass
        if not path.exists():
            # fallback sur ctx.image_paths[0] si fourni par frontend
            if ctx.image_paths and Path(ctx.image_paths[0]).exists():
                path = Path(ctx.image_paths[0])
            else:
                return ToolResult(ok=False, display_summary="vision: fichier absent", error=f"Introuvable: {args.image_path}")
        if path.stat().st_size > 5 * 1024 * 1024:
            return ToolResult(ok=False, display_summary="vision: image >5MB", error="MAX_IMAGE 5MB")
        # Lecture via manager — le vrai inference est fait dans loop via manager.use_model
        # Ici on fait l'appel si appelé hors loop (test) ; sinon loop injecte directement
        try:
            # Import paresseux pour éviter cycle
            from backend.prompts.vision import SYSTEM_VISION
            from backend.config.defaults import MODEL_CONFIGS

            # Vérif modèle supporte vision
            cfg = MODEL_CONFIGS.get("LFM2.5-VL-3B")
            if cfg and not cfg.supports_vision:
                return ToolResult(ok=False, display_summary="vision: modèle sans vision", error="supports_vision=False")
            # Simulation — vrai call fait dans loop.py via manager
            # On retourne placeholder si pas de manager (test)
            prompt = SYSTEM_VISION.format(query=args.query)
            # Le loop s'occupera du vrai Llama ; ici on signale que le tool est prêt
            return ToolResult(
                ok=True,
                content=f"[Vision prêt] {prompt[:100]} image={path.name}",
                display_summary=f"vision: prêt sur {path.name}",
            )
        except Exception as exc:
            return ToolResult(ok=False, display_summary="vision: erreur", error=str(exc)[:500])
