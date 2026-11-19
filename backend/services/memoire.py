# services/memoire.py — le registre de faits révisables par l'utilisateur (C17, D07)
#
# D'OÙ VIENT CE FICHIER : `05_classe_memoire.md` et `backend_espaces.md:90-156`.
# OÙ VA QUOI : appelé par le service mémoire et par des COMMANDES UTILISATEUR.
# Le modèle n'a aucun pouvoir d'écriture ici : sa seule action est de PROPOSER
# (`01_classe_espace.md:74-84`). « J'ai noté que ce projet utilise SQLite.
# Voulez-vous que je m'en souvienne ? » est un bouton, jamais un appel d'outil.
#
# LA RÈGLE QUI JUSTIFIQUE TOUT CE FICHIER :
# « La validation humaine n'est pas une prudence d'ergonomie, c'est la frontière
# de fiabilité » (05_classe_memoire.md:88-90). Sur la tâche de consolidation de
# faits contradictoires, séparer l'IDENTIFICATION de la preuve de l'EXÉCUTION de
# la politique fait passer 54 % → 82 % en simple saut. C'est exactement la
# séparation modèle/service que C17 décrivait sans la mesurer.
#
# QUATRE INTERDITS, POSÉS, DONT DEUX SONT DES DÉCISIONS ET NON DES PRUDENCES :
# 1. L'IA ne modifie pas la mémoire (E07, C17:250).
# 2. La promotion est validée par l'utilisateur au jalon initial (D07).
# 3. Une promotion validée n'est PAS une preuve de vérité (C17:254).
# 4. Consulter n'est pas utiliser (phase_5:96) — voir `record_usage`.
from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.search import cursor as _cur

if TYPE_CHECKING:
    from backend.db.models import MemoryEntry

log = logging.getLogger(__name__)

# `k=5` est une TAILLE DE PAGE, jamais une limite de découverte (C17:262,
# backend_espaces.md:75). Une fiche rare mais pertinente reste trouvable.
DEFAULT_PAGE_SIZE = 5

_MEMORY_STATUSES = ("candidate", "accepted", "stale", "rejected")

# Vocabulaire de la fiche de mémoire (Phase 6, T6.2) — `docs/intentions/fonctionnalites/memoire.md` §2.
# Six catégories de faits que la mémoire peut retenir, et quatre statuts
# d'affirmation qui qualifient l'affirmation elle-même (décision, hypothèse,
# observation, information à vérifier) — distincts des statuts de cycle
# `_MEMORY_STATUSES` (`candidate` / `accepted` / `stale` / `rejected`).
_MEMORY_CATEGORIES: tuple[str, ...] = (
    "contraintes",
    "decisions",
    "contexte_stable",
    "preferences",
    "connaissances_de_travail",
    "reperes_documentaires",
)
_MEMORY_AFFIRMATION_STATUSES: tuple[str, ...] = (
    "decision",
    "hypothese",
    "observation",
    "information_a_verifier",
)

# Un refus est consultable et rejouable sans le modèle. Le motif est libre :
# `phase_5:80` — « Un refus, une correction et une suppression doivent être
# accessibles sans demander au modèle de rédiger ou d'exécuter une commande ».
_TOKEN_RE = re.compile(r"[\wÀ-ɏ]+", re.UNICODE)

# Bornes de prudence : une fiche est un fait court, pas un document. Le contenu
# ne voyage jamais par un chemin libre (backend_espaces.md:104) et le rendu est
# borné comme les autres résultats.
MAX_CONTENT_CHARS = 4000
MAX_ORIGIN_CONTEXT_CHARS = 2000
MAX_SOURCES = 32
MAX_EVIDENCE_IDS = 64

# Motifs STRUCTURÉS de `stale_reason`. Une seule valeur autorise la
# réhabilitation : `source_inaccessible`. Un fait périmé par une contradiction
# ou par la suppression de sa discussion reste périmé, même si une source
# redevient visible — ces deux causes ne se réparent pas par un partage.
STALE_REASON_SOURCE_INACCESSIBLE = "source_inaccessible"
STALE_REASON_DISCUSSION_SUPPRIMEE = "discussion supprimée"

# Observabilité de la synchronisation de provenance : un compteur monotone des
# échecs de portée, et une file de reprise bornée. Une panne de lecture de
# portée ne doit jamais être interprétée comme « toutes les sources sont
# accessibles » ni comme « aucune ne l'est ».
_PROVENANCE_SYNC_FAILED = 0
_PROVENANCE_SYNC_FAILURES: dict[str, int] = {}
_PROVENANCE_SYNC_MAX_TENTATIVES = 5
_PROVENANCE_LOCK = threading.Lock()

# Seuil d'inactivité déclenchant l'entretien courant de synthèse (§5.5 L.151).
# PROVISOIRE — décision produit (memoire.md §5.5 L.151), valeur numérique non
# fixée (LIM-013/015) ; à calibrer en production.
SEUIL_ENTRETIEN_S = 300.0


def _now() -> datetime:
    """Horodatage NAÏF UTC, comme `server_default=func.now()` — voir le même
    commentaire dans `services/espaces.py`. `last_used_at` sert de critère de
    départage au classement : il doit être comparable à `created_at`."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_entry_id() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _normaliser_contenu(texte: str) -> str:
    """Forme canonique d'un contenu : espaces réduits, casse repliée.

    Deux écritures du même fait (« SQLite » / «  sqlite ») doivent produire la
    même empreinte, sinon une exclusion ne tiendrait qu'à une majuscule près.
    """
    return " ".join(str(texte or "").split()).casefold()


def hash_contenu(texte: str) -> str:
    """Empreinte stable d'un contenu normalisé (sha256 hexadécimal)."""
    return hashlib.sha256(_normaliser_contenu(texte).encode("utf-8")).hexdigest()


def _jaccard_textes(a: str, b: str) -> float:
    """Similarité de Jaccard des tokens, dans ce module pour le garde-fou utilisateur.

    `memoire_auto` porte la même mesure : elle ne peut pas être importée ici
    (cycle), donc cette copie minuscule sert UNIQUEMENT à décider si une
    correction utilisateur est sémantiquement la même affirmation. La décision
    reste un garde-fou, jamais une vérité : une clé identique et une similarité
    rompue signifient « ne pas écraser ».
    """
    ta = set(_tokens(a))
    tb = set(_tokens(b))
    if not ta and not tb:
        return 1.0 if _normaliser_contenu(a) == _normaliser_contenu(b) else 0.0
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def _correction_utilisateur(entry: Any) -> bool:
    """La fiche courante est-elle une décision humaine à ne jamais écraser ?"""
    decision = getattr(entry, "decision", None) or {}
    if not isinstance(decision, dict):
        return False
    acteur = str(decision.get("actor") or "")
    action = str(decision.get("action") or "")
    return acteur == "user" or action in ("accept", "reject", "correct")


def _decouper_references(origin_context: str | None) -> list[str]:
    """Les références d'origine, séparées par `;` ou ` ; `, sans vides."""
    return [
        ref.strip() for ref in re.split(r"\s*;\s*", origin_context or "") if ref.strip()
    ]


def _borner_references(references: list[str], maximum: int) -> str:
    """Joint des références en gardant les plus récentes sous la borne.

    Une provenance qui grandit ne doit pas perdre la preuve qu'on vient
    d'ajouter : on retire donc les références les plus anciennes avant de
    tronquer, plutôt que de couper la nouvelle au milieu.
    """
    retenues: list[str] = []
    for reference in reversed(references):
        candidate = [reference, *retenues]
        if len(" ; ".join(candidate)) <= maximum:
            retenues = candidate
            continue
        break
    if not retenues:
        # Même la référence la plus récente dépasse la borne : on garde son
        # début, jamais celui de la plus ancienne.
        return (references[-1] if references else "")[:maximum]
    return " ; ".join(retenues)


def _dedup_sources(sources: list[str] | None) -> list[str]:
    """Sources dédupliquées, dans l'ordre d'arrivée, plafonnées à MAX_SOURCES."""
    out: list[str] = []
    for raw in sources or []:
        source = str(raw or "").strip()
        if not source or source in out:
            continue
        out.append(source)
        if len(out) >= MAX_SOURCES:
            break
    return out


def _fusionner_sources(existantes: list[str], nouvelles: list[str]) -> list[str]:
    """Fusionne deux provenances sans doublon, en gardant les connues d'abord."""
    return _dedup_sources([*(str(s) for s in existantes or []), *nouvelles])


def artefacts_accessibles(
    db: Any, workspace_id: str
) -> tuple[set[str] | None, str | None]:
    """Sources accessibles de l'espace, avec l'erreur de portée SÉPARÉE.

    Retourne `(ids, None)` en succès, `(None, message)` si la portée est
    illisible. Un appelant ne doit jamais confondre les deux : l'ancien
    `accessible = alive` transformait une panne de scope en accès universel,
    et l'inverse (`set()`) transformerait une panne en invalidation massive.
    """
    from backend.db.models import Artifact as _Art

    try:
        from backend.search.scope import resolve_space_information_scope

        # La session de l'appelant voit les liens non commités (removed_at posé
        # par une mise en corbeille en cours). `strict` empêche la panne de se
        # déguiser en ensemble vide.
        allowed = set(resolve_space_information_scope(workspace_id, db=db, strict=True))
    except Exception as exc:  # noqa: BLE001 - la panne est un état rendu
        return None, f"{type(exc).__name__}: {exc}"
    alive = {
        str(a.id)
        for a in db.query(_Art)
        .filter(_Art.id.in_(sorted(allowed)), _Art.deleted_at.is_(None))
        .all()
    }
    return allowed & alive, None


def _noter_echec_sync(workspace_id: str, erreur: str) -> None:
    """Compteur + file de reprise, bornée en tentatives."""
    global _PROVENANCE_SYNC_FAILED
    with _PROVENANCE_LOCK:
        _PROVENANCE_SYNC_FAILED += 1
        essais = _PROVENANCE_SYNC_FAILURES.get(workspace_id, 0) + 1
        _PROVENANCE_SYNC_FAILURES[workspace_id] = essais
        if essais >= _PROVENANCE_SYNC_MAX_TENTATIVES:
            _PROVENANCE_SYNC_FAILURES.pop(workspace_id, None)
    log.warning(
        "[memoire] synchronisation de provenance impossible pour %s "
        "(tentative %d/%d) : %s",
        workspace_id,
        essais,
        _PROVENANCE_SYNC_MAX_TENTATIVES,
        erreur,
    )


def lire_compteur_sync() -> int:
    """Nombre d'échecs de synchronisation de provenance depuis le démarrage."""
    with _PROVENANCE_LOCK:
        return int(_PROVENANCE_SYNC_FAILED)


def file_reprise_sync() -> list[str]:
    """Espaces en attente de reprise, du plus ancien échec au plus récent."""
    with _PROVENANCE_LOCK:
        return list(_PROVENANCE_SYNC_FAILURES)


def reinitialiser_compteur_sync() -> None:
    """Remise à zéro réservée aux tests et aux diagnostics."""
    global _PROVENANCE_SYNC_FAILED
    with _PROVENANCE_LOCK:
        _PROVENANCE_SYNC_FAILED = 0
        _PROVENANCE_SYNC_FAILURES.clear()


def retenter_synchronisations(db: Any) -> dict[str, int]:
    """Rejoue les espaces en échec, au tick du planificateur mémoire.

    Une tentative réussie sort de la file ; un échec incrémente son compteur et
    disparaît au plafond. On ne boucle donc jamais à l'infini sur une portée
    durablement illisible.
    """
    reussis = 0
    echoues = 0
    for workspace_id in file_reprise_sync():
        out = syncer_validite_sources(db, workspace_id)
        if out.get("skipped"):
            echoues += 1
            continue
        with _PROVENANCE_LOCK:
            _PROVENANCE_SYNC_FAILURES.pop(workspace_id, None)
        reussis += 1
    return {"reussis": reussis, "echoues": echoues}


def _cursor_encode(fingerprint: str, offset: int) -> str:
    """Curseur opaque — encodage partagé avec `fts.py` et la recherche d'espace."""
    return _cur.encode(fingerprint, offset)


def _cursor_decode(cursor: str) -> dict[str, Any] | None:
    return _cur.decode(cursor)


def _ranking_fingerprint(
    candidates: list[dict[str, Any]], query: str, workspace_id: str
) -> str:
    """Empreinte de l'ORDRE RESULTANT, pas de la requête.

    `fts._request_fingerprint` hache la requête ; ici il faut hache l'ordre
    obtenu, parce que deux requêtes identiques ne donnent pas le même classement
    quand un usage a bougé entre les deux pages. On hache donc la suite ordonnée
    des ids et leurs poids — c'est exactement ce dont le curseur dépend.
    """
    return _cur.fingerprint(
        workspace_id,
        query.strip().lower(),
        *[f"{c['entry_id']}:{c['relevance']}:{c['usage_count']}" for c in candidates],
    )


# ---------------------------------------------------------------------------
# Proposition — l'IA propose, elle n'écrit pas
# ---------------------------------------------------------------------------


def propose(
    db: Any,
    workspace_id: str,
    content: str,
    *,
    source_artifact_ids: list[str] | None = None,
    origin_context: str | None = None,
    origin_conversation_id: str | None = None,
    promotion_reasons: list[Any] | None = None,
    evidence_ids: list[str] | None = None,
    idempotency_key: str | None = None,
    entry_key: str | None = None,
    category: str | None = None,
    affirmation_status: str | None = None,
) -> dict[str, Any]:
    """Crée une fiche au statut `candidate`. Jamais `accepted` (C17:252, D07).

    Vérifie ce que `backend_espaces.md:98-106` demande de vérifier AVANT d'écrire :
    sources accessibles et localisables, périmètre cible, et clé d'idempotence de
    la proposition. Une source inaccessible fait échouer la proposition entière —
    on n'écrit pas une fiche dont la provenance ne tient pas.

    La clé d'idempotence rend le rejeu sans doublon (C09) : relancer la même
    proposition ne crée pas une deuxième ligne.

    `category` porte la nature du fait (six catégories du vocabulaire) et
    `affirmation_status` le statut d'affirmation (décision, hypothèse,
    observation, information à vérifier) — tous deux optionnels, validés
    contre `_MEMORY_CATEGORIES` / `_MEMORY_AFFIRMATION_STATUSES`.
    """
    from sqlalchemy.exc import IntegrityError

    from backend.db.models import MemoryEntry as _Mem, Workspace as _Ws

    text = (content or "").strip()
    if not text:
        raise ValueError(
            "[CONTENT_REQUIRED] Une fiche a besoin d'un contenu synthétique."
        )
    if len(text) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"[TOO_LONG] Le contenu d'une fiche est borné à {MAX_CONTENT_CHARS} caractères."
        )
    if category is not None and category not in _MEMORY_CATEGORIES:
        raise ValueError(f"[ENUM_INVALID] Catégorie de mémoire inconnue : {category}")
    if (
        affirmation_status is not None
        and affirmation_status not in _MEMORY_AFFIRMATION_STATUSES
    ):
        raise ValueError(
            f"[ENUM_INVALID] Statut d'affirmation inconnu : {affirmation_status}"
        )

    ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
    if ws is None or ws.state != "active":
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")

    # Clé d'idempotence, CLOISONNÉE PAR ESPACE. Deux espaces peuvent proposer
    # la même chose ; sans ce filtre, la clé de l'un renverrait l'id de l'autre
    # et une proposition deviendrait un canal inter-espaces.
    sources = _dedup_sources(source_artifact_ids)
    if idempotency_key:
        existing = (
            db.query(_Mem)
            .filter(
                _Mem.workspace_id == workspace_id,
                _Mem.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing is not None:
            return {
                "ok": True,
                "entry_id": existing.id,
                "entry_key": str(existing.entry_key),
                "status": str(existing.status),
                "version": int(existing.version),
                "created": False,
                "note": "proposition déjà enregistrée (idempotence)",
            }

    # Sources ACCESSIBLES, pas seulement existantes. C17 exige des « sources
    # accessibles », et le pseudo-code ajoute : « SI elles ne sont pas
    # accessibles depuis le périmètre cible / Demander un partage explicite des
    # dépendances OU REFUSER LA PROMOTION / Ne pas transférer ces sources
    # silencieusement ». Vérifier `deleted_at IS NULL` ne vérifie rien du droit :
    # un fichier privé d'un autre espace existe très bien.
    inacces = _sources_inaccessibles(db, workspace_id, sources)
    if inacces:
        return {
            "ok": False,
            "code": "SOURCE_NOT_ACCESSIBLE",
            "detail": f"{len(inacces)} source(s) hors du périmètre de l'espace",
            "note": "aucune fiche écrite : une provenance qui ne tient pas ne se propose pas",
            "inaccessible_count": len(inacces),
        }

    # Un `entry_key` stable signifie « la même fiche logique » : on crée alors
    # une VERSION, pas une fiche sœur. Insérer une seconde ligne `is_current=1`
    # sur la même clé violerait l'index partiel `uq_memory_current_entry` — et
    # lèverait une IntegrityError qui sortirait du contrat d'erreur typée.
    key = entry_key or f"memk_{uuid.uuid4().hex[:12]}"
    current = db.query(_Mem).filter(_Mem.entry_key == key, _Mem.is_current == 1).first()
    if current is not None:
        if current.workspace_id != workspace_id:
            return {
                "ok": False,
                "code": "ENTRY_KEY_TAKEN",
                "note": "cette clé de fiche existe déjà dans un autre espace",
            }
        if str(current.status) == "rejected":
            # Une fiche REFUSÉE ne revient pas par la porte de la proposition :
            # `accept` refuse un `rejected`, et il ne servirait à rien que la
            # proposition puisse le contourner.
            return {
                "ok": False,
                "code": "ALREADY_REJECTED",
                "entry_id": str(current.id),
                "note": "fiche refusée : elle ne se repropose pas par ce chemin",
            }
        return _new_version(
            db,
            current,
            status="candidate",
            content=text,
            source_artifact_ids=list(sources),
            promotion_reasons=promotion_reasons,
            origin_context=origin_context,
            evidence_ids=evidence_ids,
            idempotency_key=idempotency_key,
            category=category,
            affirmation_status=affirmation_status,
            decision={
                "actor": "orchestrator",
                "at": _now().isoformat(),
                "action": "repropose",
            },
        )

    entry = _Mem(
        id=_new_entry_id(),
        entry_key=key,
        workspace_id=workspace_id,
        version=1,
        is_current=1,
        content=text,
        status="candidate",
        category=category,
        affirmation_status=affirmation_status,
        source_artifact_ids=list(sources),
        origin_context=(origin_context or None)
        and str(origin_context)[:MAX_ORIGIN_CONTEXT_CHARS],
        origin_conversation_id=origin_conversation_id or None,
        promotion_reasons=list(promotion_reasons or []),
        evidence_ids=list(evidence_ids or []),
        idempotency_key=idempotency_key or None,
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # Deux rejeux entrelacés de la même proposition. La contrainte unique a
        # gagné : rien n'est écrit. On rend un refus typé, comme partout dans ce
        # module, au lieu de laisser remonter une 500.
        db.rollback()
        relu = (
            db.query(_Mem)
            .filter(
                _Mem.workspace_id == workspace_id,
                _Mem.idempotency_key == idempotency_key,
            )
            .first()
            if idempotency_key
            else None
        )
        if relu is not None:
            return {
                "ok": True,
                "entry_id": str(relu.id),
                "entry_key": str(relu.entry_key),
                "status": str(relu.status),
                "version": int(relu.version),
                "created": False,
                "note": "proposition déjà enregistrée (course)",
            }
        return {
            "ok": False,
            "code": "REVISION_CONFLICT",
            "note": "la fiche a changé entre-temps ; relire la version courante",
        }
    return {
        "ok": True,
        "entry_id": entry.id,
        "entry_key": str(entry.entry_key),
        "status": "candidate",
        "version": 1,
        "created": True,
        "note": "candidat : l'interface doit le montrer avant toute promotion",
    }


def proposer_automatique(
    db: Any,
    workspace_id: str,
    content: str,
    *,
    source_artifact_ids: list[str] | None = None,
    origin_context: str | None = None,
    origin_conversation_id: str | None = None,
    promotion_reasons: list[Any] | None = None,
    evidence_ids: list[str] | None = None,
    idempotency_key: str | None = None,
    entry_key: str | None = None,
    category: str | None = None,
    affirmation_status: str | None = None,
    actor: str = "system",
    source_observation_id: str | None = None,
    extractor_version: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Crée une fiche directement en `accepted`, sans validation humaine.

    La mémoire fonctionne automatiquement pour les données DÉTERMINISTES : ce
    chemin ne passe pas par `candidate` puis `accept`. Il conserve pourtant toute
    la provenance, la version et l'idempotence de `propose` : une proposition
    déjà connue par sa clé d'idempotence n'est pas dupliquée, et une source
    inaccessible fait échouer la création sans écrire de fiche partiellette.

    La décision porte l'acteur ayant déclenché la création automatique ; la
    promotion reste tracée (`decision.action = proposer_automatique`).
    """
    from sqlalchemy.exc import IntegrityError

    from backend.db.models import MemoryEntry as _Mem, Workspace as _Ws

    text = (content or "").strip()
    if not text:
        raise ValueError(
            "[CONTENT_REQUIRED] Une fiche a besoin d'un contenu synthétique."
        )
    if len(text) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"[TOO_LONG] Le contenu d'une fiche est borné à {MAX_CONTENT_CHARS} caractères."
        )
    if category is not None and category not in _MEMORY_CATEGORIES:
        raise ValueError(f"[ENUM_INVALID] Catégorie de mémoire inconnue : {category}")
    if (
        affirmation_status is not None
        and affirmation_status not in _MEMORY_AFFIRMATION_STATUSES
    ):
        raise ValueError(
            f"[ENUM_INVALID] Statut d'affirmation inconnu : {affirmation_status}"
        )

    ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
    if ws is None or ws.state != "active":
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")

    sources = _dedup_sources(source_artifact_ids)
    if idempotency_key:
        existing = (
            db.query(_Mem)
            .filter(
                _Mem.workspace_id == workspace_id,
                _Mem.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing is not None:
            return {
                "ok": True,
                "entry_id": existing.id,
                "entry_key": str(existing.entry_key),
                "status": str(existing.status),
                "version": int(existing.version),
                "created": False,
                "note": "proposition déjà enregistrée (idempotence)",
            }

    inacces = _sources_inaccessibles(db, workspace_id, sources)
    if inacces:
        return {
            "ok": False,
            "code": "SOURCE_NOT_ACCESSIBLE",
            "detail": f"{len(inacces)} source(s) hors du périmètre de l'espace",
            "note": "aucune fiche écrite : une provenance qui ne tient pas ne se propose pas",
            "inaccessible_count": len(inacces),
        }

    key = entry_key or f"memk_{uuid.uuid4().hex[:12]}"
    current = db.query(_Mem).filter(_Mem.entry_key == key, _Mem.is_current == 1).first()
    if current is not None:
        if current.workspace_id != workspace_id:
            return {
                "ok": False,
                "code": "ENTRY_KEY_TAKEN",
                "note": "cette clé de fiche existe déjà dans un autre espace",
            }
        if str(current.status) == "rejected":
            return {
                "ok": False,
                "code": "ALREADY_REJECTED",
                "entry_id": str(current.id),
                "note": "fiche refusée : elle ne se repropose pas par ce chemin",
            }
        # GARDE-FOU PHASE 8 : une fiche que l'utilisateur a acceptée, refusée ou
        # corrigée ne se réécrit JAMAIS par une extraction automatique. Le
        # contrôle est ici, au point d'écriture unique, et pas seulement dans le
        # remplacement documentaire : un autre chemin d'extraction ne doit pas
        # pouvoir contourner la décision humaine.
        if _correction_utilisateur(current):
            return {
                "ok": False,
                "code": "USER_CORRECTION_PROTECTED",
                "entry_id": str(current.id),
                "entry_key": str(current.entry_key),
                "status": str(current.status),
                "version": int(current.version),
                "note": "correction utilisateur conservée : le texte automatique "
                "ne remplace pas une décision humaine",
            }
        return _new_version(
            db,
            current,
            status="accepted",
            content=text,
            source_artifact_ids=list(sources),
            promotion_reasons=promotion_reasons,
            origin_context=origin_context,
            evidence_ids=evidence_ids,
            idempotency_key=idempotency_key,
            category=category,
            affirmation_status=affirmation_status,
            source_observation_id=source_observation_id,
            extractor_version=extractor_version,
            commit=commit,
            decision={
                "actor": actor,
                "at": _now().isoformat(),
                "action": "proposer_automatique",
            },
        )

    entry = _Mem(
        id=_new_entry_id(),
        entry_key=key,
        workspace_id=workspace_id,
        version=1,
        is_current=1,
        content=text,
        status="accepted",
        category=category,
        affirmation_status=affirmation_status,
        source_artifact_ids=list(sources),
        origin_context=(origin_context or None)
        and str(origin_context)[:MAX_ORIGIN_CONTEXT_CHARS],
        origin_conversation_id=origin_conversation_id or None,
        promotion_reasons=list(promotion_reasons or []),
        evidence_ids=list(evidence_ids or []),
        idempotency_key=idempotency_key or None,
        source_observation_id=source_observation_id,
        extractor_version=extractor_version,
        decision={
            "actor": actor,
            "at": _now().isoformat(),
            "action": "proposer_automatique",
        },
    )
    db.add(entry)
    if not commit:
        # Écriture dans la transaction de l'appelant (extraction documentaire) :
        # le ledger et les fiches doivent tomber ensemble si le bail est perdu.
        db.flush()
        return {
            "ok": True,
            "entry_id": entry.id,
            "entry_key": str(entry.entry_key),
            "status": "accepted",
            "version": 1,
            "created": True,
            "note": "entrée créée dans la transaction de l'appelant",
        }
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        relu = (
            db.query(_Mem)
            .filter(
                _Mem.workspace_id == workspace_id,
                _Mem.idempotency_key == idempotency_key,
            )
            .first()
            if idempotency_key
            else None
        )
        if relu is not None:
            return {
                "ok": True,
                "entry_id": str(relu.id),
                "entry_key": str(relu.entry_key),
                "status": str(relu.status),
                "version": int(relu.version),
                "created": False,
                "note": "proposition déjà enregistrée (course)",
            }
        return {
            "ok": False,
            "code": "REVISION_CONFLICT",
            "note": "la fiche a changé entre-temps ; relire la version courante",
        }
    return {
        "ok": True,
        "entry_id": entry.id,
        "entry_key": str(entry.entry_key),
        "status": "accepted",
        "version": 1,
        "created": True,
        "note": "entrée créée et servie automatiquement, sans validation humaine",
    }


def ajouter_preuve(
    db: Any,
    entry_id: str,
    *,
    origin_context: str | None,
    evidence_ids: list[str] | None = None,
    source_artifact_ids: list[str] | None = None,
    workspace_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Enrichit la provenance d'une fiche courante sans changer son contenu.

    Met à jour `origin_context` (références séparées par `;` ou ` ; `),
    `evidence_ids` et `source_artifact_ids`. Ne crée pas de version : ce n'est
    pas une correction sémantique. Deux discussions qui soutiennent le même fait
    doivent laisser leurs deux traces, même si la fiche n'a été écrite qu'une fois.

    Idempotent : une référence, une preuve ou une source déjà présente n'est pas
    répétée. Une source que l'espace ne peut plus lire est écartée au moment de
    l'écriture : on enrichit avec ce qui tient, on ne rejette pas le fait.
    """
    current = _current(db, entry_id)
    if current is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    if workspace_id is not None:
        _dans_workspace(current, workspace_id)

    references = _decouper_references(current.origin_context)
    # `origin_context` peut porter plusieurs références (« A ; B ») : on les
    # ajoute séparément, sinon la provenance combinée deviendrait une référence
    # unique qui dupliquerait les références déjà connues à chaque fusion.
    for reference in _decouper_references(origin_context):
        if reference not in references:
            references.append(reference)
    prochain_origin = _borner_references(references, MAX_ORIGIN_CONTEXT_CHARS)

    preuves = [str(e) for e in (current.evidence_ids or [])]
    for evidence_id in evidence_ids or []:
        evidence_id = str(evidence_id)
        if evidence_id and evidence_id not in preuves:
            preuves.append(evidence_id)
    # Au-delà de la borne, ce sont les plus récentes qui portent le fait
    # courant : on garde la queue de la liste, jamais sa tête.
    preuves = preuves[-MAX_EVIDENCE_IDS:]

    sources_existantes = [str(s) for s in (current.source_artifact_ids or [])]
    sources_nouvelles = _dedup_sources(source_artifact_ids)
    if sources_nouvelles and workspace_id is not None:
        perdues = set(_sources_inaccessibles(db, workspace_id, sources_nouvelles))
        sources_nouvelles = [s for s in sources_nouvelles if s not in perdues]
        if perdues:
            log.info(
                "[memoire] %d source(s) non accessible(s) écartée(s) de la fiche %s",
                len(perdues),
                entry_id,
            )
    prochaines_sources = _fusionner_sources(sources_existantes, sources_nouvelles)

    sources_changees = prochaines_sources != sources_existantes
    if (
        prochain_origin != str(current.origin_context or "")
        or preuves != [str(e) for e in (current.evidence_ids or [])]
        or sources_changees
    ):
        # Écriture EN PLACE assumée : la provenance n'est pas le contenu, et
        # `phase_5:66` versionne les révisions sémantiques, pas l'accumulation
        # de preuves qui soutiennent la même affirmation.
        current.origin_context = prochain_origin or None
        current.evidence_ids = preuves
        current.source_artifact_ids = prochaines_sources
        if commit:
            db.commit()
        else:
            db.flush()

    return {
        "ok": True,
        "entry_id": str(current.id),
        "origin_context": current.origin_context,
        "evidence_ids": list(current.evidence_ids or []),
        "source_artifact_ids": list(current.source_artifact_ids or []),
    }


def _sources_inaccessibles(db: Any, workspace_id: str, sources: list[str]) -> list[str]:
    """Sources hors du périmètre de l'espace, ou supprimées.

    Le périmètre d'un espace est exactement ce que
    `scope.resolve_space_information_scope()` autorise : les fichiers de tous ses
    chats, chats supprimés compris, plus sa zone utilisateur. Une source qui n'y
    est pas n'est pas une provenance legitimate — même si elle existe.

    FAIL-CLOSED : si la portée est illisible, aucune source n'est déclarée
    accessible. Écrire une provenance qu'on n'a pas pu vérifier serait pire que
    refuser la source ; l'appelant retire alors la source, pas le fait.
    """
    if not sources:
        return []
    accessibles, erreur = artefacts_accessibles(db, workspace_id)
    if erreur is not None:
        log.warning(
            "[memoire] portée illisible pour %s, sources refusées : %s",
            workspace_id,
            erreur,
        )
        return sorted({str(s) for s in sources})
    return sorted(str(s) for s in sources if str(s) not in (accessibles or set()))


# ---------------------------------------------------------------------------
# Décision humaine
# ---------------------------------------------------------------------------


def _resolve_entry(db: Any, ident: str) -> Any:
    """Fiche COURANTE par `entry_id` OU par `entry_key`. Sans quoi c'est None.

    Un seul point de résolution pour tout le module : `accept`, `list_versions` et
    `purge_memory` doivent accepter les deux formes, sinon l'appelant change de
    forme au milieu d'un parcours de décision et la seconde forme échoue en
    `MEMORY_UNKNOWN`.
    """
    from backend.db.models import MemoryEntry as _Mem

    row = db.query(_Mem).filter(_Mem.id == ident).first()
    if row is None:
        row = (
            db.query(_Mem).filter(_Mem.entry_key == ident, _Mem.is_current == 1).first()
        )
    if row is None:
        return None
    return (
        db.query(_Mem)
        .filter(_Mem.entry_key == row.entry_key, _Mem.is_current == 1)
        .first()
    )


def _dans_workspace(entry: Any, workspace_id: str | None) -> None:
    """Garde de périmètre pour les opérations LECTURE et SUPPRESSION d'une fiche.

    Le service mémoire ne reçoit pas d'identité d'utilisateur, mais il reçoit un
    ESPACE. Sans cette garde, `purge_memory` d'une fiche d'un autre espace
    s'exécuterait : la même faille que pour la fusion, mais destructive. On
    refuse avec un code neutre, qui ne confirme rien sur l'existence ailleurs.
    """
    if workspace_id is None:
        raise ValueError(
            "[WORKSPACE_REQUIRED] Cette opération exige l'espace : une fiche de "
            "mémoire n'est jamais manipulable hors de son périmètre."
        )
    if str(entry.workspace_id) != str(workspace_id):
        raise LookupError("[MEMORY_NOT_IN_WORKSPACE] Fiche introuvable.")


def _current(db: Any, entry_id: str) -> Any:
    """Version courante d'une fiche, par `entry_id` de n'importe quelle version."""
    from backend.db.models import MemoryEntry as _Mem

    row = db.query(_Mem).filter(_Mem.id == entry_id).first()
    if row is None:
        return None
    return (
        db.query(_Mem)
        .filter(_Mem.entry_key == row.entry_key, _Mem.is_current == 1)
        .first()
    )


# ---------------------------------------------------------------------------
# Exclusions durables — un contenu refusé ne revient pas par la purge
# ---------------------------------------------------------------------------


def _exclusion_payload(row: Any, *, created: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "exclusion_id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "content_hash": str(row.content_hash),
        "entry_key": row.entry_key,
        "reason": row.reason,
        "created": created,
    }


def creer_exclusion(
    db: Any,
    workspace_id: str,
    content: str,
    *,
    entry_key: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Exclut durablement un contenu de la mémoire d'un espace.

    Vérifie l'espace actif comme toute écriture de mémoire, puis déduplique par
    empreinte : un même contenu exclu deux fois retourne l'exclusion existante
    au lieu d'en créer une seconde. Une course entre deux écritures est absorbée
    par la contrainte unique, pas par une exception qui remonterait à l'UI.
    """
    from sqlalchemy.exc import IntegrityError

    from backend.db.models import MemoryExclusion as _Exc, Workspace as _Ws

    ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
    if ws is None or ws.state != "active":
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")

    empreinte = hash_contenu(content)
    existant = (
        db.query(_Exc)
        .filter(_Exc.workspace_id == workspace_id, _Exc.content_hash == empreinte)
        .first()
    )
    if existant is not None:
        return _exclusion_payload(existant, created=False)

    exclusion = _Exc(
        id=f"mex_{uuid.uuid4().hex[:12]}",
        workspace_id=workspace_id,
        content_hash=empreinte,
        entry_key=entry_key,
        reason=reason,
    )
    db.add(exclusion)
    try:
        db.commit()
    except IntegrityError:
        # Deux exclusions concurrentes du même contenu : la contrainte unique a
        # gagné, donc la ligne existe déjà et c'est elle qui fait foi.
        db.rollback()
        existant = (
            db.query(_Exc)
            .filter(_Exc.workspace_id == workspace_id, _Exc.content_hash == empreinte)
            .first()
        )
        if existant is None:
            raise
        return _exclusion_payload(existant, created=False)
    return _exclusion_payload(exclusion, created=True)


def est_exclu(db: Any, workspace_id: str, content: str) -> bool:
    """Ce contenu est-il exclu de la mémoire de l'espace ?"""
    from backend.db.models import MemoryExclusion as _Exc

    empreinte = hash_contenu(content)
    return (
        db.query(_Exc)
        .filter(_Exc.workspace_id == workspace_id, _Exc.content_hash == empreinte)
        .first()
        is not None
    )


def lister_exclusions(db: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Exclusions d'un espace, de la plus récente à la plus ancienne."""
    from backend.db.models import MemoryExclusion as _Exc

    rows = (
        db.query(_Exc)
        .filter(_Exc.workspace_id == workspace_id)
        .order_by(_Exc.created_at.desc(), _Exc.id)
        .all()
    )
    return [
        {
            "exclusion_id": str(row.id),
            "workspace_id": str(row.workspace_id),
            "content_hash": str(row.content_hash),
            "entry_key": row.entry_key,
            "reason": row.reason,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def accept(
    db: Any, entry_id: str, *, actor: str = "user", expected_version: int | None = None
) -> dict[str, Any]:
    """Accepte une fiche. L'acceptation est une DÉCISION, pas une certification.

    « L'acceptation crée une version `accepted` » (`backend_espaces.md:114`).
    On crée donc N+1 plutôt que d'écraser : la trace de la décision reste lisible.

    Revalide droits, révision attendue et sources AU MOMENT de la confirmation
    (`:109`) : la fiche a pu être proposée avant qu'une source disparaisse.
    Une révision concurrente refuse le conflit au lieu d'écraser (`:122-123`).
    """
    from backend.db.models import MemoryEntry as _Mem

    current = _current(db, entry_id)
    if current is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    if expected_version is not None and int(current.version) != int(expected_version):
        return {
            "ok": False,
            "code": "REVISION_CONFLICT",
            "current_version": int(current.version),
            "note": "relire la version courante avant une autre confirmation",
        }
    if str(current.status) == "rejected":
        return {
            "ok": False,
            "code": "ALREADY_REJECTED",
            "note": "une fiche refusée ne se promeut pas par le même chemin",
        }

    # Champs que C17 énonce comme NÉCESSAIRES à une promotion : sources
    # accessibles, contexte d'origine, statut, et raisons structurées. Le
    # statut est posé par cet appel ; les trois autres doivent être là. Rendre
    # ces champs facultatifs revenait à.ethagger la promotion d'un fait sans
    # dire d'où il vient ni pourquoi on le garde.
    manquants: list[str] = []
    if not list(current.source_artifact_ids or []):
        manquants.append("source")
    if not str(current.origin_context or "").strip():
        manquants.append("origin_context")
    if not list(current.promotion_reasons or []):
        manquants.append("promotion_reasons")
    if manquants:
        return {
            "ok": False,
            "code": "INCOMPLETE_FOR_PROMOTION",
            "missing": manquants,
            "note": "compléter la fiche avant de la promouvoir : une promotion "
            "sans provenance n'est pas une promotion",
        }

    # Revalidation AU MOMENT de la confirmation (`backend_espaces.md:109`) :
    # droits ET sources accessibles, pas seulement « le fichier existe ».
    sources = [str(s) for s in (current.source_artifact_ids or [])]
    lost = _sources_inaccessibles(db, str(current.workspace_id), sources)
    if lost:
        # Une source perdue ne se remplace pas par une invention : la fiche
        # devient révisable par une VERSION tracée — même transition que
        # `mark_stale`, donc même mécanisme, et non une écriture en place qui
        # laisserait la décision sans auteur ni date.
        out = _new_version(
            db,
            current,
            status="stale",
            stale_reason=STALE_REASON_SOURCE_INACCESSIBLE,
            decision={
                "actor": "system",
                "at": _now().isoformat(),
                "action": "source_lost_on_accept",
                "reason": "source inaccessible à l'acceptation",
                "lost_count": len(lost),
            },
        )
        return {
            "ok": False,
            "code": "SOURCE_LOST",
            "status": "stale",
            "lost_count": len(lost),
            "entry_id": out["entry_id"],
            "version": out["version"],
            "note": "une source n'est plus accessible : la fiche est devenue révisable",
        }

    return _new_version(
        db,
        current,
        status="accepted",
        decision={"actor": actor, "at": _now().isoformat(), "action": "accept"},
    )


def reject(
    db: Any,
    entry_id: str,
    *,
    actor: str = "user",
    motif: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Refuse une fiche. Motif facultatif, aucune publication partagée.

    « Si l'utilisateur refuse : passer à rejected avec motif facultatif et sans
    publication partagée » (`backend_espaces.md:118-119`). `workspace_id`, quand
    il est fourni, interdit d'agir sur une fiche d'un autre espace.
    """
    current = _current(db, entry_id)
    if current is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    if workspace_id is not None:
        _dans_workspace(current, workspace_id)
    return _new_version(
        db,
        current,
        status="rejected",
        decision={
            "actor": actor,
            "at": _now().isoformat(),
            "action": "reject",
            "motif": motif or None,
        },
    )


def correct(
    db: Any,
    entry_id: str,
    content: str,
    *,
    actor: str = "user",
    expected_version: int | None = None,
    source_artifact_ids: list[str] | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Corrige une fiche : nouvelle version, provenance conservée.

    « Une correction ultérieure crée une version traçable » (phase_5:76). La
    version précédente n'est pas réécrite, elle est marquée `superseded`.

    Le contenu est validé ici comme à la proposition : vide ou trop long est un
    refus immédiat, pas une version abîmée. `workspace_id`, quand il est fourni,
    interdit de corriger la fiche d'un autre espace.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError(
            "[CONTENT_REQUIRED] Une fiche a besoin d'un contenu synthétique."
        )
    if len(text) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"[TOO_LONG] Le contenu d'une fiche est borné à {MAX_CONTENT_CHARS} caractères."
        )
    current = _current(db, entry_id)
    if current is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    if workspace_id is not None:
        _dans_workspace(current, workspace_id)
    if expected_version is not None and int(current.version) != int(expected_version):
        return {
            "ok": False,
            "code": "REVISION_CONFLICT",
            "current_version": int(current.version),
            "note": "relire la version courante avant de corriger",
        }
    # Les nouvelles sources sont passées À LA VERSION NOUVELLE, pas écrites sur
    # la ligne courante : celle-ci est sur le point de devenir `superseded`, et
    # y écrire réécrirait un snapshot.
    return _new_version(
        db,
        current,
        status=str(current.status),
        content=text,
        source_artifact_ids=source_artifact_ids,
        decision={"actor": actor, "at": _now().isoformat(), "action": "correct"},
    )


def mark_stale(
    db: Any,
    entry_id: str,
    *,
    reason: str = "source_change",
    stale_reason: str | None = None,
    workspace_id: str | None = None,
    evidence_ids: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Pousse une fiche vers `stale`. `stale` est une transition SUBIE, pas une création.

    « Une source supprimée, une portée révoquée ou une contradiction nouvelle doit
    rendre la fiche révisable et empêcher une réutilisation présentée comme
    certaine » (C17:256).

    Une fiche déjà `stale` retourne un résultat idempotent sans créer de version :
    `stale` est un état, pas un événement qui se rejoue. C'est aussi pourquoi
    `previous_status` n'est écrit qu'ici, à la transition : une seconde
    invalidation ne doit pas écraser le statut d'avant la première.

    `evidence_ids`, quand il est fourni, remplace la liste recopiée dans la
    version N+1. L'ingestion documentaire s'en sert pour retirer les `chk_`
    supprimés : une fiche périmée ne doit pas conserver une preuve morte comme
    si elle était encore citable.
    """
    current = _current(db, entry_id)
    if current is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    if workspace_id is not None:
        _dans_workspace(current, workspace_id)
    if str(current.status) == "rejected":
        return {"ok": True, "status": "rejected", "changed": False}
    if str(current.status) == "stale":
        return {
            "ok": True,
            "entry_id": str(current.id),
            "entry_key": str(current.entry_key),
            "status": "stale",
            "version": int(current.version),
            "changed": False,
        }
    sortie = _new_version(
        db,
        current,
        status="stale",
        stale_reason=stale_reason or reason,
        evidence_ids=evidence_ids,
        commit=commit,
        decision={
            "actor": "system",
            "at": _now().isoformat(),
            "action": "mark_stale",
            "reason": reason,
        },
    )
    return {**sortie, "changed": bool(sortie.get("ok"))}


def marquer_stale_suppression(
    db: Any, entry_id: str, *, workspace_id: str | None = None
) -> dict[str, Any]:
    """Invalide une fiche parce que sa discussion d'origine a été supprimée.

    Cette invalidation n'est pas une contradiction : elle doit rester
    distinguable d'un fait remplacé par une décision plus récente. Une fiche
    déjà `stale` voit donc son motif réécrit EN PLACE, sans nouvelle version ;
    une fiche `rejected` reste refusée. Quand la fiche était active, `mark_stale`
    crée la version tracée habituelle avec le motif de suppression.
    """
    current = _current(db, entry_id)
    if current is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    if workspace_id is not None:
        _dans_workspace(current, workspace_id)
    if str(current.status) == "rejected":
        return {"ok": True, "status": "rejected", "changed": False}
    if str(current.status) == "stale":
        current.decision = {
            "reason": STALE_REASON_DISCUSSION_SUPPRIMEE,
            "action": "mark_stale",
            "at": _now().isoformat(),
        }
        # La transition déjà faite garde son `previous_status` : réécrire la
        # raison ne doit pas inventer un statut d'avant différent.
        current.stale_reason = STALE_REASON_DISCUSSION_SUPPRIMEE
        db.commit()
        return {
            "ok": True,
            "entry_id": str(current.id),
            "entry_key": str(current.entry_key),
            "status": "stale",
            "version": int(current.version),
            "changed": False,
        }
    return mark_stale(
        db,
        str(current.id),
        reason=STALE_REASON_DISCUSSION_SUPPRIMEE,
        stale_reason=STALE_REASON_DISCUSSION_SUPPRIMEE,
        workspace_id=workspace_id,
    )


def syncer_validite_sources(
    db: Any,
    workspace_id: str,
    accessible_artifact_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Réconcilie la mémoire d'un espace avec l'accessibilité de ses sources.

    Une fiche dont au moins une source est devenue inaccessible devient
    `stale` (nouvelle version, non destructive) au lieu de rester servie
    comme acquise. Les versions précédentes restent immuables. L'opération est
    idempotente : une fiche déjà `stale` pour cette raison ne se duplique pas,
    et une fiche `rejected` ne se relève jamais.

    `accessible_artifact_ids`, quand il est fourni, est la liste close des
    sources actuellement accessibles ; à défaut, la portée de l'espace et
    l'état de ses artefacts font foi (sources actives, non supprimées).

    DEUX NIVEAUX : une fiche ne devient `stale` que si TOUTES ses sources sont
    perdues. Une source retirée d'un espace mais encore vivante ailleurs, ou une
    fiche à deux sources dont une seule tombe, garde son statut et sa provenance
    partielle. Une `candidate` n'est jamais promue par une perte partielle.
    """
    from backend.db.models import MemoryEntry as _Mem

    if accessible_artifact_ids is None:
        accessible, erreur = artefacts_accessibles(db, workspace_id)
        if erreur is not None:
            # NO-OP, jamais `accessible = alive` : une portée illisible ne
            # démontre aucune accessibilité. On note l'échec, on met l'espace en
            # file de reprise, et on ne touche à aucune fiche.
            _noter_echec_sync(workspace_id, erreur)
            return {
                "ok": True,
                "skipped": True,
                "reason": "scope_inaccessible",
                "workspace_id": workspace_id,
                "count": 0,
                "invalidated": 0,
                "rehabilitated": 0,
                "invalidated_entry_ids": [],
                "rehabilitated_entry_ids": [],
            }
        accessible = accessible or set()
    else:
        accessible = {str(a) for a in (accessible_artifact_ids or [])}

    invalidated: list[str] = []
    rehabbed: list[str] = []
    reels: list[str] = []
    for row in (
        db.query(_Mem)
        .filter(_Mem.workspace_id == workspace_id, _Mem.is_current == 1)
        .all()
    ):
        sources = [str(s) for s in (row.source_artifact_ids or [])]
        if not sources:
            continue
        accessibles = [s for s in sources if s in accessible]
        statut = str(row.status or "")

        if statut == "stale":
            # Une fiche périmée pour une autre cause (contradiction, discussion
            # supprimée) ne se réactive JAMAIS par un partage : sa provenance
            # n'est pas la raison de son état.
            if str(row.stale_reason or "") != STALE_REASON_SOURCE_INACCESSIBLE:
                continue
            if not accessibles:
                continue
            cible = _statut_rehabilitation(row)
            try:
                out = _new_version(
                    db,
                    row,
                    status=cible,
                    decision={
                        "actor": "system",
                        "at": _now().isoformat(),
                        "action": "rehabilitate_sources",
                        "reason": "au moins une source est redevenue accessible",
                    },
                )
            except LookupError:
                continue
            if out.get("ok"):
                rehabbed.append(str(out.get("entry_id") or row.id))
                reels.append(str(row.id))
            continue

        if statut == "rejected":
            continue
        if accessibles:
            # Provenance partielle : le statut courant reste (accepted ou
            # candidate), et la liste des sources n'est pas nettoyée — la
            # provenance se recalcule à chaque synchronisation, elle ne se
            # réécrit pas sur une panne temporaire.
            continue
        try:
            out = mark_stale(
                db,
                str(row.id),
                reason="source devenue inaccessible : réconciliation de validité",
                stale_reason=STALE_REASON_SOURCE_INACCESSIBLE,
                workspace_id=workspace_id,
            )
        except LookupError:
            continue
        if out.get("ok") and out.get("changed", True):
            invalidated.append(str(out.get("entry_id") or row.id))
            reels.append(str(row.id))

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "count": len(invalidated),
        "invalidated": len(invalidated),
        "marked": len(invalidated),
        "marked_stale": len(invalidated),
        "rehabilitated": len(rehabbed),
        "invalidated_entry_ids": list(invalidated),
        "stale_entry_ids": list(invalidated),
        "entry_ids": list(invalidated),
        "rehabilitated_entry_ids": list(rehabbed),
        "from_entries": list(reels),
    }


def _statut_rehabilitation(row: Any) -> str:
    """Statut à restaurer, sans jamais promouvoir une `candidate`.

    `previous_status` est la valeur écrite à la transition. NULL legacy n'a
    jamais connu la transition : on restaure `accepted` au plus — jamais mieux
    que ce que la fiche était. Une valeur inattendue (`stale`, `rejected`) est
    ramenée à `accepted`, l'état actif le plus prudent.
    """
    precedent = str(row.previous_status or "").strip()
    if precedent == "candidate":
        return "candidate"
    if precedent == "accepted":
        return "accepted"
    return "accepted"


# Alias tolérant aux variantes d'orthographe et de vocabulaire.
synchroniser_validite_sources = syncer_validite_sources
synchroniser_validite = syncer_validite_sources


def _workspaces_citant(db: Any, artifact_ids: set[str]) -> list[str]:
    """Espaces dont une fiche courante cite au moins un de ces artefacts.

    Lecture Python plutôt qu'un `LIKE` sur du JSON : un identifiant d'artefact
    peut contenir des caractères que le motif interpréterait, et un faux positif
    ici invaliderait une fiche d'un autre espace.
    """
    from backend.db.models import MemoryEntry as _Mem

    rows = (
        db.query(_Mem.workspace_id, _Mem.source_artifact_ids)
        .filter(_Mem.is_current == 1)
        .all()
    )
    return sorted(
        {
            str(workspace_id)
            for workspace_id, sources in rows
            if {str(s) for s in (sources or [])} & artifact_ids
        }
    )


def invalider_artefacts_supprimes(
    db: Any, artifact_ids: list[str] | set[str]
) -> dict[str, Any]:
    """Invalide GLOBALEMENT les fiches qui citent des artefacts disparus.

    Utilisé par `image_delete` (artefact supprimé logiquement) et par le GC de
    la corbeille (artefact physiquement supprimé). On ne marque pas une fiche
    par artefact : on synchronise CHAQUE espace concerné, pour que la règle
    « stale seulement si toutes les sources sont perdues » s'applique, et pour
    qu'un artefact partagé ne périme que les fiches qui le citent réellement.

    Un échec de portée sur un espace est un no-op pour cet espace (compteur +
    file de reprise), jamais une invalidation silencieuse.
    """
    ids = {str(a) for a in artifact_ids if str(a or "").strip()}
    if not ids:
        return {"ok": True, "workspaces": [], "count": 0, "rehabilitated": 0}
    workspaces = _workspaces_citant(db, ids)
    total_invalides = 0
    total_rehab = 0
    details: list[dict[str, Any]] = []
    for workspace_id in workspaces:
        out = syncer_validite_sources(db, workspace_id)
        total_invalides += int(out.get("invalidated", 0) or 0)
        total_rehab += int(out.get("rehabilitated", 0) or 0)
        details.append(out)
    return {
        "ok": True,
        "artifacts": sorted(ids),
        "workspaces": workspaces,
        "count": total_invalides,
        "invalidated": total_invalides,
        "rehabilitated": total_rehab,
        "details": details,
    }


def invalider_artefact_global(db: Any, artifact_id: str) -> dict[str, Any]:
    """Alias d'un seul artefact : `image_delete` retire globalement la source."""
    return invalider_artefacts_supprimes(db, [artifact_id])


def _new_version(
    db: Any,
    current: Any,
    *,
    status: str,
    decision: dict[str, Any],
    content: str | None = None,
    source_artifact_ids: list[str] | None = None,
    promotion_reasons: list[Any] | None = None,
    origin_context: str | None = None,
    evidence_ids: list[str] | None = None,
    idempotency_key: str | None = None,
    category: str | None = None,
    affirmation_status: str | None = None,
    stale_reason: str | None = None,
    previous_status: str | None = None,
    source_observation_id: str | None = None,
    extractor_version: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Crée la version N+1 et bascule `is_current`. Jamais d'écrasement.

    Tout ce qui change passe PAR ICI, en paramètre. Modifier `current` avant
    l'appel réécrirait une version déjà marquée `superseded` : c'était un défaut
    réel, et il est invisible tant qu'on ne regarde que `content` — c'est
    `sources` qui_CHANGEAIT sans trace. Un snapshot est une archive, pas un champ
    modifiable (`01_classe_memoire.md`, « jamais réécrite en place »).

    L'index partiel `uq_memory_current_entry` garantit une seule version courante
    par fiche ; on désactive donc l'ancienne AVANT d'insérer la nouvelle, sinon
    la contrainte urine et non l'inverse.
    """
    from sqlalchemy.exc import IntegrityError

    from backend.db.models import MemoryEntry as _Mem

    if status not in _MEMORY_STATUSES:
        raise ValueError(f"[ENUM_INVALID] Statut de mémoire inconnu : {status}")
    # `previous_status` n'est écrit QU'À la transition vers `stale` : une fiche
    # qui reste `stale` conserve celui de sa première transition, et une
    # réhabilitation l'efface en revenant à un statut actif. Une fiche legacy
    # n'a pas de valeur — l'absence sera lue comme `accepted` au plus.
    if status == "stale" and previous_status is None:
        previous_status = (
            str(current.status)
            if str(current.status) != "stale"
            else current.previous_status
        )
    if status != "stale":
        previous_status = None
    if status == "stale" and stale_reason is None:
        stale_reason = current.stale_reason or (decision or {}).get("reason")
    if status != "stale":
        stale_reason = None
    current.is_current = 0
    current.superseded_at = _now()
    db.flush()
    nxt = _Mem(
        id=_new_entry_id(),
        entry_key=current.entry_key,
        workspace_id=current.workspace_id,
        version=int(current.version) + 1,
        is_current=1,
        content=content or str(current.content),
        status=status,
        # Le vocabulaire est porté par la VERSION, jamais par l'écriture en
        # place : une révision hérite de la catégorie/statut courant quand le
        # paramètre est absent, comme les listes et le contexte d'origine.
        category=category if category is not None else current.category,
        affirmation_status=(
            affirmation_status
            if affirmation_status is not None
            else current.affirmation_status
        ),
        # Listes NEUVES : les colonnes JSON ne sont pas suivies en place. Quand
        # le paramètre est absent, on RECOPIE la version courante — on n'écrit
        # jamais dans la ligne qu'on vient de périmer.
        source_artifact_ids=_dedup_sources(
            source_artifact_ids
            if source_artifact_ids is not None
            else list(current.source_artifact_ids or [])
        ),
        origin_context=(
            origin_context if origin_context is not None else current.origin_context
        ),
        origin_conversation_id=current.origin_conversation_id,
        promotion_reasons=list(
            promotion_reasons
            if promotion_reasons is not None
            else (current.promotion_reasons or [])
        ),
        decision=dict(decision),
        evidence_ids=list(
            (
                evidence_ids
                if evidence_ids is not None
                else (current.evidence_ids or [])
            )[-MAX_EVIDENCE_IDS:]
        ),
        stale_reason=stale_reason,
        previous_status=previous_status,
        # La provenance documentaire se RECOPIE comme les listes : une version
        # qui ne la porte pas est une fiche dont on ne sait plus de quelle
        # observation elle vient.
        source_observation_id=(
            source_observation_id
            if source_observation_id is not None
            else current.source_observation_id
        ),
        extractor_version=(
            extractor_version
            if extractor_version is not None
            else current.extractor_version
        ),
        superseded_from=current.id,
        usage_count=int(current.usage_count or 0),
        last_used_at=current.last_used_at,
        # La clé d'idempotence est RECOMPOSÉE, pas perdue : sans elle, un rejeu
        # de la même proposition créerait une version à chaque fois, et
        # `uq_memory_idem` ne surveillerait qu'un `NULL`.
        idempotency_key=idempotency_key,
    )
    db.add(nxt)
    if not commit:
        # Même transaction que le ledger documentaire : un `flush` rend la
        # nouvelle version visible au reste de la transaction, sans jamais
        # publier une fiche que le bail perdu devrait annuler.
        db.flush()
        return {
            "ok": True,
            "entry_id": nxt.id,
            "entry_key": str(nxt.entry_key),
            "status": status,
            "version": int(nxt.version),
            "superseded_from": str(current.id),
        }
    try:
        db.commit()
    except IntegrityError:
        # Course avec une autre écriture de la même fiche. La contrainte a gagné,
        # donc rien n'est écrit : on rend un refus typé plutôt qu'une 500, et on
        # relit la version courante comme le contrat l'exige (`:122-123`).
        db.rollback()
        relu = (
            db.query(_Mem)
            .filter(_Mem.entry_key == current.entry_key, _Mem.is_current == 1)
            .first()
        )
        return {
            "ok": False,
            "code": "REVISION_CONFLICT",
            "current_version": int(relu.version) if relu is not None else None,
            "note": "la fiche a changé entre-temps ; relire la version courante",
        }
    return {
        "ok": True,
        "entry_id": nxt.id,
        "entry_key": str(nxt.entry_key),
        "status": status,
        "version": int(nxt.version),
        "superseded_from": str(current.id),
    }


def merge(
    db: Any, entry_ids: list[str], content: str, *, actor: str = "user"
) -> dict[str, Any]:
    """Fusionne des fiches. La plus populaire ne gagne PAS silencieusement.

    « Une fusion conserve les références des fiches d'origine et les désaccords
    utiles » (`backend_espaces.md:148`). On garde donc les sources et les preuves
    de toutes les fiches, et la fiche résultante porte la provenance de chacune.
    """
    from backend.db.models import MemoryEntry as _Mem

    # Résolution ET déduplication. `merge([a, a])` ou `merge([v1, v2])` d'une même
    # fiche mettrait deux fois la MÊME ligne courante dans la liste : la seconde
    # insertion réutiliserait sa `entry_key` et violerait `uq_memory_current_entry`.
    vus: list[str] = []
    currents: list[Any] = []
    inconnus: list[str] = []
    for ident in entry_ids:
        c = _resolve_entry(db, ident)
        if c is None:
            inconnus.append(ident)
            continue
        key = str(c.entry_key)
        if key in vus:  # même fiche, deuxième version ou doublon d'identifiant
            continue
        vus.append(key)
        currents.append(c)
    if inconnus:
        # Ne pas ignorer silencieusement : une fusion qui n'a pas prélevé
        # autant de fiches que demandé a l'air d'avoir réussi.
        raise ValueError(
            f"[MERGE_UNKNOWN_ENTRY] Fiche(s) introuvable(s) : {len(inconnus)}"
        )
    if len(currents) < 2:
        raise ValueError(
            "[MERGE_NEEDS_TWO] Une fusion exige au moins deux fiches distinctes."
        )

    # Cloisonnement AVANT toute écriture. Sans ce contrôle, une fusion entre
    # espaces déposait dans l'espace du premier les `artifact_id` de l'autre, et
    # poussait la fiche d'origine `stale` sans que son espace l'ait demandé :
    # « Aucun titre, aperçu ou compteur d'un autre espace » (phase_5:90) serait
    # violé par le service de mémoire lui-même.
    espaces = {str(c.workspace_id) for c in currents}
    if len(espaces) > 1:
        raise ValueError(
            "[MERGE_CROSSES_WORKSPACES] Une fusion ne peut pas traverser des espaces : "
            f"{len(espaces)} périmètres distincts. Proposez plutôt une fiche par espace."
        )

    merged_sources: list[str] = []
    merged_evidence: list[str] = []
    for c in currents:
        for s in c.source_artifact_ids or []:
            if str(s) not in merged_sources:
                merged_sources.append(str(s))
        for e in c.evidence_ids or []:
            if str(e) not in merged_evidence:
                merged_evidence.append(str(e))

    # Une fusion EST une promotion : elle passe par la même porte que `accept`.
    # Sinon M8 ne tient que sur un des trois chemins qui atteignent `accepted`,
    # et deux fiches sans provenance deviendraient une mémoire acceptée.
    manquants: list[str] = []
    if not merged_sources:
        manquants.append("source")
    if not any(str(c.origin_context or "").strip() for c in currents):
        manquants.append("origin_context")
    if not any(list(c.promotion_reasons or []) for c in currents):
        manquants.append("promotion_reasons")
    if manquants:
        return {
            "ok": False,
            "code": "INCOMPLETE_FOR_PROMOTION",
            "missing": manquants,
            "note": "compléter les fiches avant de les fusionner",
        }

    # Les sources réunies sont passées à la VERSION NOUVELLE. Les affecter sur `first`
    # réécrirait sa provenance alors qu'elle est déjà marquée `superseded`.
    first = currents[0]
    out = _new_version(
        db,
        first,
        status="accepted",
        content=content,
        source_artifact_ids=merged_sources[:MAX_SOURCES],
        evidence_ids=merged_evidence,
        decision={
            "actor": actor,
            "at": _now().isoformat(),
            "action": "merge",
            "merged_from": [str(c.id) for c in currents],
        },
    )
    if not out.get("ok"):
        return out  # conflit de révision : rien n'est écrit

    # Les fiches absorbées deviennent `stale` plutôt que d'être effacées : le
    # désaccord qu'elles portaient reste traçable (« Propager stale aux
    # dépendances affectées plutôt que réécrire le passé », :149).
    for c in currents[1:]:
        _new_version(
            db,
            c,
            status="stale",
            decision={
                "actor": actor,
                "at": _now().isoformat(),
                "action": "merged_away",
                "merged_into": str(first.entry_key),
            },
        )
    return {
        **out,
        "merged_count": len(currents),
        "sources_preserved": len(merged_sources),
    }


def list_versions(
    db: Any, entry_id_or_key: str, workspace_id: str | None = None
) -> list[dict[str, Any]]:
    """Toutes les versions d'une fiche, de la plus récente à la plus ancienne.

    Accepte un `entry_id` comme un `entry_key` : toutes les autres fonctions
    publiques de ce module prennent un `entry_id`, et demander une clé ici
    seulement obligerait l'appelant à revenir chercher un identifiant différent
    au milieu d'un parcours de décision.

    `workspace_id` est OPTIONNEL ici — lire l'historique d'une fiche pour
    l'afficher n'est pas une opération destructive — mais s'il est fourni, il
    filtre : afficher la provenance d'un autre espace serait déjà une fuite.

    L'interface doit pouvoir montrer contenu, sources, versions et
    contradictions avant de corriger (`:145`). « Versionné » ne signifie pas
    rétention indéfinie contre la volonté de l'utilisateur (phase_5:68) : c'est
    `purge_memory` qui décide, pas l'affichage.
    """
    from backend.db.models import MemoryEntry as _Mem

    current = _resolve_entry(db, entry_id_or_key)
    if current is None:
        return []
    if workspace_id is not None:
        _dans_workspace(current, workspace_id)
    key = str(current.entry_key)

    rows = (
        db.query(_Mem).filter(_Mem.entry_key == key).order_by(_Mem.version.desc()).all()
    )
    return [
        {
            "entry_id": str(r.id),
            "version": int(r.version),
            "status": str(r.status),
            "content": str(r.content),
            "sources": list(r.source_artifact_ids or []),
            "decision": r.decision,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "superseded_at": r.superseded_at.isoformat() if r.superseded_at else None,
            "usage_count": int(r.usage_count or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Usage réel — le point que T-M07 vérifie
# ---------------------------------------------------------------------------


def usage_key(
    response_id: str, entry_id: str, version: int, evenement: str | None = None
) -> str:
    """Clé d'usage idempotente (C09) : même réponse rejouée, un seul incrément.

    `evenement` qualifie la nature de la sollicitation — une ressource retournée
    par un outil de recherche, une interrogation réellement exécutée — et entre
    dans la clé : deux natures distinctes ne partagent jamais un même compteur. Une
    relance de la MÊME sollicitation, elle, reste une seule et même chose : la clé la
    neutralise, et l'incrément ne se reproduira pas.
    """
    raw = f"{response_id}|{entry_id}|{int(version)}"
    if evenement:
        raw = f"{raw}|{evenement}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def record_usage(
    db: Any, entry_id: str, response_id: str, evenement: str | None = None
) -> dict[str, Any]:
    """Enregistre un usage RÉEL : la preuve a été retenue pour la réponse.

    À appeler depuis la fin d'une réponse validée, jamais depuis une ouverture,
    une recherche ou un affichage (`backend_espaces.md:141`).

    La déduplication se fait par (réponse, version) : « Dédupliquer l'usage par
    réponse et révision de fiche » (`:138`). T-M07 — dix consultations sans
    usage effectif ne gonflent rien ; une relance n'incrémente qu'une fois.

    `evenement` qualifie la nature de la sollicitation — une ressource retournée
    par un outil de recherche, une interrogation réellement exécutée — et entre
    dans la clé : deux natures distinctes ne partagent jamais un même compteur.
    Une relance de la MÊME sollicitation, elle, reste une seule et même chose :
    la clé la neutralise, et l'incrément ne se reproduira pas.
    """
    from backend.db.models import MemoryEntry as _Mem, MemoryUsage as _Use
    from sqlalchemy.exc import IntegrityError

    entry = db.query(_Mem).filter(_Mem.id == entry_id).first()
    if entry is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    if not int(entry.is_current or 0):
        # Un usage sur une version périmée ne serait jamais lu par `search()`,
        # qui ne lit que `is_current = 1` : le signal serait perdu. Mieux vaut
        # le dire que l'écrire dans le vide.
        return {
            "ok": False,
            "code": "SUPERSEDED_VERSION",
            "entry_id": str(entry.id),
            "version": int(entry.version),
            "note": "enregistrer l'usage sur la version courante",
        }
    key = usage_key(response_id, str(entry.id), int(entry.version), evenement)

    already = db.query(_Use).filter(_Use.usage_key == key).first()
    if already is not None:
        return {
            "ok": True,
            "counted": False,
            "usage_count": int(entry.usage_count or 0),
            "note": "usage déjà compté pour cette réponse et cette version",
        }

    db.add(
        _Use(
            id=f"mus_{uuid.uuid4().hex[:12]}",
            entry_id=str(entry.id),
            usage_key=key,
            response_id=response_id,
        )
    )
    entry.usage_count = int(entry.usage_count or 0) + 1
    entry.last_used_at = _now()
    try:
        db.commit()
    except IntegrityError:
        # Course entre deux rejeux de la même réponse : la contrainte unique a
        # gagné, donc l'usage n'est compté qu'une fois. Une course n'est pas une
        # panne : elle se rend comme un refus idempotent, pas comme une 500.
        db.rollback()
        courant = db.query(_Mem).filter(_Mem.id == entry_id).first()
        return {
            "ok": True,
            "counted": False,
            "usage_count": int(courant.usage_count or 0) if courant is not None else 0,
            "note": "usage déjà compté (course sur la même réponse)",
        }
    return {"ok": True, "counted": True, "usage_count": int(entry.usage_count or 0)}


def enregistrer_usages(
    db: Any,
    workspace_id: str,
    artifact_ids: list[str],
    *,
    evenement: str,
    response_id: str,
) -> list[str]:
    """Émet le signal d'importance pour les ressources sollicitées d'un tour.

    Le signal (recherche_locale.md §6.4) naît d'un usage réel : une ressource
    effectivement rendue par un outil de recherche, ou interrogée réellement.
    Chaque fiche courante du périmètre coupable d'une ressource sollicitée est
    alors incrémentée — et rien d'autre : une ressource sans fiche, ou hors
    périmètre, ne crée pas de signal, et la consultation silencieuse ne compte
    pas.

    `evenement` nomme la nature de la sollicitation — un retour, une
    interrogation — et entre dans la clé d'usage : deux natures distinctes ne
    partagent jamais un compteur, là où une relance de la même sollicitation reste
    une seule et même chose.

    Retourne les identifiants de fiches comptés. L'appelant reste responsable
    du reste (journalisation, jamais le signal lui-même) : ce module ne lève pas.
    """
    from backend.db.models import MemoryEntry as _Mem

    ids = {str(a) for a in (artifact_ids or []) if str(a or "").strip()}
    if not ids or not str(workspace_id or "").strip():
        return []
    comptes: list[str] = []
    for row in (
        db.query(_Mem)
        .filter(_Mem.workspace_id == workspace_id, _Mem.is_current == 1)
        .all()
    ):
        sources = {str(s) for s in (row.source_artifact_ids or [])}
        if not (sources & ids):
            continue
        if str(row.status) == "rejected":
            continue
        sortie = record_usage(db, str(row.id), response_id, evenement=evenement)
        if sortie.get("ok") and sortie.get("counted"):
            comptes.append(str(row.id))
    return comptes


# ---------------------------------------------------------------------------
# Signal d'importance - usage reel d'une ressource retournee ou interrogee
# ---------------------------------------------------------------------------


def remettre_a_neutre_signaux(
    db: Any,
    workspace_id: str,
    artifact_ids: list[str] | None = None,
    *,
    motif: str | None = None,
) -> dict[str, Any]:
    """Remet les signaux d'importance des ressources déplacées à zéro.

    « Le transfert + synthèses actualisées + signaux déplacés remis à niveau
    neutre (pas transfert) » (`Bible_des_intentions.md:320`,
    `cycle_de_vie_des_informations.md:119-121`) : un score d'importance acquis
    dans l'ancien espace n'est pas une importance acquise dans le nouveau, il
    repart d'un signal neutre. Ici « neutre » signifie que le compteur d'usage
    (`usage_count`) et sa récence (`last_used_at`) sont remis à zéro — le signal
    est retiré, jamais transféré.

    `motif` porte la justification de la décision sans jamais influencer le reset :
    un signal neutre n'a pas besoin de raison pour être neutre.
    """
    from backend.db.models import MemoryEntry as _Mem, MemoryUsage as _Use

    ids = sorted({str(a) for a in (artifact_ids or []) if str(a or "").strip()})
    resultats = {
        "workspace_id": workspace_id,
        "artifacts": [],
        "entrees": 0,
        "signaux_supprimes": 0,
    }
    if not ids:
        # Sans contribution ciblée, un reset serait une écriture muette : rien
        # à neutraliser, donc rien à compter.
        return resultats

    ressources = set(ids)
    fiches: list[str] = []
    signaux: set[str] = set()
    signaux_supprimes = 0
    for fiche in (
        db.query(_Mem)
        .filter(
            _Mem.workspace_id == workspace_id,
            _Mem.is_current == 1,
            _Mem.status == "accepted",
        )
        .all()
    ):
        cites = {str(s) for s in (fiche.source_artifact_ids or [])}
        if not (cites & ressources):
            continue
        # Compteur puis récence : le signal d'importance est détruit ici, dans ce
        # contexte — l'ancien score ne transite pas vers le nouveau.
        connus = int(db.query(_Use).filter(_Use.entry_id == fiche.id).count() or 0)
        db.query(_Use).filter(_Use.entry_id == fiche.id).delete(
            synchronize_session=False
        )
        fiche.usage_count = 0
        fiche.last_used_at = None
        db.flush()
        fiches.append(str(fiche.id))
        signaux |= cites & ressources
        signaux_supprimes += connus

    db.commit()
    return {
        "workspace_id": workspace_id,
        "artifacts": sorted(signaux),
        "entrees": len(fiches),
        "signaux_supprimes": int(signaux_supprimes),
    }


# Mêmes fonctions sous leurs autres noms de lecture : le geste (« remettre à
# neutre ») et le mécanisme (signal d'usage) portent des vocabulaires distincts.
remise_a_neutre_signaux = remettre_a_neutre_signaux
remettre_a_neutre = remettre_a_neutre_signaux
remettre_a_neutre_usage = remettre_a_neutre_signaux


# ---------------------------------------------------------------------------
# Synthèse — les orientations courantes d'un espace, pour le contexte initial
# ---------------------------------------------------------------------------


def faits_courants(
    db: Any, workspace_id: str, *, statuts: tuple[str, ...] | list[str] | None = None
) -> list[dict[str, Any]]:
    """Les orientations courantes d'un espace, pour la synthèse du début de discussion.

    Retourne les fiches courantes (`is_current = 1`) sous la forme que la synthèse
    courte de l'espace consomme : contenu, statut, origine et fraîcheur. Le service ne
    tranche pas — la synthèse rend ce que la fiche porte, dans le respect de son
    statut, sans la transformer en affirmation.

    Sans `statuts`, le service ne filtre pas : la synthèse conserve le vocabulaire
    exact de la fiche (décision, hypothèse, observation, information à vérifier, plus
    les statuts de cycle `candidate` / `accepted` / `stale` / `rejected`). Avec
    `statuts`, l'appelant restreint explicitement la liste — par exemple
    `statuts=("accepted",)` pour ne servir que les faits promus.
    """
    from backend.db.models import MemoryEntry as _Mem

    query = db.query(_Mem).filter(
        _Mem.workspace_id == workspace_id, _Mem.is_current == 1
    )
    if statuts is not None:
        query = query.filter(_Mem.status.in_(tuple(statuts)))
    faits: list[dict[str, Any]] = []
    for row in query.all():
        faits.append(
            {
                "entry_id": str(row.id),
                "content": str(row.content or ""),
                "status": str(row.status or ""),
                "stale_reason": (
                    row.stale_reason
                    or (
                        (row.decision or {}).get("reason")
                        if str(row.status) == "stale"
                        else None
                    )
                ),
                "previous_status": row.previous_status,
                "origin": str(row.origin_context or "")
                or str(row.origin_conversation_id or "")
                or "",
                "freshness": str(row.last_used_at or row.created_at or ""),
                "created_at": _iso(row.created_at),
            }
        )
    return faits


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            pass
    return str(v)


def lister_elements_courants(
    db_or_workspace: Any, workspace_id: Any = None
) -> list[dict[str, Any]]:
    """Liste les éléments courants de la mémoire d'un espace (pour l'onglet).

    Retourne, pour chaque version courante : contenu, catégorie, statut
    d'affirmation, date de création, origine/contexte, fraîcheur, nombre et
    références de sources. Lisible sans passer par le modèle ; sert de porte
    d'entrée IPC et de vue pour `EspacePage`.
    """
    # Tolérance sur l'ordre des arguments : `lister_elements_courants(db, ws)`
    # ou `lister_elements_courants(ws)` avec SessionLocal résolu localement.
    close = False
    if workspace_id is None:
        workspace_id = db_or_workspace
        from backend.db.database import SessionLocal

        db = SessionLocal()
        close = True
    else:
        db = db_or_workspace
    try:
        from backend.db.models import MemoryEntry as _Mem

        rows = (
            db.query(_Mem)
            .filter(_Mem.workspace_id == workspace_id, _Mem.is_current == 1)
            .order_by(_Mem.created_at.desc(), _Mem.id)
            .all()
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            sources = [str(s) for s in (r.source_artifact_ids or [])]
            out.append(
                {
                    "entry_id": str(r.id),
                    "entry_key": str(r.entry_key),
                    "content": str(r.content or ""),
                    "status": str(r.status or ""),
                    "stale_reason": (
                        r.stale_reason
                        or (
                            (r.decision or {}).get("reason")
                            if str(r.status) == "stale"
                            else None
                        )
                    ),
                    "previous_status": r.previous_status,
                    "category": r.category,
                    "affirmation_status": r.affirmation_status,
                    "created_at": _iso(r.created_at),
                    "origin": str(r.origin_context or r.origin_conversation_id or ""),
                    "origin_context": r.origin_context,
                    "origin_conversation_id": r.origin_conversation_id,
                    "context": r.origin_context,
                    "freshness": _iso(r.last_used_at or r.created_at),
                    "sources_count": len(sources),
                    "source_refs": list(sources),
                    "source_artifact_ids": list(sources),
                    "version": int(r.version),
                }
            )
        return out
    finally:
        if close:
            try:
                db.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entretien courant de la synthèse — seuil d'inactivité, dépôt et admission
# ---------------------------------------------------------------------------
# D'OÙ VIENT CE FICHIER : `memoire.md` §5.5 (L.149-L.153) et `orchestration.md` §2 L.65.
# OÙ VA QUOI : transformer l'inactivité de l'utilisateur en DEMANDE d'entretien, puis
# la distinguer de l'ADMISSION EFFECTIVE de l'exécution (dépôt ≠ exécution).
# La demande est déposée ; l'entretien n'est exécuté que si l'admission est accordée
# par l'ordonnancement — jamais en parallèle d'un tour actif.

# Registre d'activité par espace : l'activité utilisateur est accumulée ici, et le
# seuil d'inactivité s'en évalue. Clé = identifiant d'espace, valeur = dernier instant.
REGISTRE_ACTIVITE: dict[str, datetime] = {}
_REGISTRE_ACTIVITE_LOCK = threading.Lock()


def enregistrer_activite(workspace_id: str, maintenant: datetime | None = None) -> None:
    """Enregistre une activité utilisateur pour l'espace : l'instant du dernier usage.

    Le registre est thread-safe (un verrou module-level) et n'est pas un compteur :
    une seule valeur par espace, celle du dernier événement d'activité, suffit à la
    durée d'inactivité. L'appelant est celui qui constate une activité réelle — jamais
    une sollicitation, pas plus une consultation silencieuse.
    """
    with _REGISTRE_ACTIVITE_LOCK:
        REGISTRE_ACTIVITE[str(workspace_id)] = maintenant or _now()


def derniere_activite(workspace_id: str) -> datetime | None:
    """Dernier instant d'activité enregistré pour l'espace, ou None si aucune."""
    with _REGISTRE_ACTIVITE_LOCK:
        return REGISTRE_ACTIVITE.get(str(workspace_id))


def duree_inactivite(
    workspace_id: str, maintenant: datetime | None = None
) -> float | None:
    """Durée d'inactivité (en secondes) pour l'espace, depuis son dernier usage.

    Retourne None quand l'espace n'a enregistré aucune activité : l'absence d'entrée
    est un état, pas un zéro — on ne peut pas dire « il est inactif depuis toujours » sans
    une intention explicite.
    """
    derniere = derniere_activite(workspace_id)
    if derniere is None:
        return None
    horloge = maintenant or _now()
    return (horloge - derniere).total_seconds()


def depasse_seuil_inactivite(
    dernier_interaction: datetime | None,
    maintenant: datetime,
    *,
    seuil: float | None = None,
) -> bool:
    """Le seuil d'inactivité cumulée est-il franchi ?

    Helper PUR : l'interaction est fournie par l'appelant, jamais lue depuis le
    registre — la durée reste calculable sur n'importe quelle paire instant/instant,
    et le seuil est un paramètre (testable) plutôt qu'une constante figée dans le cœur
    de la décision.
    """
    if dernier_interaction is None:
        return False
    if seuil is None:
        seuil = SEUIL_ENTRETIEN_S
    return (maintenant - dernier_interaction).total_seconds() > seuil


def executer_entretien(
    db: Any,
    workspace_id: str,
    *,
    dernier_interaction: datetime | None,
    maintenant: datetime | None = None,
    tache_active: bool = False,
    contribution_inchangee: bool = False,
) -> dict[str, Any]:
    """Propose l'entretien courant de la synthèse d'un espace, après inactivité.

    Découpe le besoin en deux : le DEPÔT d'une demande d'entretien (« entretien:<id> »)
    et son ADMISSION EFFECTIVE à l'exécution — une demande déposée ne vaut PAS exécution
    (memoire.md §5.5 L.151). La proposition n'écrit jamais : `propose` ne crée que des
    fiches `candidate`, l'acceptation restant une décision humaine (`accept`).

    Retourne un objet neutre :
      ``{"depot": bool, "demande": str | None, "seuil_depasse": bool, "admis": bool,
        "statut": "pas_de_demande|differe|pas_de_changement|effectue",
        "raison": str, "resultat": ...}``

    * `pas_de_demande` : inactivité insuffisante (ou absence d'activité) — rien déposé.
    * `differe` : seuil dépassé mais une tâche de traitement est active — la demande est
      déposée (`depot`) et reste en attente, jamais exécutée en parallèle d'un tour.
    * `pas_de_changement` : seuil dépassé, mais aucun message nouveau ou seuils internes
      non franchis — l'entretien n'est pas relancé sans nouvelle contribution (SF-MEM-04).
    * `effectue` : admission effective — l'extraction déterministe et la synthèse ont
      tourné (`resultat` porte leurs comptes, jamais une promotion humaine).

    « Le traitement de mémoire utilise le même ordonnancement contrôlé que les tours de
    discussion : il ne charge pas un LLM en parallèle avec un tour actif, y compris sous
    couvert d'un traitement différé ou discret. Le retour de l'utilisateur ne doit pas
    lancer automatiquement une nouvelle inférence concurrente » (memoire.md §5.5 L.153) :
    une fois admis, l'entretien délègue au chemin unique `memoire_auto` — qui ne charge
    aucun modèle.
    """
    horloge = maintenant or _now()
    if dernier_interaction is None or not depasse_seuil_inactivite(
        dernier_interaction, horloge
    ):
        # Garde : absence d'activité ou durée sous le seuil → pas de demande de dépôt.
        return {
            "depot": False,
            "demande": None,
            "seuil_depasse": False,
            "admis": False,
            "statut": "pas_de_demande",
            "raison": "inactivité_insuffisante",
            "resultat": None,
        }

    demande = f"entretien:{workspace_id}"
    if tache_active:
        # Dépôt de la demande (elle attend dans la file), mais admission différée :
        # aucune reconstruction, aucun chargement de modèle en parallèle d'un tour.
        return {
            "depot": True,
            "demande": demande,
            "seuil_depasse": True,
            "admis": False,
            "statut": "differe",
            "raison": "tache_active",
            "resultat": None,
        }
    if contribution_inchangee:
        # SF-MEM-04 : inactivité dépassée plusieurs fois sans évolution — le maintien
        # ne se relance pas ; il n'y a rien à réviser dans la synthèse.
        return {
            "depot": True,
            "demande": demande,
            "seuil_depasse": True,
            "admis": True,
            "statut": "pas_de_changement",
            "raison": "aucune_nouvelle_contribution",
            "resultat": None,
        }

    # Admission effective : le WRAPPER d'entretien, seul chemin autorisé. Il
    # acquiert la réservation (et émet `memory_deferred` si un tour la tient),
    # puis délègue à l'extraction déterministe — aucun LLM n'est chargé.
    # Import LOCAL : `memoire_auto` consomme ce module, le cycle ne doit pas se figer.
    from backend.services import memoire_auto

    res = memoire_auto.executer_entretien_memoire(
        db,
        str(workspace_id),
        force=False,
        mode="auto",
        maintenant=horloge,
    )
    statut_auto = str(res.get("statut") or "")

    if statut_auto == "differe":
        return {
            "depot": True,
            "demande": demande,
            "seuil_depasse": True,
            "admis": False,
            "statut": "differe",
            "raison": str(res.get("raison") or "tour_actif"),
            "resultat": res,
        }

    if statut_auto == "effectue":
        return {
            "depot": True,
            "demande": demande,
            "seuil_depasse": True,
            "admis": True,
            "statut": "effectue",
            "raison": "entretien_effectue",
            "resultat": res,
        }

    if statut_auto == "rien_a_promouvoir":
        synthese = res.get("synthese") or {}
        atteint = str(synthese.get("statut") or "") in ("creee", "inchangee")
        return {
            "depot": True,
            "demande": demande,
            "seuil_depasse": True,
            "admis": True,
            "statut": "effectue" if atteint else "pas_de_changement",
            "raison": "synthese_construite" if atteint else "aucune_synthese",
            "resultat": res,
        }

    # `pas_encore` (seuils internes) et `pas_de_nouveaux_messages` sont deux
    # formes du même fait : rien à réviser, donc rien à reconstruire.
    return {
        "depot": True,
        "demande": demande,
        "seuil_depasse": True,
        "admis": True,
        "statut": "pas_de_changement",
        "raison": str(res.get("raison") or "aucune_nouvelle_contribution"),
        "resultat": res,
    }


# ---------------------------------------------------------------------------
# Recherche — les droits filtrent AVANT le classement (C17:262)
# ---------------------------------------------------------------------------


def search(
    db: Any,
    workspace_id: str,
    query: str,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    include_stale: bool = True,
) -> dict[str, Any]:
    """Cherche dans la mémoire d'un espace, dans l'ordre imposé par le contrat.

    L'ordre n'est pas négociable, il est écrit tel quel
    (`backend_espaces.md:66-88`) :

    1. **droits et sources accessibles d'abord** — un candidat dont une source a
       disparu est écarté avant d'être classé ;
    2. `rejected` écarté de l'usage ordinaire ;
    3. `candidate` n'est PAS une mémoire acceptée : il est rendu avec son statut,
       jamais mélangé aux acceptées ;
    4. `stale` est signalé, et sa synthèse n'est pas présentée comme un fait actuel ;
    5. pertinence pour la question, PUIS usage/recence en signal secondaire ;
    6. égalités stabilisées par un critère déterministe.

    Le classement est LEXICAL et déterministe. Ce n'est pas un pis-aller : sur la
    tâche de récupération précise, BM25 atteint 61 % contre 65 % pour le vectoriel
    (05_classe_memoire.md:111-113), et le rapport de recherche conclut que le
    routage déterministe est la doctrine qui gagne en local.
    """
    from backend.db.models import Artifact as _Art, MemoryEntry as _Mem

    rows = (
        db.query(_Mem)
        .filter(_Mem.workspace_id == workspace_id, _Mem.is_current == 1)
        .all()
    )
    # 1. Droits + sources accessibles : filtre AVANT tout score.
    alive: set[str] | None = None
    if rows:
        wanted = sorted({str(s) for r in rows for s in (r.source_artifact_ids or [])})
        if wanted:
            alive = {
                str(a.id)
                for a in db.query(_Art)
                .filter(_Art.id.in_(wanted), _Art.deleted_at.is_(None))
                .all()
            }

    q_tokens = set(_tokens(query))
    candidates: list[dict[str, Any]] = []
    dropped_no_source = 0
    for r in rows:
        status = str(r.status)
        if status == "rejected":
            continue
        sources = [str(s) for s in (r.source_artifact_ids or [])]
        if (
            sources
            and alive is not None
            and any(s not in alive for s in sources)
            and status != "stale"
        ):
            # Une source perdue ne se présente pas comme une fiche valide. Une
            # fiche déjà marquée `stale` reste, elle, lisible sous ce statut :
            # c'est la distinction stale/accepted que la recherche doit pouvoir
            # servir après réconciliation.
            dropped_no_source += 1
            continue
        if status == "stale" and not include_stale:
            continue
        content = str(r.content or "")
        haystack = set(_tokens(content))
        overlap = q_tokens & haystack
        # Une fiche sans source (mémoire déclarative de l'utilisateur) reste
        # trouvable : sa pertinence se juge sur son contenu. Une fiche sans
        # recouvrement garde un score nul : elle n'est pas pertinente, mais elle
        # reste PAGINABLE par recherche lexicale ciblée.
        score = (len(overlap) / len(q_tokens)) if q_tokens else 0.0
        candidates.append(
            {
                "entry_id": str(r.id),
                "entry_key": str(r.entry_key),
                "status": status,
                "category": r.category,
                "affirmation_status": r.affirmation_status,
                "content": content,
                "version": int(r.version),
                "sources_count": len(sources),
                "is_candidate": status == "candidate",
                "is_stale": status == "stale",
                "relevance": round(score, 4),
                "usage_count": int(r.usage_count or 0),
                "last_used_at": r.last_used_at,
                "origin_conversation_id": r.origin_conversation_id,
                "created_at": r.created_at,
            }
        )

    # 5. Pertinence d'abord, usage et récence ensuite, égalités stabilisées.
    #    Un usage élevé ne valide pas la vérité d'une fiche (C17:260).
    candidates.sort(
        key=lambda c: (
            -c["relevance"],
            -int(c["usage_count"] or 0),
            -(c["last_used_at"].timestamp() if c["last_used_at"] else 0.0),
            str(c["entry_id"]),
        )
    )

    # Curseur LÉ par la révision du classement, comme `fts.py:108-123`. Deux
    # des quatre clés de tri sont mutables par construction (usage, récence) :
    # un décalage nu ferait sauter une fiche entre deux pages, et le contrat
    # l'interdit nommément — « ne pas masquer des résultats par décalage
    # silencieux ». On invalide donc explicitement, comme `fts.search` rend
    # `SEARCH_CURSOR_MISMATCH`.
    fingerprint = _ranking_fingerprint(candidates, query, workspace_id)
    start = 0
    if cursor:
        dec = _cursor_decode(cursor)
        if not dec or dec.get("qh") != fingerprint:
            return {
                "ok": False,
                "code": "MEMORY_CURSOR_MISMATCH",
                "message": "Le classement a changé entre deux pages : "
                "relance la recherche sans curseur plutôt que de décaler la page.",
                "workspace_id": workspace_id,
                "total": len(candidates),
            }
        try:
            start = max(0, int(dec.get("off", 0)))
        except Exception:
            start = 0

    total = len(candidates)
    page = candidates[start : start + max(1, int(limit))]
    next_cursor = (
        _cursor_encode(fingerprint, start + len(page))
        if start + len(page) < total
        else None
    )

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "total": total,
        "returned_count": len(page),
        "cursor": start,
        "next_cursor": next_cursor,
        # Une page n'est pas une limite : le curseur permet d'aller chercher
        # la fiche rare au-delà des cinq premières (T-M06). Il est lié à la
        # révision du classement — voir `_ranking_fingerprint`.
        "has_more": next_cursor is not None,
        "page_size": max(1, int(limit)),
        # `backend_espaces.md:74` demande les raisons structurées du match à côté
        # du curseur. Ici, elles sont déterministes par construction.
        "match_reasons": {
            "relevance": "recouvrement lexical avec la question",
            "usage": "signal secondaire, jamais une validation de vérité (C17)",
            "recency": "signal secondaire",
        },
        "dropped_unreachable_source": dropped_no_source,
        "results": [
            {
                "entry_id": c["entry_id"],
                "version": c["version"],
                "status": c["status"],
                "category": c["category"],
                "affirmation_status": c["affirmation_status"],
                "is_candidate": c["is_candidate"],
                "is_stale": c["is_stale"],
                "content": c["content"],
                "relevance": c["relevance"],
                "usage_count": c["usage_count"],
                "sources_count": c["sources_count"],
                # Rappel de hiérarchie : le FICHIER prime sur la synthèse distillée
                # (C17, 05_classe_memoire.md:59-61).
                "citation_hint": "la synthèse ne remplace pas la preuve d'origine : cite la source",
            }
            for c in page
        ],
    }


def purge_memory(
    db: Any,
    entry_id_or_key: str,
    workspace_id: str | None,
    *,
    actor: str = "user",
    exclure: bool = False,
) -> dict[str, Any]:
    """Supprime une fiche et ses versions, à la demande de l'utilisateur.

    `workspace_id` est OBLIGATOIRE, y compris pour la lecture par
    `list_versions` : la suppression est l'opération la plus destructive du
    module, et sans périmètre déclaré elle s'exécuterait sur la fiche d'un autre
    espace. Une fiche de mémoire n'est jamais manipulable hors de son espace.

    Accepte `entry_id` comme `entry_key`, comme `list_versions` : les deux
    fonctions sont appelées dans le même geste d'interface, et exiger une forme
    différente à chacune transformait une confirmation en `MEMORY_UNKNOWN`.

    « Purger la mémoire ne doit pas altérer la zone utilisateur » (phase_5:128) :
    on ne touche qu'aux lignes de mémoire et à leurs usages. Les sources, les
    blobs et les liens d'espace restent intacts.

    `exclure=True` dépose AVANT la suppression une exclusion durable du contenu
    courant : la fiche disparaît, mais ce qu'elle disait ne revient pas par une
    extraction ultérieure. Le défaut (`False`) ne change rien au contrat existant.
    """
    from backend.db.models import MemoryEntry as _Mem, MemoryUsage as _Use

    current = _resolve_entry(db, entry_id_or_key)
    if current is None:
        raise LookupError("[MEMORY_UNKNOWN] Fiche introuvable.")
    _dans_workspace(current, workspace_id)
    exclusion = (
        creer_exclusion(
            db,
            str(current.workspace_id),
            str(current.content),
            entry_key=str(current.entry_key),
            reason="purge_memory",
        )
        if exclure
        else None
    )
    key = str(current.entry_key)
    ids = [str(r.id) for r in db.query(_Mem).filter(_Mem.entry_key == key).all()]
    db.query(_Use).filter(_Use.entry_id.in_(ids)).delete(synchronize_session=False)
    db.query(_Mem).filter(_Mem.entry_key == key).delete(synchronize_session=False)
    db.commit()
    result = {
        "ok": True,
        "entry_key": key,
        "versions_removed": len(ids),
        "user_zone_untouched": True,
        "actor": actor,
    }
    if exclusion is not None:
        # Le défaut ne change pas la forme du retour ; l'exclusion est un
        # supplément explicite, présent seulement quand elle a été demandée.
        result["excluded"] = True
        result["exclusion"] = exclusion
    return result


# ---------------------------------------------------------------------------
# Synthèse synthétique — dimension distincte du corpus et des fiches
# ---------------------------------------------------------------------------


def creer_synthese(
    db: Any,
    workspace_id: str,
    content: str,
    *,
    source_entry_ids: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Crée une version de synthèse d'espace dans `memory_syntheses`.

    Ne recycle aucune fiche : la synthèse est une dimension à part, dont la
    provenance est la liste des fiches qu'elle résume (`source_entry_ids`).
    """
    from backend.db.models import MemorySynthesis as _Syn, Workspace as _Ws

    text = (content or "").strip()
    if not text:
        raise ValueError("[CONTENT_REQUIRED] Une synthèse a besoin d'un contenu.")
    ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
    if ws is None or ws.state != "active":
        raise LookupError("[WORKSPACE_NOT_FOUND] Espace introuvable ou en corbeille.")

    if idempotency_key:
        existant = (
            db.query(_Syn)
            .filter(
                _Syn.workspace_id == workspace_id,
                _Syn.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existant is not None:
            if int(existant.is_current or 0) == 1:
                return {
                    "ok": True,
                    "synthesis_id": existant.id,
                    "workspace_id": workspace_id,
                    "version": int(existant.version),
                    "source_count": len(existant.source_entry_ids or []),
                }
            # La clé appartient à une version PÉRIMÉE. La réutiliser violerait
            # l'unicité de `uq_memory_idem_synthese` ; on crée donc une nouvelle
            # version sans clé, qui devient la courante. Rendre l'ancienne
            # version présenterait un contenu périmé comme la synthèse du jour.
            idempotency_key = None

    from sqlalchemy.exc import IntegrityError

    # L'index unique partiel `uq_memory_syntheses_current` peut faire échouer
    # l'écriture si une autre session a publié une courante entre la lecture et
    # le commit. On relit alors l'existante ; à défaut, une seule seconde
    # tentative suffit — au-delà, la course est réelle et doit remonter.
    derniere_erreur: IntegrityError | None = None
    for tentative in range(2):
        courantes = (
            db.query(_Syn)
            .filter(_Syn.workspace_id == workspace_id, _Syn.is_current == 1)
            .all()
        )
        version = max((int(row.version or 0) for row in courantes), default=0) + 1
        maintenant = _now()
        # TOUTES les courantes basculent, pas seulement la première : une base
        # ayant déjà deux `is_current=1` (écriture brute, ancienne course) ne
        # doit pas conserver un doublon de courant après une nouvelle synthèse.
        for row in courantes:
            row.is_current = 0
            row.superseded_at = maintenant
        db.flush()
        entry = _Syn(
            id=f"syn_{uuid.uuid4().hex[:12]}",
            workspace_id=workspace_id,
            version=version,
            is_current=1,
            content=text,
            source_entry_ids=list(source_entry_ids or []),
            idempotency_key=idempotency_key or None,
        )
        db.add(entry)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            derniere_erreur = exc
            existant = None
            if idempotency_key:
                existant = (
                    db.query(_Syn)
                    .filter(
                        _Syn.workspace_id == workspace_id,
                        _Syn.idempotency_key == idempotency_key,
                    )
                    .first()
                )
            if existant is not None and int(existant.is_current or 0) == 1:
                return {
                    "ok": True,
                    "synthesis_id": existant.id,
                    "workspace_id": workspace_id,
                    "version": int(existant.version),
                    "source_count": len(existant.source_entry_ids or []),
                }
            existant = (
                db.query(_Syn)
                .filter(
                    _Syn.workspace_id == workspace_id,
                    _Syn.is_current == 1,
                )
                .order_by(_Syn.version.desc(), _Syn.created_at.desc())
                .first()
            )
            if existant is not None:
                return {
                    "ok": True,
                    "synthesis_id": existant.id,
                    "workspace_id": workspace_id,
                    "version": int(existant.version),
                    "source_count": len(existant.source_entry_ids or []),
                }
            if tentative == 0:
                continue
            raise
        return {
            "ok": True,
            "synthesis_id": entry.id,
            "workspace_id": workspace_id,
            "version": version,
            "source_count": len(source_entry_ids or []),
        }

    if derniere_erreur is not None:
        raise derniere_erreur
    raise RuntimeError("[SYNTHESIS_CONFLICT] Aucune version de synthèse écrite.")


def syntheses_courantes(db: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Synthèses courantes (`is_current=1`) d'un espace."""
    from backend.db.models import MemorySynthesis as _Syn

    out: list[dict[str, Any]] = []
    for row in (
        db.query(_Syn)
        .filter(_Syn.workspace_id == workspace_id, _Syn.is_current == 1)
        .all()
    ):
        out.append(
            {
                "synthesis_id": str(row.id),
                "content": str(row.content or ""),
                "version": int(row.version),
                "source_entry_ids": list(row.source_entry_ids or []),
                "created_at": _iso(row.created_at),
            }
        )
    return out


def synthese_courante(db: Any, workspace_id: str) -> dict[str, Any] | None:
    """La synthèse courante la plus récente d'un espace, ou None.

    `syntheses_courantes` rend la liste complète ; ce point d'entrée rend la
    seule synthèse qui fait foi pour le contexte initial. Les deux formes
    existent parce que l'affichage (toutes les courantes) et la consommation
    (la courante) ne demandent pas la même chose.
    """
    from backend.db.models import MemorySynthesis as _Syn

    row = (
        db.query(_Syn)
        .filter(_Syn.workspace_id == workspace_id, _Syn.is_current == 1)
        .order_by(_Syn.created_at.desc(), _Syn.version.desc(), _Syn.id.desc())
        .first()
    )
    if row is None:
        return None
    return {
        "synthesis_id": str(row.id),
        "content": str(row.content or ""),
        "version": int(row.version),
        "created_at": _iso(row.created_at),
        "source_entry_ids": list(row.source_entry_ids or []),
    }


# Alias FR/EN
synthese_courantes = syntheses_courantes
creer_synthese_espace = creer_synthese


# ---------------------------------------------------------------------------
# Entretien en arrière-plan — minuteur d'inactivité (5 min)
# ---------------------------------------------------------------------------


def planifier_entretien_idle(
    workspace_id: str,
    *,
    delay: float | None = None,
    tache_active: bool = False,
    db_factory: Any = None,
    dernier_interaction: datetime | None = None,
    maintenant: datetime | None = None,
) -> dict[str, Any]:
    """Planifie `executer_entretien` après `delay` secondes d'inactivité.

    Respecte l'absence de tour actif via `tache_active` : si un tour est en
    cours, la demande est déposée mais non exécutée. Un délai nul ou négatif
    exécute immédiatement (utile aux tests), sinon un minuteur démon est lancé.
    """
    if delay is None:
        delay = SEUIL_ENTRETIEN_S

    def _run() -> None:
        from backend.db.database import SessionLocal

        factory = db_factory or SessionLocal
        db = factory()
        try:
            if dernier_interaction is None:
                dernier = derniere_activite(workspace_id)
            else:
                dernier = dernier_interaction
            if maintenant is None:
                horloge = _now()
            else:
                horloge = maintenant
            executer_entretien(
                db,
                workspace_id,
                dernier_interaction=dernier,
                maintenant=horloge,
                tache_active=tache_active,
            )
        except Exception:  # noqa: BLE001 - jamais un crash de fond
            pass
        finally:
            try:
                db.close()
            except Exception:
                pass

    if delay <= 0:
        _run()
        return {
            "scheduled": True,
            "delay": delay,
            "workspace_id": workspace_id,
            "executed": True,
        }
    timer = threading.Timer(float(delay), _run)
    timer.daemon = True
    timer.start()
    return {
        "scheduled": True,
        "delay": float(delay),
        "workspace_id": workspace_id,
        "timer": timer,
    }


# Alias tolérants aux variantes.
demarrer_minuteur_entretien = planifier_entretien_idle
planifier_entretien = planifier_entretien_idle
lancer_entretien_idle = planifier_entretien_idle
demarrer_entretien_arriere_plan = planifier_entretien_idle
idle_timer = planifier_entretien_idle
hook_entretien = planifier_entretien_idle
idle_timer = planifier_entretien_idle
hook_entretien = planifier_entretien_idle

_rafraichi_counter = {}


def _auto_contenu_memoire(db, workspace_id):
    try:
        faits = faits_courants(db, str(workspace_id)) or []
    except Exception:
        faits = []
    morceaux = []
    for fait in faits:
        texte = str(fait.get("content", "") or "").strip()
        if texte:
            morceaux.append(texte)
    if not morceaux:
        return None
    unic = []
    for m in morceaux:
        if m not in unic:
            unic.append(m)
    return "\n".join(unic[:10])


def comptabiliser_tour_memoire(db, workspace_id, *, seuil_messages=5, force=False):
    """DÉPRÉCIÉ : ne rafraîchit plus la mémoire, seulement le compteur d'activité.

    Le rafraîchissement est un entretien complet, et un entretien complet ne se
    déclenche plus à la fin d'un tour : il appartient au planificateur
    d'inactivité (`memoire_scheduler`) ou au chemin de service mémoire. Cette
    fonction reste pour les appelants historiques : elle incrémente le compteur
    et note l'activité, sans jamais reconstruire de synthèse.
    """
    from backend.db.models import Workspace as _Ws

    if not workspace_id:
        return {"ok": False, "status": "ignored", "reason": "workspace_id absent"}
    ws = db.query(_Ws).filter(_Ws.id == str(workspace_id)).first()
    if ws is None:
        return {"ok": False, "status": "ignored", "reason": "workspace inconnu"}
    tour = int(getattr(ws, "auto_memory_turn_count", 0) or 0) + 1
    ws.auto_memory_turn_count = tour
    maintenant = _now()
    ws.last_activity_at = maintenant
    enregistrer_activite(str(workspace_id), maintenant)
    try:
        db.commit()
    except Exception:
        db.rollback()
    return {
        "ok": True,
        "statut": "attente",
        "deprecated": True,
        "workspace_id": str(workspace_id),
        "tour_count": tour,
        "seuil_messages": int(seuil_messages or 5),
    }


def rafraichir_memoire_espace(
    db,
    workspace_id,
    *,
    force=True,
    contenu=None,
    seuil_messages=0,
    category=None,
    affirmation_status=None,
    idempotency_key=None,
    entry_key=None,
):
    from backend.db.models import Workspace as _Ws

    if not workspace_id:
        return {"ok": False, "statut": "ignored", "raison": "workspace_id absent"}
    ws = db.query(_Ws).filter(_Ws.id == str(workspace_id)).first()
    if ws is None:
        return {"ok": False, "statut": "ignored", "raison": "workspace inconnu"}

    if contenu is None:
        # Plus de contenu synthétique fabriqué ici : le chemin déterministe
        # d'extraction est le seul à décider ce qui entre dans la mémoire.
        from backend.services import memoire_auto

        return memoire_auto.forcer_memoire_espace(db, str(workspace_id))

    if not force:
        count = int(getattr(ws, "auto_memory_turn_count", 0) or 0)
        if count < int(seuil_messages or 5):
            return {
                "ok": True,
                "statut": "attente",
                "tour_count": count,
                "seuil_messages": int(seuil_messages or 5),
            }

    # Contenu FOURNI : chemin explicite `space_auto`, borné comme une fiche.
    contenu = str(contenu).strip()
    if not contenu:
        raise ValueError(
            "[CONTENT_REQUIRED] Une fiche a besoin d'un contenu synthétique."
        )
    if len(contenu) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"[TOO_LONG] Le contenu d'une fiche est borné à {MAX_CONTENT_CHARS} caractères."
        )
    resultat = proposer_automatique(
        db,
        str(workspace_id),
        contenu,
        category=category,
        affirmation_status=affirmation_status,
        actor="auto_refresh",
        # Une clé stable par fiche : les rafraîchissements successifs sont des
        # VERSIONS de la même fiche, pas des fiches sœurs à chaque tour.
        entry_key=entry_key or f"space_auto:{workspace_id}",
        # L'idempotence suit le CONTENU : même contenu = un seul enregistrement,
        # contenu nouveau = nouvelle version.
        idempotency_key=idempotency_key
        or f"auto_refresh:{workspace_id}:{hash_contenu(contenu)[:16]}",
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
    return resultat | {
        "statut": "rafraichi" if resultat.get("ok") else resultat.get("status", "error")
    }


refresh_space_memory = rafraichir_memoire_espace
refresh_memory = rafraichir_memoire_espace
memory_refresh = rafraichir_memoire_espace
refresh_memory_space = rafraichir_memoire_espace
