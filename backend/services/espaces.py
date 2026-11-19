# services/espaces.py — le conteneur de rattachement (C17, phase 5)
#
# D'OÙ VIENT CE FICHIER : C17 + `01_architecture_registre_index.md:73-112`, rendus
# exécutables. Le dossier `docs/espaces/` documente la conception ; ici on écrit
# le code, et chaque fonction cite la règle qu'elle applique.
# OÙ VA QUOI : appelé par des COMMANDES UTILISATEUR distinctes du chat, jamais par
# un effet d'outil. L'IA n'a aucun droit d'écriture ici (E07, `01_classe_espace.md:74-84`).
# POURQUOI ICI ET PAS DANS UN TOOL : un espace n'est pas un objet de conversation.
# Il n'est ni un dossier, ni une catégorie, ni un projet d'un outil externe.
#
# TROIS RÈGLES QUI GOUVERNENT TOUT LE FICHIER :
# 1. Un espace ne possède AUCUN octet. Les blobs restent dans la racine gérée
#    (C10:172, portabilite.md:43) ; la portée est une requête sur ArtifactLink.
# 2. « La propriété d'un fichier ne change jamais » (`:80-87`). Déplacer une
#    conversation ne touche aucun fichier, aucun id, aucun chunk, aucun vecteur.
# 3. Le partage est un lien, jamais une copie (C10:172). Un hash identique ne
#    donne jamais le droit de lire (`:61`) : il ne doit pas révéler l'existence
#    d'un document d'un autre espace.
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# Compteur monotone des enqueues documentaires ratés APRÈS un partage. Le partage
# est déjà committé : l'échec ne doit pas le défaire, mais il ne doit pas
# disparaître non plus. Le backfill de 5 min récupère la ligne ; ce compteur rend
# la panne observable au lieu de la laisser au seul journal (relevé Phase 8, 🟡 8).
_ENQUEUE_DOCUMENTAIRE_ECHECS = 0
_ENQUEUE_DOCUMENTAIRE_LOCK = threading.Lock()

# Une conversation supprimée part en corbeille sept jours (annexe10:146). La
# décision E-SUPPR applique la même durée à l'espace.
TRASH_RETENTION_DAYS = 7

# Un SEUL message pour « ce document n'existe pas » ET pour « ce document
# existe mais n'est pas dans ta portée ». Deux messages distincts seraient une
# oracle d'existence : il suffirait de partager un id deviné pour apprendre qu'il
# désigne quelque chose. Le refus doit être indifférenciable.
_NOT_SHAREABLE = "[ARTIFACT_NOT_FOUND] Document introuvable."


def _new_workspace_id() -> str:
    return f"ws_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    """Horodatage NAÏF UTC, comme `server_default=func.now()`.

    SQLite rend `CURRENT_TIMESTAMP` en UTC, et `fts.py:383` comme le filtre de
    récence de `loop.py` supposent déjà que `created_at` est naïf-UTC. Les
    colonnes que CE service écrit (`trashed_at`, `purge_after`) doivent donc
    suivre la même horloge : un `datetime.now()` local donnerait une purge
    décalée d'un fuseau, invisible aujourd'hui et fabricante demain.

    Naïf volontairement — la colonne est `DateTime` sans fuseau, et c'est
    `datetime.utcnow()` (déprécié en 3.12) qu'on remplace, pas le fuseau.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Création et identité
# ---------------------------------------------------------------------------


def create_workspace(db: Any, name: str, description: str | None = None) -> Any:
    """Crée un espace : identité stable, paramètres non secrets (`backend_espaces.md:26-30`).

    L'espace est un contenant, pas une unité de stockage : on ne crée aucun
    dossier, aucun dossier mémoire, aucun chemin. Les blobs restent dans la
    racine gérée et « aucun chemin absolu dans Workspace » (`:30`).
    """
    from backend.db.models import Workspace as _Ws

    clean = (name or "").strip()
    if not clean:
        raise ValueError("[NAME_REQUIRED] Un espace a besoin d'un nom.")
    ws = _Ws(
        id=_new_workspace_id(),
        name=clean[:200],
        description=(description or None),
        state="active",
    )
    db.add(ws)
    db.commit()
    return ws


def get_workspace(
    db: Any, workspace_id: str, *, include_trashed: bool = True
) -> Any | None:
    from backend.db.models import Workspace as _Ws

    ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
    if ws is None:
        return None
    if not include_trashed and ws.state != "active":
        return None
    return ws


def list_workspaces(db: Any, *, include_trashed: bool = False) -> list[Any]:
    """Espaces actifs, du plus récent au plus ancien.

    Un espace en corbeille n'est pas listé par défaut : il est restaurable, pas
    invisible — `list_workspaces(include_trashed=True)` le rend d'un coup.
    """
    from backend.db.models import Workspace as _Ws

    q = db.query(_Ws)
    if not include_trashed:
        q = q.filter(_Ws.state == "active")
    return list(q.order_by(_Ws.created_at.desc(), _Ws.id.asc()).all())


def rename_workspace(db: Any, workspace_id: str, name: str) -> Any:
    """Renommer ne touche que l'étiquette : aucun octet, aucun lien, aucun id."""
    ws = get_workspace(db, workspace_id)
    if ws is None:
        raise LookupError("[WORKSPACE_UNKNOWN] Espace introuvable.")
    clean = (name or "").strip()
    if not clean:
        raise ValueError("[NAME_REQUIRED] Un espace a besoin d'un nom.")
    ws.name = clean[:200]
    ws.updated_at = _now()
    db.commit()
    return ws


# ---------------------------------------------------------------------------
# Rattachement d'une conversation
# ---------------------------------------------------------------------------


def conversation_workspace(
    db: Any, conversation_id: str, *, include_trashed: bool = False
) -> str | None:
    """Espace d'une conversation, ou None. Server-side, jamais une valeur d'appelant.

    C18:264 : « Le backend constitue la frontière d'autorisation ». C'est LA
    fonction qui rend cette phrase vraie pour les six outils : ils appellent
    `resolve_allowed_artifacts(conv_id)` et le périmètre se déduit d'ici, jamais
    d'un `workspace_id` fourni par le client ou par l'orchestrateur.

    Un espace en corbeille n'est PAS renvoyé par défaut : c'est un état, pas une
    portée. Sans ce filtre, les deux chemins d'accès à l'information de l'espace
    accorteraient encore des résultats alors que `resolve_allowed_artifacts` refuse
    déjà les fichiers — deux réponses contradictoires pour une seule corbeille.
    """
    from backend.db.models import Conversation as _Conv, Workspace as _Ws

    q = db.query(_Conv.workspace_id).filter(_Conv.id == conversation_id)
    if not include_trashed:
        q = q.join(_Ws, _Ws.id == _Conv.workspace_id).filter(_Ws.state == "active")
    row = q.first()
    return str(row[0]) if row and row[0] else None


def attach_conversation(
    db: Any, conversation_id: str, workspace_id: str
) -> dict[str, Any]:
    """Rattache une conversation à un espace. Zéro octet déplacé (`backend_espaces.md:32-36`).

    Le rattachement est l'unique champ modifié. L'historique, les pièces jointes,
    les `artifact_id`, les chunks et les vecteurs ne bougent pas, et les
    références déjà citées dans l'historique restent valides
    (`01_architecture_registre_index.md:80-87`).

    L'adhésion est facultative et au plus une : une conversation a zéro ou un
    espace (C17:248), et la migration n'est jamais forcée.
    """
    from backend.db.models import Conversation as _Conv

    ws = get_workspace(db, workspace_id, include_trashed=False)
    if ws is None:
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")
    conv = db.query(_Conv).filter(_Conv.id == conversation_id).first()
    if conv is None:
        raise LookupError("[CONVERSATION_NOT_FOUND] Conversation introuvable.")

    previous = conv.workspace_id
    if previous == workspace_id:
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "changed": False,
            "artifacts_moved": 0,
            "note": "déjà rattachée",
        }
    # Changer de portée n'est pas un simple UPDATE quand la conversation sort
    # d'un espace : c'est un parcours soumis à la règle de déplacement.
    if previous:
        # Changer de périmètre n'est pas un UPDATE quand la conversation sort
        # d'un espace : c'est le parcours de déplacement, qui peut se terminer
        # par « non ». Le retour garde la même enveloppe que les autres branches.
        return request_move(db, conversation_id, workspace_id, confirm=False)

    conv.workspace_id = workspace_id
    conv.updated_at = _now()
    db.commit()
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "changed": True,
        "artifacts_moved": 0,
        "note": "rattachement seul : aucun fichier, aucun id, aucun chunk touché",
    }


def detach_conversation(db: Any, conversation_id: str) -> dict[str, Any]:
    """Détache : la conversation redevient un chat-simple. Elle ne perd aucun fichier.

    C'est le geste miroir de l'adhésion facultative. Le retrait d'un espace ne
    supprime rien : il retire un droit d'accès supplémentaire.
    """
    from backend.db.models import Conversation as _Conv

    conv = db.query(_Conv).filter(_Conv.id == conversation_id).first()
    if conv is None:
        raise LookupError("[CONVERSATION_NOT_FOUND] Conversation introuvable.")
    previous = conv.workspace_id
    conv.workspace_id = None
    conv.updated_at = _now()
    db.commit()
    return {
        "ok": True,
        "previous_workspace_id": previous,
        "changed": previous is not None,
        "artifacts_removed": 0,
        "note": "retiré de l'espace ; ses fichiers restent les siens",
    }


# ---------------------------------------------------------------------------
# Partage d'un fichier dans l'espace
# ---------------------------------------------------------------------------


def share_artifact(
    db: Any,
    workspace_id: str,
    artifact_id: str,
    *,
    from_conv_id: str | None = None,
    from_workspace_id: str | None = None,
) -> dict[str, Any]:
    """Partage un artefact dans la zone utilisateur de l'espace (`backend_espaces.md:38-43`).

    Crée un `ArtifactLink(scope_kind='workspace')` et NE RECOPIE PAS le blob.
    Le lien est idempotent : re-partager le même artefact ne duplique rien.

    `from_conv_id` / `from_workspace_id` désignent la portée depuis laquelle
    l'utilisateur voit CE fichier. C'est obligatoire, et c'est le cœur de la
    règle : le pseudo-code exige « Vérifier droit de lecture et autorisation de
    partager cette ressource », pas seulement « le fichier existe ». Sans cette
    origine, un partage deviendrait un moyen de faire sortir un fichier privé
    d'un espace vers un autre — le défaut exact que
    `01_architecture_registre_index.md:63-70` interdit.

    ET la portée d'origine doit être EN RELATION avec la cible. Sinon il
    suffirait de nommer une portée que l'on contrôle et qui possède par hasard
    le lien — c'est-à-dire écrire « depuis Beta » en parlant d'un fichier d'Alpha.
    Deux formes sont admises, et ce sont les deux parcours réels :

    - la conversation source appartient à l'espace cible (on.shared en
      conversation, on le rend à tout l'espace) ;
    - la conversation source n'appartient à AUCUN espace (l'utilisateur importe
      depuis un chat simple vers son espace — le « inviter des fichiers »).

    Un partage depuis un AUTRE espace est refusé : transferring un fichier
    d'un espace à un autre n'est pas un partage, c'est un déplacement, et il
    obéit aux règles de `request_move` (C17:260).

    On ne partage PAS automatiquement toutes les pièces jointes d'une
    conversation : « Les autres conversations de l'espace voient seulement ce
    lien explicite » (`:43`).
    """
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link

    if not from_conv_id and not from_workspace_id:
        raise ValueError(
            "[SOURCE_SCOPE_REQUIRED] Partager exige de dire d'où vient le fichier : "
            "une conversation ou un espace. Sans origine, un partage n'est pas vérifiable."
        )
    ws = get_workspace(db, workspace_id, include_trashed=False)
    if ws is None:
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")
    art = db.query(_Art).filter(_Art.id == artifact_id).first()
    if art is None or art.deleted_at is not None:
        # Un artefact supprimé ne se partage pas, et l'erreur ne dit pas s'il
        # a jamais existé (C18 — pas de fuite d'existence).
        raise LookupError(_NOT_SHAREABLE)

    # Droit de lecture ET autorisation de partager, vérifiés sur la portée
    # d'origine DÉCLARÉE — puis vérifié que cette origine a un rapport avec la
    # cible. On compare l'ID, jamais le hash ni le nom : deux espaces peuvent
    # hériter des fichiers de même nom.
    if from_conv_id:
        porte = (
            db.query(_Link)
            .filter(
                _Link.artifact_id == artifact_id,
                _Link.scope_kind == "conversation",
                _Link.scope_id == from_conv_id,
                _Link.removed_at.is_(None),
            )
            .first()
            is not None
        )
        if porte:
            source_workspace = conversation_workspace(db, from_conv_id)
            if source_workspace is not None and source_workspace != workspace_id:
                # Fichier d'un autre espace : refuser SANS dire quel espace, ce
                # qui confirmerait son existence.
                raise LookupError(_NOT_SHAREABLE)
    else:
        if str(from_workspace_id) != workspace_id:
            # Partager depuis un autre espace n'est pas un partage.
            raise LookupError(_NOT_SHAREABLE)
        porte = (
            db.query(_Link)
            .filter(
                _Link.artifact_id == artifact_id,
                _Link.scope_kind == "workspace",
                _Link.scope_id == workspace_id,
                _Link.removed_at.is_(None),
            )
            .first()
            is not None
        )
    if not porte:
        # Message identique que si le fichier n'existait pas : dire « il existe
        # mais pas dans ta portée » confirmerait son existence.
        raise LookupError(_NOT_SHAREABLE)

    existing = (
        db.query(_Link)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.scope_kind == "workspace",
            _Link.scope_id == workspace_id,
        )
        .first()
    )
    if existing is not None:
        if existing.removed_at is None:
            return {
                "ok": True,
                "link_id": existing.id,
                "created": False,
                "bytes_copied": 0,
                "artifact_id": artifact_id,
            }
        existing.removed_at = None
        db.commit()
        _resynchroniser_memoire_apres_partage(db, workspace_id)
        _enfiler_documentaire_apres_partage(db, workspace_id, artifact_id)
        return {
            "ok": True,
            "link_id": existing.id,
            "created": False,
            "bytes_copied": 0,
            "artifact_id": artifact_id,
            "note": "lien rétabli",
        }

    link = _Link(
        id=f"lnk_{uuid.uuid4().hex[:12]}",
        artifact_id=artifact_id,
        scope_kind="workspace",
        scope_id=workspace_id,
        role="shared",
    )
    db.add(link)
    db.commit()
    _resynchroniser_memoire_apres_partage(db, workspace_id)
    _enfiler_documentaire_apres_partage(db, workspace_id, artifact_id)
    return {
        "ok": True,
        "link_id": link.id,
        "created": True,
        "bytes_copied": 0,
        "artifact_id": artifact_id,
        "note": "lien créé ; les octets ne sont pas dupliqués",
    }


def _resynchroniser_memoire_apres_partage(db: Any, workspace_id: str) -> None:
    """Re-partage actif : la mémoire réhabilite les fiches `source_inaccessible`.

    Appelé APRÈS le commit du lien, donc la portée voit le nouveau partage. Un
    échec de synchronisation ne défait pas un partage déjà écrit : il est
    journalisé et la file de reprise du service mémoire le rejouera.
    """
    try:
        from backend.services import memoire as _memoire

        _memoire.syncer_validite_sources(db, workspace_id)
    except Exception:  # noqa: BLE001 - le lien est déjà committé
        log.warning(
            "[espaces] resynchronisation mémoire impossible après partage dans %s",
            workspace_id,
            exc_info=True,
        )


def _enfiler_documentaire_apres_partage(
    db: Any, workspace_id: str, artifact_id: str
) -> None:
    """Enqueue documentaire APRÈS le commit du lien, jamais dans sa transaction.

    Un document lié après coup n'a aucune ligne de ledger : sans ce geste, il
    ne serait jamais traité par la boucle documentaire.

    La ligne écrite par `enfiler_apres_relink` appartient à la transaction
    COURANTE : sans le `commit` ci-dessous, `_dispatch_ipc` fermerait la session
    et la ligne `pending` serait annulée. Le réveil ne suit que ce commit, et
    seulement si une ligne a été créée.

    Un échec (enqueue ou commit) est journalisé et compté ; le partage déjà
    committé reste acquis et le backfill de 5 min recréera la ligne.
    """
    global _ENQUEUE_DOCUMENTAIRE_ECHECS
    try:
        from backend.services import memoire_documentaire as _md

        crees = _md.enfiler_apres_relink(db, workspace_id, artifact_id)
        db.commit()
        if crees > 0:
            # Le ledger est committé : la boucle voit une ligne réelle, pas un
            # fantôme. Aucune ligne créée -> aucun réveil inutile.
            _md._reveiller_extraction_documentaire()
    except Exception:  # noqa: BLE001 — le lien est déjà committé
        try:
            db.rollback()
        except Exception:
            pass
        with _ENQUEUE_DOCUMENTAIRE_LOCK:
            _ENQUEUE_DOCUMENTAIRE_ECHECS += 1
        log.warning(
            "[espaces] enqueue documentaire impossible après partage dans %s",
            workspace_id,
            exc_info=True,
        )


def compteurs_enqueue_documentaire() -> dict[str, int]:
    """Compteur monotone des enqueues documentaires ratés après un partage.

    Exposé pour l'observabilité : un échec silencieux serait indistinguable
    d'une absence de document à traiter (relevé Phase 8, 🟡 8).
    """
    with _ENQUEUE_DOCUMENTAIRE_LOCK:
        return {"echecs": _ENQUEUE_DOCUMENTAIRE_ECHECS}


def unshare_artifact(db: Any, workspace_id: str, artifact_id: str) -> dict[str, Any]:
    """Retire le lien d'espace. Le blob reste : il peut être encore référencé.

    « Supprimer un lien partagé conserve le blob » (phase_5:160). Le retrait est
    un `removed_at`, pas une suppression physique : la purge est de la
    maintenance, jamais d'une commande d'interface (C11:182).
    """
    from backend.db.models import ArtifactLink as _Link

    link = (
        db.query(_Link)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.scope_kind == "workspace",
            _Link.scope_id == workspace_id,
        )
        .first()
    )
    if link is None:
        raise LookupError(
            "[LINK_NOT_FOUND] Ce document n'est pas partagé dans cet espace."
        )
    if link.removed_at is not None:
        return {"ok": True, "link_id": link.id, "already_removed": True}
    link.removed_at = _now()
    db.commit()
    # Décision D5 (cycle de vie des informations §3.1) : « l'état `obsolète` est
    # ajouté au vocabulaire d'ingestion. Définition : la source a été remplacée ou
    # retirée. Le document reste consultable dans son dernier état connu, cesse
    # d'être indexé, et n'est plus proposé à la recherche ni aux outils. »
    #
    # Le retrait est donc AUSSI une écriture d'état, et pas seulement un `removed_at`
    # : le lien mort explique pourquoi le fichier a quitté le dépôt, mais rien ne
    # lisait ce motif — l'artefact lui-même restait dans un état antérieur
    # (`extrait`, `depose`…) qui, lui, ne dit pas que sa SOURCE est sortie du
    # périmètre. Un dépôt qui congédierait un fichier en silence porterait
    # exactement l'état que ce relevé supprime.
    #
    # LA CONDITION se lit sur les liens, jamais sur le retrait seul : le même
    # artefact peut rester vivant ailleurs (pièce jointe d'une discussion encore
    # active). La source est retirée SEULEMENT quand son dernier lien actif
    # disparaît ; un artefact encore servi quelque part garde son état courant,
    # sinon un retrait de dépôt marquerait « obsolète » un fichier que les outils
    # continuent de proposer — un état qui mentirait.
    #
    # `marquer_une_fois` et non `marquer` : le VRAI travail — le retrait — est
    # déjà fait et committé ; perdre le relevé doit se voir au journal, pas faire
    # échouer un retrait qui a eu lieu.
    reste_actif = (
        db.query(_Link)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.id != link.id,
            _Link.removed_at.is_(None),
        )
        .first()
    )
    if reste_actif is None:
        from backend.ingestion.etat import ETAT_OBSOLETE, marquer_une_fois

        marquer_une_fois(
            db,
            artifact_id,
            ETAT_OBSOLETE,
            "la source a été retirée du dépôt : ce document n'est plus proposé "
            "à la recherche ni aux outils",
        )
    # Réconciliation de la mémoire : toute fiche dont ce document était une
    # source devient `stale` (nouvelle version, non destructive) au lieu de
    # rester servie comme acquise. La portée restante fait foi : un artefact
    # encore actif ailleurs ne déclenche rien.
    try:
        from backend.services import memoire as _memoire

        _memoire.syncer_validite_sources(db, workspace_id)
    except Exception:  # noqa: BLE001 - tolérance : le retrait est déjà committé
        log.warning(
            "[espaces] resynchronisation mémoire impossible après retrait dans %s",
            workspace_id,
            exc_info=True,
        )
    return {
        "ok": True,
        "link_id": link.id,
        "already_removed": False,
        "blob_preserved": True,
    }


def renommer_depot(
    db: Any, workspace_id: str, artifact_id: str, nouveau_nom: str
) -> dict[str, Any]:
    """Renomme le nom affiché d'un document du dépôt (`depot.md` §5.2).

    « Cette action modifie le nom affiché par Thot, sans altérer son contenu ni
    les informations extraites qui en dépendent » (`depot.md:106`). Deux écritures,
    jamais une troisième :

    - `artifacts.display_name` — l'étiquette que le dépôt affiche et que
      `find_files` préfère quand l'original manque ;
    - la FICHE servie à `find_files` — une révision N+1 (`revise_card_on_rename`)
      dont le titre porte le nouveau nom. Le « moteur de recherche doit
      actualiser les métadonnées utiles au repérage par nom ou titre » est donc
      honoré SANS réextraction : `original_filename` reste l'identité du
      fichier, la ligne d'index continue de matcher l'ancien nom, et le
      contenu de la fiche ne dit pas ce que personne n'a mesuré.

    Le nom identique est IDEMPOTENT : renommer un fichier vers son nom courant
    ne crée pas une révision de fiche qui ne ferait que copier la précédente.
    """
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link
    from backend.search import cards as _cards

    clean = (nouveau_nom or "").strip()
    if not clean:
        raise ValueError("[NAME_REQUIRED] Un document a besoin d'un nom affichable.")
    ws = get_workspace(db, workspace_id, include_trashed=False)
    if ws is None:
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")
    link = (
        db.query(_Link)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.scope_kind == "workspace",
            _Link.scope_id == workspace_id,
            _Link.removed_at.is_(None),
        )
        .first()
    )
    if link is None:
        # Refus unique, celui du retrait : dire « le lien existe mais le fichier
        # est parti » confirmerait un existant à qui devinait des identifiants.
        raise LookupError(
            "[LINK_NOT_FOUND] Ce document n'est pas partagé dans cet espace."
        )
    art = db.query(_Art).filter(_Art.id == artifact_id).first()
    # Même message pour un artefact absent ou en corbeille : le lien vivant est
    # la seule chose qui dit « ce document EST dans l'espace ».
    if art is None or art.deleted_at is not None or not (art.display_name or ""):
        raise LookupError(
            "[LINK_NOT_FOUND] Ce document n'est pas partagé dans cet espace."
        )

    clean = clean[:200]
    if art.display_name == clean:
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "changed": False,
            "display_name": art.display_name,
        }
    art.display_name = clean
    _cards.revise_card_on_rename(db, art, clean)
    db.commit()
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "changed": True,
        "display_name": clean,
    }


# ---------------------------------------------------------------------------
# Le dépôt : une requête, jamais un dossier
# ---------------------------------------------------------------------------


def space_conversation_ids(db: Any, workspace_id: str) -> list[str]:
    """Conversations ACTIVES de l'espace, corbeille exclue.

    §4.1 de `cycle_de_vie_des_informations.md` : la suppression d'une
    discussion retire ses fichiers et recherches web conservées du corpus
    documentaire de l'espace. `01_architecture_registre_index.md:73-78`
    écrivait « y compris chats supprimés », une décision qu'un DELETE physique
    ne pouvait de toute façon pas tenir (une conversation détruite sortait de
    cette requête comme la suivante) — la fiche cycle de vie, plus récente,
    INVALIDE cette formulation : une conversation en corbeille a quitté le
    corpus de l'espace depuis le jour de sa mise en corbeille, pas à
    l'expiration. Harmonisation documentaire à faire (voir rapport) : la
    rédaction de `01_architecture_registre_index.md` n'a pas suivi.
    """
    from backend.db.models import Conversation as _Conv

    rows = (
        db.query(_Conv.id)
        .filter(_Conv.workspace_id == workspace_id, _Conv.deleted_at.is_(None))
        .all()
    )
    return [str(r[0]) for r in rows]


def _raison_ecarte(display_name: str, supprime: bool, vu_en_base: bool) -> str | None:
    """Pourquoi ce lien n'est PAS présenté, ou `None` s'il l'est.

    Trois motifs, stables et lisibles, parce que `hidden` est relu par l'écran et
    par les outils sans ce fichier sous les yeux : `supprime` (un lien résiduel
    vers un artefact en corbeille), `lien_orphelin` (un lien dont l'artefact
    n'existe plus), `sans_nom` (un artefact vivant sans nom affichable). Un
    motif absent de cette liste ne serait pas un silence : ce serait un mensonge
    de plus sur la longueur du dépôt.
    """
    if supprime:
        return "supprime"
    if not vu_en_base:
        return "lien_orphelin"
    if not display_name:
        return "sans_nom"
    return None


def depot(db: Any, workspace_id: str) -> dict[str, Any]:
    """Le dépôt de l'espace — les fichiers que L'UTILISATEUR y a déposés.

    Une seule catégorie : ce que l'utilisateur a versé dans cet espace. Ce qui
    traîne dans les discussions de l'espace n'en fait PAS partie — un fichier
    déposé dans une discussion y reste. Confondre les deux revient à présenter
    comme « ma référence » ce qu'une conversation a produit au passage, et
    `02_classe_depot.md:26-49` décritait cet assemblage à titre d'hypothèse
    aujourd'hui abandonnée.

    C'est une requête (option A de `02_classe_depot.md`, la voie retenue) : pas
    d'arborescence, pas de « Chemin » copiable, pas de dossier physique. Une vue
    de portée n'a pas de chemin affichable, et C10:172 interdit d'en fabriquer un.

    Chaque fichier déposé ici est déjà visible par TOUTES les discussions de
    l'espace : c'est `scope.py:92-142` qui le déduit, à partir de la conversation
    et non d'un identifiant fourni par le client. Cette fonction ne fait que
    l'inventaire ; elle ne décide d'aucun accès.

    L'inventaire ne se TAIT pas sur ce qu'il écarte. `hidden` nomme chaque lien
    retiré de `files` et son motif, et `size_known` distingue « le fichier est
    vide » de « sa taille n'a pas été trouvée » : sans ces deux clés, un dépôt
    amputé se lisait comme un dépôt complet, et un blob absent s'affichait « 0 o ».
    Les deux sont des fautes d'inventaire, pas des états du dépôt.

    L'ÉTAT D'INGESTION VIENT PAR LIGNE, et il passe par LE seul chemin de
    lecture (`backend/ingestion/etat.py:lire`). `depot.md` §6 : « L'état
    d'ingestion est visible et distingue disponibilité, traitement partiel
    et échec » ; §6.1 (l.141) : « La liste doit permettre d'identifier
    clairement les fichiers utilisables, en cours d'ingestion ou en
    erreur. » Un dépôt qui afficherait un PDF binaire_refuse comme utilisable mentirait exactement
    là où l'utilisateur ne peut pas vérifier.
    """
    from backend.db.models import Artifact as _Art, ArtifactLink as _Link, Blob as _Blob
    from backend.ingestion import etat as _etat

    user_links = (
        db.query(_Link)
        .filter(
            _Link.scope_kind == "workspace",
            _Link.scope_id == workspace_id,
            _Link.removed_at.is_(None),
        )
        .all()
    )

    by_artifact: dict[str, dict[str, Any]] = {}
    for link in user_links:
        aid = str(link.artifact_id)
        by_artifact.setdefault(
            aid,
            {
                "artifact_id": aid,
                "display_name": "",
                "kind": "",
                "size_bytes": 0,
                "size_known": False,
                "added_at": "",
                # Absent de la base = ligne orpheline (écartée par `hidden`) :
                # les deux clés restent uniformes, au lieu d'exister à moitié.
                "ingest_state": "",
                "ingest_reason": "",
            },
        )

    supprimes: set[str] = set()
    vus: set[str] = set()
    if by_artifact:
        arts = db.query(_Art).filter(_Art.id.in_(sorted(by_artifact))).all()
        tailles: dict[str, int] = {}
        blob_ids = [str(a.blob_id) for a in arts]
        if blob_ids:
            # UNE requête pour tous les blobs, et non une par artefact : c'est le
            # N+1 le plus évident du fichier, et `stats()` appelle `depot()`.
            tailles = {
                str(b.id): int(b.size_bytes)
                for b in db.query(_Blob)
                .filter(_Blob.id.in_(sorted(set(blob_ids))))
                .all()
            }
        for art in arts:
            aid = str(art.id)
            vus.add(aid)
            if art.deleted_at is not None:
                supprimes.add(aid)
            row = by_artifact[aid]
            row["display_name"] = str(art.display_name or art.original_filename or "")
            row["kind"] = str(art.kind or "")
            # Un blob ABSENT et un blob de taille NULLE ne sont pas le même fait.
            # `size_bytes` reste 0 pour ne rien casser, et `size_known` porte la
            # distinction : c'est lui qui autorise l'écran à écrire « taille
            # inconnue » au lieu d'affirmer un « 0 o ».
            row["size_known"] = str(art.blob_id) in tailles
            row["size_bytes"] = tailles.get(str(art.blob_id), 0)
            row["added_at"] = str(art.created_at or "")
            # L'état d'ingestion est PARSÉ par le seul chemin de lecture
            # (etat.lire) : un vocabulaire fantaisiste ou une ancienneté de
            # base ne se lit jamais comme un état favorable.
            relevé = _etat.lire(art)
            row["ingest_state"] = relevé.etat
            row["ingest_reason"] = relevé.raison

    # Le dépôt ne présente que des documents VIVANTS : un lien résiduel vers un
    # artefact supprimé ne fait pas partie du corpus visible. Un lien sans nom
    # affichable non plus — il n'y aurait rien à montrer. Ce filtre écartait les
    # deux EN SILENCE : l'utilisateur lisait un dépôt plus court, sans savoir que
    # des fichiers en étaient absents. `hidden` rend l'écart lisible sans le
    # faire disparaître.
    files: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    for aid in sorted(by_artifact):
        row = by_artifact[aid]
        raison = _raison_ecarte(
            row["display_name"], supprime=aid in supprimes, vu_en_base=aid in vus
        )
        if raison is None:
            files.append(row)
            continue
        hidden.append(
            {"artifact_id": aid, "display_name": row["display_name"], "reason": raison}
        )

    files.sort(
        key=lambda r: (r["added_at"], r["display_name"], r["artifact_id"]), reverse=True
    )
    return {
        "workspace_id": workspace_id,
        "files": files,
        "hidden": hidden,
        "conversation_count": len(space_conversation_ids(db, workspace_id)),
        "is_physical_folder": False,
    }


def stats(db: Any, workspace_id: str) -> dict[str, Any]:
    """Compteurs DISPONIBLES ET RECALCULÉS, jamais stockés.

    Un compteur stocké se désynchronise et devient un mensonge
    (`01_classe_espace.md:66-68`). « Mémoire : Active » des maquettes n'a pas de
    statut correspondant : les statuts de C17 sont candidate/accepted/stale/
    rejected, et aucun ne s'appelle « active ».

    `fichiers` compte les fichiers PRÉSENTABLES, pas les liens rattachés — la
    réserve de `depot()` ne présente que des documents vivants et nommés, et
    l'écran compte déjà `files` : compter les liens donnerait deux chiffres
    différents pour un seul dépôt. Ce qui n'est pas présenté n'est pas pour autant
    muet : `depot()` le nomme dans `hidden`, et l'onglet Dépôt le dit.
    """
    from backend.db.models import Conversation as _Conv, MemoryEntry as _Mem

    content = depot(db, workspace_id)
    convs = db.query(_Conv).filter(_Conv.workspace_id == workspace_id).all()
    mems = (
        db.query(_Mem)
        .filter(_Mem.workspace_id == workspace_id, _Mem.is_current == 1)
        .all()
    )
    by_status: dict[str, int] = {}
    for m in mems:
        by_status[str(m.status)] = by_status.get(str(m.status), 0) + 1

    return {
        "workspace_id": workspace_id,
        "discussions": len(convs),
        "fichiers": len(content["files"]),
        # `octets` est un fait recalculé, pas un quota. Aucun contrat ne définit
        # de plafond ; le maquette affiche « 245 Mo / 10 Go » et le « 10 Go » n'a
        # aucune source. On mesure donc, on n'impose rien (`02_classe_depot.md:74-86`).
        "octets": sum(int(f.get("size_bytes") or 0) for f in content["files"]),
        "memoire": by_status.get("accepted", 0),
        "memoire_par_statut": {
            "candidate": by_status.get("candidate", 0),
            "accepted": by_status.get("accepted", 0),
            "stale": by_status.get("stale", 0),
            "rejected": by_status.get("rejected", 0),
        },
    }


# ---------------------------------------------------------------------------
# Cycle de vie : corbeille 7 jours puis rétrogradation (décision E-SUPPR)
# ---------------------------------------------------------------------------


def trash_workspace(db: Any, workspace_id: str) -> dict[str, Any]:
    """Met l'espace en corbeille pour sept jours. Rien n'est purgé ici.

    Les conversations ne sont PAS supprimées : elles perdent l'accès aux
    ressources de l'espace et redeviennent des chats simples à l'expiration.
    """
    ws = get_workspace(db, workspace_id, include_trashed=False)
    if ws is None:
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable.")
    ws.state = "trashed"
    ws.trashed_at = _now()
    ws.purge_after = _now() + timedelta(days=TRASH_RETENTION_DAYS)
    ws.updated_at = _now()
    db.commit()
    return {
        "ok": True,
        "state": "trashed",
        "purge_after": ws.purge_after.isoformat(),
        "retention_days": TRASH_RETENTION_DAYS,
    }


def restore_workspace(db: Any, workspace_id: str) -> dict[str, Any]:
    """Restaure un espace depuis la corbeille. Aucune rétention n'a eu lieu."""
    ws = get_workspace(db, workspace_id)
    if ws is None:
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable.")
    ws.state = "active"
    ws.trashed_at = None
    ws.purge_after = None
    ws.updated_at = _now()
    db.commit()
    return {"ok": True, "state": "active"}


def purge_workspace(db: Any, workspace_id: str) -> dict[str, Any]:
    """Purge l'espace : RÉTROGRADATION, pas cascade (décision E-SUPPR).

    Répond à la question ouverte de `06_classe_cycle_de_vie.md:60-64` et de
    `10_decisions_et_seuils.md:26`. Les conversations deviennent des chats
    simples ; les blobs et les liens d'espace sont retirés, pas les octets ;
    la mémoire part avec l'espace.

    Pourquoi rétrograder plutôt que propager en cascade (Unsloth fait
    `ON DELETE CASCADE`) : l'utilisateur peut perdre des échanges qu'il n'avait
    pas compris comme dépendants de cet espace, et C17:260 refuse précisément
    qu'un historique dépendant soit traité comme un simple ensemble de lignes.
    """
    from backend.db.models import (
        ArtifactLink as _Link,
        Conversation as _Conv,
        MemoryEntry as _Mem,
        MemoryUsage as _Use,
    )

    # Garde d'état : la rétention de sept jours est un CALENDRIER, pas une
    # dispense. Purger un espace actif d'office supprimerait des conversations
    # sans que l'utilisateur ait jamais rien mis à la corbeille, alors que le
    # contrat exige « choix utilisateur, confirmation, dépendances ».
    ws = get_workspace(db, workspace_id, include_trashed=True)
    if ws is None:
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable.")
    if str(ws.state) != "trashed":
        raise LookupError(
            "[WORKSPACE_NOT_TRASHED] Espace encore actif : mets-le à la corbeille "
            "avant de le purger."
        )

    conv_ids = space_conversation_ids(db, workspace_id)
    # Rétrogradation EXPLICITE, et non laissée au `ON DELETE SET NULL`.
    # Cette contrainte n'est active que si `PRAGMA foreign_keys = ON`, ce que la
    # PRAGMA du moteur applicatif pose mais qu'aucune connexion tierce ne
    # garantit : sans elle, des conversations resteraient pointant sur un espace
    # inexistant. La décision de cycle de vie ne dépend pas d'un pragma.
    if conv_ids:
        db.query(_Conv).filter(_Conv.id.in_(conv_ids)).update(
            {_Conv.workspace_id: None}, synchronize_session=False
        )
    # Même raison pour la mémoire : le `ON DELETE CASCADE` de
    # `memory_entries.workspace_id` ne peut pas être le seul mécanisme, sinon
    # une purge laisse des fiches orphelines qui redeviendront lisibles si un
    # jour un id d'espace est réattribué (il ne l'est pas, mais la défense
    # contre l'orphelin ne doit pas dépendre de cette promesse).
    entry_ids = [
        str(r[0])
        for r in db.query(_Mem.id).filter(_Mem.workspace_id == workspace_id).all()
    ]
    if entry_ids:
        db.query(_Use).filter(_Use.entry_id.in_(entry_ids)).delete(
            synchronize_session=False
        )
    mem_removed = (
        db.query(_Mem)
        .filter(_Mem.workspace_id == workspace_id)
        .delete(synchronize_session=False)
    )
    # Les liens d'espace sont retirés ; ceux des conversations ne le sont pas —
    # ils appartiennent aux conversations.
    removed = (
        db.query(_Link)
        .filter(_Link.scope_kind == "workspace", _Link.scope_id == workspace_id)
        .delete(synchronize_session=False)
    )
    db.delete(ws)  # filet de sécurité : SET NULL rattrape ce que l'UPDATE a manqué
    db.commit()
    return {
        "ok": True,
        "conversations_demoted": len(conv_ids),
        "conversation_ids": conv_ids,
        "links_removed": int(removed),
        "memory_versions_removed": int(mem_removed),
        "blobs_preserved": True,
        "note": "les conversations redeviennent des chats simples ; leurs fichiers restent les leurs",
    }


def expired_trash(db: Any, *, now: datetime | None = None) -> list[str]:
    """Espaces dont la corbeille est échue. La purge reste une décision utilisateur.

    On LISTE, on n'exécute pas : « Exiger choix utilisateur, confirmation,
    dépendances et option de sauvegarde » (`backend_espaces.md:187-191`).
    """
    from backend.db.models import Workspace as _Ws

    ref = now or _now()
    rows = (
        db.query(_Ws)
        .filter(
            _Ws.state == "trashed", _Ws.purge_after.is_not(None), _Ws.purge_after <= ref
        )
        .all()
    )
    return [str(r.id) for r in rows]


# ---------------------------------------------------------------------------
# Déplacement — le parcours de §7.3, évalué avant toute modification
# ---------------------------------------------------------------------------
#
# LA FICHE QUI GOUVERNE : `docs/intentions/fonctionnalites/espaces.md` §7.3
# (l.160-168). Trois phrases y fixent le contrat, citées où elles s'appliquent :
#
#   « Déplacer une discussion de l'espace X vers l'espace Y déplace la discussion
#     et les fichiers qui lui sont liés. »
#   « Le corpus documentaire de X perd les contributions de ces fichiers ; celui
#     de Y les reçoit, avec leurs représentations exploitables et leur
#     provenance. Le dépôt et la mémoire générale de X ne sont pas déplacés
#     en bloc. »
#   « Les synthèses des deux espaces sont actualisées selon leurs contributions
#     présentes. »
#   « Les images liées à la discussion restent préservées et visibles dans la
#     galerie tout en changeant de rattachement ; le déplacement ne les
#     supprime pas et ne les duplique pas inutilement. »
#
# La remise à neutre des signaux d'importance et de tendance des ressources
# déplacées (« repartent de façon neutre dans le nouveau contexte ») vit dans
# `services/memoire.py` (`remettre_a_neutre_signaux`) : c'est elle qu'appelle
# `request_move` à l'accord, et c'est elle qu'il compte dans le rapport.


def _contributions_du_depart(db: Any, conversation_id: str) -> list[str]:
    """Artefacts liés À CETTE conversation par un lien actif : ils suivront à Y.

    §7.3 : « Déplacer une discussion de l'espace X vers l'espace Y déplace la
    discussion et les fichiers qui lui sont liés. » Le transfert ne coûte
    AUCUNE écriture : le corpus de l'espace est une requête qui déduit des
    conversations (`scope.resolve_space_information_scope`), donc Y reçoit les
    contributions dès que `conversations.workspace_id` change. On les liste
    quand même, pour que le rapport et l'écran nomment ce qui transite — un
    transfert invisible est un transfert suspect.
    """
    from backend.db.models import ArtifactLink as _Link

    rows = (
        db.query(_Link.artifact_id)
        .filter(
            _Link.scope_kind == "conversation",
            _Link.scope_id == conversation_id,
            _Link.removed_at.is_(None),
        )
        .all()
    )
    return sorted({str(r[0]) for r in rows})


def _syntheses_a_invalider(
    db: Any, workspace_id: str, artifact_ids: list[str]
) -> list[str]:
    """Fiches acceptées DE CET espace qui citent une contribution en départ.

    §7.3 : « Les synthèses des deux espaces sont actualisées selon leurs
    contributions présentes. » Une fiche acceptée dont une source quitte
    l'espace n'est plus soutenue telle qu'elle le prétend : identifier les
    fiches concernées ici (l'évaluation) pour les marquer à l'application
    (`request_move`), sinon la synthèse de X affirmerait « d'après le
    document… » sur un document qui est aujourd'hui dans Y.
    """
    if not artifact_ids:
        return []
    from backend.db.models import MemoryEntry as _Mem

    partants = set(artifact_ids)
    fiches: list[str] = []
    for mem in (
        db.query(_Mem)
        .filter(
            _Mem.workspace_id == workspace_id,
            _Mem.is_current == 1,
            _Mem.status == "accepted",
        )
        .all()
    ):
        cites = {str(s) for s in (mem.source_artifact_ids or [])}
        if cites & partants:
            fiches.append(str(mem.id))
    return sorted(fiches)


def _rapport_deplacement(
    conversation_id: str,
    source: str | None,
    cible: str,
    *,
    contributions: list[str],
    validees: list[str],
    restent: list[dict[str, Any]],
    note: str,
    changed: bool = False,
) -> dict[str, Any]:
    """Le rapport d'évaluation. UNE SEULE forme pour toutes les branches.

    `blocking` et `offer` restent dans l'enveloppe : §7.3 ne bloque plus, la
    clarté du rapport a changé, pas la forme que lisent les appelants. User d'un
    autre vocabulaire pour « ce qui reste en arrière » (« left_behind »)
    reproduirait deux listes pour un seul fait.
    """
    return {
        "ok": True,
        "allowed": True,
        "conversation_id": conversation_id,
        "source_workspace_id": source,
        "target_workspace_id": cible,
        # Vides par §7.3 : le déplacement traverse, il n'exige plus la preuve
        # d'indépendance. Les clés restent, le refus n'existe plus.
        "blocking": [],
        "offer": None,
        # Ce qui reste à X, et NON ce qui empêche : l'utilisateur doit le savoir
        # avant que rien n'ait bougé.
        "lost_links": restent,
        "left_behind": {
            "fichiers_depot": restent,
            "memoire_acceptee_a_invalider": len(validees),
        },
        # Le transfert : les contributions listées par identifiant (§7.3),
        # et les fiches de X que la modification rendra révisables.
        "contributions": contributions,
        "syntheses_a_invalider": validees,
        # Vrai sur TOUTES les branches, et c'est le point : un déplacement de
        # conversation ne déplace AUCUN octet. Ni blob, ni chunk, ni vecteur ne
        # bougent ; une image liée ne change pas d'identité — elle change de
        # rattachement avec sa discussion, et la galerie la garde.
        "artifacts_moved": 0,
        # Vrai dès que la source diffère de la cible — y compris sans source :
        # une discussion sans espace gagne un rattachement, ce n'est pas un
        # non-événement. Faux seulement quand elle est déjà dans la cible.
        "changed": changed,
        "note": note,
    }


def assess_move(
    db: Any, conversation_id: str, target_workspace_id: str
) -> dict[str, Any]:
    """Évalue un déplacement SANS rien modifier (`06_classe_cycle_de_vie.md:45`).

    ASSOUPLISSEMENT VOLONTAIRE, traçable : le profil initial refusait le
    déplacement tant que l'indépendance vis-à-vis des ressources de l'origine
    n'était pas démontrée (C17:260). §7.3 de
    `docs/intentions/fonctionnalites/espaces.md` fixe désormais le
    comportement POSITIF : « Déplacer une discussion de l'espace X vers l'espace
    Y déplace la discussion et les fichiers qui lui sont liés », avec ses
    conséquences écrites (corpus, synthèses, images). Le déplacement est donc
    une opération normale : ses conséquences sont LISTÉES dans le rapport, plus
    retranchées derrière des refus.

    De côté restent — nommés, jamais bloquants — ce que la fiche garde à X :
    « Le dépôt et la mémoire générale de X ne sont pas déplacés en bloc. Les
    références historiques de l'ancien dépôt restent historiques : elles ne
    donnent pas de nouveaux droits sur X et ne deviennent pas des fichiers de
    Y par simple mention. » Resté en arrière n'est pas une panne : l'écran doit
    pouvoir le dire sans que le geste devienne impossible.
    """
    from backend.db.models import Artifact as _Art, Conversation as _Conv

    conv = db.query(_Conv).filter(_Conv.id == conversation_id).first()
    if conv is None:
        raise LookupError("[CONVERSATION_NOT_FOUND] Conversation introuvable.")
    target = get_workspace(db, target_workspace_id, include_trashed=False)
    if target is None:
        raise LookupError(
            "[WORKSPACE_NOT_FOUND] Espace de destination introuvable ou en corbeille."
        )

    source = conv.workspace_id
    if source and source == target_workspace_id:
        return _rapport_deplacement(
            conversation_id,
            source,
            target_workspace_id,
            contributions=[],
            validees=[],
            restent=[],
            note="déjà dans l'espace de destination",
            changed=False,
        )

    partants = _contributions_du_depart(db, conversation_id)
    # §7.3 : « Les images liées à la discussion restent préservées et visibles
    # dans la galerie tout en changeant de rattachement ; le déplacement ne les
    # supprime pas et ne les duplique pas inutilement. » Compte, pas traitement :
    # leur lien est à la portée conversation, il suit comme les autres, et
    # AUCUN octet ne bouge — le rapport nomme ce que l'utilisateur vérifiera.
    images = 0
    if source and partants:
        images = (
            db.query(_Art)
            .filter(
                _Art.id.in_(partants), _Art.deleted_at.is_(None), _Art.kind == "image"
            )
            .count()
        )
    if source:
        # Zone utilisateur de l'ORIGINE : elle reste à X (§7.3, en en-tête).
        restent = [
            {
                "artifact_id": f["artifact_id"],
                "display_name": f["display_name"],
                "origin": "user",
                "reason": "fichier de la zone utilisateur de l'espace d'origine",
            }
            for f in depot(db, source)["files"]
        ]
        fiches_partantes = _syntheses_a_invalider(db, source, partants)
        if images:
            complement = (
                f"{len(partants)} contribution(s) suivent la discussion ({images} image(s) "
                f"préservée(s), ni supprimée(s) ni dupliquée(s)) ; "
                f"{len(fiches_partantes)} fiche(s) de X citent une contribution"
            )
        else:
            complement = (
                f"{len(partants)} contribution(s) suivent la discussion ; "
                f"{len(fiches_partantes)} fiche(s) de X citent une contribution"
            )
    else:
        # Un espace d'origine inexistant n'a ni dépôt ni fiches : rien ne reste
        # en arrière. Les contributions, elles, suivent bel et bien — c'est le
        # rattachement qui change, pas un non-événement.
        restent = []
        fiches_partantes = []
        sujet = (
            "1 contribution suit la discussion"
            if len(partants) == 1
            else f"{len(partants)} contributions suivent la discussion"
        )
        complement = f"{sujet} ; rattachement à l'espace de destination"
    return _rapport_deplacement(
        conversation_id,
        source,
        target_workspace_id,
        contributions=partants,
        validees=fiches_partantes,
        restent=restent,
        note=complement,
        changed=True,
    )


def request_move(
    db: Any, conversation_id: str, target_workspace_id: str, *, confirm: bool = False
) -> dict[str, Any]:
    """Déplacement §7.3 : un rapport d'abord, une application ensuite.

    Sans `confirm`, rien n'est modifié : le rapport de `assess_move` sert de
    pare-feu. Avec `confirm`, le rattachement change ; les fiches de l'espace
    source qui citent une contribution déplacée sont marquées révisables
    (`memoire.mark_stale`) — même transition subie que pour une source perdue,
    et c'est bien de cela qu'il s'agit ici : §7.3 : « Les synthèses des deux
    espaces sont actualisées selon leurs contributions présentes. » La remise à
    neutre des signaux d'importance des ressources déplacées suit le même
    parcours (`memoire.remettre_a_neutre_signaux`) : le signal accumulé dans
    l'ancien contexte n'est pas transféré comme une importance acquise dans le
    nouveau.

    Le rattachement est COMMITTER à ce point : l'étape suivante (remise à neutre
    des signaux, invalidation des fiches) qui échouerait ne doit pas remonter une
    exception — le transfert serait déjà engagé, et un half-state serait pire
    qu'un signal. L'échec est donc nommé dans le résultat (`syntheses_erreurs`),
    jamais masqué.
    """
    report = assess_move(db, conversation_id, target_workspace_id)
    if not confirm:
        return report
    from backend.db.models import Conversation as _Conv
    from backend.services import memoire as memoire_v

    conv = db.query(_Conv).filter(_Conv.id == conversation_id).first()
    if conv is None:
        raise LookupError("[CONVERSATION_NOT_FOUND] Conversation introuvable.")
    conv.workspace_id = target_workspace_id
    conv.updated_at = _now()
    db.commit()

    # Le contexte d'origine, tel que l'évaluation l'a constaté : c'est lui que
    # la remise à neutre des signaux vise — la contribution a quitté cet espace.
    source = str(report.get("source_workspace_id") or "")
    # Les contributions qui suivent la discussion : ce sont elles, et pas
    # l'espace entier, que le reset de signaux doit viser.
    partants = list(report.get("contributions") or [])

    # Ce reset précède l'invalidation : il compte le signal sur les versions
    # courantes encore acceptées, avant qu'elles ne deviennent des versions
    # révisées — une invalidation (`mark_stale`) refait remonter le signal copié
    # dans la version suivante, et masquerait ainsi le signal accumulé.
    # Remise à neutre des signaux d'importance : le contexte a changé, l'ancien
    # score n'est pas transféré comme une importance acquise dans le nouveau
    # contexte (§7.3, `espaces.md:166`). Reset en place via le service mémoire —
    # et tolérance : l'accord de déplacement est déjà engagé, un échec de session
    # ne doit pas l'annuler, il se signale dans le rapport.
    signaux: dict[str, Any] = {
        "workspace_id": source,
        "artifacts": [],
        "entrees": 0,
        "signaux_supprimes": 0,
    }
    erreurs: list[dict[str, Any]] = []
    # Sans espace d'origine, il n'y a aucun contexte à remettre à neutre : un
    # `workspace_id=""` ne désigne rien et produirait une `syntheses_erreurs`
    # inutile. Le bloc reste le même quand la source existe.
    if source:
        try:
            signaux = memoire_v.remettre_a_neutre_signaux(db, source, partants)
        except Exception as e:  # noqa: BLE001 — tolérance assumée, jamais un silence
            # Le transfert est déjà commité et ne se déconnecte pas de l'accord, mais
            # l'échec remonte dans le rapport.
            erreurs.append({"fiche": source, "raison": str(e)})

    # Les fiches de X dont une source vient de quitter : révisables, avec la
    # raison qui nomme LE FAIT (la discussion est partie), pas l'action.
    invalidees: list[str] = []
    for fiche in report.get("syntheses_a_invalider") or []:
        try:
            out = memoire_v.mark_stale(
                db,
                fiche,
                reason="discussion déplacée : ses contributions ont quitté le corpus de l'espace",
            )
        except LookupError:
            # Une fiche déjà tombée (supprimée entre l'évaluation et l'accord)
            # ne bloque pas le déplacement : le reste de la liste est traité.
            continue
        except Exception as e:  # noqa: BLE001 — un rattrapage ne doit rien casser
            # Le transfert est déjà commité : on ne l'annule pas (le commit point
            # tient), mais l'échec ne doit plus rester invisible — la fiche sort
            # de la liste des invalidées et remonte dans le rapport.
            erreurs.append({"fiche": str(fiche), "raison": str(e)})
            continue
        if out.get("ok"):
            # L'identifiant RENDU est celui de la version créée : celui que la
            # relecture (écran ou recherche) lira comme version courante.
            invalidees.append(str(out.get("entry_id") or fiche))

    resultat: dict[str, Any] = {
        **report,
        "applied": True,
        "syntheses_invalidees": invalidees,
        "signaux_remis_a_neutre": signaux,
        "note": "rattachement seul ; aucun fichier, id, chunk ou vecteur touché",
    }
    if erreurs:
        # Tolérance assumée, jamais un silence : les étapes d'après le commit qui
        # ont échoué sont nommées ici, sans annuler un transfert déjà engagé.
        resultat["syntheses_erreurs"] = erreurs
    return resultat
