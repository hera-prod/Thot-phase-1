# services/corbeille.py — le cycle de vie public d'une conversation
#
# D'OÙ VIENT CE FICHIER : `cycle_de_vie_des_informations.md` §4.1/§4.2 — la
# suppression d'une discussion retire messages, briefs, réponses, fichiers
# documentaires et recherches web conservées DES VUES ET USAGES ACTIFS, sans
# détruire les images préservées (§galerie) ni purger les citations déjà
# présentes dans d'autres discussions (§4.2, l. 156). L'ancien `DELETE` en
# cascade (`ipc/base.py`) contredisait §4.2 ligne à ligne : il emportait la
# conversation entière ET l'accès à ses images.
#
# OÙ VA QUOI : appelé par la commande utilisateur `delete.conversation`
# (`ipc/base.py`), jamais par un effet d'outil. L'exécution est en deux temps :
#
#   1. `mettre_en_corbeille` (déclenché par le verbe `delete.conversation`) :
#      la conversation est MARQUÉE (`deleted_at`, `purge_after` à sept jours —
#      annexe10:146, même durée que les espaces `TRASH_RETENTION_DAYS` et le
#      registre des artefacts `backend/search/trash.py:TRASH_DAYS`), et le
#      nettoyage CIBLÉ s'exécute immédiatement — c'est le choix que fait cette
#      implémentation sur le point que la fiche laisse ouvert (§5.3 liste la
#      corbeille parmi les décisions non tranchées) : les liens et les index de
#      portée conversation quittent le corpus DÈS la mise en corbeille. La
#      corbeille ne garde que ce qui est nécessaire à la restauration de la
#      conversation elle-même (la ligne `conversations`, ses messages), pas une
#      copie du corpus retiré.
#   2. `purger_conversation` / `balayer_corbeille` (au démarrage du sidecar) :
#      à l'expiration des sept jours, les données propres restantes sont
#      supprimées physiquement.
#
# CE QUE CE FICHIER NE FAIT JAMAIS : supprimer un artefact, un blob, une fiche
# ou une observation. Une suppression de discussion n'est pas une demande de
# purge des objets cités ailleurs (verrou anti-purge rétroactive, cycle de vie
# §4.2 l. 156) ; le GC des artefacts orphelins reste le privilège du registre
# (`backend/search/trash.py:purge`), jamais d'un cycle de conversation.
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

log = logging.getLogger(__name__)

# Sept jours (annexe10:146) : la durée déjà appliquée aux espaces
# (`services/espaces.py:TRASH_RETENTION_DAYS`) et aux artefacts
# (`backend/search/trash.py:TRASH_DAYS`). Un même acte de suppression doit
# avoir la même corbeille partout.
TRASH_RETENTION_DAYS = 7

_UNKNOWN = "[CONVERSATION_NOT_FOUND] Conversation introuvable."


def _now() -> datetime:
    """Horodatage NAÏF UTC, comme `server_default=func.now()`.

    Même idiome que `services/espaces.py:_now` : SQLite rend `CURRENT_TIMESTAMP`
    en UTC et le supposé « naïf-UTC » gouverne déjà `created_at` de tout le
    schéma ; un `datetime.now()` local créerait une purge décalée d'un fuseau.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def est_en_corbeille(conv: Any) -> bool:
    """Vrai si la conversation est marquée supprimée. `deleted_at` EST l'état."""
    return conv is not None and getattr(conv, "deleted_at", None) is not None


def _table_existe(db: Any, table: str) -> bool:
    """Une table absente n'a aucune ligne : ce n'est pas une panne.

    `artifact_search`, `chunk_search`, `web_queries` et `web_snapshots` naissent
    d'initialisations conditionnelles (DDL d'`init_db`, migrations v2). Une base
    qui n'a jamais indexé et jamais cherché ne les a pas encore ; le nettoyage
    ciblé ne doit pas en faire une erreur. Toute suppression SQL de ce module
    passe par ce garde FORCÉMENT, sinon une base neuve crashe sur son premier
    delete.
    """
    return (
        db.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :t"),
            {"t": table},
        ).first()
        is not None
    )


def _purger_lignes_conversationnelles(
    db: Any, conversation_id: str, horodatage: datetime
) -> None:
    """Nettoyage CIBLÉ : la portée conversation disparaît des usages actifs.

    Un acte, cinq familles, aucune donnée conversationnelle précieuse détruite
    (cycle de vie §4.2) :

    - `artifact_links` de scope `conversation` : retirés LOGIQUEMENT
      (`removed_at`), pas détruits. Un lien posé en corbeille reste
      restaurable pendant les sept jours, comme dans le registre des artefacts
      (`backend/search/trash.py:move_to_trash`) : la restauration exige
      l'historique des liens ;
    - `doc_refs` de la discussion : idem, et pour une raison ferme — le
      `doc_N` n'est JAMAIS recyclé (« une suppression garde son numéro »,
      `db/models.py:DocRef`), contradictoire avec un `DELETE` de la ligne sous
      la seule corbeille ;
    - `artifact_search` et `chunk_search` de portée conversation : les entrées
      d'index sont retirées (« entrées d'index… retirés ou invalidés », §4.1) —
      ce sont des dérivés reconstruisibles, jamais une source ;
    - `web_queries` et `web_snapshots` : les résultats web conservés du chat
      quittent le corpus de l'espace (§4.2, « résultat web conservé dans le
      chat »), et un cache ne doit pas réexposer ce qui est retiré.

    Les tables `artifact_search` / `chunk_search` sont des DDL d'initialisation
    conditionnelle : elles n'existent pas nécessairement sur une base neuve qui
    n'a jamais indexé. `sqlite_master` tranche — une table absente n'a aucune
    ligne, ce n'est pas une panne.
    """
    from sqlalchemy import text as _text
    from backend.db.models import ArtifactLink, DocRef

    liens = (
        db.query(ArtifactLink)
        .filter(
            ArtifactLink.scope_kind == "conversation",
            ArtifactLink.scope_id == conversation_id,
            ArtifactLink.removed_at.is_(None),
        )
        .all()
    )
    for ligne in liens:
        ligne.removed_at = horodatage

    refs = (
        db.query(DocRef)
        .filter(DocRef.conv_id == conversation_id, DocRef.removed_at.is_(None))
        .all()
    )
    for ligne in refs:
        ligne.removed_at = horodatage

    for table, sql in (
        (
            "artifact_search",
            "DELETE FROM artifact_search WHERE scope_kind = 'conversation' AND scope_id = :id",
        ),
        (
            "chunk_search",
            "DELETE FROM chunk_search WHERE scope_kind = 'conversation' AND scope_id = :id",
        ),
        ("web_queries", "DELETE FROM web_queries WHERE conv_id = :id"),
        ("web_snapshots", "DELETE FROM web_snapshots WHERE conv_id = :id"),
    ):
        if _table_existe(db, table):
            db.execute(_text(sql), {"id": conversation_id})

    # Une discussion qui s'en va ne doit plus soutenir une fiche de mémoire.
    # La conversation existe encore ici (seulement marquée `deleted_at`) : elle
    # peut donc résoudre son espace. Une mémoire indisponible ne bloque pas la
    # corbeille, mais elle ne doit pas non plus échouer en silence.
    try:
        from backend.services import memoire_auto as _memoire_auto

        _memoire_auto.invalider_conversation(db, conversation_id)
    except Exception:
        log.warning(
            "invalidations mémoire impossibles pour la discussion %s",
            conversation_id,
            exc_info=True,
        )
    db.flush()


def mettre_en_corbeille(db: Any, conversation_id: str) -> dict[str, Any]:
    """Mets la discussion à la corbeille. Rien n'est détruit, tout est dépublié.

    Le retour est EXPANSIF : `purge_after` et `retention_days` sont de retour
    d'appel à part entière, parce que l'échéance de sept jours est ce que
    l'utilisateur doit pouvoir lire avant que quoi que ce soit ne disparaisse.
    Idempotent sur une conversation déjà en corbeille : l'échéance en cours
    NE bouge pas.
    """
    from backend.db.models import Conversation

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise LookupError(_UNKNOWN)
    if est_en_corbeille(conv):
        return {
            "ok": True,
            "id": conversation_id,
            "already_trashed": True,
            "purge_after": conv.purge_after.isoformat() if conv.purge_after else None,
            "retention_days": TRASH_RETENTION_DAYS,
        }

    horodatage = _now()
    conv.deleted_at = horodatage
    conv.purge_after = horodatage + timedelta(days=TRASH_RETENTION_DAYS)
    conv.updated_at = horodatage
    _purger_lignes_conversationnelles(db, conversation_id, horodatage)
    db.commit()
    return {
        "ok": True,
        "id": conversation_id,
        "already_trashed": False,
        "state": "trashed",
        "purge_after": conv.purge_after.isoformat(),
        "retention_days": TRASH_RETENTION_DAYS,
    }


def purger_conversation(
    db: Any, conversation_id: str, *, seulement_expiree: bool = True
) -> dict[str, Any]:
    """Purge physique d'une conversation en corbeille. Les seuls contenants.

    Détruit ce qui n'appartient QU'À la discussion : son row, ses messages, ses
    runs, ses snapshots, ses lignes `message_attachments` / `toolcalls` /
    `sources`, ses liens `conversation` résiduels et ses `doc_refs`. Un
    artefact, un blob, une observation, une fiche ou une image ne sont JAMAIS
    touchés ici : la corbeille d'une conversation ne décide du sort d'aucun
    objet qu'une autre conversation peut encore citer (verrou anti-purge).

    `purge_after` est le CALENDRIER : sans lui, balayer écraserait des
    conversations que personne n'a laissées expirer. `seulement_expiree=False`
    n'existe que pour le test unitaire — le chemin de production n'écrit pas
    dans ce garde-fou.
    """
    from sqlalchemy import text as _text
    from backend.db.models import (
        ArtifactLink,
        ContextSnapshot,
        Conversation,
        DocRef,
        Message,
        MessageAttachment,
        Run,
        Source,
        ToolCall,
    )

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise LookupError(_UNKNOWN)
    if not est_en_corbeille(conv):
        raise LookupError(
            "[CONVERSATION_NOT_TRASHED] Conversation encore active : mets-la "
            "à la corbeille avant de la purger."
        )
    if seulement_expiree:
        ref = _now()
        if conv.purge_after is None or conv.purge_after > ref:
            return {
                "ok": True,
                "id": conversation_id,
                "purged": False,
                "purge_after": conv.purge_after.isoformat()
                if conv.purge_after
                else None,
            }

    # La mise en corbeille invalide déjà la mémoire ; ce second appel couvre les
    # conversations mises en corbeille par une version antérieure du code. Il
    # est idempotent et ne doit pas empêcher la purge physique.
    try:
        from backend.services import memoire_auto as _memoire_auto

        _memoire_auto.invalider_conversation(db, conversation_id)
    except Exception:
        log.warning(
            "invalidations mémoire impossibles avant purge de %s",
            conversation_id,
            exc_info=True,
        )

    msg_ids = [
        str(r[0])
        for r in db.query(Message.id).filter(Message.conv_id == conversation_id).all()
    ]
    if msg_ids:
        db.query(MessageAttachment).filter(
            MessageAttachment.message_id.in_(msg_ids)
        ).delete(synchronize_session=False)
        db.query(ToolCall).filter(ToolCall.msg_id.in_(msg_ids)).delete(
            synchronize_session=False
        )
        db.query(Source).filter(Source.msg_id.in_(msg_ids)).delete(
            synchronize_session=False
        )
        db.query(Message).filter(Message.id.in_(msg_ids)).delete(
            synchronize_session=False
        )
    db.query(Run).filter(Run.conv_id == conversation_id).delete(
        synchronize_session=False
    )
    db.query(ContextSnapshot).filter(
        ContextSnapshot.conversation_id == conversation_id
    ).delete(synchronize_session=False)
    db.query(DocRef).filter(DocRef.conv_id == conversation_id).delete(
        synchronize_session=False
    )
    db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "conversation",
        ArtifactLink.scope_id == conversation_id,
    ).delete(synchronize_session=False)
    db.query(Conversation).filter(Conversation.id == conversation_id).delete(
        synchronize_session=False
    )
    # Résiduel, jamais une seconde source : le nettoyage ciblé a déjà retiré
    # ces lignes à la mise en corbeille ; ces DELETE rejouent le vide pour une
    # conversation mise en corbeille par une version antérieure du code.
    if _table_existe(db, "web_queries"):
        db.execute(
            _text("DELETE FROM web_queries WHERE conv_id = :id"),
            {"id": conversation_id},
        )
    if _table_existe(db, "web_snapshots"):
        db.execute(
            _text("DELETE FROM web_snapshots WHERE conv_id = :id"),
            {"id": conversation_id},
        )
    db.commit()
    return {"ok": True, "id": conversation_id, "purged": True}


def balayer_corbeille(db: Any, *, maintenant: datetime | None = None) -> int:
    """Purge les conversations dont la corbeille est échue. Retourne le nombre.

    On ne LISTE pas comme les espaces (leur purge reste une décision
    utilisateur, `services/espaces.py:expired_trash`) : la corbeille des
    conversations a une ÉCHÉANCE annoncée (`purge_after`, retourné par la mise
    en corbeille), et l'attente de sept jours EST la fenêtre de grâce
    utilisateur, pas une externalisation à jamais. Retirer seulement avec
    confirmation ferait une corbeille qui grossit sans fin.
    """
    from backend.db.models import Conversation

    ref = maintenant if maintenant is not None else _now()
    echues = [
        str(row[0])
        for row in db.query(Conversation.id)
        .filter(
            Conversation.deleted_at.is_not(None),
            Conversation.purge_after.is_not(None),
            Conversation.purge_after <= ref,
        )
        .all()
    ]
    purgées = 0
    for conversation_id in echues:
        rapport = purger_conversation(db, conversation_id, seulement_expiree=False)
        if rapport.get("purged"):
            purgées += 1
    return purgées
