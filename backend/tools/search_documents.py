# tools/search_documents.py — déprécié côté LLM, gardé pour compat
# Découverte bornée (annexe 11 Lot A, T-SRCH). La découverte LLM utilise MAINTENANT
# search_info/find_files (brouillon) ; cet outil reste câblé comme moteur FTS
# sous-jacent des exécuteurs brouillon, plus proposé directement au modèle.
# Façade Pydantic du moteur backend/search (scope AVANT retrieval, jamais de chemins).
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.base import BaseTool, ToolContext, ToolResult


class SearchDocumentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300)
    scope: Literal["conversation", "workspace"] = "conversation"

    types: list[Literal["image", "pdf", "text", "markdown", "csv", "docx"]] | None = (
        Field(default=None, max_length=6)
    )

    status: Literal[
        "any", "unanalysed", "indexed", "partial", "analysed", "failed", "stale"
    ] = "any"

    limit: int = Field(default=10, ge=1, le=20)
    cursor: str | None = None

    purpose: str = Field(
        min_length=1,
        max_length=500,
        description="Ce que la recherche doit permettre de vérifier ou de retrouver.",
    )


class SearchDocumentsTool(BaseTool[SearchDocumentsArgs]):
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
            from backend.db.database import SessionLocal as _SL

            if args.scope == "workspace":
                # Recherche d'INFORMATION au niveau espace (matrice, ligne 3).
                # L'espace est déduit serveur-side de la conversation : le modèle
                # ne choisit pas son périmètre, et aucun artifact_id n'est rendu
                # parce qu'un extrait n'est pas une poignée sur le fichier.
                from backend.search import space_info as _sinfo
                from backend.services import espaces as _esp

                db0 = _SL()
                try:
                    ws_id = _esp.conversation_workspace(db0, conv_id)
                finally:
                    db0.close()
                if not ws_id:
                    # Code de la grille, plus de variant locale : le `scope:
                    # workspace` est un argument LÉGAL — ce qui manque est la
                    # RESSOURCE (l'espace), pas l'argument. RESOURCE_UNAVAILABLE
                    # (« ressource non prête »), l'espace nommé dans le détail.
                    from backend.tools.errors import error_text, ToolErrorCode

                    return ToolResult(
                        ok=False,
                        display_summary="search_documents: hors espace",
                        error=error_text(
                            ToolErrorCode.RESOURCE_UNAVAILABLE,
                            "Cette conversation n'appartient à aucun espace.",
                        ),
                    )
                db = _SL()
                try:
                    page = _sinfo.search_space_information(
                        db,
                        ws_id,
                        query=args.query,
                        limit=args.limit,
                        cursor=args.cursor,
                    )
                finally:
                    db.close()
                return ToolResult(
                    ok=True,
                    content=_json.dumps(page, ensure_ascii=False),
                    display_summary=(
                        f"search_documents: {page.get('returned_count')} extrait(s) "
                        f"dans l'espace (information seule)"
                    ),
                )
            from backend.search import scope as _scope, fts as _fts

            allowed = _scope.resolve_allowed_artifacts(conv_id)
            db = _SL()
            try:
                _fts.ensure_fts(db)
                page = _fts.search(
                    db,
                    query=args.query,
                    scope_kind="conversation",
                    scope_id=conv_id,
                    allowed_ids=allowed,
                    types=list(args.types) if args.types is not None else None,
                    status=args.status,
                    limit=args.limit,
                    cursor=args.cursor,
                )
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            return ToolResult(
                ok=False, display_summary="search_documents: erreur", error=str(e)[:500]
            )
        if not page.get("ok"):
            return ToolResult(
                ok=False,
                display_summary=f"search_documents: {page.get('code')}",
                error=page.get("message", "")[:500],
            )
        return ToolResult(
            ok=True,
            content=_json.dumps(page, ensure_ascii=False),
            display_summary=f"search_documents: {page.get('returned_count')} résultat(s)",
        )
