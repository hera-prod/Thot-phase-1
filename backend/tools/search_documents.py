# tools/search_documents.py — Découverte bornée (annexe 11 Lot A, T-SRCH)
# Façade Pydantic du moteur backend/search (scope AVANT retrieval, jamais de chemins).
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.base import BaseTool, ToolContext, ToolResult


class SearchDocumentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300)
    scope: Literal["conversation", "workspace"] = "conversation"

    types: list[Literal["image", "pdf", "text", "markdown", "csv", "docx"]] | None = Field(
        default=None, max_length=6)

    status: Literal["any", "unanalysed", "indexed", "partial", "analysed",
                    "failed", "stale"] = "any"

    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = None

    purpose: str = Field(min_length=1, max_length=500,
                         description="Ce que la recherche doit permettre de vérifier ou de retrouver.")


class SearchDocumentsTool(BaseTool):
    name = "search_documents"
    description = (
        "Recherche des fichiers accessibles dans la conversation ou l'espace courant. "
        "Cherche dans le nom, le titre, les mots-clés, les fiches et les extraits indexés. "
        "Utilise query avec des mots-clés précis ; filtre types si le type est connu. "
        "Retourne une page courte de candidats avec artifact_id, nom, type, résumé et raisons du match. "
        "N'utilise pas cet outil pour relire une pièce jointe unique du message courant : "
        "utilise directement vision, doc_ocr ou read_text avec son artifact_id."
    )
    input_model = SearchDocumentsArgs
    timeout_s = 10.0
    max_output_chars = 6000

    async def execute(self, args: SearchDocumentsArgs, ctx: ToolContext) -> ToolResult:
        import json as _json
        conv_id = getattr(ctx, "conv_id", None) or (ctx.request_id or "")
        try:
            from backend.search import scope as _scope, fts as _fts
            from backend.db.database import SessionLocal as _SL
            allowed = _scope.resolve_allowed_artifacts(conv_id)
            db = _SL()
            try:
                _fts.ensure_fts(db)
                page = _fts.search(
                    db, query=args.query, scope_kind=args.scope,
                    scope_id=conv_id, allowed_ids=allowed,
                    types=args.types, status=args.status,
                    limit=args.limit, cursor=args.cursor)
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            return ToolResult(ok=False, display_summary="search_documents: erreur",
                              error=str(e)[:500])
        if not page.get("ok"):
            return ToolResult(ok=False,
                              display_summary=f"search_documents: {page.get('code')}",
                              error=page.get("message", "")[:500])
        return ToolResult(ok=True,
                          content=_json.dumps(page, ensure_ascii=False),
                          display_summary=f"search_documents: {page.get('returned_count')} résultat(s)")
