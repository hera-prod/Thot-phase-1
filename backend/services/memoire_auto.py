# services/memoire_auto.py — l'extraction déterministe de la mémoire d'espace.
#
# D'OÙ VIENT CE FICHIER : la mémoire d'espace doit se nourrir des messages de
# l'utilisateur sans jamais appeler un LLM. Tout ici est reproductible : les
# phrases sont classées par règles, les fiches sont créées avec provenance et
# idempotence, et la synthèse est reconstruite par le gabarit déterministe de
# `backend.inference.brief`.
#
# OÙ VA QUOI : `executer_entretien_memoire` est le chemin unique (réservation,
# événements) appelé par le service mémoire, le planificateur d'inactivité et le
# forçage manuel ; `entretenir_memoire` est son cœur déterministe. Aucune de ces
# portes ne charge de modèle.
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.services import memoire as memoire
from backend.services.activity import (
    Reservation,
    etat_activite,
    liberer,
    tour_actif,
    try_reserver,
)

log = logging.getLogger(__name__)

# Seuils de déclenchement : cinq tours minimum, puis cinq minutes d'inactivité.
SEUIL_TOURS_MEMOIRE = 5
SEUIL_INACTIVITE_S = 300.0
MAX_MESSAGES = 200
MAX_FAITS_PAR_MESSAGE = 5
MAX_FAIT_CHARS = 300
MAX_SYNTHESE_FAITS = 12
MAX_SYNTHESE_CHARS = 6000

# Dernier état d'entretien émis, par espace : `(statut, bloque_par, timestamp)`.
# Un `memory_deferred` répété à chaque réveil du planificateur serait du bruit ;
# il est donc réémis seulement si le bloqueur change ou après un heartbeat, pour
# que le TTL de 120 s du renderer ne fasse pas disparaître un différé qui tient.
_DERNIER_ETAT: dict[str, tuple[str, str | None, float]] = {}
_DERNIER_ETAT_LOCK = threading.Lock()

# Le renderer efface un différé après 120 s sans nouvel événement : ce heartbeat
# de 60 s le maintient vivant tant que le verrou reste occupé.
MEMORY_DEFERRED_HEARTBEAT_S = 60.0

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_PHRASE_SPLIT_RE = re.compile(r"(?<=[.!?])(?:\s+|$)")

_MEMORISATION_EXPLICITE_RE = re.compile(
    r"note\s+que|retiens|mémorise|souviens-toi|à\s+retenir", re.IGNORECASE
)
_DECISION_RE = re.compile(
    r"décision|décidé|décide|on\s+adopte|on\s+retient|on\s+choisit|on\s+utilise|"
    r"on\s+va\s+utiliser|allons\s+(?:\w+\s+){0,2}utiliser|on\s+part\s+sur|on\s+garde|convention|règle",
    re.IGNORECASE,
)
_CONTRAINTE_RE = re.compile(
    r"doit|doivent|obligatoire|interdit|interdite|contrainte|limite|maximum|"
    r"minimum|pas\s+plus\s+de|au\s+moins|au\s+plus",
    re.IGNORECASE,
)
_PREFERENCE_RE = re.compile(
    r"je\s+préfère|préfère|j'aime|je\s+veux|je\s+souhaite|style|ton|format|"
    r"langue|réponse\s+courte|réponse\s+détaillée",
    re.IGNORECASE,
)
_DOCUMENT_RE = re.compile(
    r"document|fichier|référence|spec|spécification|cahier\s+des\s+charges",
    re.IGNORECASE,
)
_DOCUMENT_SIGNAL_RE = re.compile(
    r"important|référence|source|à\s+consulter", re.IGNORECASE
)
_CONNAISSANCE_RE = re.compile(
    r"définition|signifie|veut\s+dire|appelé|nommé|convention\s+de\s+naming|glossaire",
    re.IGNORECASE,
)
_CONTEXTE_RE = re.compile(
    r"projet|objectif|but|environnement|machine|gpu|vram|ram|modèle|architecture|"
    r"stack|techno",
    re.IGNORECASE,
)
_VERBE_DECLARATIF_RE = re.compile(
    r"\b(?:est|sont|utilise|tourne|reste|doit)\b", re.IGNORECASE
)
_HYPOTHESE_RE = re.compile(
    r"peut-être|hypothèse|je\s+pense|il\s+se\s+peut|à\s+vérifier", re.IGNORECASE
)
_A_VERIFIER_RE = re.compile(r"à\s+vérifier|il\s+se\s+peut", re.IGNORECASE)

_CONTRADICTION_RE = re.compile(
    r"finalement|au\s+lieu\s+de|remplace|désormais|à\s+partir\s+de\s+maintenant|"
    r"plutôt",
    re.IGNORECASE,
)

# Une question sans « ? » n'est pas un fait. Le motif ne reconnaît que les
# ouvertures interrogatives : il ne doit pas transformer une phrase déclarative
# qui contient « comment » ou « quel » au milieu en question.
_QUESTION_RE = re.compile(
    r"^\s*(peux-tu|pouvez-vous|peut-on|est-ce que|quelle|quel|quels|quelles|"
    r"comment|pourquoi|quand|où|qui|combien|je voudrais savoir|je me demande|"
    r"sais-tu|savez-vous|as-tu|avez-vous)\b",
    re.I,
)

_SALUTATION_RE = re.compile(
    r"^(?:bonjour|bonsoir|salut|coucou|hello|hi|hey|merci(?:\s+beaucoup)?|"
    r"thanks|thank\s+you|ok|okay|d'accord|parfait|super|génial|bien|oui|non)"
    r"[\s!.,;:…-]*$",
    re.IGNORECASE,
)

_MARQUEURS_LISTE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")

# Un label public de document, jamais un mot qui commencerait par « doc ».
# `doc_0` n'existe pas : le registre commence à 1 (`docrefs._PUBLIC_ID_RE`).
_DOC_REF_RE = re.compile(r"(?<![0-9A-Za-z_])doc_([1-9]\d*)(?![0-9A-Za-z_])")


@dataclass(frozen=True)
class FaitExtrait:
    content: str
    category: str
    affirmation_status: str
    origin_context: str
    origin_conversation_id: str
    evidence_ids: list[str]
    message_id: str
    source_artifact_ids: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _emit_memory(event: str, payload: dict) -> None:
    """Émet un événement mémoire sans jamais faire échouer l'entretien.

    L'import est différé : `backend.inference.ipc` importe le runtime d'inférence,
    et la mémoire ne doit pas créer de cycle au chargement du module.
    """
    try:
        from backend.inference.ipc import emit, log

        if not emit(event, payload, run_id=None, conversation_id=None):
            log(f"[memoire] événement {event} non livré")
    except Exception as exc:
        try:
            from backend.inference.ipc import log

            log(f"[memoire] événement {event} impossible: {exc}")
        except Exception:
            pass


def _vers_datetime(valeur: Any) -> datetime | None:
    """Horodatage naïf UTC, ou None. Tolère une chaîne ISO déjà relue."""
    if valeur is None or isinstance(valeur, datetime):
        return valeur
    try:
        return datetime.fromisoformat(str(valeur))
    except (TypeError, ValueError):
        return None


def collecter_messages(
    db: Any,
    workspace_id: str,
    *,
    depuis: datetime | None = None,
    limite: int = MAX_MESSAGES,
) -> list[Any]:
    """Messages utilisateur d'un espace actif, du plus ancien au plus récent.

    La borne `depuis` est décalée de deux secondes vers le passé : SQLite écrit
    `CURRENT_TIMESTAMP` à la seconde, et un message de la même seconde que la
    dernière passe ne doit pas être perdu.
    """
    from sqlalchemy import func

    from backend.db.models import Conversation as _Conv, Message as _Msg

    query = (
        db.query(_Msg)
        .join(_Conv, _Conv.id == _Msg.conv_id)
        .filter(
            _Conv.workspace_id == workspace_id,
            _Conv.deleted_at.is_(None),
            _Msg.role == "user",
            _Msg.content.isnot(None),
            func.trim(_Msg.content) != "",
        )
    )
    if depuis is not None:
        query = query.filter(_Msg.created_at >= depuis - timedelta(seconds=2))
    rows = (
        query.order_by(_Msg.created_at.desc(), _Msg.id.desc())
        .limit(max(0, int(limite)))
        .all()
    )
    return list(reversed(rows))


def _retirer_fences(texte: str) -> str:
    return _FENCE_RE.sub(" ", texte or "")


def _majoritairement_du_code(texte: str) -> bool:
    """Plus de la moitié du message est-elle dans des fences de code ?"""
    brut = str(texte or "").strip()
    if not brut:
        return False
    blocs = _FENCE_RE.findall(brut)
    if not blocs:
        return False
    return sum(len(bloc) for bloc in blocs) / len(brut) > 0.5


def _normaliser_phrase(texte: str) -> str:
    """Forme lisible d'une phrase : markdown retiré, espaces réduits."""
    phrase = str(texte or "")
    phrase = re.sub(r"`([^`]*)`", r"\1", phrase)
    phrase = phrase.replace("**", "").replace("__", "")
    phrase = _MARQUEURS_LISTE_RE.sub("", phrase)
    phrase = re.sub(r"\s+", " ", phrase).strip()
    return phrase


def _tronquer(texte: str, maximum: int) -> str:
    """Tronque sur une frontière de mot, ellipsis comprise dans la borne."""
    if len(texte) <= maximum:
        return texte
    coupe = texte[: maximum - 1]
    frontiere = coupe.rfind(" ")
    if frontiere >= maximum // 2:
        coupe = coupe[:frontiere]
    return coupe.rstrip(" .,;:") + "…"


def _classifier(phrase: str) -> tuple[str, str] | None:
    """Catégorie et statut d'affirmation d'une phrase, ou None si aucune règle."""
    explicit = bool(_MEMORISATION_EXPLICITE_RE.search(phrase))

    category: str | None = None
    statut = "observation"

    if explicit:
        if _DECISION_RE.search(phrase):
            category, statut = "decisions", "decision"
        else:
            category, statut = "connaissances_de_travail", "observation"
    elif _DECISION_RE.search(phrase):
        category, statut = "decisions", "decision"
    elif _CONTRAINTE_RE.search(phrase):
        category, statut = "contraintes", "decision"
    elif _PREFERENCE_RE.search(phrase):
        category, statut = "preferences", "decision"
    elif _DOCUMENT_RE.search(phrase) and _DOCUMENT_SIGNAL_RE.search(phrase):
        category, statut = "reperes_documentaires", "information_a_verifier"
    elif _CONNAISSANCE_RE.search(phrase):
        category, statut = "connaissances_de_travail", "observation"
    elif _CONTEXTE_RE.search(phrase) and _VERBE_DECLARATIF_RE.search(phrase):
        category, statut = "contexte_stable", "observation"

    if _HYPOTHESE_RE.search(phrase):
        # L'hypothèse qualifie l'affirmation ; elle ne remplace pas sa catégorie.
        # Sans catégorie, elle reste une connaissance de travail à confirmer.
        statut = (
            "information_a_verifier" if _A_VERIFIER_RE.search(phrase) else "hypothese"
        )
        category = category or "connaissances_de_travail"

    if category is None:
        return None
    return category, statut


def _borner_sources(sources: list[Any]) -> list[str]:
    """Déduplique les sources dans l'ordre d'arrivée, plafonnées à MAX_SOURCES."""
    retenues: list[str] = []
    for raw in sources:
        source = str(raw or "").strip()
        if not source or source in retenues:
            continue
        retenues.append(source)
        if len(retenues) >= memoire.MAX_SOURCES:
            break
    return retenues


def _labels_doc_cites(texte: str) -> list[str]:
    """Labels `doc_N` explicitement cités, dans l'ordre du texte, sans doublon."""
    labels: list[str] = []
    for chiffres in _DOC_REF_RE.findall(str(texte or "")):
        label = f"doc_{chiffres}"
        if label not in labels:
            labels.append(label)
    return labels


def _sources_par_message(db: Any, messages: list[Any]) -> dict[str, list[str]]:
    """Artefacts attachés ou explicitement cités, par message utilisateur.

    Deux provenances seulement, jamais une devinette :

    1. les `MessageAttachment` du message, joints à un `Artifact` vivant
       (`deleted_at IS NULL`) ;
    2. les labels `doc_N` que le TEXTE du message cite, résolus par les
       `DocRef` de SA conversation. On ne prend donc jamais tous les documents
       du dépôt : seulement ceux que l'utilisateur a nommés.
    """
    from backend.db.models import (
        Artifact as _Art,
        DocRef as _DocRef,
        MessageAttachment as _MA,
    )

    user_messages = [
        m
        for m in messages
        if str(getattr(m, "role", "user") or "user") == "user"
        and str(getattr(m, "id", "") or "")
    ]
    if not user_messages:
        return {}
    message_ids = [str(getattr(m, "id")) for m in user_messages]
    par_message: dict[str, list[str]] = {mid: [] for mid in message_ids}

    # Chargement EN LOT : une requête pour tous les messages, pas une par tour.
    attachments = (
        db.query(_MA.message_id, _MA.artifact_id)
        .join(_Art, _Art.id == _MA.artifact_id)
        .filter(
            _MA.message_id.in_(message_ids),
            _Art.deleted_at.is_(None),
        )
        .order_by(_MA.message_id, _MA.ordinal)
        .all()
    )
    for message_id, artifact_id in attachments:
        par_message.setdefault(str(message_id), []).append(str(artifact_id))

    # Les labels sont locaux à une conversation : la paire (conv, label) est la
    # seule clé valide. On collecte d'abord, on résout ensuite, en un lot.
    labels_par_conv: dict[str, set[str]] = {}
    citations_par_message: dict[str, list[str]] = {}
    for message in user_messages:
        mid = str(getattr(message, "id"))
        conv_id = str(getattr(message, "conv_id", "") or "")
        labels = _labels_doc_cites(str(getattr(message, "content", "") or ""))
        if not conv_id or not labels:
            continue
        citations_par_message[mid] = labels
        labels_par_conv.setdefault(conv_id, set()).update(labels)

    if labels_par_conv:
        refs = (
            db.query(_DocRef.conv_id, _DocRef.doc_label, _DocRef.artifact_id)
            .join(_Art, _Art.id == _DocRef.artifact_id)
            .filter(
                _DocRef.conv_id.in_(sorted(labels_par_conv)),
                _DocRef.doc_label.in_(
                    sorted(
                        {
                            label
                            for labels in labels_par_conv.values()
                            for label in labels
                        }
                    )
                ),
                _DocRef.removed_at.is_(None),
                _Art.deleted_at.is_(None),
            )
            .all()
        )
        resolution = {
            (str(conv_id), str(label)): str(artifact_id)
            for conv_id, label, artifact_id in refs
        }
        for message in user_messages:
            mid = str(getattr(message, "id"))
            conv_id = str(getattr(message, "conv_id", "") or "")
            for label in citations_par_message.get(mid, []):
                artifact_id = resolution.get((conv_id, label))
                if artifact_id:
                    par_message.setdefault(mid, []).append(artifact_id)

    return {mid: _borner_sources(sources) for mid, sources in par_message.items()}


def extraire_faits(
    messages: list[Any], *, workspace_id: str, db: Any | None = None
) -> list[FaitExtrait]:
    """Extrait les faits classables des messages, sans jamais recopier le message.

    Seule la phrase classée est retenue. Quand le même contenu apparaît dans
    plusieurs messages d'un même lot, il ne produit qu'un fait : ses origines
    et ses preuves sont fusionnées, car deux discussions qui disent la même
    chose doivent laisser deux traces — pas produire deux fiches sœurs.
    """
    del workspace_id  # la portée est déjà portée par les messages collectés
    faits: list[FaitExtrait] = []
    index_par_empreinte: dict[str, int] = {}

    own = None
    if db is None:
        from backend.db.database import SessionLocal as _SL

        own = _SL()
        db = own
    try:
        sources_par_message = _sources_par_message(db, messages)
    except Exception as exc:  # noqa: BLE001 - le fait vaut mieux que sa provenance
        log.warning(
            "[memoire] provenance documentaire illisible, faits sans source : %s",
            exc,
        )
        sources_par_message = {}
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass

    for message in messages:
        role = getattr(message, "role", None)
        if role is not None and str(role) != "user":
            continue
        texte = str(getattr(message, "content", "") or "")
        if _majoritairement_du_code(texte):
            continue

        sans_fences = _retirer_fences(texte)
        produits = 0
        for brut in _PHRASE_SPLIT_RE.split(sans_fences):
            phrase = _normaliser_phrase(brut)
            if not phrase:
                continue
            memoire_explicite = bool(_MEMORISATION_EXPLICITE_RE.search(phrase))
            # Une phrase très courte est du bruit (« Ok. », « Bonjour ! ») —
            # sauf quand elle porte déjà un marqueur décisif : « On utilise
            # SQLite. » est court mais c'est une décision, pas une politesse.
            if len(phrase) < 20 and not (
                _DECISION_RE.search(phrase)
                or memoire_explicite
                or _CONTRADICTION_RE.search(phrase)
            ):
                continue
            if brut.lstrip().startswith(">"):
                continue
            if _SALUTATION_RE.match(phrase):
                continue
            # Une question sans « ? » reste une question : l'ouverture
            # interrogative suffit, sauf mémorisation explicite demandée.
            if not memoire_explicite and (
                phrase.endswith("?") or _QUESTION_RE.match(phrase)
            ):
                continue

            classification = _classifier(phrase)
            if classification is None:
                continue
            category, statut = classification
            contenu = _tronquer(phrase, MAX_FAIT_CHARS)
            empreinte = memoire.hash_contenu(contenu)

            conv_id = str(getattr(message, "conv_id", "") or "")
            message_id = str(getattr(message, "id", "") or "")
            origine = f"discussion:{conv_id}:message:{message_id}"
            sources_message = sources_par_message.get(message_id, [])

            deja = index_par_empreinte.get(empreinte)
            if deja is not None:
                existant = faits[deja]
                references = memoire._decouper_references(existant.origin_context)
                if origine not in references:
                    references.append(origine)
                preuves = list(existant.evidence_ids)
                if message_id and message_id not in preuves:
                    preuves.append(message_id)
                sources = _borner_sources(
                    [*existant.source_artifact_ids, *sources_message]
                )
                faits[deja] = replace(
                    existant,
                    origin_context=" ; ".join(references),
                    evidence_ids=preuves[-memoire.MAX_EVIDENCE_IDS :],
                    source_artifact_ids=sources,
                )
                continue

            index_par_empreinte[empreinte] = len(faits)
            faits.append(
                FaitExtrait(
                    content=contenu,
                    category=category,
                    affirmation_status=statut,
                    origin_context=origine,
                    origin_conversation_id=conv_id,
                    evidence_ids=[message_id],
                    message_id=message_id,
                    source_artifact_ids=sources_message,
                )
            )
            produits += 1
            if produits >= MAX_FAITS_PAR_MESSAGE:
                break

    return faits


def _jaccard(a: str, b: str) -> float:
    ta = set(memoire._tokens(a))
    tb = set(memoire._tokens(b))
    if not ta and not tb:
        return (
            1.0
            if memoire._normaliser_contenu(a) == memoire._normaliser_contenu(b)
            else 0.0
        )
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


def _fiches_courantes(db: Any, workspace_id: str) -> list[dict[str, Any]]:
    from backend.db.models import MemoryEntry as _Mem

    rows = (
        db.query(_Mem)
        .filter(
            _Mem.workspace_id == workspace_id,
            _Mem.is_current == 1,
            _Mem.status.in_(("accepted", "stale")),
        )
        .all()
    )
    return [
        {
            "entry_id": str(row.id),
            "content": str(row.content or ""),
            "category": row.category,
            "status": str(row.status or ""),
            "origin_context": str(row.origin_context or ""),
            "evidence_ids": list(row.evidence_ids or []),
            "source_artifact_ids": list(row.source_artifact_ids or []),
        }
        for row in rows
    ]


def appliquer_faits(
    db: Any, workspace_id: str, faits: list[FaitExtrait]
) -> dict[str, int]:
    """Écrit les faits dans la mémoire : exclusion d'abord, doublon ensuite.

    L'exclusion est consultée AU POINT D'ÉCRITURE, jamais seulement à la
    lecture : un contenu exclu ne doit pas renaître d'une extraction ultérieure.
    """
    compteurs = {
        "crees": 0,
        "mis_a_jour": 0,
        "ignores": 0,
        "exclus": 0,
        "doublons": 0,
    }
    fiches = _fiches_courantes(db, workspace_id)
    accessibles: set[str] = set()
    if any(fait.source_artifact_ids for fait in faits):
        trouvees, erreur_scope = memoire.artefacts_accessibles(db, workspace_id)
        if erreur_scope is not None:
            # Fail-closed : une provenance non vérifiée ne s'écrit pas. Le FAIT,
            # lui, reste écrit : retirer la source n'est pas supprimer le fait.
            log.warning(
                "[memoire] portée illisible pour %s, aucune source attachée : %s",
                workspace_id,
                erreur_scope,
            )
        else:
            accessibles = trouvees or set()

    for fait in faits:
        if memoire.est_exclu(db, workspace_id, fait.content):
            compteurs["exclus"] += 1
            continue

        sources = [str(s) for s in fait.source_artifact_ids if str(s) in accessibles]

        # La contradiction se teste AVANT la déduplication. Une reformulation
        # « finalement PostgreSQL » ressemble à 80 % à l'ancienne décision :
        # passer par le chemin doublon la perdrait au lieu de périmer l'ancienne.
        contradiction = bool(_CONTRADICTION_RE.search(fait.content))
        if contradiction:
            for fiche in fiches:
                if fiche["status"] != "accepted" or fiche["category"] != fait.category:
                    continue
                # Une répétition littérale de la même décision n'est pas une
                # contradiction : elle repasse par le chemin doublon et laisse
                # la fiche `accepted` intacte. Sans ce garde-fou, une passe
                # ultérieure périmait la fiche par elle-même.
                if memoire.hash_contenu(fiche["content"]) == memoire.hash_contenu(
                    fait.content
                ):
                    continue
                if _jaccard(fait.content, fiche["content"]) < 0.35:
                    continue
                try:
                    memoire.mark_stale(
                        db,
                        fiche["entry_id"],
                        reason="remplacée par une décision plus récente",
                        workspace_id=workspace_id,
                    )
                except LookupError:
                    continue
                fiche["status"] = "stale"

        existante = (
            None
            if contradiction
            else next(
                (
                    fiche
                    for fiche in fiches
                    if _jaccard(fait.content, fiche["content"]) >= 0.8
                ),
                None,
            )
        )
        if existante is not None:
            # Le même fait soutenu par une autre discussion : la fiche ne doit
            # pas perdre la seconde provenance. Une provenance déjà connue ne
            # change rien et reste un doublon.
            deja_presente = fait.origin_context in memoire._decouper_references(
                existante.get("origin_context")
            )
            preuve = memoire.ajouter_preuve(
                db,
                existante["entry_id"],
                origin_context=fait.origin_context,
                evidence_ids=list(fait.evidence_ids),
                source_artifact_ids=sources,
                workspace_id=workspace_id,
            )
            existante["origin_context"] = str(preuve.get("origin_context") or "")
            existante["evidence_ids"] = list(preuve.get("evidence_ids") or [])
            existante["source_artifact_ids"] = list(
                preuve.get("source_artifact_ids") or []
            )
            if deja_presente:
                compteurs["doublons"] += 1
            else:
                compteurs["mis_a_jour"] += 1
            continue

        empreinte = memoire.hash_contenu(fait.content)
        resultat = memoire.proposer_automatique(
            db,
            workspace_id,
            fait.content,
            category=fait.category,
            affirmation_status=fait.affirmation_status,
            source_artifact_ids=sources,
            origin_context=fait.origin_context,
            origin_conversation_id=fait.origin_conversation_id,
            evidence_ids=list(fait.evidence_ids),
            entry_key=f"fact:{workspace_id}:{empreinte[:16]}",
            idempotency_key=f"auto_disc:{workspace_id}:{empreinte[:24]}",
            actor="auto_discussion",
        )
        if not resultat.get("ok"):
            compteurs["ignores"] += 1
            continue
        if resultat.get("created"):
            compteurs["crees"] += 1
            fiches.append(
                {
                    "entry_id": str(resultat.get("entry_id") or ""),
                    "content": fait.content,
                    "category": fait.category,
                    "status": "accepted",
                    "origin_context": fait.origin_context,
                    "evidence_ids": list(fait.evidence_ids),
                    "source_artifact_ids": list(sources),
                }
            )
        else:
            compteurs["mis_a_jour"] += 1

    return compteurs


def _trier_faits(faits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accepted d'abord, puis stale ; à statut égal, le plus frais d'abord."""
    acceptes = [f for f in faits if f.get("status") == "accepted"]
    stale = [f for f in faits if f.get("status") != "accepted"]
    acceptes.sort(key=lambda f: str(f.get("freshness") or ""), reverse=True)
    stale.sort(key=lambda f: str(f.get("freshness") or ""), reverse=True)
    return acceptes + stale


def construire_synthese(
    db: Any, workspace_id: str, *, inclure_stale: bool = True
) -> dict[str, Any]:
    """Reconstruit la synthèse courante à partir des faits courants.

    Aucune donnée n'est réutilisée d'une synthèse précédente : le texte est
    toujours recalculé depuis les fiches, puis comparé à la courante.

    `inclure_stale=False` réserve la synthèse aux faits toujours acceptés :
    une discussion supprimée ne doit plus nourrir le contexte de l'espace.
    """
    from backend.inference.brief import build_space_synthesis

    statuts = ("accepted", "stale") if inclure_stale else ("accepted",)
    faits = memoire.faits_courants(db, workspace_id, statuts=statuts)
    # Une fiche invalidée par la suppression de sa discussion ne revient JAMAIS
    # dans la synthèse, même quand `inclure_stale=True` : une contradiction
    # reste un fait révisable, une suppression n'est plus une provenance.
    faits = [
        fait
        for fait in faits
        if not (
            fait.get("status") == "stale"
            and fait.get("stale_reason") == "discussion supprimée"
        )
    ]
    faits = _trier_faits(faits)[:MAX_SYNTHESE_FAITS]
    texte = str(build_space_synthesis(faits)["synthese"] or "")
    texte = _tronquer(texte, MAX_SYNTHESE_CHARS)

    courante = memoire.synthese_courante(db, workspace_id)
    if not faits:
        if courante is None:
            return {"statut": "aucune_synthese"}
        texte = str(build_space_synthesis([])["synthese"] or "")
        texte = _tronquer(texte, MAX_SYNTHESE_CHARS)

    if courante is not None and str(courante.get("content") or "") == texte:
        return {"statut": "inchangee"}

    source_entry_ids = [str(f.get("entry_id")) for f in faits if f.get("entry_id")]
    out = memoire.creer_synthese(
        db,
        workspace_id,
        texte,
        source_entry_ids=source_entry_ids,
        idempotency_key=(f"synth:{workspace_id}:{memoire.hash_contenu(texte)[:16]}"),
    )
    # `creer_synthese` peut retrouver une version par sa clé d'idempotence :
    # n'annoncer une création que si la version rendue est bien la courante.
    apres = memoire.synthese_courante(db, workspace_id)
    est_courante = apres is not None and str(apres.get("synthesis_id")) == str(
        out.get("synthesis_id")
    )
    return {
        "statut": "creee" if est_courante else "inchangee",
        "synthesis_id": out.get("synthesis_id"),
        "version": out.get("version"),
    }


def _iso(value: Any) -> str | None:
    """Horodatage ISO : le service mémoire détient déjà cette sérialisation."""
    return memoire._iso(value)


def synthese_pour_ui(db: Any, workspace_id: str) -> dict[str, Any] | None:
    """La synthèse courante sous la forme exacte attendue par l'onglet.

    `synthese_courante` rend un dictionnaire interne ; l'écran ne lit que le
    texte, sa version, sa date et la provenance. Le contrat est nommé ici pour
    que l'IPC n'ait pas à connaître le modèle de la synthèse.
    """
    synthese = memoire.synthese_courante(db, workspace_id)
    if not synthese:
        return None
    return {
        "content": synthese.get("content"),
        "version": synthese.get("version"),
        "created_at": _iso(synthese.get("created_at")),
        "source_entry_ids": synthese.get("source_entry_ids") or [],
    }


def invalider_conversation(db: Any, conversation_id: str) -> dict[str, Any]:
    """Une discussion supprimée ne doit plus soutenir une fiche.

    - retirer sa référence de `origin_context` ;
    - retirer les preuves (`evidence_ids`) des messages de cette discussion ;
    - s'il ne reste aucune autre discussion, marquer la fiche `stale` pour
      suppression — motif distinct d'une contradiction ;
    - reconstruire la synthèse : le filtre sur le motif de suppression écarte
      les fiches privées de provenance, mais une fiche `stale` pour
      contradiction reste un fait révisable et doit y figurer.

    La mise en place est idempotente : le marquage de suppression ne recrée pas
    de version quand la fiche est déjà `stale`, et la référence retirée ne
    revient pas.
    """
    from backend.db.models import (
        Conversation as _Conv,
        DocRef as _DocRef,
        MemoryEntry as _Mem,
        Message as _Msg,
    )

    conv = db.query(_Conv).filter(_Conv.id == conversation_id).first()
    workspace_id = (
        str(conv.workspace_id)
        if conv is not None and conv.workspace_id is not None
        else None
    )
    if not workspace_id:
        return {"ok": True, "statut": "ignore"}

    marqueur = f"discussion:{conversation_id}:"
    message_ids = {
        str(row[0])
        for row in db.query(_Msg.id).filter(_Msg.conv_id == conversation_id).all()
    }
    # Les labels `doc_N` de CETTE discussion sont des preuves locales. On ne
    # retire que ceux qui apparaissent tels quels dans `evidence_ids` ; les
    # `source_artifact_ids` ne sont jamais retirés aveuglément, car le même
    # artefact peut être partagé avec une autre portée.
    # Les preuves `obs_...` ne sont pas encore produites par la mémoire
    # automatique (Phase 8) : on n'en crée pas et on n'en retire pas ici.
    doc_labels = {
        str(row[0])
        for row in db.query(_DocRef.doc_label)
        .filter(_DocRef.conv_id == conversation_id)
        .all()
    }
    # Filtrage Python plutôt que `LIKE` : une discussion dont l'identifiant
    # contient « % » ou « _ » ne doit pas élargir le motif à d'autres fiches.
    fiches = [
        row
        for row in db.query(_Mem)
        .filter(
            _Mem.workspace_id == workspace_id,
            _Mem.is_current == 1,
            _Mem.status != "rejected",
        )
        .all()
        if marqueur in str(row.origin_context or "")
    ]

    examinees = 0
    stalees = 0
    conservees = 0
    for row in fiches:
        examinees += 1
        preuves = [str(e) for e in (row.evidence_ids or [])]
        preuves_restantes = [
            e for e in preuves if e not in message_ids and e not in doc_labels
        ]
        if preuves_restantes != preuves:
            row.evidence_ids = preuves_restantes
        restantes = [
            reference
            for reference in memoire._decouper_references(row.origin_context)
            if marqueur not in reference
        ]
        if restantes:
            # La fiche reste soutenue par au moins une autre discussion : on ne
            # retire que la référence perdue, en place et sans nouvelle version.
            row.origin_context = " ; ".join(restantes)[
                : memoire.MAX_ORIGIN_CONTEXT_CHARS
            ]
            conservees += 1
            continue

        # Plus aucune discussion d'origine. Une source documentaire encore
        # accessible est une provenance suffisante : la fiche garde son statut
        # (provenance partielle). Sinon seulement, la suppression l'invalide.
        # `flush` obligatoire : la session est en `autoflush=False`, et la
        # lecture de portée doit voir le `deleted_at` et les `removed_at` que la
        # mise en corbeille vient de poser dans CETTE transaction.
        db.flush()
        accessibles, erreur_scope = memoire.artefacts_accessibles(db, workspace_id)
        if erreur_scope is not None:
            log.warning(
                "[memoire] portée illisible pour %s pendant la suppression de %s : %s",
                workspace_id,
                conversation_id,
                erreur_scope,
            )
            accessibles = set()
        sources = [str(s) for s in (row.source_artifact_ids or [])]
        if any(source in (accessibles or set()) for source in sources):
            row.origin_context = None
            conservees += 1
            continue

        out = memoire.marquer_stale_suppression(
            db, str(row.id), workspace_id=workspace_id
        )
        if out.get("changed"):
            stalees += 1
        # La version stale hérite du contexte d'origine : retirer la référence
        # de la version courante évite qu'elle survive à l'invalidation.
        courante = memoire._current(db, str(out.get("entry_id") or row.id))
        if courante is not None and marqueur in str(courante.origin_context or ""):
            courante.origin_context = None

    db.commit()
    synthese = construire_synthese(db, workspace_id)
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "fiches_examinees": examinees,
        "fiches_stalees": stalees,
        "fiches_conservees": conservees,
        "synthese": synthese,
    }


def _derniere_activite(ws: Any) -> datetime | None:
    return _vers_datetime(getattr(ws, "last_activity_at", None)) or _vers_datetime(
        getattr(ws, "updated_at", None)
    )


def _inactivite_s(ws: Any, horloge: datetime) -> float | None:
    derniere = _derniere_activite(ws)
    if derniere is None:
        return None
    return max(0.0, (horloge - derniere).total_seconds())


def _payload_fini(
    ws: Any,
    *,
    statut: str,
    raison: str | None,
    duree_ms: int,
    tours: int,
    out: dict[str, Any] | None = None,
    code: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Payload de `memory_finished`, succès comme échec, dans un seul contrat."""
    resultat = out or {}
    return {
        "workspace_id": str(ws.id),
        "workspace_name": str(ws.name),
        "statut": statut,
        "raison": raison,
        "tours": int(resultat.get("tours", tours) or 0),
        "messages": int(resultat.get("messages", 0) or 0),
        "faits": int(resultat.get("faits", 0) or 0),
        "fiches": resultat.get("fiches"),
        "synthese": resultat.get("synthese"),
        "duree_ms": duree_ms,
        "code": code if code is not None else resultat.get("code"),
        "message": message if message is not None else resultat.get("message"),
    }


def entretenir_memoire(
    db: Any,
    workspace_id: str,
    *,
    force: bool = False,
    maintenant: datetime | None = None,
    reservation: Reservation | None = None,
) -> dict[str, Any]:
    """Entretient la mémoire d'un espace : extraction, écriture, synthèse.

    Cœur déterministe appelé UNIQUEMENT par `executer_entretien_memoire` en
    production (le planificateur, le service mémoire et le forçage manuel
    passent tous par le wrapper). Il refuse de tourner pendant un tour actif et
    ne charge aucun modèle.

    Une `reservation` fournie signifie que l'appelant tient déjà le verrou :
    `tour_actif()` n'est alors pas revérifié, mais la réservation doit être une
    réservation mémoire — une réservation chat ne donne pas le droit d'écrire.
    """
    from backend.db.models import Workspace as _Ws

    ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
    if ws is None or ws.state != "active":
        return {"ok": False, "statut": "espace_invalide"}

    if reservation is None:
        if tour_actif():
            return {"ok": False, "statut": "differe", "raison": "tour_actif"}
    elif reservation.kind != "memoire":
        raise ValueError(f"[RESERVATION_KIND] {reservation.kind}")

    horloge = maintenant or _now()
    if not force:
        tours = int(getattr(ws, "auto_memory_turn_count", 0) or 0)
        if tours < SEUIL_TOURS_MEMOIRE:
            return {
                "ok": True,
                "statut": "pas_encore",
                "raison": "seuil_tours",
                "tours": tours,
            }
        derniere = _derniere_activite(ws)
        if (
            derniere is not None
            and (horloge - derniere).total_seconds() < SEUIL_INACTIVITE_S
        ):
            return {
                "ok": True,
                "statut": "pas_encore",
                "raison": "activite_recente",
            }

    messages = collecter_messages(
        db,
        workspace_id,
        depuis=getattr(ws, "memory_last_processed_at", None),
    )
    if not messages:
        if force:
            synthese = construire_synthese(db, workspace_id)
            return {
                "ok": True,
                "statut": "rien_a_promouvoir",
                "synthese": synthese,
            }
        # Le planificateur réveille les espaces dès que le compteur atteint le
        # seuil. Sans nouveaux messages, laisser le compteur au-dessus du seuil
        # relancerait un entretien inutile toutes les 30 secondes : on le remet
        # à zéro pour que l'espace quitte la file jusqu'au prochain tour réussi.
        ws.auto_memory_turn_count = 0
        db.commit()
        return {
            "ok": True,
            "statut": "pas_de_nouveaux_messages",
            "raison": "aucun_message",
        }

    faits = extraire_faits(messages, workspace_id=workspace_id, db=db)
    fiches = appliquer_faits(db, workspace_id, faits)
    synthese = construire_synthese(db, workspace_id)

    ws.memory_last_processed_at = horloge
    ws.auto_memory_turn_count = 0
    db.commit()

    return {
        "ok": True,
        "statut": "effectue",
        "messages": len(messages),
        "faits": len(faits),
        "fiches": fiches,
        "synthese": synthese,
    }


def executer_entretien_memoire(
    db: Any,
    workspace_id: str,
    *,
    force: bool = False,
    mode: str = "auto",
    maintenant: datetime | None = None,
) -> dict[str, Any]:
    """Réserve, émet started, exécute, émet finished, libère toujours.

    Le verrou est binaire et couvre chat, image et mémoire : `timeout=0` signifie
    que la mémoire ne fait jamais attendre un tour. Un refus n'est pas une erreur
    et ne produit pas de `memory_finished` : `memory_deferred` le dit, une seule
    fois tant que l'état reste différé.
    """
    if mode not in ("auto", "force"):
        raise ValueError(f"[MEMORY_MODE] {mode}")

    from backend.db.models import Workspace as _Ws

    ws = db.query(_Ws).filter(_Ws.id == workspace_id).first()
    if ws is None or ws.state != "active":
        return {"ok": False, "statut": "espace_invalide"}

    horloge = maintenant or _now()
    identifiant = str(ws.id)
    tours = int(getattr(ws, "auto_memory_turn_count", 0) or 0)
    inactivite_s = _inactivite_s(ws, horloge)

    res = try_reserver("memoire", f"memoire:{identifiant}", timeout=0.0)
    if res is None:
        bloque_par = etat_activite()["kind"]
        maintenant_mono = time.monotonic()
        with _DERNIER_ETAT_LOCK:
            dernier = _DERNIER_ETAT.get(identifiant)
            deja_differe = (
                dernier is not None
                and dernier[0] == "differe"
                and dernier[1] == bloque_par
                and (maintenant_mono - dernier[2]) < MEMORY_DEFERRED_HEARTBEAT_S
            )
            if not deja_differe:
                _DERNIER_ETAT[identifiant] = ("differe", bloque_par, maintenant_mono)
        if not deja_differe:
            _emit_memory(
                "memory_deferred",
                {
                    "workspace_id": identifiant,
                    "workspace_name": str(ws.name),
                    "statut": "differe",
                    "raison": "verrou_occupe",
                    "bloque_par": bloque_par,
                    "tours": tours,
                    "retry_after_s": 30,
                },
            )
        return {
            "ok": True,
            "statut": "differe",
            "raison": "verrou_occupe",
            "bloque_par": bloque_par,
        }

    _emit_memory(
        "memory_started",
        {
            "request_id": None,
            "workspace_id": identifiant,
            "workspace_name": str(ws.name),
            "kind": "memory",
            "mode": mode,
            "raison": "force" if mode == "force" else "seuil_atteint",
            "tours": tours,
            "inactivite_s": inactivite_s,
            "started_at": _iso(horloge),
        },
    )
    debut = time.monotonic()
    try:
        out = entretenir_memoire(
            db,
            identifiant,
            force=force,
            maintenant=maintenant,
            reservation=res,
        )
    except Exception as exc:
        _emit_memory(
            "memory_finished",
            _payload_fini(
                ws,
                statut="echec",
                raison="exception",
                duree_ms=int((time.monotonic() - debut) * 1000),
                tours=tours,
                code="MEMORY_MAINTENANCE_FAILED",
                message=str(exc)[:500],
            ),
        )
        raise
    else:
        _emit_memory(
            "memory_finished",
            _payload_fini(
                ws,
                statut=str(out.get("statut") or "inconnu"),
                raison=out.get("raison"),
                duree_ms=int((time.monotonic() - debut) * 1000),
                tours=tours,
                out=out,
            ),
        )
    finally:
        liberer(res)
        with _DERNIER_ETAT_LOCK:
            _DERNIER_ETAT.pop(identifiant, None)
    return out


def forcer_memoire_espace(db: Any, workspace_id: str) -> dict[str, Any]:
    """Entretien forcé, sans attendre les seuils de tours ou d'inactivité."""
    return executer_entretien_memoire(db, workspace_id, force=True, mode="force")


def enregistrer_activite_espace(
    db: Any, conversation_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Marque une activité à la RÉCEPTION d'un message utilisateur.

    Le compteur de tours n'est pas touché ici : un message reçu n'est pas un
    tour réussi. Le registre module-level et la colonne durable avancent ensemble.
    """
    from backend.db.models import Workspace as _Ws
    from backend.services import espaces

    wsid = workspace_id or espaces.conversation_workspace(db, conversation_id)
    if not wsid:
        return {"ok": False, "statut": "sans_espace"}
    ws = db.query(_Ws).filter(_Ws.id == wsid).first()
    if ws is None or ws.state != "active":
        return {"ok": False, "statut": "espace_invalide"}

    maintenant = _now()
    ws.last_activity_at = maintenant
    memoire.enregistrer_activite(str(wsid), maintenant)
    db.commit()
    return {"ok": True, "statut": "activite", "workspace_id": str(wsid)}


def finaliser_tour_reussi(
    db: Any, conversation_id: str, workspace_id: str | None = None
) -> dict[str, Any]:
    """Comptabilise un tour RÉUSSI et rafraîchit l'activité de l'espace.

    Jamais depuis `message_completed` : cet événement peut être émis plusieurs
    fois, et un compteur ne rattrape pas un doublon.
    """
    from backend.db.models import Workspace as _Ws
    from backend.services import espaces

    wsid = workspace_id or espaces.conversation_workspace(db, conversation_id)
    if not wsid:
        return {"ok": False, "statut": "sans_espace"}
    ws = db.query(_Ws).filter(_Ws.id == wsid).first()
    if ws is None or ws.state != "active":
        return {"ok": False, "statut": "espace_invalide"}

    maintenant = _now()
    ws.auto_memory_turn_count = int(getattr(ws, "auto_memory_turn_count", 0) or 0) + 1
    ws.last_activity_at = maintenant
    memoire.enregistrer_activite(str(wsid), maintenant)
    db.commit()
    return {
        "ok": True,
        "statut": "tour_compte",
        "workspace_id": str(wsid),
        "tours": int(ws.auto_memory_turn_count),
    }
