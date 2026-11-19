# services/memoire_documentaire.py — extraction de faits documentaires (Phase 8).
#
# D'OÙ VIENT CE FICHIER : la mémoire d'espace se nourrissait uniquement des
# messages de l'utilisateur (`memoire_auto.extraire_faits` exige `role=="user"`).
# Un document ingéré, même parfaitement lu, ne produisait donc aucun fait.
#
# OÙ VA QUOI : trois étages, séparés par des frontières nettes :
#   1. le LEDGER (`memory_document_extractions`) décide QUOI traiter, quand,
#      avec quel bail et combien de tentatives ;
#   2. l'EXTRACTION transforme un texte en faits cités et vérifiés ;
#   3. le REMPLACEMENT écrit des fiches versionnées, sans jamais écraser une
#      correction utilisateur.
#
# POINT D'INSERTION DU LLM STRUCTURÉ (tâche 8.4, second coder) :
#   `extraire_faits_document(..., extracteur_structure=...)` reçoit l'extracteur
#   LLM en paramètre. Le module expose aussi `EXTRACTEUR_STRUCTURE`, un hook
#   global optionnel. Le LLM doit rendre des `FaitDocument` (ou `None` pour
#   laisser le repli) ; en cas d'erreur, de schéma invalide ou d'absence de
#   citation vérifiée, le repli DÉTERMINISTE ci-dessous s'applique. Aucun autre
#   étage ne change : le ledger, le claim, la provenance et le remplacement
#   restent identiques.
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.services import memoire

log = logging.getLogger(__name__)

# Version du contrat d'extraction documentaire. Le LLM 8.4 pourra introduire sa
# propre version : c'est elle qui distingue deux lignes de ledger et permet de
# rejouer un document quand l'extracteur progresse.
VERSION_EXTRACTEUR = "doc-faits-deterministe/1"

# Version EFFECTIVEMENT active. Elle vaut le déterministe tant que l'extracteur
# LLM n'est pas installé, puis bascule avec `installer_extracteur_llm`. Tous les
# enqueue/backfill la lisent AU MOMENT DE L'APPEL : un défaut figé à la
# définition d'une fonction ferait entrer des documents en `doc-faits-llm/1`
# dans le ledger alors que le LLM n'est pas branché — ou l'inverse.
VERSION_EXTRACTEUR_ACTIVE = VERSION_EXTRACTEUR


def version_extracteur_active() -> str:
    """Version d'extracteur à inscrire dans les NOUVELLES lignes de ledger."""
    return VERSION_EXTRACTEUR_ACTIVE


def definir_version_extracteur_active(version: str) -> None:
    """Fixe la version active. `installer_extracteur_llm` en est l'appelant.

    DÉCISION : il n'existe pas de chemin de désinstallation. Dans un processus
    donné, l'installation du LLM est définitive ; au redémarrage, le module
    repart du déterministe et `installer_extracteur_llm` rebascule la version.
    Aucune ligne existante n'est réécrite ici : le backfill réarme les `done`
    dont la version diffère.
    """
    version_propre = str(version or "").strip()
    if not version_propre:
        raise ValueError("[VERSION_EXTRACTEUR] une version active est requise")
    global VERSION_EXTRACTEUR_ACTIVE
    VERSION_EXTRACTEUR_ACTIVE = version_propre


STATUTS_LEDGER = frozenset(
    {"pending", "running", "done", "failed", "skipped", "deferred"}
)

MAX_FAITS_DOCUMENT = 5
MIN_FAIT_CHARS = 40
MAX_FAIT_CHARS = 300
MAX_ENTREE_EXTRACTION_CHARS = 16_000

# Bail documentaire (décision du plan) : TTL 5 min, heartbeat 60 s.
TTL_BAIL_S = 300
HEARTBEAT_S = 60.0
CADENCE_DIFFERE_S = 300
PLAFOND_EXECUTIONS = 3

# Budget d'une passe : 2 documents, 60 s. Un document par unité de travail.
BUDGET_DOCUMENTS = 2
BUDGET_SECONDES = 60.0
BACKFILL_INTERVALLE_S = 300

# Motifs STRUCTURÉS de péremption documentaire.
STALE_REASON_DOCUMENT_MIS_A_JOUR = "document_mis_a_jour"
STALE_REASON_PREUVE_INVALIDEE = "preuve_documentaire_invalidee"

# Hook global optionnel pour la tâche 8.4. Le second coder peut soit le
# renseigner, soit passer `extracteur_structure=` à l'appel. Le repli
# déterministe reste TOUJOURS disponible.
EXTRACTEUR_STRUCTURE: Callable[..., list["FaitDocument"] | None] | None = None

# Verbes déclaratifs observés dans des documents réels. Le motif de
# `memoire_auto` est volontairement étroit pour les messages ; un document
# technique dit « contient », « mesure », « exige », « possède ». Les deux
# motifs se cumulent, sans modifier celui de la mémoire de discussion.
_VERBE_DOCUMENTAIRE_RE = re.compile(
    r"\b(?:est|sont|était|étaient|utilise|utilisent|tourne|tournent|reste|"
    r"restent|doit|doivent|contient|contiennent|comporte|comportent|possède|"
    r"possèdent|préfère|préfèrent|exige|exigent|mesure|mesurent|coûte|coûtent|"
    r"date|datent|équivaut|équivalent|signifie|signifient|concerne|concernent|"
    r"permet|permettent|produit|produisent|stocke|stockent|conserve|conservent|"
    r"appartient|appartiennent|correspond|correspondent|représente|représentent|"
    r"définit|définissent)\b",
    re.IGNORECASE,
)

_ENQUEUE_ECHECS = 0
_ENQUEUE_LOCK = threading.Lock()


def _now() -> datetime:
    """Horodatage naïf UTC, comme les autres colonnes du dépôt."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session():
    """Session courte, résolue au moment de l'appel (tests et redémarrages)."""
    from backend.db.database import SessionLocal

    return SessionLocal()


def _reveiller_extraction_documentaire() -> None:
    """Réveille la boucle documentaire après un enqueue — best-effort.

    Import DIFFÉRÉ : `memoire_scheduler` importe déjà ce module dans ses
    fonctions ; un import de haut niveau créerait le cycle. Ce signal ne doit
    jamais coûter l'enqueue : toute erreur est avalée, le backfill reste le
    filet.
    """
    try:
        from backend.services.memoire_scheduler import reveiller_extraction_documentaire

        reveiller_extraction_documentaire()
    except Exception:  # pragma: no cover — le fond ne casse jamais l'appelant
        pass


def noter_echec_enqueue(observation: Any, exc: Exception) -> None:
    """Compteur monotone des enqueues ratés — le backfill les rattrapera."""
    global _ENQUEUE_ECHECS
    with _ENQUEUE_LOCK:
        _ENQUEUE_ECHECS += 1
    log.warning(
        "[memoire-doc] enqueue raté pour %s : %s: %s",
        getattr(observation, "artifact_id", "?"),
        type(exc).__name__,
        exc,
    )


def compteurs_enqueue() -> dict[str, int]:
    with _ENQUEUE_LOCK:
        return {"echecs": _ENQUEUE_ECHECS}


def reinitialiser_compteurs_enqueue() -> None:
    global _ENQUEUE_ECHECS
    with _ENQUEUE_LOCK:
        _ENQUEUE_ECHECS = 0


@dataclass(frozen=True)
class FaitDocument:
    """Un fait documentaire, avec la citation qui le soutient.

    `quote` est la phrase EXACTE du document (normalisation d'espaces seulement) ;
    `chunk_id` désigne le fragment persisté qui la porte. Les deux sont
    obligatoires : un fait sans citation vérifiée n'existe pas.
    """

    content: str
    quote: str
    category: str
    affirmation_status: str
    entry_key: str
    origin_context: str
    evidence_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    observation_id: str | None = None
    chunk_id: str | None = None
    chunk_ordinal: int | None = None
    card_revision: int = 0
    extractor_version: str = VERSION_EXTRACTEUR
    method: str = "deterministe"


# ---------------------------------------------------------------------------
# 8.2 — enqueue transactionnel
# ---------------------------------------------------------------------------


def _liens_workspace_vivants(db: Any, artifact_id: str) -> list[str]:
    """Espaces ACTIFS qui voient l'artefact par un lien vivant.

    DEUX chemins, et ils sont tous les deux réels :

    - le partage explicite (`scope_kind='workspace'`) ;
    - une pièce jointe d'une CONVERSATION rattachée à l'espace. C'est le cas
      majoritaire : un document déposé dans une discussion de l'espace doit
      nourrir la mémoire de cet espace sans avoir été « partagé » en plus.

    Une conversation en corbeille (`deleted_at`) ne compte pas : sa portée est
    retirée. L'espace doit être `active`, jamais en corbeille.
    """
    from backend.db.models import (
        ArtifactLink as _Link,
        Conversation as _Conv,
        Workspace as _Ws,
    )

    explicites = (
        db.query(_Link.scope_id)
        .join(_Ws, _Ws.id == _Link.scope_id)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.scope_kind == "workspace",
            _Link.removed_at.is_(None),
            _Ws.state == "active",
        )
        .all()
    )
    conversations = (
        db.query(_Conv.workspace_id)
        .join(_Link, _Link.scope_id == _Conv.id)
        .join(_Ws, _Ws.id == _Conv.workspace_id)
        .filter(
            _Link.artifact_id == artifact_id,
            _Link.scope_kind == "conversation",
            _Link.removed_at.is_(None),
            _Conv.deleted_at.is_(None),
            _Conv.workspace_id.isnot(None),
            _Ws.state == "active",
        )
        .all()
    )
    return sorted(
        {
            str(row[0])
            for row in [*explicites, *conversations]
            if str(row[0] or "").strip()
        }
    )


def _revision_courante(db: Any, artifact_id: str) -> int:
    try:
        from backend.search import cards as _cards

        card = _cards.get_current_card(db, artifact_id)
        return int(getattr(card, "revision", 0) or 0) if card is not None else 0
    except Exception:
        return 0


def _deja_couvrante(
    db: Any,
    *,
    workspace_id: str,
    artifact_id: str,
    observation_id: str | None,
    extractor_version: str,
    card_revision: int,
) -> bool:
    """Une ligne de ledger couvre-t-elle DÉJÀ cette tentative ?

    La clé inclut `observation_id` quand il existe : une ligne `skipped`/`failed`
    d'une observation ANTÉRIEURE ne doit pas masquer une nouvelle observation
    quand le hook d'enqueue a échoué (relevé Phase 8, 🟠 1). Sans observation,
    la clé reste `(workspace, artifact, version)` — le backfill d'un document lu
    avant l'existence du ledger.

    `pending`/`running`/`deferred` restent idempotents. Un `done` ne couvre
    que si la version d'extracteur est identique ; une version différente
    réarme la ligne en `pending` pour rejouer la MÊME observation avec le
    nouvel extracteur. Un `skipped`/`failed` ne couvre que si l'observation
    ET la révision sont identiques ; une révision différente réarme la ligne
    en `pending` au lieu d'en insérer une seconde (l'index unique
    l'interdirait).
    """
    from backend.db.models import MemoryDocumentExtraction as _Mdx

    requete = db.query(_Mdx).filter(
        _Mdx.workspace_id == workspace_id,
        _Mdx.artifact_id == artifact_id,
    )
    if observation_id is not None:
        # Clé de la revue : `(workspace, artifact, observation)`. La version
        # d'extracteur n'entre pas dans le FILTRE quand l'observation est
        # connue : une nouvelle version doit pouvoir reprendre la MÊME
        # observation. Le statut `done` reste terminal si la version est
        # identique ; sinon il est réarmé en `pending` ci-dessous.
        requete = requete.filter(_Mdx.observation_id == observation_id)
    else:
        requete = requete.filter(_Mdx.extractor_version == extractor_version)
    ligne = requete.first()
    if ligne is None:
        return False
    statut = str(ligne.status or "pending")
    if statut == "done":
        if observation_id is not None and str(ligne.extractor_version) != str(
            extractor_version
        ):
            # Une nouvelle version reprend la MÊME observation : réarmer la
            # ligne au lieu de la considérer comme couvrante. L'index unique
            # inclut la version, mais cette ligne est retrouvée par
            # `(workspace, artifact, observation)` : on la ramène à `pending`
            # avec la version courante, sans en insérer une seconde.
            n = (
                db.query(_Mdx)
                .filter(_Mdx.id == ligne.id, _Mdx.status == "done")
                .update(
                    {
                        "status": "pending",
                        "extractor_version": extractor_version,
                        "attempts": 0,
                        "deferrals": 0,
                        "claim_token": None,
                        "claimed_at": None,
                        "heartbeat_at": None,
                        "next_attempt_at": None,
                        "last_error": None,
                        "card_revision": int(card_revision or 0),
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            if n:
                db.flush()
        return True
    if statut in ("pending", "running", "deferred"):
        return True
    if statut not in ("skipped", "failed"):
        return True
    if int(ligne.card_revision or 0) == int(card_revision or 0):
        return True
    # La ligne terminale ne couvre plus l'état courant : on la réarme. Un
    # `skipped`/`failed` d'une AUTRE observation n'arrive jamais ici, la clé
    # incluant `observation_id`.
    n = (
        db.query(_Mdx)
        .filter(_Mdx.id == ligne.id, _Mdx.status == statut)
        .update(
            {
                "status": "pending",
                "attempts": 0,
                "deferrals": 0,
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "next_attempt_at": None,
                "last_error": None,
                "card_revision": int(card_revision or 0),
                "updated_at": _now(),
            },
            synchronize_session=False,
        )
    )
    if n:
        db.flush()
    return True


def _inserer_ligne(
    db: Any,
    *,
    workspace_id: str,
    artifact_id: str,
    observation_id: str | None,
    extractor_version: str,
    status: str,
    card_revision: int,
    last_error: str | None = None,
    chunk_id: str | None = None,
    entry_key: str | None = None,
) -> int:
    """Insertion idempotente d'une ligne de ledger (unique partiel gère NULL).

    `ON CONFLICT DO NOTHING` laisse la contrainte décider, sans transformer un
    rejeu en exception qui ferait tomber l'observation.
    """
    from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

    from backend.db.models import MemoryDocumentExtraction as _Mdx

    if status not in STATUTS_LEDGER:
        raise ValueError(f"[STATUT_LEDGER] statut inconnu : {status}")
    stmt = (
        _sqlite_insert(_Mdx)
        .values(
            id=f"mdx_{uuid.uuid4().hex[:12]}",
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            observation_id=observation_id,
            chunk_id=chunk_id,
            card_revision=int(card_revision or 0),
            extractor_version=extractor_version,
            entry_key=entry_key,
            status=status,
            attempts=0,
            deferrals=0,
            last_error=last_error,
        )
        .on_conflict_do_nothing()
    )
    result = db.execute(stmt)
    return int(result.rowcount or 0)


def enfiler_observation(
    db: Any,
    observation: Any,
    *,
    extractor_version: str | None = None,
    reveiller: bool = False,
) -> int:
    """Crée les lignes `pending`/`skipped` d'une observation réussie.

    La version est résolue À L'APPEL quand elle n'est pas fournie : un enqueue
    qui précède l'installation du LLM doit rester déterministe, et un enqueue
    postérieur doit porter `doc-faits-llm/1`.

    Appelée DANS la transaction de `record_extraction_observation`. Ne lève
    jamais de son propre chef pour un cas métier (artefact absent, sans lien,
    sans texte) : elle rend 0. Les erreurs techniques remontent, l'appelant les
    isole dans un savepoint.

    `reveiller=True` n'est réservé qu'aux appelants qui committent EUX-MÊMES
    après l'enqueue. Par défaut aucun signal n'est émis : réveiller avant le
    commit ferait une passe à vide et, si le commit échouait, réveillerait pour
    des lignes annulées. `record_extraction_observation` signale explicitement
    APRÈS son commit.
    """
    from backend.db.models import Artifact as _Art
    from backend.ingestion import etat as _etat

    version = extractor_version or version_extracteur_active()
    artifact_id = str(getattr(observation, "artifact_id", "") or "")
    if not artifact_id:
        return 0
    if str(getattr(observation, "status", "") or "").lower() != "completed":
        return 0
    art = db.query(_Art).filter(_Art.id == artifact_id).first()
    if art is None or art.deleted_at is not None:
        return 0
    espaces = _liens_workspace_vivants(db, artifact_id)
    if not espaces:
        # Aucun lien d'espace vivant : pas de ledger. Un partage ultérieur
        # passera par `enfiler_apres_relink`.
        return 0

    etat = _etat.lire(art)
    payload = str(getattr(observation, "payload_ref", "") or "")
    sans_texte = etat.etat in _etat.ETATS_SANS_TEXTE or not payload.strip()
    statut = "skipped" if sans_texte else "pending"
    motif = (
        "aucun texte exploitable : " + (etat.phrase or etat.etat)
        if sans_texte
        else None
    )
    revision = _revision_courante(db, artifact_id)
    crees = 0
    for workspace_id in espaces:
        crees += _inserer_ligne(
            db,
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            observation_id=str(getattr(observation, "id", "") or "") or None,
            extractor_version=version,
            status=statut,
            card_revision=revision,
            last_error=motif,
        )
    # Réveil OPT-IN : cette fonction écrit DANS la transaction de l'observation.
    # Par défaut, c'est `record_extraction_observation` qui signale APRÈS son
    # commit — un réveil pré-commit ferait une passe à vide et, si le commit
    # échouait, réveillerait pour une ligne annulée.
    if crees and reveiller:
        _reveiller_extraction_documentaire()
    return crees


def enfiler_apres_relink(
    db: Any,
    workspace_id: str,
    artifact_id: str,
    *,
    extractor_version: str | None = None,
) -> int:
    """Enqueue après la CRÉATION d'un lien workspace, hors transaction du lien.

    La version active est lue à l'appel (voir `version_extracteur_active`).

    Un artefact qui n'a pas encore de texte ne produit AUCUNE ligne — même pas
    une ligne `skipped` : le ledger ne porte que des tentatives réelles, et le
    backfill créera la ligne `pending` quand le texte arrive.

    Un échec TECHNIQUE remonte à l'appelant : c'est lui qui committe le ledger
    et sait s'il doit compter un échec d'enqueue. Cette fonction ne réveille
    jamais la boucle documentaire : le réveil appartient à l'appelant, APRÈS
    son commit.
    """
    from backend.db.models import (
        Artifact as _Art,
        Workspace as _Ws,
    )
    from backend.ingestion import etat as _etat

    version = extractor_version or version_extracteur_active()
    ws = db.query(_Ws).filter(_Ws.id == workspace_id, _Ws.state == "active").first()
    art = db.query(_Art).filter(_Art.id == artifact_id).first()
    if ws is None or art is None or art.deleted_at is not None:
        return 0
    etat = _etat.lire(art)
    if etat.etat in _etat.ETATS_SANS_TEXTE or not etat.a_texte:
        # « Pas de ledger sans texte » : ne pas créer de ligne `skipped` ici.
        # Une ligne terminale sans texte masquerait un enqueue ultérieur
        # quand le document devient lisible (relevé Phase 8, 🟡 7). Le
        # backfill créera la ligne `pending` dès qu'un texte existe.
        return 0
    texte, observation_id = _etat.texte_ingeste(db, art)
    if not (texte or "").strip():
        return 0
    revision = _revision_courante(db, artifact_id)
    if _deja_couvrante(
        db,
        workspace_id=workspace_id,
        artifact_id=artifact_id,
        observation_id=observation_id,
        extractor_version=version,
        card_revision=revision,
    ):
        return 0
    return _inserer_ligne(
        db,
        workspace_id=workspace_id,
        artifact_id=artifact_id,
        observation_id=observation_id,
        extractor_version=version,
        status="pending",
        card_revision=revision,
    )


# ---------------------------------------------------------------------------
# 8.2b — backfill idempotent
# ---------------------------------------------------------------------------


def backfiller_ledger_documentaire(
    db: Any | None = None,
    *,
    extractor_version: str | None = None,
    limite: int | None = None,
) -> dict[str, int]:
    """Crée les lignes manquantes pour les documents liés AVEC texte.

    Sans argument, la version est celle ACTIVE au moment de l'appel : après
    `installer_extracteur_llm`, le backfill produit donc `doc-faits-llm/1` et
    réarme les `done` déterministes pour les réextraire.

    Idempotent par OBSERVATION quand elle est connue : une ligne
    `(workspace, artifact, observation, version)` couvre la tentative. Un
    `skipped`/`failed` d'une observation antérieure ne masque donc pas une
    nouvelle observation (relevé Phase 8, 🟠 1). Sans observation, la clé reste
    `(workspace, artifact, version)`. Un `skipped`/`failed` n'est réarmé que si
    la révision de fiche a changé.

    Indépendant des tours de conversation : un espace sans message mais avec un
    document lié est traité exactement comme les autres.
    """
    from backend.db.models import (
        Artifact as _Art,
        ArtifactLink as _Link,
        Conversation as _Conv,
        Workspace as _Ws,
    )
    from backend.ingestion import etat as _etat

    version = extractor_version or version_extracteur_active()
    own = db is None
    if own:
        db = _session()
    compteurs = {"scannes": 0, "crees": 0, "ignores": 0, "sans_texte": 0}
    try:
        explicites = (
            db.query(_Link.scope_id, _Art.id)
            .join(_Art, _Art.id == _Link.artifact_id)
            .join(_Ws, _Ws.id == _Link.scope_id)
            .filter(
                _Link.scope_kind == "workspace",
                _Link.removed_at.is_(None),
                _Art.deleted_at.is_(None),
                _Ws.state == "active",
            )
            .all()
        )
        conversations = (
            db.query(_Conv.workspace_id, _Art.id)
            .join(_Link, _Link.scope_id == _Conv.id)
            .join(_Art, _Art.id == _Link.artifact_id)
            .join(_Ws, _Ws.id == _Conv.workspace_id)
            .filter(
                _Link.scope_kind == "conversation",
                _Link.removed_at.is_(None),
                _Art.deleted_at.is_(None),
                _Conv.deleted_at.is_(None),
                _Conv.workspace_id.isnot(None),
                _Ws.state == "active",
            )
            .all()
        )
        rows = [*explicites, *conversations]
        vus: set[tuple[str, str]] = set()
        for workspace_id, artifact_id in rows:
            couple = (str(workspace_id), str(artifact_id))
            if couple in vus:
                continue
            vus.add(couple)
            if limite is not None and compteurs["crees"] >= int(limite):
                break
            compteurs["scannes"] += 1
            art = db.query(_Art).filter(_Art.id == couple[1]).first()
            if art is None:
                compteurs["ignores"] += 1
                continue
            etat = _etat.lire(art)
            if not etat.a_texte:
                compteurs["sans_texte"] += 1
                continue
            texte, observation_id = _etat.texte_ingeste(db, art)
            if not (texte or "").strip():
                compteurs["sans_texte"] += 1
                continue
            revision = _revision_courante(db, couple[1])
            if _deja_couvrante(
                db,
                workspace_id=couple[0],
                artifact_id=couple[1],
                observation_id=observation_id,
                extractor_version=version,
                card_revision=revision,
            ):
                compteurs["ignores"] += 1
                continue
            compteurs["crees"] += _inserer_ligne(
                db,
                workspace_id=couple[0],
                artifact_id=couple[1],
                observation_id=observation_id,
                extractor_version=version,
                status="pending",
                card_revision=revision,
            )
        if own:
            db.commit()
        else:
            db.flush()
    except Exception:
        if own:
            try:
                db.rollback()
            except Exception:
                pass
        raise
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass
    return compteurs


# ---------------------------------------------------------------------------
# 8.3 — extraction déterministe (repli) + point d'extension LLM
# ---------------------------------------------------------------------------


def _chunk_pour_ordinal(
    db: Any, artifact_id: str, ordinal: int, card_revision: int = 0
):
    """Fragment persisté qui porte un ordinal, révision la plus récente d'abord.

    `ordinal_of_quote` ne juge que le CONTENU ; le fragment retenu est celui qui
    le porte réellement. La révision du fragment est celle qu'on écrit dans la
    provenance, jamais une révision devinée. Quand `card_revision` est fourni
    (> 0), on ne retient que les fragments de cette révision : une citation de la
    révision courante ne doit pas être adossée à un fragment antérieur.
    """
    from backend.db.models import ArtifactChunk as _Chk

    requete = db.query(_Chk).filter(
        _Chk.artifact_id == artifact_id, _Chk.ordinal == int(ordinal)
    )
    if card_revision is not None and int(card_revision) > 0:
        requete = requete.filter(_Chk.card_revision == int(card_revision))
    return requete.order_by(_Chk.card_revision.desc(), _Chk.id).first()


def _a_entite(phrase: str) -> bool:
    """Heuristique d'entité : un mot capitalisé APRÈS le premier mot.

    Le premier mot est exclu parce qu'il est capitalisé par la phrase, pas par
    le fait qu'il nomme quelque chose. Les sigles (SQLite, GPU, API) comptent.
    """
    for mot in str(phrase or "").split()[1:]:
        net = mot.strip(".,;:!?()[]{}«»\"'")
        if not net:
            continue
        if net[0].isupper() and any(c.isalpha() for c in net):
            return True
    return False


def _extraire_deterministe(
    db: Any,
    *,
    artifact_id: str,
    workspace_id: str,
    texte: str,
    observation_id: str | None,
    extractor_version: str,
    card_revision: int,
) -> list[FaitDocument]:
    """Repli sans modèle : phrases déclaratives, citées et vérifiées."""
    from backend.search import chunks as _chunks
    from backend.services import memoire_auto as _auto

    chiffre = re.compile(r"\d")
    faits: list[FaitDocument] = []
    empreintes: set[str] = set()
    for brut in _auto._PHRASE_SPLIT_RE.split(str(texte or "")):
        phrase = _auto._normaliser_phrase(brut)
        quote = " ".join(str(brut or "").split())
        if not phrase or not (MIN_FAIT_CHARS <= len(phrase) <= MAX_FAIT_CHARS):
            continue
        if phrase.endswith("?") or _auto._QUESTION_RE.match(phrase):
            continue
        if _auto._SALUTATION_RE.match(phrase):
            continue
        if not (
            _auto._VERBE_DECLARATIF_RE.search(phrase)
            or _VERBE_DOCUMENTAIRE_RE.search(phrase)
        ):
            continue
        if not (chiffre.search(phrase) or _a_entite(phrase)):
            continue
        # La citation est le texte EXACT ; l'ordinal n'est accepté que si un
        # fragment PERSISTÉ la contient réellement.
        ordinal = _chunks.ordinal_of_quote(db, artifact_id, quote, card_revision)
        if ordinal is None:
            continue
        chunk = _chunk_pour_ordinal(db, artifact_id, ordinal, card_revision)
        if chunk is None:
            continue
        content = _auto._tronquer(phrase, MAX_FAIT_CHARS)
        empreinte = memoire.hash_contenu(content)
        if empreinte in empreintes:
            continue
        empreintes.add(empreinte)
        classification = _auto._classifier(phrase) or (
            "connaissances_de_travail",
            "observation",
        )
        category, statut = classification
        chunk_id = str(getattr(chunk, "id", "") or "")
        origin = (
            f"document:{artifact_id}:observation:{observation_id or '-'}:"
            f"chunk:{chunk_id}"
        )
        preuves = [
            valeur
            for valeur in (
                str(observation_id or "").strip(),
                chunk_id.strip(),
            )
            if valeur
        ]
        faits.append(
            FaitDocument(
                content=content,
                quote=quote,
                category=category,
                affirmation_status=statut,
                entry_key=f"doc:{workspace_id}:{artifact_id}:{empreinte[:16]}",
                origin_context=origin,
                evidence_ids=preuves,
                source_artifact_ids=[artifact_id],
                observation_id=observation_id,
                chunk_id=chunk_id or None,
                chunk_ordinal=int(getattr(chunk, "ordinal", ordinal) or ordinal),
                card_revision=int(getattr(chunk, "card_revision", card_revision) or 0),
                extractor_version=extractor_version,
                method="deterministe",
            )
        )
        if len(faits) >= MAX_FAITS_DOCUMENT:
            break
    return faits


def _quote_appartient_au_chunk(
    db: Any, artifact_id: str, chunk_id: str, quote: str
) -> bool:
    """La citation est-elle réellement portée par le fragment cité ?

    Un extracteur structuré peut inventer un `chunk_id` ou rattacher une phrase
    au mauvais fragment. Le contrat de provenance exige que la citation EXISTE
    dans le texte du fragment désigné, sinon le fait n'a pas de preuve et le
    repli déterministe doit s'appliquer (relevé Phase 8, 🟡 4).
    """
    from backend.db.models import ArtifactChunk as _Chk
    from backend.search.chunks import _normalized

    identifiant = str(chunk_id or "").strip()
    if not identifiant:
        return False
    try:
        chunk = (
            db.query(_Chk)
            .filter(_Chk.id == identifiant, _Chk.artifact_id == artifact_id)
            .first()
        )
    except Exception:
        return False
    if chunk is None:
        return False
    return _normalized(quote) in _normalized(chunk.text)


def _valider_faits_structure(
    faits: Any,
    *,
    extractor_version: str,
    db: Any | None = None,
    artifact_id: str | None = None,
) -> list[FaitDocument] | None:
    """Borne et vérifie la sortie d'un extracteur LLM (tâche 8.4).

    Le second coder peut enrichir cette fonction ; le contrat de base est déjà
    posé : liste de `FaitDocument`, au plus 5, chacun avec une citation et un
    fragment. `None` déclenche le repli déterministe.

    Quand `db` et `artifact_id` sont fournis, la citation est VÉRIFIÉE contre le
    texte du fragment désigné : un `chunk_id` inventé ou une citation rattachée
    au mauvais fragment rend le lot invalide (le repli s'applique).
    """
    if not isinstance(faits, list):
        return None
    retenus: list[FaitDocument] = []
    for fait in faits:
        if not isinstance(fait, FaitDocument):
            return None
        if not (fait.content or "").strip() or not (fait.quote or "").strip():
            return None
        if not (fait.chunk_id or "").strip():
            return None
        if (
            db is not None
            and artifact_id is not None
            and not _quote_appartient_au_chunk(
                db, artifact_id, str(fait.chunk_id), fait.quote
            )
        ):
            return None
        retenus.append(fait)
        if len(retenus) >= MAX_FAITS_DOCUMENT:
            break
    return retenus


def extraire_faits_document(
    db: Any,
    *,
    artifact_id: str,
    workspace_id: str,
    texte: str,
    observation_id: str | None,
    extractor_version: str | None = None,
    card_revision: int = 0,
    extracteur_structure: Callable[..., list[FaitDocument] | None] | None = None,
) -> list[FaitDocument]:
    """Extrait jusqu'à 5 faits cités d'un document.

    Sans version fournie, la version ACTIVE est résolue à l'appel : le claim la
    passe toujours explicitement, un appel direct retombe sur la version active.

    POINT D'EXTENSION 8.4 : si `extracteur_structure` (ou le hook global
    `EXTRACTEUR_STRUCTURE`) est fourni, il est essayé d'abord. Toute exception,
    `None` ou sortie invalide revient au repli déterministe : un moteur qui
    échoue ne doit jamais faire perdre le document.
    """
    version = extractor_version or version_extracteur_active()
    if not str(artifact_id or "").strip() or not str(texte or "").strip():
        return []
    extracteur = extracteur_structure or EXTRACTEUR_STRUCTURE
    if extracteur is not None:
        try:
            sortie = extracteur(
                db=db,
                artifact_id=artifact_id,
                workspace_id=workspace_id,
                texte=str(texte)[:MAX_ENTREE_EXTRACTION_CHARS],
                observation_id=observation_id,
                extractor_version=version,
                card_revision=card_revision,
            )
            valides = _valider_faits_structure(
                sortie,
                extractor_version=version,
                db=db,
                artifact_id=artifact_id,
            )
            if valides is not None:
                return valides
        except Exception as exc:  # noqa: BLE001 — repli obligatoire
            # La contention GPU n'est PAS une panne de l'extracteur : le ledger
            # doit la voir pour marquer `deferred` (sans incrémenter attempts).
            # La replier sur le déterministe transformerait une ressource
            # occupée en exécution réussie, et priverait le document du LLM.
            from backend.inference.resource_coordinator import ResourceBusy

            if isinstance(exc, ResourceBusy):
                raise
            log.warning(
                "[memoire-doc] extracteur structuré indisponible pour %s : %s: %s",
                artifact_id,
                type(exc).__name__,
                exc,
            )
    return _extraire_deterministe(
        db,
        artifact_id=artifact_id,
        workspace_id=workspace_id,
        texte=str(texte)[:MAX_ENTREE_EXTRACTION_CHARS],
        observation_id=observation_id,
        extractor_version=version,
        card_revision=card_revision,
    )


# ---------------------------------------------------------------------------
# 8.7 — remplacement par identité, garde-fou correction utilisateur
# ---------------------------------------------------------------------------


def _est_fait_documentaire(entry: Any, artifact_id: str) -> bool:
    """Cette fiche a-t-elle été produite par CE document ?"""
    marker = f"document:{artifact_id}:"
    return marker in str(getattr(entry, "origin_context", "") or "")


def invalider_preuves_par_fragments(
    db: Any,
    *,
    artifact_id: str,
    chunk_ids: set[str] | list[str],
    commit: bool = False,
) -> dict[str, int]:
    """Périme les fiches courantes qui citent des fragments SUPPRIMÉS.

    Appelée DANS la transaction qui supprime les fragments (Option A du relevé
    Phase 8) : aucune fiche courante ne peut citer un `chk_` inexistant entre
    l'ingestion et l'application des faits. La transition reste versionnée —
    `mark_stale` crée la version N+1 — et la nouvelle version ne conserve pas
    les fragments morts dans `evidence_ids`.

    Les fiches déjà `stale`/`rejected` ne sont pas empilées : elles ne sont plus
    servies comme certaines, et leur trace garde la preuve morte pour l'audit.

    Une fiche qui cite AUSSI un chunk encore vivant (autre artefact ou autre
    fragment du même artefact) n'est PAS périmée : seuls ses `evidence_ids`
    filtrés sont remplacés. La péremption est réservée aux fiches dont TOUTES
    les preuves documentaires proviennent des fragments supprimés.
    """
    from backend.db.models import ArtifactChunk as _Chk
    from backend.db.models import MemoryEntry as _Mem

    ids = {str(c) for c in chunk_ids or () if str(c).startswith("chk_")}
    compteurs = {"perimees": 0, "ignorees": 0, "mises_a_jour": 0}
    if not ids:
        return compteurs
    rows = db.query(_Mem).filter(_Mem.is_current == 1).all()
    for row in rows:
        if not _est_fait_documentaire(row, artifact_id):
            continue
        preuves = [str(e) for e in (row.evidence_ids or [])]
        if not (set(preuves) & ids):
            continue
        if str(row.status) not in ("accepted", "candidate"):
            compteurs["ignorees"] += 1
            continue
        restantes = [e for e in preuves if e not in ids]
        restantes_chk = [e for e in restantes if e.startswith("chk_")]
        vivantes = (
            db.query(_Chk).filter(_Chk.id.in_(restantes_chk)).count()
            if restantes_chk
            else 0
        )
        if vivantes > 0:
            # Au moins une preuve documentaire encore vivante : la fiche
            # reste courante, on remplace seulement ses preuves filtrées.
            row.evidence_ids = restantes
            if commit:
                db.commit()
            else:
                db.flush()
            compteurs["mises_a_jour"] += 1
            continue
        resultat = memoire.mark_stale(
            db,
            str(row.id),
            reason="fragment documentaire remplacé pendant l'ingestion",
            stale_reason=STALE_REASON_PREUVE_INVALIDEE,
            evidence_ids=restantes,
            commit=commit,
        )
        if resultat.get("changed"):
            compteurs["perimees"] += 1
        else:
            compteurs["ignorees"] += 1
    return compteurs


def _raison_document(fait: FaitDocument) -> dict[str, Any]:
    return {
        "kind": "document",
        "method": fait.method,
        "version": fait.extractor_version,
    }


def _fusionner_provenance(
    entry: Any, fait: FaitDocument
) -> tuple[str, list[str], list[str], list[Any]]:
    references = memoire._decouper_references(entry.origin_context)
    for reference in memoire._decouper_references(fait.origin_context):
        if reference not in references:
            references.append(reference)
    origin = memoire._borner_references(references, memoire.MAX_ORIGIN_CONTEXT_CHARS)
    preuves = [str(e) for e in (entry.evidence_ids or [])]
    for preuve in fait.evidence_ids:
        if preuve and preuve not in preuves:
            preuves.append(preuve)
    sources = memoire._fusionner_sources(
        [str(s) for s in (entry.source_artifact_ids or [])],
        list(fait.source_artifact_ids),
    )
    raisons = list(entry.promotion_reasons or [])
    if _raison_document(fait) not in raisons:
        raisons.append(_raison_document(fait))
    return origin, preuves[-memoire.MAX_EVIDENCE_IDS :], sources, raisons


def _creer_candidate_documentaire(
    db: Any,
    *,
    workspace_id: str,
    fait: FaitDocument,
    observation_id: str | None,
    extractor_version: str,
) -> str:
    """Rattache le texte ré-extrait comme CANDIDAT, sans toucher à la correction.

    La fiche utilisateur garde son `entry_key` et reste courante. Le candidat
    reçoit une clé dérivée du contenu : il est consultable, mais il ne remplace
    rien.
    """
    from backend.db.models import MemoryEntry as _Mem

    empreinte = memoire.hash_contenu(fait.content)
    key = (
        f"doc_candidate:{workspace_id}:{fait.source_artifact_ids[0] if fait.source_artifact_ids else '-'}:"
        f"{empreinte[:16]}"
    )
    existing = (
        db.query(_Mem).filter(_Mem.entry_key == key, _Mem.is_current == 1).first()
    )
    if existing is not None:
        memoire.ajouter_preuve(
            db,
            existing.id,
            origin_context=fait.origin_context,
            evidence_ids=fait.evidence_ids,
            source_artifact_ids=fait.source_artifact_ids,
            workspace_id=workspace_id,
            commit=False,
        )
        return str(existing.id)
    entry = _Mem(
        id=memoire._new_entry_id(),
        entry_key=key,
        workspace_id=workspace_id,
        version=1,
        is_current=1,
        content=fait.content,
        status="candidate",
        category=fait.category,
        affirmation_status=fait.affirmation_status,
        source_artifact_ids=list(fait.source_artifact_ids),
        origin_context=fait.origin_context[: memoire.MAX_ORIGIN_CONTEXT_CHARS],
        promotion_reasons=[_raison_document(fait)],
        evidence_ids=list(fait.evidence_ids)[-memoire.MAX_EVIDENCE_IDS :],
        source_observation_id=observation_id,
        extractor_version=extractor_version,
        decision={
            "actor": "system",
            "at": _now().isoformat(),
            "action": "document_candidate",
            "reason": "correction utilisateur conservée ; texte ré-extrait "
            "rattaché comme candidat",
        },
    )
    db.add(entry)
    db.flush()
    return str(entry.id)


def appliquer_faits_documentaires(
    db: Any,
    *,
    workspace_id: str,
    artifact_id: str,
    faits: list[FaitDocument],
    observation_id: str | None,
    extractor_version: str,
    commit: bool = True,
) -> dict[str, Any]:
    """Écrit les faits d'un document avec remplacement par identité.

    - clé identique ⇒ version N+1 ;
    - clé différente mais Jaccard ≥ 0,8 ⇒ même fait, version N+1 de la clé
      existante (une reformulation ne crée pas un doublon) ;
    - sinon ⇒ nouveau fait ;
    - une fiche non reproduite ⇒ `stale` motivé ;
    - une fiche corrigée par l'utilisateur n'est JAMAIS réécrite : si le texte
      est sémantiquement le même, on n'ajoute que la provenance ; sinon on
      rattache un candidat séparé et la version utilisateur reste courante.
    """
    from backend.db.models import ArtifactChunk as _Chk, MemoryEntry as _Mem

    compteurs = {
        "crees": 0,
        "versions": 0,
        "candidats": 0,
        "provenances": 0,
        "ignores": 0,
        "stale": 0,
        "exclus": 0,
    }
    fragments_actuels = {
        str(c.id)
        for c in db.query(_Chk.id).filter(_Chk.artifact_id == artifact_id).all()
    }
    rows = (
        db.query(_Mem)
        .filter(_Mem.workspace_id == workspace_id, _Mem.is_current == 1)
        .all()
    )
    documentaires = [row for row in rows if _est_fait_documentaire(row, artifact_id)]
    matches: set[str] = set()

    for fait in faits:
        if memoire.est_exclu(db, workspace_id, fait.content):
            compteurs["exclus"] += 1
            continue
        courante = next(
            (row for row in rows if str(row.entry_key) == fait.entry_key), None
        )
        if courante is None:
            courante = next(
                (
                    row
                    for row in documentaires
                    if memoire._jaccard_textes(row.content, fait.content) >= 0.8
                ),
                None,
            )
        if courante is None:
            resultat = memoire.proposer_automatique(
                db,
                workspace_id,
                fait.content,
                category=fait.category,
                affirmation_status=fait.affirmation_status,
                source_artifact_ids=list(fait.source_artifact_ids),
                origin_context=fait.origin_context,
                promotion_reasons=[_raison_document(fait)],
                evidence_ids=list(fait.evidence_ids),
                entry_key=fait.entry_key,
                actor="document",
                source_observation_id=observation_id,
                extractor_version=extractor_version,
                commit=commit,
            )
            if resultat.get("ok") and resultat.get("created"):
                compteurs["crees"] += 1
                matches.add(fait.entry_key)
            else:
                compteurs["ignores"] += 1
            continue

        matches.add(str(courante.entry_key))
        identique = memoire._normaliser_contenu(courante.content) == (
            memoire._normaliser_contenu(fait.content)
        )
        similaire = identique or (
            memoire._jaccard_textes(courante.content, fait.content) >= 0.8
        )
        if memoire._correction_utilisateur(courante):
            if similaire:
                memoire.ajouter_preuve(
                    db,
                    courante.id,
                    origin_context=fait.origin_context,
                    evidence_ids=fait.evidence_ids,
                    source_artifact_ids=fait.source_artifact_ids,
                    workspace_id=workspace_id,
                    commit=commit,
                )
                compteurs["provenances"] += 1
            else:
                _creer_candidate_documentaire(
                    db,
                    workspace_id=workspace_id,
                    fait=fait,
                    observation_id=observation_id,
                    extractor_version=extractor_version,
                )
                compteurs["candidats"] += 1
            continue
        if str(courante.status) == "rejected":
            compteurs["ignores"] += 1
            continue

        origin, preuves, sources, raisons = _fusionner_provenance(courante, fait)
        resultat = memoire.proposer_automatique(
            db,
            workspace_id,
            fait.content,
            category=fait.category,
            affirmation_status=fait.affirmation_status,
            source_artifact_ids=sources,
            origin_context=origin,
            promotion_reasons=raisons,
            evidence_ids=preuves,
            entry_key=str(courante.entry_key),
            actor="document",
            source_observation_id=observation_id,
            extractor_version=extractor_version,
            commit=commit,
        )
        if resultat.get("ok"):
            compteurs["versions"] += 1
        else:
            compteurs["ignores"] += 1

    # Faits documentaires non reproduits : périmés, mais jamais une correction
    # utilisateur (elle reste la décision qui fait foi).
    for row in documentaires:
        if str(row.entry_key) in matches:
            continue
        if str(row.status) not in ("accepted", "candidate"):
            continue
        if memoire._correction_utilisateur(row):
            continue
        preuves_fragments = {
            str(e) for e in (row.evidence_ids or []) if str(e).startswith("chk_")
        }
        # Deux causes distinctes, deux motifs distincts :
        # - le fragment cité a disparu (contenu changé) → preuve invalidée ;
        # - le fragment existe encore mais l'extracteur ne reproduit plus le
        #   fait → fait simplement non reproduit.
        preuve_invalidee = bool(preuves_fragments) and not (
            preuves_fragments & fragments_actuels
        )
        resultat = memoire.mark_stale(
            db,
            row.id,
            reason="fait documentaire non reproduit après réextraction",
            stale_reason=(
                STALE_REASON_PREUVE_INVALIDEE
                if preuve_invalidee
                else STALE_REASON_DOCUMENT_MIS_A_JOUR
            ),
            workspace_id=workspace_id,
            commit=commit,
        )
        if resultat.get("changed"):
            compteurs["stale"] += 1

    if commit:
        db.commit()
    else:
        db.flush()
    return compteurs


# ---------------------------------------------------------------------------
# 8.6 — claim, exécution, budget
# ---------------------------------------------------------------------------


def _candidat(db: Any, maintenant: datetime):
    """Prochaine ligne due : pending/deferred due, ou running expiré."""
    from sqlalchemy import and_, func, or_

    from backend.db.models import MemoryDocumentExtraction as _Mdx

    seuil = maintenant - timedelta(seconds=TTL_BAIL_S)
    return (
        db.query(_Mdx)
        .filter(
            or_(
                and_(
                    _Mdx.status.in_(("pending", "deferred")),
                    or_(
                        _Mdx.next_attempt_at.is_(None),
                        _Mdx.next_attempt_at <= maintenant,
                    ),
                ),
                and_(_Mdx.status == "running", _Mdx.claimed_at < seuil),
            )
        )
        .order_by(
            func.coalesce(_Mdx.next_attempt_at, _Mdx.created_at),
            _Mdx.created_at,
            _Mdx.id,
        )
        .first()
    )


def claim_prochain_document(
    db: Any, *, maintenant: datetime | None = None
) -> dict[str, Any] | None:
    """Claim atomique d'UN document. Rend le bail gagné, ou None.

    La garde est un UPDATE conditionnel : deux workers qui lisent la même ligne
    ne peuvent pas gagner tous les deux. Un `running` expiré est repris avec un
    NOUVEAU token. POLITIQUE DE REPRISE : chaque expiration compte une
    tentative dans `attempts` (le stall est un échec de worker, pas une
    exécution réussie) ; au plafond `PLAFOND_EXECUTIONS`, la ligne devient
    `failed` terminale. Sans cela, un stall en boucle contournerait la borne 3.
    """
    from backend.db.models import MemoryDocumentExtraction as _Mdx

    maintenant = maintenant or _now()
    seuil = maintenant - timedelta(seconds=TTL_BAIL_S)
    for _ in range(5):
        ligne = _candidat(db, maintenant)
        if ligne is None:
            return None
        token = uuid.uuid4().hex
        base = db.query(_Mdx).filter(_Mdx.id == ligne.id)
        tentatives = int(ligne.attempts or 0)
        try:
            if str(ligne.status) == "running":
                tentatives += 1
                if tentatives >= PLAFOND_EXECUTIONS:
                    termine = base.filter(
                        _Mdx.status == "running", _Mdx.claimed_at < seuil
                    ).update(
                        {
                            "status": "failed",
                            "attempts": tentatives,
                            "claim_token": None,
                            "claimed_at": None,
                            "heartbeat_at": None,
                            "next_attempt_at": None,
                            "last_error": "bail expiré : plafond de reprises atteint",
                            "updated_at": maintenant,
                        },
                        synchronize_session=False,
                    )
                    if termine:
                        db.commit()
                    continue
                gagne = base.filter(
                    _Mdx.status == "running", _Mdx.claimed_at < seuil
                ).update(
                    {
                        "claim_token": token,
                        "claimed_at": maintenant,
                        "heartbeat_at": maintenant,
                        "next_attempt_at": None,
                        "attempts": tentatives,
                        "updated_at": maintenant,
                    },
                    synchronize_session=False,
                )
            else:
                from sqlalchemy import or_

                gagne = base.filter(
                    _Mdx.status.in_(("pending", "deferred")),
                    or_(
                        _Mdx.next_attempt_at.is_(None),
                        _Mdx.next_attempt_at <= maintenant,
                    ),
                ).update(
                    {
                        "status": "running",
                        "claim_token": token,
                        "claimed_at": maintenant,
                        "heartbeat_at": maintenant,
                        "next_attempt_at": None,
                        "updated_at": maintenant,
                    },
                    synchronize_session=False,
                )
            if gagne:
                db.commit()
                return {
                    "id": str(ligne.id),
                    "workspace_id": str(ligne.workspace_id),
                    "artifact_id": str(ligne.artifact_id),
                    "observation_id": ligne.observation_id,
                    "extractor_version": str(ligne.extractor_version),
                    "card_revision": int(ligne.card_revision or 0),
                    "attempts": tentatives,
                    "claim_token": token,
                }
            db.rollback()
        except Exception:
            # Course ou verrou SQLite : un autre worker a peut-être gagné.
            # On relit, on réessaie, et on rend None si personne n'est prenable.
            try:
                db.rollback()
            except Exception:
                pass
    return None


def _differer_prochain(db: Any, maintenant: datetime) -> int:
    """Reporte UN candidat due, sans le compter comme une exécution."""
    from sqlalchemy import or_

    from backend.db.models import MemoryDocumentExtraction as _Mdx

    ligne = (
        db.query(_Mdx)
        .filter(
            _Mdx.status.in_(("pending", "deferred")),
            or_(_Mdx.next_attempt_at.is_(None), _Mdx.next_attempt_at <= maintenant),
        )
        .order_by(_Mdx.next_attempt_at.asc().nullsfirst(), _Mdx.created_at.asc())
        .first()
    )
    if ligne is None:
        return 0
    report = (
        db.query(_Mdx)
        .filter(
            _Mdx.id == ligne.id,
            _Mdx.status.in_(("pending", "deferred")),
            or_(_Mdx.next_attempt_at.is_(None), _Mdx.next_attempt_at <= maintenant),
        )
        .update(
            {
                "status": "deferred",
                "deferrals": _Mdx.deferrals + 1,
                "next_attempt_at": maintenant + timedelta(seconds=CADENCE_DIFFERE_S),
                "last_error": "tour actif ou ressource occupée",
                "updated_at": maintenant,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return int(report or 0)


def _battre(ledger_id: str, token: str) -> bool:
    db = _session()
    try:
        from backend.db.models import MemoryDocumentExtraction as _Mdx

        n = (
            db.query(_Mdx)
            .filter(
                _Mdx.id == ledger_id,
                _Mdx.claim_token == token,
                _Mdx.status == "running",
            )
            .update({"heartbeat_at": _now()}, synchronize_session=False)
        )
        db.commit()
        return bool(n)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


class _Battement:
    """Heartbeat 60 s d'un bail documentaire ; signale la perte du bail."""

    def __init__(self, ledger_id: str, token: str) -> None:
        self.ledger_id = ledger_id
        self.token = token
        self.perdu = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._boucle,
            name=f"thot-memoire-doc-heartbeat-{self.ledger_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _boucle(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            if not _battre(self.ledger_id, self.token):
                self.perdu = True
                return

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)


def _marquer_skipped(ledger_id: str, token: str, motif: str) -> bool:
    db = _session()
    try:
        from backend.db.models import MemoryDocumentExtraction as _Mdx

        n = (
            db.query(_Mdx)
            .filter(
                _Mdx.id == ledger_id,
                _Mdx.claim_token == token,
                _Mdx.status == "running",
            )
            .update(
                {
                    "status": "skipped",
                    "claim_token": None,
                    "claimed_at": None,
                    "heartbeat_at": None,
                    "next_attempt_at": None,
                    "last_error": motif,
                    "updated_at": _now(),
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(n)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _differer_claim(ledger_id: str, token: str, motif: str) -> bool:
    """Reporte un bail déjà pris : deferrals oui, attempts NON."""
    db = _session()
    try:
        from backend.db.models import MemoryDocumentExtraction as _Mdx

        maintenant = _now()
        n = (
            db.query(_Mdx)
            .filter(
                _Mdx.id == ledger_id,
                _Mdx.claim_token == token,
                _Mdx.status == "running",
            )
            .update(
                {
                    "status": "deferred",
                    "claim_token": None,
                    "claimed_at": None,
                    "heartbeat_at": None,
                    "deferrals": _Mdx.deferrals + 1,
                    "next_attempt_at": maintenant
                    + timedelta(seconds=CADENCE_DIFFERE_S),
                    "last_error": motif,
                    "updated_at": maintenant,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(n)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _finaliser_echec(ledger_id: str, token: str, exc: Exception) -> bool:
    """Échec réel : `attempts` avance, terminal motivé au plafond."""
    db = _session()
    try:
        from backend.db.models import MemoryDocumentExtraction as _Mdx

        ligne = (
            db.query(_Mdx)
            .filter(
                _Mdx.id == ledger_id,
                _Mdx.claim_token == token,
                _Mdx.status == "running",
            )
            .first()
        )
        if ligne is None:
            db.rollback()
            return False
        maintenant = _now()
        tentatives = int(ligne.attempts or 0) + 1
        terminal = tentatives >= PLAFOND_EXECUTIONS
        n = (
            db.query(_Mdx)
            .filter(
                _Mdx.id == ledger_id,
                _Mdx.claim_token == token,
                _Mdx.status == "running",
            )
            .update(
                {
                    "status": "failed" if terminal else "deferred",
                    "attempts": tentatives,
                    "claim_token": None,
                    "claimed_at": None,
                    "heartbeat_at": None,
                    "next_attempt_at": (
                        None
                        if terminal
                        else maintenant + timedelta(seconds=CADENCE_DIFFERE_S)
                    ),
                    "last_error": f"{type(exc).__name__}: {exc}"[:2000],
                    "updated_at": maintenant,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(n)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _finaliser_succes(
    ledger_id: str,
    token: str,
    faits: list[FaitDocument],
    *,
    workspace_id: str,
    artifact_id: str,
    observation_id: str | None,
    extractor_version: str,
) -> bool:
    """Écrit fiches + résultat du ledger DANS UNE SEULE transaction fencée.

    Le garde-fou est l'UPDATE conditionnel au `claim_token` : si le bail a été
    volé, zéro ligne, rollback, et AUCUNE fiche n'est écrite. Un worker stallé
    ne peut donc jamais publier un résultat pour un document repris ailleurs.
    """
    from backend.db.models import MemoryDocumentExtraction as _Mdx

    db = _session()
    try:
        ligne = (
            db.query(_Mdx)
            .filter(
                _Mdx.id == ledger_id,
                _Mdx.claim_token == token,
                _Mdx.status == "running",
            )
            .first()
        )
        if ligne is None:
            db.rollback()
            return False
        appliquer_faits_documentaires(
            db,
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            faits=faits,
            observation_id=observation_id,
            extractor_version=extractor_version,
            commit=False,
        )
        premier = faits[0] if faits else None
        maintenant = _now()
        n = (
            db.query(_Mdx)
            .filter(
                _Mdx.id == ledger_id,
                _Mdx.claim_token == token,
                _Mdx.status == "running",
            )
            .update(
                {
                    "status": "done",
                    "attempts": _Mdx.attempts + 1,
                    "claim_token": None,
                    "claimed_at": None,
                    "heartbeat_at": maintenant,
                    "next_attempt_at": None,
                    "chunk_id": premier.chunk_id if premier else ligne.chunk_id,
                    "card_revision": (
                        int(premier.card_revision)
                        if premier is not None
                        else int(ligne.card_revision or 0)
                    ),
                    "entry_key": premier.entry_key if premier else ligne.entry_key,
                    "last_error": None,
                    "updated_at": maintenant,
                },
                synchronize_session=False,
            )
        )
        if not n:
            db.rollback()
            return False
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass


def _journaliser_extraction(
    *,
    claim: dict[str, Any],
    texte: str,
    faits: list[FaitDocument],
    duration_ms: float,
) -> None:
    """Trace structurée d'une extraction, sans jamais interrompre celle-ci.

    L'application (sidecar) ne configure ni handler ni niveau global : le niveau
    effectif reste WARNING. On garde donc WARNING en production pour que cette
    mesure reste visible. Si une configuration active INFO (tests, outillage),
    on journalise à INFO pour ne pas produire un WARNING par document. Le
    contenu de la ligne ne dépend pas du niveau.
    """
    try:
        premier = faits[0] if faits else None
        methode = str(getattr(premier, "method", "") or "aucun")
        extracteur_configure = EXTRACTEUR_STRUCTURE is not None
        repli = "oui" if extracteur_configure and methode == "deterministe" else "non"
        moteur = os.environ.get("THOT_TOOL_RUNTIME", "llama_cpp").strip().lower()
        provider = (
            "déterministe"
            if methode == "deterministe"
            else ("llama_server" if moteur == "llama_server" else "local")
        )
        niveau = logging.INFO if log.isEnabledFor(logging.INFO) else logging.WARNING
        log.log(
            niveau,
            "memoire_documentaire extraction "
            "artifact_id=%s document_id=%s workspace_id=%s "
            "duration_ms=%.1f method=%s facts=%d fallback=%s "
            "prompt_chars=%d provider=%s extractor_version=%s",
            claim.get("artifact_id", "?"),
            claim.get("artifact_id", "?"),
            claim.get("workspace_id", "?"),
            duration_ms,
            methode,
            len(faits),
            repli,
            len(str(texte or "")[:MAX_ENTREE_EXTRACTION_CHARS]),
            provider,
            claim.get("extractor_version", "?"),
        )
    except Exception:  # pragma: no cover — tracer ne doit jamais perdre un document
        pass


def _charger_contexte(claim: dict[str, Any]) -> tuple[str, int, str | None]:
    """(texte, révision de fiche, observation) d'un ledger, sans transaction longue."""
    from backend.db.models import (
        Artifact as _Art,
        ArtifactCard as _Card,
        Observation as _Obs,
    )
    from backend.search import chunks as _chunks

    db = _session()
    try:
        art = db.query(_Art).filter(_Art.id == claim["artifact_id"]).first()
        if art is None or art.deleted_at is not None:
            return "", int(claim.get("card_revision") or 0), claim.get("observation_id")
        from backend.ingestion import etat as _etat

        if _etat.lire(art).etat in _etat.ETATS_SANS_TEXTE:
            # Le document a perdu son texte entre l'enqueue et l'exécution
            # (refus binaire, source retirée, contenu vide). Ne pas servir un
            # ancien payload comme s'il était encore lisible.
            return "", int(claim.get("card_revision") or 0), claim.get("observation_id")
        observation_id = claim.get("observation_id")
        texte = ""
        if observation_id:
            obs = db.query(_Obs).filter(_Obs.id == observation_id).first()
            if obs is not None:
                texte = str(obs.payload_ref or "")
        if not texte.strip():
            texte = str(_chunks.best_source_text(db, art) or "")
        revision = int(claim.get("card_revision") or 0)
        if revision <= 0:
            card = (
                db.query(_Card)
                .filter(_Card.artifact_id == art.id, _Card.is_current == 1)
                .order_by(_Card.revision.desc())
                .first()
            )
            revision = int(getattr(card, "revision", 0) or 0) if card else 0
        return texte, revision, observation_id
    finally:
        try:
            db.close()
        except Exception:
            pass


def _executer_claim(claim: dict[str, Any]) -> str:
    """Exécute un bail gagné. Rend le statut observé (done/deferred/…)."""
    from backend.services.activity import liberer, tour_actif, try_reserver

    ledger_id = claim["id"]
    token = claim["claim_token"]
    try:
        texte, revision, observation_id = _charger_contexte(claim)
    except Exception:
        texte, revision, observation_id = (
            "",
            int(claim.get("card_revision") or 0),
            claim.get("observation_id"),
        )
    if not texte.strip():
        return (
            "skipped"
            if _marquer_skipped(ledger_id, token, "aucun texte exploitable")
            else "bail_perdu"
        )

    if tour_actif():
        return (
            "deferred"
            if _differer_claim(ledger_id, token, "tour actif")
            else "bail_perdu"
        )

    reservation = try_reserver("memoire", f"doc:{ledger_id}", timeout=0.0)
    if reservation is None:
        return (
            "deferred"
            if _differer_claim(ledger_id, token, "réservation mémoire occupée")
            else "bail_perdu"
        )
    try:
        if tour_actif():
            return (
                "deferred"
                if _differer_claim(ledger_id, token, "tour actif après réservation")
                else "bail_perdu"
            )
        with _Battement(ledger_id, token) as battement:
            db = _session()
            debut_extraction = time.perf_counter()
            faits: list[FaitDocument] = []
            try:
                faits = extraire_faits_document(
                    db,
                    artifact_id=claim["artifact_id"],
                    workspace_id=claim["workspace_id"],
                    texte=texte,
                    observation_id=observation_id,
                    extractor_version=claim["extractor_version"],
                    card_revision=revision,
                )
            finally:
                _journaliser_extraction(
                    claim=claim,
                    texte=texte,
                    faits=faits,
                    duration_ms=(time.perf_counter() - debut_extraction) * 1000.0,
                )
                try:
                    db.close()
                except Exception:
                    pass
            if battement.perdu:
                return "bail_perdu"
        if _finaliser_succes(
            ledger_id,
            token,
            faits,
            workspace_id=claim["workspace_id"],
            artifact_id=claim["artifact_id"],
            observation_id=observation_id,
            extractor_version=claim["extractor_version"],
        ):
            return "done"
        return "bail_perdu"
    except Exception as exc:  # noqa: BLE001 — l'échec est écrit, pas avalé
        try:
            from backend.inference.resource_coordinator import ResourceBusy
        except Exception:  # pragma: no cover — import défensif
            ResourceBusy = ()  # type: ignore[assignment]
        if isinstance(exc, ResourceBusy):
            return (
                "deferred"
                if _differer_claim(ledger_id, token, f"ressource occupée : {exc}")
                else "bail_perdu"
            )
        return "failed" if _finaliser_echec(ledger_id, token, exc) else "bail_perdu"
    finally:
        liberer(reservation)


def executer_lot_documentaire(
    *,
    budget_documents: int = BUDGET_DOCUMENTS,
    budget_secondes: float = BUDGET_SECONDES,
    maintenant: datetime | None = None,
) -> dict[str, int]:
    """Une passe bornée : 2 documents / 60 s, réservation relâchée entre eux.

    La sélection vient du LEDGER, jamais du compteur de tours : un espace sans
    conversation mais avec un document est servi. Un `tour_actif` reporte le
    document (5 min) au lieu de bloquer le chat.
    """
    from backend.services.activity import tour_actif

    debut = time.monotonic()
    compteurs = {
        "traites": 0,
        "done": 0,
        "deferred": 0,
        "failed": 0,
        "skipped": 0,
        "bail_perdu": 0,
    }
    while compteurs["traites"] < max(0, int(budget_documents)):
        if compteurs["traites"] > 0 and time.monotonic() - debut >= max(
            0.0, float(budget_secondes)
        ):
            break
        if tour_actif():
            db = _session()
            try:
                _differer_prochain(db, maintenant or _now())
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                try:
                    db.close()
                except Exception:
                    pass
            break
        db = _session()
        try:
            claim = claim_prochain_document(db, maintenant=maintenant)
        finally:
            try:
                db.close()
            except Exception:
                pass
        if claim is None:
            break
        statut = _executer_claim(claim)
        compteurs["traites"] += 1
        if statut in compteurs:
            compteurs[statut] += 1
    return compteurs
