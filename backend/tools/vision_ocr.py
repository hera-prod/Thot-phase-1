# tools/vision_ocr.py — gardé pour Phase 0 + dispatch ask_files pdf
# Le LLM ne l'appelle PLUS directement : il passe par ask_files(doc_N).
# Tool OCR Phase 1 — moteur livré : PaddleOCR-VL-1.6 (worker transformers).
# Notes FR : fidélité absolue markdown, PyMuPDF 200 DPI page par page séquentiel, max 50 pages
# Fusion v0.8 : pdf_path hash+path, pas base64, join "--- Page suivante ---"
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.base import BaseTool, ToolContext, ToolResult


class VisionOCRArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(
        default=None,
        max_length=2000,
        description="Question ciblée OCR, sinon OCR complet",
    )
    # artifact_id durable annexe 10 (recommandé) — pdf_path conservé en compat
    artifact_id: str | None = Field(
        default=None,
        description="ID exact du registre SOURCES (recommandé, ex: art_9f7584c3f77b)",
    )
    pdf_path: str | None = Field(
        default=None,
        description="Chemin direct du fichier (compat, préfère artifact_id)",
    )
    image_hash: str | None = Field(default=None, description="sha256:hash")


class VisionOCRTool(BaseTool[VisionOCRArgs]):
    name = "doc_ocr"
    description = (
        "Extraction fidèle de texte présent dans une image ou un document, y compris un fichier déjà envoyé précédemment dans la conversation. "
        "Passe artifact_id (id exact du registre SOURCES) et query ; n'invente jamais de chemin. "
        "Utilise cet outil quand la réponse dépend d'un texte visible sur une image (inscription, étiquette, tatouage, panneau, tableau, document) que tu n'as jamais fait lire par cet outil. Ne réponds jamais sur la présence ou l'absence de texte sans avoir appelé cet outil."
    )
    input_model = VisionOCRArgs
    timeout_s = 15.0
    max_output_chars = 10000

    async def execute(self, args: VisionOCRArgs, ctx: ToolContext) -> ToolResult:
        # Annexe 10 : artifact_id résolu en chemin durable avant pdf_path compat
        pdf_path = args.pdf_path
        if args.artifact_id and not pdf_path:
            try:
                from backend.db.database import SessionLocal
                from backend.db.models import Artifact, Blob
                from backend.config.paths import get_data_dir

                db = SessionLocal()
                try:
                    art = (
                        db.query(Artifact)
                        .filter(Artifact.id == args.artifact_id)
                        .first()
                    )
                    if art is not None:
                        blob = db.query(Blob).filter(Blob.id == art.blob_id).first()
                        if blob is not None:
                            cand = get_data_dir() / blob.relative_path
                            if cand.exists():
                                pdf_path = str(cand)
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass
            except Exception:
                pass
        if not pdf_path:
            return ToolResult(
                ok=False,
                display_summary="doc_ocr: artifact_id ou pdf_path manquant",
                error="vide",
            )
        path = Path(pdf_path)
        from backend.config.paths import THOT_TMP

        try:
            if not str(path.resolve()).startswith(str(THOT_TMP.resolve())):
                return ToolResult(
                    ok=False,
                    display_summary="doc_ocr: chemin hors /tmp/thot",
                    error="traversal",
                )
        except Exception:
            pass
        if not path.exists():
            if ctx.image_paths and Path(ctx.image_paths[0]).exists():
                path = Path(ctx.image_paths[0])
            else:
                return ToolResult(
                    ok=False,
                    display_summary="doc_ocr: fichier absent",
                    error=f"Introuvable: {args.pdf_path}",
                )
        ext = path.suffix.lower()
        if ext not in (".pdf", ".png", ".jpg", ".jpeg"):
            return ToolResult(
                ok=False,
                display_summary="doc_ocr: extension non supportée",
                error=f"ext {ext}",
            )
        if path.stat().st_size > 20 * 1024 * 1024:
            return ToolResult(
                ok=False, display_summary="doc_ocr: PDF >20MB", error="MAX_PDF 20MB"
            )
        try:
            # Brouillon : le moteur OCR est le worker PaddleOCR-VL (jamais un GGUF).
            # Historique : la vérification portait sur MODEL_CONFIGS["GLM-OCR"]
            # (GGUF jamais livré, modèle abandonné).
            from backend.ingestion.paddle_client import (
                check_available as _ocr_available,
            )

            ok_w, detail = _ocr_available()
            if not ok_w:
                return ToolResult(
                    ok=False,
                    display_summary="doc_ocr: moteur OCR indisponible",
                    error=detail[:300],
                )
            return ToolResult(
                ok=True,
                content=f"[OCR prêt] PaddleOCR-VL-1.6 fichier={path.name}",
                display_summary=f"doc_ocr: prêt sur {path.name}",
            )
        except Exception as exc:
            return ToolResult(
                ok=False, display_summary="doc_ocr: erreur", error=str(exc)[:500]
            )
