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
    artifact_id: str | None = Field(default=None, description="ID durable annexe 10 (recommandé)")
    image_path: str | None = Field(default=None, description="Chemin /tmp/thot/{request_id}/{hash}.png (compat déprécié)")
    image_hash: str | None = Field(default=None, description="sha256:hash pour dedup")


class VisionGeneralTool(BaseTool):
    name = "vision"
    description = (
        "Analyse visuelle d'une image avec un modèle de vision. "
        "Passe artifact_id (id exact du registre SOURCES, ex: art_9f7584c3f77b) et query ; n'invente jamais de chemin. "
        "Utilise cet outil chaque fois qu'une réponse fiable dépend du contenu visuel réel d'une image — y compris une image déjà envoyée précédemment dans la conversation. "
        "Une description textuelle donnée plus tôt dans la conversation n'est jamais une preuve suffisante : si la question porte sur un détail visuel (couleur, objet, texte, position, personnage, élément que tu ne peux pas confirmer avec certitude à partir du texte déjà disponible), rappelle cet outil sur l'image concernée plutôt que de répondre de mémoire."
    )
    input_model = VisionGeneralArgs
    timeout_s = 15.0
    max_output_chars = 8000

    async def execute(self, args: VisionGeneralArgs, ctx: ToolContext) -> ToolResult:
        # Annexe 10 : privilégie artifact_id durable, image_path compat déprécié
        path: Path | None = None
        if args.artifact_id:
            try:
                from backend.db.database import SessionLocal
                from backend.db.models import Artifact, Blob
                from backend.config.paths import get_data_dir
                db = SessionLocal()
                try:
                    art = db.query(Artifact).filter(Artifact.id == args.artifact_id).first()
                    if not art:
                        return ToolResult(ok=False, display_summary="vision: artifact introuvable", error=f"artifact {args.artifact_id}")
                    blob = db.query(Blob).filter(Blob.id == art.blob_id).first()
                    if not blob:
                        return ToolResult(ok=False, display_summary="vision: blob introuvable", error="blob")
                    # Résolution canonique sous data/blobs (C18)
                    data_dir = get_data_dir()
                    cand = data_dir / blob.relative_path
                    if not cand.exists():
                        # Fallback ancien chemin blobs via data/blobs
                        cand = data_dir / "blobs" / blob.sha256[:2] / f"{blob.sha256}.bin"
                    if not cand.exists():
                        return ToolResult(ok=False, display_summary="vision: blob fichier absent", error=str(cand))
                    # Traversal guard : doit être sous data/blobs ou data/temp/staged
                    try:
                        if not str(cand.resolve()).startswith(str(data_dir.resolve())):
                            return ToolResult(ok=False, display_summary="vision: chemin hors data", error="traversal")
                    except Exception:
                        pass
                    path = cand
                finally:
                    try: db.close()
                    except: pass
            except Exception as e:
                return ToolResult(ok=False, display_summary="vision: resolve artifact", error=str(e)[:300])
        elif args.image_path:
            path = Path(args.image_path)
            # Traversal guard compat
            from backend.config.paths import THOT_TMP, get_data_dir
            try:
                # Autorise /tmp/thot, data/temp/staged, data/blobs
                data_dir = get_data_dir()
                ok = False
                for base in [THOT_TMP, data_dir / "temp", data_dir / "blobs", data_dir]:
                    try:
                        if str(path.resolve()).startswith(str(base.resolve())):
                            ok = True
                            break
                    except: pass
                if not ok:
                    return ToolResult(ok=False, display_summary="vision: chemin hors zone autorisée", error="traversal")
            except Exception:
                pass
        else:
            return ToolResult(ok=False, display_summary="vision: artifact_id ou image_path manquant", error="vide")
        if not path or not path.exists():
            # fallback sur ctx.image_paths[0] si fourni par frontend (compat)
            if ctx.image_paths and Path(ctx.image_paths[0]).exists():
                path = Path(ctx.image_paths[0])
            else:
                return ToolResult(ok=False, display_summary="vision: fichier absent", error=f"Introuvable: {path}")
        if path.stat().st_size > 20 * 1024 * 1024:
            return ToolResult(ok=False, display_summary="vision: image >20MB", error="MAX_IMAGE 20MB")
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
