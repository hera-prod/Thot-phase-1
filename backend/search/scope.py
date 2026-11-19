# backend/search/scope.py — Autorisation AVANT retrieval (annexe 11, Étape 0)
#
# D'OÙ VIENT CE FICHIER : Google/Microsoft (permission trimming) + annexe 11 §Étape 0 —
# filtrer les droits AVANT FTS/embeddings/rerank/LLM. Un artifact_id connu ou deviné
# n'est JAMAIS une autorisation d'accès.
# OÙ VA QUOI : resolve_allowed_artifacts() est appelé par fts.search() et par les
# exécuteurs d'outils (vision/doc_ocr/read_text/read_document) AVANT toute lecture.
# POURQUOI ICI ET PAS DANS loop.py : un seul point de vérité du scope, testable sans LLM.
#
# LOT B — ESPACES. Le périmètre d'une conversation n'est plus « ses fichiers » mais
# la COMPOSITION de deux ensembles (`01_architecture_registre_index.md:91-97`) :
#   pièces jointes propres  +  fichiers utilisateurs partagés de l'espace
# et jamais les pièces privées d'une autre conversation, ni un autre espace, même
# si un hash ou un nom est identique (`:63-70`).
#
# L'espace est DÉDUIT DU SERVEUR, jamais reçu de l'appelant. C'est la phrase qui
# rend C18:264 vrai : « Le backend constitue la frontière d'autorisation ». Les six
# outils appellent `resolve_allowed_artifacts(conv_id)` et n'ont rien à changer —
# l'élargissement à l'espace ne peut donc pas être oublié à un endroit.
from __future__ import annotations

from typing import Any


def _workspace_of_conversation(
    conversation_id: str, db: object | None = None
) -> str | None:
    """Espace d'une conversation, lu en base. Jamais une valeur fournie par le client.

    C18:264 : « Le backend constitue la frontière d'autorisation ». C'est LA
    fonction qui rend cette phrase vraie pour les six outils : ils appellent
    `resolve_allowed_artifacts(conv_id)` et le périmètre se déduit d'ici, jamais
    d'un `workspace_id` fourni par le client ou par l'orchestrateur.

    Un espace en corbeille n'accorde rien : la corbeille est un état, pas une
    portée. Si l'appelant demande explicitement un espace, on n'applique PAS ce
    filtre — c'est à l'appelant de décider, et il est le serveur.
    """
    from backend.db.models import Conversation as _Conv, Workspace as _Ws

    own = None
    try:
        if db is None:
            from backend.db.database import SessionLocal as _SL

            own = _SL()
            db = own
        row = (
            db.query(_Conv.workspace_id)  # type: ignore[attr-defined]
            .join(_Ws, _Ws.id == _Conv.workspace_id)
            .filter(_Conv.id == conversation_id, _Ws.state == "active")
            .first()
        )
        return str(row[0]) if row and row[0] else None
    except Exception:
        return None
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass


def _alive(artifact_ids: set[str], db: Any | None = None) -> set[str]:
    """Retire les artefacts supprimés (soft-delete) même si le lien subsiste.

    Réutilise la session de l'appelant quand il en a une : une lecture par
    requête et par couple, c'est du bruit dans un chemin traversé six fois.
    """
    if not artifact_ids:
        return artifact_ids
    own = None
    try:
        from backend.db.models import Artifact as _Art

        if db is None:
            from backend.db.database import SessionLocal as _SL

            own = _SL()
            db = own
        session: Any = db
        doomed = {
            a.id
            for a in session.query(_Art)
            .filter(
                _Art.id.in_(sorted(artifact_ids)),  # type: ignore[attr-defined]
                _Art.deleted_at.is_not(None),
            )
            .all()
        }
        artifact_ids.difference_update(doomed)
    except Exception:
        pass
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass
    return artifact_ids


def resolve_allowed_artifacts(
    conversation_id: str | None, workspace_id: str | None = None
) -> set[str]:
    """Ensemble des artifact_ids accessibles dans le scope effectif du run.

    Périmètre composé : les liens `conversation` de CETTE conversation, plus — si
    elle appartient à un espace actif — les liens `workspace` de cet espace.

    `workspace_id` reste un paramètre explicite pour les appelants qui Resolve
    d'eux-mêmes (recherche au niveau espace, tests). Quand il est absent, il est
    DÉDUIT de la conversation : c'est ce qui évite que la composition soit
    oublié à un seul des six outils.
    """
    allowed: set[str] = set()
    if not conversation_id and not workspace_id:
        return allowed
    try:
        from backend.db.database import SessionLocal as _SL
        from backend.db.models import ArtifactLink as _Link

        db = _SL()
        try:
            # Une seule session pour les trois lectures : cet appel est traversé
            # par les six outils à chaque tour.
            if workspace_id is None and conversation_id:
                workspace_id = _workspace_of_conversation(conversation_id, db)
            if conversation_id:
                rows = (
                    db.query(_Link.artifact_id)
                    .filter(
                        _Link.scope_kind == "conversation",
                        _Link.scope_id == conversation_id,
                        _Link.removed_at.is_(None),
                    )
                    .all()
                )
                allowed.update(r[0] for r in rows)
            if workspace_id:
                rows = (
                    db.query(_Link.artifact_id)
                    .filter(
                        _Link.scope_kind == "workspace",
                        _Link.scope_id == workspace_id,
                        _Link.removed_at.is_(None),
                    )
                    .all()
                )
                allowed.update(r[0] for r in rows)
            _alive(allowed, db)
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        pass
    return allowed


def resolve_space_information_scope(
    workspace_id: str, *, db: Any | None = None, strict: bool = False
) -> set[str]:
    """Périmètre d'INFORMATION d'un espace : le corpus complet, hors manipulable.

    Troisième ligne de la matrice (`01_architecture_registre_index.md:91-97`) :
    la recherche d'information au niveau espace voit « tous les fichiers de
    chats de l'espace + fichiers utilisateurs », et « JAMAIS » l'accès fichier.

    Les conversations en corbeille n'y contribuent plus : leurs liens
    conversationnels sont retirés à la mise en corbeille (§4.1 du cycle de vie
    — « ses fichiers documentaires et recherches web conservées sont retirés
    du corpus documentaire de l'espace avec leurs dérivés »). Le filtre ici est
    le même que `space_conversation_ids` : une conversation supprimée — même
    encore dans sa fenêtre de sept jours — n'est plus une source d'information
    de l'espace.
    """
    # `db` fourni : l'appelant voit ses liens NON COMMITTÉS (mise en corbeille,
    # retrait de dépôt). Sans cela, une invalidation lue dans une SessionLocal
    # séparée ne verrait pas le `removed_at` que la transaction courante vient
    # d'écrire et conclurait à tort que la source est encore accessible.
    # `strict=True` : l'erreur remonte au lieu de devenir un ensemble vide, pour
    # que la mémoire puisse faire un no-op explicite et non une invalidation
    # silencieuse. Par défaut, le contrat historique (set() fail-closed) reste.
    own = None
    try:
        from backend.db.database import SessionLocal as _SL
        from backend.db.models import Conversation as _Conv, ArtifactLink as _Link

        if db is None:
            own = _SL()
            db = own
        session: Any = db
        try:
            conv_ids = [
                str(r[0])
                for r in session.query(_Conv.id)
                .filter(_Conv.workspace_id == workspace_id, _Conv.deleted_at.is_(None))
                .all()
            ]
            allowed: set[str] = set()
            if conv_ids:
                rows = (
                    session.query(_Link.artifact_id)
                    .filter(
                        _Link.scope_kind == "conversation",
                        _Link.scope_id.in_(conv_ids),
                        _Link.removed_at.is_(None),
                    )
                    .all()
                )
                allowed.update(r[0] for r in rows)
            rows = (
                session.query(_Link.artifact_id)
                .filter(
                    _Link.scope_kind == "workspace",
                    _Link.scope_id == workspace_id,
                    _Link.removed_at.is_(None),
                )
                .all()
            )
            allowed.update(r[0] for r in rows)
            return _alive(allowed, session)
        finally:
            if own is not None:
                try:
                    own.close()
                except Exception:
                    pass
    except Exception:
        if strict:
            raise
        return set()


def check_artifact_scope(
    artifact_id: str, allowed: set[str], *, exists: bool | None = None
) -> dict | None:
    """Retourne None si autorisé, sinon erreur spec 01/02 : ID_UNKNOWN vs ID_OUT_OF_SCOPE.

    - ID_UNKNOWN : référence inconnue du registre → retrouver via find_files.
    - ID_OUT_OF_SCOPE : existe mais hors périmètre conversation → ne pas retenter, find_files.
    `exists` permet à l'appelant (DocRef/Artifact) de distinguer sans requête supplémentaire.
    """
    if artifact_id in allowed:
        return None
    if exists is False:
        return {
            "ok": False,
            "code": "ID_UNKNOWN",
            "message": f"L'id {artifact_id} n'existe pas dans le registre. Retrouve le bon id avec find_files.",
        }
    if exists is True:
        return {
            "ok": False,
            "code": "ID_OUT_OF_SCOPE",
            "message": f"L'id {artifact_id} existe mais n'appartient pas au périmètre de cette conversation. Retrouve un fichier accessible avec find_files.",
        }
    # Compat : existence inconnue → message neutre historique (appelants legacy sans info)
    return {
        "ok": False,
        "code": "ARTIFACT_NOT_IN_SCOPE",
        "message": "Document non accessible dans le périmètre de cette conversation.",
    }
