# tools/read_text.py — gardé comme lecteur sous ask_files text
# Lecture déterministe TXT/MD/JSON/CSV (Réponses.md n°10). Le LLM passe MAINTENANT par ask_files.
from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from backend.tools.base import BaseTool, ToolContext, ToolResult


class ReadTextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(
        min_length=1, description="Identifiant exact de la source textuelle"
    )
    purpose: str = Field(
        min_length=1,
        max_length=1000,
        description="Question ou information à rechercher",
    )
    max_chars: int = Field(
        default=10000, ge=500, le=30000, description="Maximum de caractères renvoyés"
    )


class ReadTextTool(BaseTool[ReadTextArgs]):
    name = "read_text"
    description = (
        "Lit le contenu d'un fichier textuel disponible dans la conversation : TXT, Markdown, JSON, CSV, code source ou texte déjà extrait. "
        "Passe source_id (id exact du registre SOURCES, fourni par find_files) et purpose ; n'invente jamais d'id. "
        "Utilise-le lorsqu'une réponse fiable dépend du contenu exact du fichier."
    )
    input_model = ReadTextArgs
    timeout_s = 5.0
    max_output_chars = 30000

    async def execute(self, args: ReadTextArgs, ctx: ToolContext) -> ToolResult:
        try:
            from backend.db.database import SessionLocal
            from backend.db.models import Source

            db = SessionLocal()
            try:
                src = db.get(Source, args.source_id)
                if src is None:
                    return ToolResult(
                        ok=False,
                        display_summary="read_text: introuvable",
                        error=f"source_id {args.source_id} introuvable",
                    )
                working_path = None
                if src.relative_path:
                    media_root = Path.home() / ".local" / "share" / "thot"
                    cand = media_root / src.relative_path
                    if cand.exists():
                        working_path = str(cand)
                if not working_path and src.path and Path(src.path).exists():
                    working_path = src.path
                if not working_path:
                    return ToolResult(
                        ok=False,
                        display_summary="read_text: fichier indisponible",
                        error="fichier indisponible",
                    )
                p = Path(working_path)
                if p.suffix.lower() not in (
                    ".txt",
                    ".md",
                    ".json",
                    ".csv",
                    ".ts",
                    ".js",
                    ".py",
                    ".html",
                    ".css",
                ):
                    # Si c'est image/pdf, suggère doc_ocr/vision
                    return ToolResult(
                        ok=False,
                        display_summary="read_text: type non textuel",
                        error="utilise vision/doc_ocr pour image/pdf",
                    )
                text = p.read_text(encoding="utf-8", errors="replace")
                if len(text) > args.max_chars:
                    text = (
                        text[: args.max_chars]
                        + f"\n\n[Tronqué {len(text)}->{args.max_chars}]"
                    )
                return ToolResult(
                    ok=True,
                    content=text,
                    display_summary=f"read_text: {src.original_name} {len(text)} chars",
                )
            finally:
                try:
                    db.close()
                except:
                    pass
        except Exception as e:
            return ToolResult(
                ok=False, display_summary="read_text: erreur", error=str(e)[:500]
            )
