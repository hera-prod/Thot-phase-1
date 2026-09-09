# tools/read_document.py — Lecture localisée avec evidence_id (annexe 11 Lot B)
# Le backend retourne un extrait + localisateur + evidence_id, jamais 500 pages d'un coup.
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.base import BaseTool, ToolContext, ToolResult


class ReadDocumentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    query: str | None = Field(default=None, max_length=500)
    locator: str | None = None  # offset:N | chars:A-B | page:N (OCR)
    max_chars: int = Field(default=8000, ge=500, le=16000)
    purpose: str = Field(min_length=1, max_length=500)


class ReadDocumentTool(BaseTool):
    name = "read_document"
    description = (
        "Lit un extrait localisé d'un document (texte, Markdown, CSV, code, PDF déjà extrait). "
        "Arguments : artifact_id, locator (offset:N, chars:A-B ou page:N pour l'OCR), "
        "max_chars, purpose. Retourne l'extrait, le localisateur utilisé et un evidence_id "
        "à citer dans la réponse. Pour une image ou un PDF non extrait, utilise vision/doc_ocr."
    )
    input_model = ReadDocumentArgs
    timeout_s = 10.0
    max_output_chars = 16000

    async def execute(self, args: ReadDocumentArgs, ctx: ToolContext) -> ToolResult:
        import json as _json
        conv_id = getattr(ctx, "conv_id", None) or (ctx.request_id or "")
        try:
            from backend.search import observations as _obs, reader as _reader, scope as _scope
            from backend.db.database import SessionLocal as _SL
            allowed = _scope.resolve_allowed_artifacts(conv_id)
            db = _SL()
            try:
                payload = _reader.read_payload(
                    db, args.artifact_id, allowed, query=args.query,
                    locator=args.locator, max_chars=args.max_chars)
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            return ToolResult(ok=False, display_summary="read_document: erreur",
                              error=str(e)[:500])
        if not payload.get("ok"):
            return ToolResult(ok=False,
                              display_summary=f"read_document: {payload.get('code')}",
                              error=payload.get("message", "")[:500])
        # Lecture = travail réutilisable → observation (fiche N+1 en prime)
        try:
            _obs.record_tool_observation(
                args.artifact_id, "read_document", args.query or args.purpose,
                payload.get("excerpt", "") or "", True, run_id=ctx.run_id,
                tool_call_id=None, model_id=None)
        except Exception:
            pass
        return ToolResult(ok=True, content=_json.dumps(payload, ensure_ascii=False),
                          display_summary=f"read_document: {payload.get('locator_used')}")
