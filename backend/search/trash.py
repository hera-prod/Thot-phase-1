# backend/search/trash.py — Corbeille 7 jours + purge GC (annexe 11, Lot B)
#
# D'OÙ VIENT CE FICHIER : T-SRCH-13/14 — un fichier supprimé reste trouvable/restaurable
# 7 jours dans le périmètre autorisé, puis index + blob éligibles au GC sécurisé.
# OÙ VA QUOI : move_to_trash()/restore() posent/retirent removed_at sur les liens du scope
# (± lignes FTS) ; purge() supprime les liens échus puis GC les artefacts dont AUCUN
# lien n'est actif ou récent (cartes, lignes FTS, blob si orphelin + fichier).
# scope.py exclut déjà removed_at.
# POURQUOI removed_at ET PAS DELETE : la restauration exige l'historique des liens.
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

TRASH_DAYS = 7
# Réparation bornée des liens incohérents : un artefact logiquement supprimé
# (`deleted_at` non nul, ex. `image_delete`) ne doit pas garder un lien actif
# qui le protègerait à vie du GC. On répare par lots pour que le démarrage
# reste borné.
MAX_REPARATIONS_LIENS_PAR_PASSE = 500
# Purge des liens échus par lots : une corbeille de milliers de liens ne doit
# pas charger toute la table en mémoire avant le premier `commit`.
MAX_PURGE_LIENS_PAR_LOT = 500


def _chemin_blob_confine(relative_path: str):
    """Chemin disque d'un blob, REFUSÉ s'il sort de la racine de données.

    `relative_path` vient de la table `blobs`. Une ligne corrompue ne doit
    jamais transformer un `unlink` en suppression hors de `data/`. On résout
    le chemin (les liens symboliques sont suivis) et on exige qu'il reste sous
    `data_dir`. Le chemin rendu est celui d'origine : l'appelant supprime
    l'entrée, pas la cible résolue d'un lien.
    """
    from pathlib import Path

    from backend.config.paths import get_data_dir

    data_dir = get_data_dir().resolve()
    if not str(relative_path or "").strip():
        raise ValueError("relative_path vide")
    brut = Path(str(relative_path))
    if brut.is_absolute():
        raise ValueError("chemin absolu refusé")
    candidat = data_dir / brut
    if candidat.is_symlink():
        raise ValueError("lien symbolique refusé")
    resolu = candidat.resolve(strict=False)
    if resolu == data_dir or data_dir not in resolu.parents:
        raise ValueError(f"chemin hors de {data_dir}")
    return candidat


def _now() -> datetime:
    """UTC naïf, comme les colonnes `DateTime` sans fuseau de SQLite.

    `removed_at` est comparé à un cutoff : une horloge aware rendrait la
    comparaison dépendante du fuseau de la machine. `espaces._now()` suit déjà
    cette règle ; la corbeille doit suivre la même horloge.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def move_to_trash(session, artifact_id: str, scope_kind: str, scope_id: str) -> int:
    """Corbeille : marque les liens actifs + retire les lignes FTS du scope. Retourne n."""
    from backend.db.models import ArtifactLink as _Link
    from backend.search import fts as _fts
    from sqlalchemy import text as _text

    now = _now()
    rows = (
        session.query(_Link)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.scope_kind == scope_kind,
            _Link.scope_id == scope_id,
            _Link.removed_at.is_(None),
        )
        .all()
    )
    for link in rows:
        link.removed_at = now
    session.execute(
        _text(
            "DELETE FROM artifact_search WHERE artifact_id = :a AND scope_kind = :sk AND scope_id = :sid"
        ),
        {"a": artifact_id, "sk": scope_kind, "sid": scope_id},
    )
    session.flush()
    return len(rows)


def restore(session, artifact_id: str, scope_kind: str, scope_id: str) -> int:
    """Restauration : réactive les liens + réindexe la fiche active. Retourne n."""
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link
    from backend.search import cards as _cards, fts as _fts

    rows = (
        session.query(_Link)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.scope_kind == scope_kind,
            _Link.scope_id == scope_id,
            _Link.removed_at.is_not(None),
        )
        .all()
    )
    for link in rows:
        link.removed_at = None
    session.flush()
    art = session.query(_Art).filter(_Art.id == artifact_id).first()
    if art is not None:
        try:
            card = _cards.get_current_card(session, artifact_id)
            if card is None:
                card = _cards.ensure_card_on_import(session, art)
            _fts.ensure_fts(session)
            # `reindexer_carte`, pas `index_card_row` : une restauration réécrit la
            # fiche au moment où elle redevient visible, donc c'est là aussi que le
            # verdict d'indexation doit se lire dans son statut.
            _cards.reindexer_carte(
                session,
                art,
                card,
                scope_kind,
                scope_id,
                extracted_preview=_cards._text_preview(session, art),
            )
        except Exception:
            pass
    session.flush()
    return len(rows)


def list_trash(session, scope_kind: str, scope_id: str) -> list[dict]:
    """Contenu de la corbeille du scope (restorable avant échéance)."""
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link

    rows = (
        session.query(_Link, _Art)
        .join(_Art, _Link.artifact_id == _Art.id)
        .filter(
            _Link.scope_kind == scope_kind,
            _Link.scope_id == scope_id,
            _Link.removed_at.is_not(None),
        )
        .order_by(_Link.removed_at.desc())
        .all()
    )
    out = []
    for link, art in rows:
        try:
            removed = link.removed_at
            if removed is not None and removed.tzinfo is None:
                removed = removed.replace(tzinfo=timezone.utc)
            eligible = (removed + timedelta(days=TRASH_DAYS)) if removed else None
        except Exception:
            eligible = None
        out.append(
            {
                "artifact_id": art.id,
                "name": art.original_filename or art.display_name or art.id,
                "removed_at": link.removed_at.isoformat() if link.removed_at else None,
                "purge_eligible_at": eligible.isoformat() if eligible else None,
            }
        )
    return out


def purge_eligible(session, days: int = TRASH_DAYS, limite: int | None = None) -> list:
    """Liens en corbeille depuis plus de `days` jours, au plus `limite`.

    La borne est portée par la requête, pas par l'appelant : sans elle, une
    corbeille de milliers de liens matérialisait tous ses objets ORM avant la
    première suppression. `None` garde le comportement historique (lecture
    complète) pour les usages de diagnostic ; `purge` passe toujours une limite
    et boucle par lots.
    """
    from backend.db.models import ArtifactLink as _Link

    cutoff = _now() - timedelta(days=days)
    requete = session.query(_Link).filter(
        _Link.removed_at.is_not(None), _Link.removed_at < cutoff
    )
    if limite is not None:
        requete = requete.limit(max(0, int(limite)))
    return requete.all()


def _reparer_liens_artefacts_supprimes(
    session, limite: int = MAX_REPARATIONS_LIENS_PAR_PASSE
) -> int:
    """Aligne les liens d'un artefact supprimé sur son `deleted_at`.

    Un artefact `deleted_at IS NOT NULL` sans `removed_at` sur ses liens est un
    état impossible en principe (`images.image_delete` retire les deux). Une
    écriture interrompue ou un ancien chemin peut le produire ; laisser ce lien
    actif protégerait l'artefact à vie du GC. On répare par lot borné.
    """
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link

    lignes = (
        session.query(_Link, _Art.deleted_at)
        .join(_Art, _Link.artifact_id == _Art.id)
        .filter(_Link.removed_at.is_(None), _Art.deleted_at.is_not(None))
        .limit(max(0, int(limite)))
        .all()
    )
    for lien, supprime_le in lignes:
        lien.removed_at = supprime_le
    session.flush()
    return len(lignes)


def purge(session, days: int = TRASH_DAYS, limite: int | None = None) -> dict:
    """Purge : liens échus, puis GC des artefacts sans lien actif NI récent.

    Le prédicat corrigé est le cœur de cette fonction. Un artefact n'est
    orphelin que si AUCUN de ses liens n'est actif (`removed_at IS NULL`) ou
    récent (`removed_at >= cutoff`). « Aucun lien actif » seul comptait à tort
    la corbeille de moins de `days` jours comme orpheline : un fichier mis à la
    corbeille hier était emporté aujourd'hui. Ici, la corbeille récente survit,
    et un artefact partagé avec au moins un lien actif ou récent n'est jamais
    purgé.

    Chaque artefact est une transaction COMPLÈTE : sa suppression en base est
    commitée avant que le fichier blob ne soit retiré. Un échec de commit
    annule la suppression sans avoir touché aux octets — l'inverse laisserait
    une ligne en base pointant vers un fichier absent, c'est-à-dire une
    corruption silencieuse. Le rapport expose compteurs et `errors` ; aucun
    échec n'est avalé.
    """
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link

    cutoff = _now() - timedelta(days=days)
    rapport = {
        "links": 0,
        "links_repares": 0,
        "artifacts": 0,
        "blobs": 0,
        "blobs_conserves": 0,
        "blobs_failed": 0,
        "artifacts_failed": 0,
        "chunks": 0,
        "index_chunks": 0,
        "vecteurs": 0,
        "index": 0,
        "errors": [],
    }

    # 0. Réparer la cohérence `artifact.deleted_at` -> liens retirés. Un
    #    artefact logiquement supprimé (image_delete) garde sinon un lien actif
    #    qui le protègerait indéfiniment : ce n'est pas une référence vivante,
    #    c'est une écriture incomplète. La réparation est bornée et idempotente.
    rapport["links_repares"] = _reparer_liens_artefacts_supprimes(session)
    session.flush()

    # 1. Les liens échus partent, même si leur artefact survit grâce à un autre
    #    lien actif/récent : la rétention annoncée porte sur le LIEN, pas sur
    #    l'artefact partagé. On supprime par LOTS bornés : une corbeille de
    #    milliers de liens ne doit pas être matérialisée d'un coup. Chaque lot
    #    est commité avant le suivant, donc la rétention est fixée avant le GC
    #    sans tenir toute la table en mémoire.
    while True:
        lot = purge_eligible(session, days, limite=MAX_PURGE_LIENS_PAR_LOT)
        if not lot:
            break
        for lien in lot:
            session.delete(lien)
            rapport["links"] += 1
        session.commit()

    # 2. Candidats : un artefact est orphelin si AUCUN lien « protecteur » ne
    #    subsiste. Un lien RÉCENT protège pendant toute la rétention, même si sa
    #    portée est en corbeille : c'est précisément la fenêtre de restauration.
    #    Un lien ACTIF, lui, n'est une référence réelle que si sa portée est
    #    vivante — un lien actif vers une conversation supprimée ou un espace
    #    en corbeille ne doit pas protéger indéfiniment. Les portées inconnues
    #    restent conservatrices (elles protègent), pour ne jamais supprimer ce
    #    qu'on ne sait pas lire.
    from sqlalchemy import exists as _exists, and_ as _and, or_ as _or

    from backend.db.models import Conversation as _Conv, Workspace as _Ws

    portee_vivante = _or(
        _and(
            _Link.scope_kind == "conversation",
            _exists().where(
                _and(_Conv.id == _Link.scope_id, _Conv.deleted_at.is_(None))
            ),
        ),
        _and(
            _Link.scope_kind == "workspace",
            _exists().where(_and(_Ws.id == _Link.scope_id, _Ws.state == "active")),
        ),
        ~_Link.scope_kind.in_(("conversation", "workspace")),
    )
    lien_protecteur = _and(
        _Link.artifact_id == _Art.id,
        _or(
            _and(_Link.removed_at.is_(None), portee_vivante),
            _Link.removed_at >= cutoff,
        ),
    )
    requete = (
        session.query(_Art).filter(~_exists().where(lien_protecteur)).order_by(_Art.id)
    )
    if limite is not None:
        requete = requete.limit(max(0, int(limite)))
    candidats = requete.all()

    purges: list[str] = []
    for art in candidats:
        artifact_id = art.id
        try:
            resultat = _purger_artefact(session, art)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - compté et nommé, jamais avalé
            session.rollback()
            rapport["artifacts_failed"] += 1
            rapport["errors"].append(
                {"artifact_id": artifact_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue

        rapport["artifacts"] += 1
        purges.append(str(artifact_id))
        rapport["chunks"] += resultat["chunks"]
        rapport["index_chunks"] += resultat.get("index_chunks", 0)
        rapport["vecteurs"] += resultat["vecteurs"]
        rapport["index"] += resultat["index"]
        if resultat.get("blob_error"):
            rapport["blobs_failed"] += 1
            rapport["errors"].append(
                {"artifact_id": artifact_id, "error": resultat["blob_error"]}
            )
            continue
        chemin = resultat.get("blob_path")
        if chemin is None:
            continue
        # Le blob n'est retiré qu'APRÈS le commit : si le commit avait échoué,
        # la ligne aurait été restaurée et le fichier serait toujours là.
        #
        # Course avec une promotion : le verrou `flock` est inter-processus et
        # réentrant dans ce processus, donc il ne sérialise pas le thread GC et
        # le thread IPC. Une promotion qui re-dépose le même digest entre le
        # commit ci-dessus et l'`unlink` peut voir le fichier encore présent
        # (`written=False`), insérer sa ligne, puis perdre les octets. Juste
        # avant de supprimer, on relit donc `blobs` et l'identité du fichier :
        # une ligne réapparue ou un fichier remplacé interdit l'`unlink`.
        if not _blob_encore_supprimable(
            session,
            chemin,
            resultat.get("blob_identite"),
            resultat.get("blob_sha256"),
            resultat.get("blob_relative_path"),
        ):
            rapport["blobs_conserves"] += 1
            continue
        try:
            chemin.unlink(missing_ok=True)
            rapport["blobs"] += 1
        except OSError as exc:  # noqa: BLE001 - fichier orphelin signalé
            rapport["blobs_failed"] += 1
            rapport["errors"].append(
                {
                    "artifact_id": artifact_id,
                    "error": f"blob non supprimé ({chemin}): {type(exc).__name__}: {exc}",
                }
            )

    # GC → mémoire : un artefact physiquement supprimé ne peut plus soutenir une
    # fiche. La synchronisation est faite par espace après les commits, donc elle
    # voit l'absence réelle et n'invalide que les fiches dont TOUTES les sources
    # sont parties. Un échec ne transforme pas une purge réussie en échec.
    if purges:
        try:
            from backend.services import memoire as _memoire

            _memoire.invalider_artefacts_supprimes(session, purges)
        except Exception as exc:  # noqa: BLE001 - purge déjà committée
            log.warning(
                "[gc] invalidation mémoire impossible pour %d artefact(s) : %s",
                len(purges),
                exc,
            )
    return rapport


def _identite_fichier(chemin) -> dict[str, int] | None:
    """Identité disque lue une fois : détecte un remplacement avant `unlink`."""
    try:
        stat = chemin.stat()
    except OSError:
        return None
    return {
        "ino": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _blob_encore_supprimable(session, chemin, identite, sha256, relative_path) -> bool:
    """Le fichier est-il TOUJOURS l'orphelin observé avant le commit ?

    Trois refus, tous conservateurs :
    - le fichier a disparu, changé de type ou changé d'identité (inode, taille,
      mtime) : une promotion l'a recréé ou réécrit entre-temps ;
    - une ligne `blobs` porte à nouveau ce `sha256` ou ce `relative_path` :
      une promotion a inséré sa référence après le commit du GC ;
    - la base est illisible : on ne sait pas, donc on ne supprime pas.
    """
    if identite is None or chemin.is_symlink() or not chemin.is_file():
        return False
    if _identite_fichier(chemin) != identite:
        return False
    from sqlalchemy import text as _text

    try:
        ligne = session.execute(
            _text(
                "SELECT 1 FROM blobs"
                " WHERE relative_path = :rel OR sha256 = :sha LIMIT 1"
            ),
            {"rel": relative_path, "sha": sha256},
        ).first()
    except Exception as exc:  # noqa: BLE001 - fail-safe : ne pas supprimer
        log.warning("[gc] vérification base impossible pour %s : %s", chemin, exc)
        return False
    return ligne is None


def _purger_artefact(session, art) -> dict:
    """Supprime UN artefact et ses traces en base. Le fichier blob est rendu.

    Les opérations d'index et de chunks rendent un résultat structuré : une
    erreur y est transformée en exception ICI, pour que le commit de l'appelant
    annule la suppression entière au lieu de laisser un artefact à moitié
    effacé. Le chemin du blob n'est PAS délié ici : `purge` ne le fait qu'après
    le commit, pour ne jamais perdre les octets d'une ligne qui survivrait.
    """
    from backend.db.models import (
        Artifact as _Art,
        ArtifactCard as _Card,
        Blob as _Blob,
        MessageAttachment as _MA,
    )
    from backend.search import chunks as _chk
    from backend.search import fts as _fts

    index = _fts.remove_artifact(session, art.id)
    if not index.get("ok"):
        raise RuntimeError(
            "suppression FTS échouée : " + "; ".join(index.get("errors", []))
        )
    fragments = _chk.remove_chunks_for_artifact(session, art.id)
    if not fragments.get("ok"):
        raise RuntimeError(
            "suppression chunks/vecteurs échouée : "
            + "; ".join(fragments.get("errors", []))
        )

    for card in session.query(_Card).filter(_Card.artifact_id == art.id).all():
        session.delete(card)
    for lien in session.query(_MA).filter(_MA.artifact_id == art.id).all():
        session.delete(lien)

    blob_id = art.blob_id
    session.delete(art)
    session.flush()

    blob_path = None
    blob_error = None
    blob_sha256 = None
    blob_relative_path = None
    blob_identite = None
    encore_utilise = session.query(_Art).filter(_Art.blob_id == blob_id).count()
    if not encore_utilise:
        blob = session.query(_Blob).filter(_Blob.id == blob_id).first()
        if blob is not None:
            blob_sha256 = blob.sha256
            blob_relative_path = blob.relative_path
            try:
                blob_path = _chemin_blob_confine(blob.relative_path)
                # L'identité est lue AVANT le commit : `purge` la compare juste
                # avant l'`unlink` pour reconnaître un fichier remplacé par une
                # promotion concurrente.
                blob_identite = _identite_fichier(blob_path)
            except (OSError, ValueError) as exc:
                # On refuse le `unlink`, mais la ligne est tout de même retirée :
                # un chemin corrompu ne doit ni sortir de la racine ni laisser
                # une ligne `blobs` sans artefact. L'échec est compté.
                blob_error = (
                    f"blob non supprimé (chemin refusé {blob.relative_path!r}): "
                    f"{type(exc).__name__}: {exc}"
                )
            session.delete(blob)
    return {
        "blob_path": blob_path,
        "blob_error": blob_error,
        "blob_sha256": blob_sha256,
        "blob_relative_path": blob_relative_path,
        "blob_identite": blob_identite,
        "chunks": int(fragments.get("chunks", 0)),
        "index_chunks": int(fragments.get("index_chunks", 0)),
        "vecteurs": int(fragments.get("vecteurs", 0)),
        "index": int(index.get("lignes", 0)),
    }
