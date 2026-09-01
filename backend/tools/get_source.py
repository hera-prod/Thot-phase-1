# tools/get_source.py — Retrouve une source historique (Réponses.md n°10)
from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from backend.tools.base import BaseTool, ToolContext, ToolResult

class GetSourceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, description="Identifiant exact d'une source listée dans le registre")
    reason: str = Field(min_length=1, max_length=500, description="Pourquoi la source doit être récupérée")

class GetSourceTool(BaseTool):
    name = "get_source"
    description = (
        "Retrouve une source précédemment jointe à cette conversation. "
        "Utilise cet outil lorsqu'une question semble faire référence à une image, un PDF, un texte ou un autre fichier déjà mentionné plus tôt, mais que le fichier n'est pas joint au tour courant. "
        "L'outil renvoie les métadonnées sûres et un chemin de travail utilisable uniquement par les outils autorisés."
    )
    input_model = GetSourceArgs
    timeout_s = 5.0
    max_output_chars = 2000

    async def execute(self, args: GetSourceArgs, ctx: ToolContext) -> ToolResult:
        try:
            from backend.db.database import SessionLocal
            from backend.db.models import Source
            db = SessionLocal()
            try:
                src = db.get(Source, args.source_id)
                if src is None:
                    return ToolResult(ok=False, display_summary="get_source: introuvable", error=f"source_id {args.source_id} introuvable")
                # Résout working_path persistant
                working_path = None
                if src.relative_path:
                    media_root = Path.home() / ".local" / "share" / "thot"
                    cand = media_root / src.relative_path
                    if cand.exists():
                        working_path = str(cand)
                if not working_path and src.path and Path(src.path).exists():
                    working_path = src.path
                if not working_path:
                    return ToolResult(ok=False, display_summary="get_source: fichier indisponible", error="working_path introuvable")
                # Update last_accessed
                try:
                    from datetime import datetime, timezone
                    src.last_accessed_at = datetime.now(timezone.utc)
                    db.commit()
                except Exception:
                    pass
                return ToolResult(ok=True, content=f'{{"source_id":"{src.id}","media_kind":"{src.media_kind}","mime_type":"{src.mime_type}","original_name":"{src.original_name}","working_path":"{working_path}","analysis_version":{src.analysis_version or 0}}}', display_summary=f"get_source: {src.original_name} -> {working_path}")
            finally:
                try: db.close()
                except: pass
        except Exception as e:
            return ToolResult(ok=False, display_summary="get_source: erreur", error=str(e)[:500])
