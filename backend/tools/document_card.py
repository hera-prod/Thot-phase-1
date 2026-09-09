# tools/document_card.py — Ouvre une fiche (annexe 11 Lot B)
# get_source reste l'alias compat ; get_document_card est l'outil explicite et scalable.
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.base import BaseTool, ToolContext, ToolResult


class GetDocumentCardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=500)


class GetDocumentCardTool(BaseTool):
    name = "get_document_card"
    description = (
        "Ouvre la fiche et les métadonnées d'un document déjà identifié par artifact_id. "
        "Utilise-le après search_documents ou lorsqu'un artifact_id est présent dans le registre. "
        "Il ne lit pas le contenu complet ; utilise ensuite read_document, doc_ocr ou vision "
        "si la réponse dépend du contenu exact."
    )
    input_model = GetDocumentCardArgs
    timeout_s = 5.0
    max_output_chars = 3000

    async def execute(self, args: GetDocumentCardArgs, ctx: ToolContext) -> ToolResult:
        import json as _json
        conv_id = getattr(ctx, "conv_id", None) or (ctx.request_id or "")
        try:
            from backend.search import reader as _reader, scope as _scope
            from backend.db.database import SessionLocal as _SL
            allowed = _scope.resolve_allowed_artifacts(conv_id)
            db = _SL()
            try:
                payload = _reader.get_card_payload(db, args.artifact_id, allowed)
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            return ToolResult(ok=False, display_summary="get_document_card: erreur",
                              error=str(e)[:500])
        if not payload.get("ok"):
            return ToolResult(ok=False,
                              display_summary=f"get_document_card: {payload.get('code')}",
                              error=payload.get("message", "")[:500])
        return ToolResult(ok=True, content=_json.dumps(payload, ensure_ascii=False),
                          display_summary=f"get_document_card: {payload.get('name')}")
