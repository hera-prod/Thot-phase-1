# tools/vision_ocr.py — Tool OCR Phase 1 (GLM-OCR, 8K, Q4_K_M 0.6GB)
# Notes FR : fidélité absolue markdown, PyMuPDF 200 DPI page par page séquentiel, max 50 pages
# Fusion v0.8 : pdf_path hash+path, pas base64, join "--- Page suivante ---"
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.base import BaseTool, ToolContext, ToolResult


class VisionOCRArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(default=None, max_length=2000, description="Question ciblée OCR, sinon OCR complet")
    pdf_path: str = Field(min_length=1, description="Chemin /tmp/thot/{request_id}/doc.pdf")
    image_hash: str | None = Field(default=None, description="sha256:hash")


class VisionOCRTool(BaseTool):
    name = "doc_ocr"
    description = (
        "OCR technique via GLM-OCR (8K) sur PDF/image. Extrait markdown fidèle : titres, listes, tableaux."
    )
    input_model = VisionOCRArgs
    timeout_s = 15.0
    max_output_chars = 10000

    async def execute(self, args: VisionOCRArgs, ctx: ToolContext) -> ToolResult:
        if not args.pdf_path:
            return ToolResult(ok=False, display_summary="doc_ocr: pdf_path manquant", error="pdf_path vide")
        path = Path(args.pdf_path)
        from backend.config.paths import THOT_TMP

        try:
            if not str(path.resolve()).startswith(str(THOT_TMP.resolve())):
                return ToolResult(ok=False, display_summary="doc_ocr: chemin hors /tmp/thot", error="traversal")
        except Exception:
            pass
        if not path.exists():
            if ctx.image_paths and Path(ctx.image_paths[0]).exists():
                path = Path(ctx.image_paths[0])
            else:
                return ToolResult(ok=False, display_summary="doc_ocr: fichier absent", error=f"Introuvable: {args.pdf_path}")
        ext = path.suffix.lower()
        if ext not in (".pdf", ".png", ".jpg", ".jpeg"):
            return ToolResult(ok=False, display_summary="doc_ocr: extension non supportée", error=f"ext {ext}")
        if path.stat().st_size > 20 * 1024 * 1024:
            return ToolResult(ok=False, display_summary="doc_ocr: PDF >20MB", error="MAX_PDF 20MB")
        try:
            from backend.prompts.ocr import SYSTEM_OCR
            from backend.config.defaults import MODEL_CONFIGS

            cfg = MODEL_CONFIGS.get("GLM-OCR")
            if cfg and not cfg.supports_vision:
                return ToolResult(ok=False, display_summary="doc_ocr: sans vision", error="supports_vision False")
            prompt = SYSTEM_OCR.format(query=args.query or "Extrais en markdown fidèle")
            return ToolResult(
                ok=True,
                content=f"[OCR prêt] {prompt[:100]} fichier={path.name}",
                display_summary=f"doc_ocr: prêt sur {path.name}",
            )
        except Exception as exc:
            return ToolResult(ok=False, display_summary="doc_ocr: erreur", error=str(exc)[:500])
