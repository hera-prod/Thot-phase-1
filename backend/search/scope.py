# backend/search/scope.py — Autorisation AVANT retrieval (annexe 11, Étape 0)
#
# D'OÙ VIENT CE FICHIER : Google/Microsoft (permission trimming) + annexe 11 §Étape 0 —
# filtrer les droits AVANT FTS/embeddings/rerank/LLM. Un artifact_id connu ou deviné
# n'est JAMAIS une autorisation d'accès.
# OÙ VA QUOI : resolve_allowed_artifacts() est appelé par fts.search() et par les
# exécuteurs d'outils (vision/doc_ocr/read_text/get_source) AVANT toute lecture.
# POURQUOI ICI ET PAS DANS loop.py : un seul point de vérité du scope, testable sans LLM.
from __future__ import annotations


def resolve_allowed_artifacts(conversation_id: str | None,
                              workspace_id: str | None = None) -> set[str]:
    """Ensemble des artifact_ids accessibles dans le scope effectif du run.

    Lot A : scope conversation (liens ArtifactLink actifs). Le scope workspace
    suivra au Lot B quand les espaces seront activés — le paramètre est déjà là.
    """
    allowed: set[str] = set()
    if not conversation_id and not workspace_id:
        return allowed
    try:
        from backend.db.database import SessionLocal as _SL
        from backend.db.models import Artifact as _Art, ArtifactLink as _Link
        db = _SL()
        try:
            if conversation_id:
                rows = (
                    db.query(_Link.artifact_id)
                    .filter(_Link.scope_kind == "conversation",
                            _Link.scope_id == conversation_id,
                            _Link.removed_at.is_(None))
                    .all()
                )
                allowed.update(r[0] for r in rows)
            if workspace_id:
                rows = (
                    db.query(_Link.artifact_id)
                    .filter(_Link.scope_kind == "workspace",
                            _Link.scope_id == workspace_id,
                            _Link.removed_at.is_(None))
                    .all()
                )
                allowed.update(r[0] for r in rows)
            if allowed:
                # Exclut les artefacts supprimés (soft-delete) même si le lien subsiste
                try:
                    doomed = {
                        a.id for a in
                        db.query(_Art).filter(_Art.id.in_(sorted(allowed)),
                                              _Art.deleted_at.is_not(None)).all()
                    }
                    allowed.difference_update(doomed)
                except Exception:
                    pass
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        pass
    return allowed


def check_artifact_scope(artifact_id: str, allowed: set[str]) -> dict | None:
    """Retourne une erreur ARTIFACT_NOT_IN_SCOPE si hors scope, sinon None.

    Ne révèle JAMAIS si le fichier existe ailleurs (pas de différence de message
    entre « inexistant » et « interdit ») — cf. T-SRCH-07.
    """
    if artifact_id in allowed:
        return None
    return {
        "ok": False,
        "code": "ARTIFACT_NOT_IN_SCOPE",
        "message": "Document non accessible dans le périmètre de cette conversation.",
    }
