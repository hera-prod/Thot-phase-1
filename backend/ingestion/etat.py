"""backend/ingestion/etat.py — l'état d'ingestion d'un artefact, dans la donnée.

D'OÙ VIENT CE FICHIER : d'un défaut mesuré, pas d'une intuition. L'état
d'ingestion n'était DÉDUIT de rien — un lecteur cherchait « une observation
`completed` », et ne trouvait pas, répondait « contenu non extrait ». Un artefact
déposé et jamais traité, un artefact dont l'extraction avait échoué, et un artefact
dont le contenu n'était pas du texte produisaient le MÊME silence. La Bible tranche
sans appel (2.6, « Qualité de l'ingestion ») : « un fichier qui n'a pas été bien
traité ne doit jamais être considéré silencieusement comme correctement traité ».

OÙ VA QUOI : `marquer` est le seul point d'écriture des colonnes
`artifacts.ingest_*`. `lire` et `texte_ingeste` sont le seul chemin de LECTURE de
l'état, et tous les lecteurs passent par là : `reader.read_payload`,
`chunks.best_source_text`, `tools/ask_files`, `services/depot_integrite`. Une seule
source de vérité, appliquée partout — sinon l'état resterait indécidable ailleurs,
ce qui est précisément ce qu'on corrige.

CE QUE CE MODULE GARDE, ET QUOI :

  - UN SEUL ÉCRIVAIN. `marquer` valide l'état (Law 4 : un état inconnu lève, il ne
    se déguise pas en état quelconque) et journalise. Un état écrit ailleurs ne
    serait pas un état.Write dedans-même doit échouerBruyamment, sinon on
    réintroduirait exactement le silence qu'on supprime.
  - LE TEMPS. `age_secondes` est la SEULE façon de mesurer l'âge d'un horodatage
    ici, parce que ce dépôt mêle trois horloges : `func.now()` (naïf UTC),
    `datetime.now(timezone.utc)` (aware) et les chaînes ISO brutes du pont de dépôt.
    Une comparaison naïve entre les deux lève `TypeError` au pire moment — le
    balayage de reprise, qui doit justement pouvoir dire « c'est mort ».
  - LA PHRASE. `phrase_sans_texte` rend la MÊME explication à l'écran, à l'outil et
    au rapport d'intégrité. Une raison qui change selon le lecteur est une raison
    qu'on n'a pas comprise.

LES ÉTATS, et pourquoi ceux-là. Le nom dit CE QUI EST ARRIVÉ AU FICHIER, jamais ce
qu'il reste à faire — c'est ce qui rend `extrait` et `echec` également terminaux,
et `depose` et `en_cours` non terminaux, donc rejouables :

  `depose`         déposé, extraction JAMAIS demandée. C'est l'état d'un dépôt dont
                   personne n'a rien demandé : il vaut une valeur, pas une absence de
                   ligne. C'est aussi ce que vaut un artefact déposé puis laissé par
                   l'extraction refusée faute de modèle — la dette reste visible.
  `en_cours`       extraction demandée, pas encore de verdict.
  `extrait`        texte obtenu, indexé en entier.
  `tronque`        texte obtenu, indexé PARTIELLEMENT (borne de conception, pas une
                   faute) — donc terminal, mais pas « traité » : une partie du
                   document reste hors de portée de la recherche.
  `partiel`        texte obtenu, mais une partie du document n'a pas été reconnue
                   (page en panne, contenu incomplet) : du texte reste utilisable,
                   et l'état n'est donc pas terminal — le rejeu peut le compléter.
  `binaire_refuse` RIEN n'a été indexé : le contenu n'est pas du texte.
  `vide`           extraction menée à terme, aucun texte reconnu. Distinct d'un
                   échec : le fichier est peut-être une photo du vide.
  `echec`          extraction impossible, motif consigné.
  `obsolète`       la SOURCE a été remplacée ou retirée (Décision D5, cycle de vie
                   des informations §3.1) — et non le fichier lui-même : le
                   document reste consultable dans son dernier état connu, CESSE
                   d'être indexé, et n'est plus proposé à la recherche ni aux
                   outils. Sa citation dans une réponse antérieure reste valable :
                   la traçabilité ne s'efface pas, l'état la complète. Une
                   réingestion crée une NOUVELLE ressource (chaque promotion crée
                   un artefact), donc un état neuf, sans altérer le précédent —
                   d'où le nom, qui dit CE QUI EST ARRIVÉ À LA SOURCE, jamais ce
                   qu'il reste à faire.
  `inconnu`        jamais observé depuis l'introduction de ce registre. Ce n'est PAS
                   un défaut : c'est la seule réponse honnête pour ce que la donnée
                   ne dit pas, et elle empêche le backfill de prétendre le contraire.

`inconnu` est ce qui borne le danger que j'assume : un état posé sur l'artefact peut
se dégrader si on oublie de le remettre à jour. Une valeur `inconnu` — lisible,
distincte, et que le balayage de reprise sait traiter — est un oubli VISIBLE. Le
silence, lui, n'a pas de nom : c'est ce qu'on supprime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.ingestion.erreurs import BailPerdu

log = logging.getLogger(__name__)

# Les dix valeurs. Un littéral écrit ailleurs est un bug : `marquer` lève. Les
# accents sont portés par le vocabulaire lui-même (D5 nomme `obsolète`) : la
# normalisation de casse de `marquer` et de `lire` suffit — on n'a jamais remis en
# ASCII un registre qui se lit à l'écran.
ETAT_DEPOSE = "depose"
ETAT_EN_COURS = "en_cours"
ETAT_EXTRAIT = "extrait"
ETAT_TRONQUE = "tronque"
ETAT_PARTIEL = "partiel"
ETAT_BINAIRE = "binaire_refuse"
ETAT_VIDE = "vide"
ETAT_ECHEC = "echec"
ETAT_OBSOLETE = "obsolète"
ETAT_INCONNU = "inconnu"

ETATS: tuple[str, ...] = (
    ETAT_DEPOSE,
    ETAT_EN_COURS,
    ETAT_EXTRAIT,
    ETAT_TRONQUE,
    ETAT_PARTIEL,
    ETAT_BINAIRE,
    ETAT_VIDE,
    ETAT_ECHEC,
    ETAT_OBSOLETE,
    ETAT_INCONNU,
)

# Terminal = plus rien à reprendre sans une action
# humaine. `echec` en fait partie, délibérément : rejouer un échec à chaque démarrage
# ferait boucler une base entière sur un fichier illisible, et un échec SANS issue
# n'apprend rien de plus.
#
# `obsolète` y entre pour la même raison, à un geste près : il est écrit par un ACTE
# de l'utilisateur (le retrait du dernier lien actif), et sa reprise n'existe pas —
# la source n'est plus dans le périmètre, le balayage de reprise n'a rien à rejouer,
# et une réingestion crée une nouvelle ressource avec un état neuf plutôt que de
# réécrire celui-ci. Cf. `reprise.py:ETATS_CIBLES`, qui ne rejoue que `depose`,
# `en_cours` et `inconnu`.
ETATS_TERMINAUX: frozenset[str] = frozenset(
    {ETAT_EXTRAIT, ETAT_TRONQUE, ETAT_BINAIRE, ETAT_VIDE, ETAT_ECHEC, ETAT_OBSOLETE}
)

# Non terminaux : une dette, que le balayage de reprise peut solder.
ETATS_NON_TERMINAUX: frozenset[str] = frozenset({ETAT_DEPOSE, ETAT_EN_COURS})

# Ce que le balayage de reprise peut solder sans demander l'avis de personne.
# `partiel` y entre : une extraction partielle a du texte utilisable, et le
# rejeu doit pouvoir la compléter. Il n'est PAS terminal.
ETATS_REPRENABLES: frozenset[str] = ETATS_NON_TERMINAUX | {ETAT_INCONNU, ETAT_PARTIEL}

# L'extraction a été MÉNÉE et n'a rien donné d'exploitable. `echec` n'y est plus :
# un échec peut suivre un succès, et l'ancien texte doit continuer à être servi.
# `partiel` n'y est pas non plus : il porte du texte, incomplet mais utilisable.
ETATS_SANS_TEXTE: frozenset[str] = frozenset({ETAT_VIDE, ETAT_BINAIRE, ETAT_OBSOLETE})

# États dont un échec peut restaurer le texte après un `en_cours`. La valeur est
# mémorisée dans `artifacts.ingest_previous_state` AVANT le travail, puis relue
# à l'échec : un crash ne doit pas transformer un document déjà lu en `echec`.
ETATS_RESTAURABLES_APRES_ECHEC: frozenset[str] = frozenset(
    {ETAT_EXTRAIT, ETAT_TRONQUE, ETAT_PARTIEL}
)

# TABLE DE TRANSITIONS explicite — DOCUMENTAIRE, PAS APPLIQUÉE.
# `marquer` ne consulte pas cette table : elle valide le vocabulaire, pas la
# légalité du mouvement. La table décrit les transitions que le reste du dépôt
# est censé produire, mais aucun contrôle ne les empêche aujourd'hui. La rendre
# active relèverait d'une décision séparée (et casserait des reprises
# légitimes) ; d'ici là, ce commentaire est le contrat : ne pas la lire comme
# une garde. Un état absent d'une ligne n'est pas un « peut-être » : c'est un
# mouvement qu'aucun chemin de production ne produit.
TRANSITIONS: dict[str, frozenset[str]] = {
    ETAT_DEPOSE: frozenset(
        {
            ETAT_EN_COURS,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_EN_COURS: frozenset(
        {
            ETAT_DEPOSE,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_EXTRAIT: frozenset(
        {
            ETAT_EN_COURS,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_TRONQUE: frozenset(
        {
            ETAT_EN_COURS,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_PARTIEL: frozenset(
        {
            ETAT_EN_COURS,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_VIDE: frozenset(
        {
            ETAT_EN_COURS,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_BINAIRE: frozenset(
        {
            ETAT_EN_COURS,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_ECHEC: frozenset(
        {
            ETAT_EN_COURS,
            ETAT_EXTRAIT,
            ETAT_TRONQUE,
            ETAT_PARTIEL,
            ETAT_VIDE,
            ETAT_BINAIRE,
            ETAT_ECHEC,
            ETAT_OBSOLETE,
        }
    ),
    ETAT_OBSOLETE: frozenset({ETAT_OBSOLETE}),
    ETAT_INCONNU: frozenset(ETATS),
}

# Ce que le rapport d'intégrité appelle « extraction absente ». `binaire_refuse` en
# est EXCLU, et c'est délibéré : il a sa propre anomalie, `extraction_binaire`.
# Un état qui déclencherait deux anomalies gonflerait le total du rapport et ferait
# apparaître un dépôt plus abîmé qu'il ne l'est — donc un rapport qui ment, et c'est
# précisément ce qu'on répare. UN ÉTAT, UNE ANOMALIE.
#
# `depose` et `en_cours` n'y sont pas non plus : ce sont des DETTES que le balayage de
# reprise solde, pas des défauts constatés. `inconnu` pas davantage : c'est une absence
# d'information, et aucune donnée ne permet de la lever en faute.
ETATS_EXTRACTION_NULLE: frozenset[str] = frozenset({ETAT_VIDE, ETAT_ECHEC})

# Backoff de reprise (tâche 2.2) : délais entre deux tentatives, en minutes.
# Pas de jitter : le dépôt sérialise déjà les extractions, et un délai lisible
# vaut mieux qu'une dispersion qu'aucun opérateur ne peut prévoir. Le plafond
# est le nombre de tentatives COMPTÉES, pas l'index du dernier délai.
BACKOFF_MINUTES: tuple[int, ...] = (1, 2, 5, 15, 30)
PLAFOND_ESSAIS = 5


def delai_backoff(index: int) -> int:
    """Minutes à attendre pour un index de reprise 0-based. Pur.

    L'index est BORNÉ plutôt que refusé : un index négatif (tentative annulée)
    ou au-delà de la table ne doit pas lever au milieu d'une écriture de dossier.
    """
    borne = min(max(0, int(index)), len(BACKOFF_MINUTES) - 1)
    return BACKOFF_MINUTES[borne]


# Les phrases. Elles sont ÉCRITES ICI et nulle part ailleurs : trois lecteurs qui
# formulent l'explication différemment produisent trois verdicts sur un même fichier.
PHRASES: dict[str, str] = {
    ETAT_DEPOSE: "extraction jamais demandée pour ce fichier",
    ETAT_EN_COURS: "extraction en cours, aucun verdict pour l'instant",
    ETAT_VIDE: "extraction menée à terme, aucun texte reconnu dans ce fichier",
    ETAT_BINAIRE: "contenu binaire refusé à l'indexation : ce fichier n'est pas du texte",
    ETAT_ECHEC: "extraction impossible",
    ETAT_TRONQUE: "texte extrait mais tronqué à la borne d'indexation",
    ETAT_PARTIEL: (
        "texte extrait mais incomplet : une partie du document n'a pas été reconnue"
    ),
    ETAT_OBSOLETE: (
        "source remplacée ou retirée : ce document n'est plus proposé "
        "à la recherche ni aux outils"
    ),
    ETAT_INCONNU: "état d'ingestion inconnu : ce fichier n'a pas été observé depuis "
    "l'introduction du relevé d'ingestion",
}
PHRASE_DE_REPLI = "état d'ingestion illisible : la donnée est incohérente"

# Les outils dont l'observation porte le TEXTE d'un fichier. Cette liste est la
# définition de « une extraction » : hors de cette liste, une observation prouve
# qu'un outil a tourné, pas qu'un fichier a été lu.
OUTILS_EXTRACTION: tuple[str, ...] = (
    "attachment_ingest",
    "doc_ocr",
    "read_text",
    "read_document",
)


class EtatInconnu(RuntimeError):
    """Un état d'ingestion qui n'appartient pas au vocabulaire.

    Elle porte un nom parce qu'un état écrit à côté ne serait pas un état : le
    rapport d'intégrité le lirait comme un défaut et l'écran ne saurait pas le dire.
    """

    def __init__(self, etat: object) -> None:
        super().__init__(
            f"état d'ingestion inconnu : {etat!r}. Valeurs autorisées : {', '.join(ETATS)}"
        )
        self.etat = etat


class ArtefactAbsent(RuntimeError):
    """L'artefact à marquer n'existe pas : aucune ligne n'a été écrite.

    Un no-op silencieux rendait l'appelant convaincu d'avoir écrit un état qui
    n'existe nulle part. Lever rend l'absence visible et laisse `marquer_une_fois`
    la journaliser.
    """

    def __init__(self, artifact_id: str) -> None:
        super().__init__(
            f"artefact absent : impossible d'écrire l'état d'ingestion de "
            f"{artifact_id!r} (aucune ligne mise à jour)"
        )
        self.artifact_id = artifact_id


class VerdictInconnu(RuntimeError):
    """Un verdict d'indexation qui ne se traduit vers aucun état d'ingestion."""

    def __init__(self, verdict: object) -> None:
        super().__init__(
            f"verdict d'indexation inconnu : {verdict!r}. Valeurs attendues : "
            "indexe, tronque, binaire_refuse"
        )
        self.verdict = verdict


@dataclass(frozen=True)
class Etat:
    """L'état d'un artefact, PARSÉ. Un objet lu ne peut plus être un état fantaisiste.

    `inconnu` est le repli, et il est intentionnel : une colonne absente (base
    antérieure à la migration), nulle, ou illisible ne doit jamais se lire comme un
    état favorable. Elle se lit comme une IGNORANCE, qui est ce qu'elle est.
    """

    etat: str
    raison: str
    maj_le: datetime | None

    @property
    def est_terminal(self) -> bool:
        return self.etat in ETATS_TERMINAUX

    @property
    def est_reprenable(self) -> bool:
        return self.etat in ETATS_REPRENABLES

    @property
    def a_texte(self) -> bool:
        return self.etat in (ETAT_EXTRAIT, ETAT_TRONQUE, ETAT_PARTIEL)

    @property
    def phrase(self) -> str:
        """L'explication lisible, la raison d'abord quand il y en a une."""
        base = PHRASES.get(self.etat, PHRASE_DE_REPLI)
        return f"{self.raison} ({base})" if self.raison else base


def lire(artifact) -> Etat:
    """L'état d'un artefact déjà chargé. Pur, ne lève jamais.

    Les colonnes sont lues par `getattr` et non par attribut : un artefact d'une base
    antérieure à la migration — ou d'un appelant qui a construit un objet à la main —
    n'a pas ces attributs. Une lecture qui lève ferait tomber le rapport d'intégrité
    pour un motif qui n'est pas une anomalie du dépôt.
    """
    brut = (getattr(artifact, "ingest_state", None) or "").strip().lower()
    etat = brut if brut in ETATS else ETAT_INCONNU
    raison = str(getattr(artifact, "ingest_reason", None) or "").strip()
    maj = getattr(artifact, "ingest_updated_at", None)
    return Etat(
        etat=etat, raison=raison, maj_le=maj if isinstance(maj, datetime) else None
    )


def marquer(
    session,
    artifact_id: str,
    etat: str,
    raison: str = "",
    *,
    claim_token: str | None = None,
) -> str:
    """Écrit l'état d'ingestion d'un artefact. LE SEUL point d'écriture. Committe.

    LEVE `EtatInconnu` sur un état hors vocabulaire, et laisse remonter l'échec
    d'écriture : une trace d'ingestion qui laisse son état derrière elle est une
    trace muette. C'est une exception à la règle « un enrichissement ne lève jamais »,
    et elle est justifiée par le fait que CET enrichissement EST la donnée.

    FENCING (Phase 3, tâche 3.3) : quand `claim_token` est fourni, l'UPDATE est
    conditionné au claim encore détenu. Zéro ligne mise à jour signifie que le
    bail a été perdu : `BailPerdu` est levée, et l'appelant doit s'arrêter sans
    écrire davantage. La condition est dans la MÊME requête que l'écriture :
    aucune fenêtre entre la vérification et le changement d'état.

    La raison est BORNÉE et jamais un code : c'est elle que l'écran, l'outil et le
    rapport afficheront. Une raison tronquée qui perd sa fin devient une accusation
    sans motif.
    """
    etat = str(etat or "").strip().lower()
    if etat not in ETATS:
        raise EtatInconnu(etat)
    raison = " ".join(str(raison or "").split())[:400]
    from backend.db.models import Artifact as _Art

    artefact = session.query(_Art).filter(_Art.id == artifact_id).first()
    if artefact is None:
        # Jamais de faux succès : `update` sur zéro ligne ne dit rien et
        # l'appelant repartait avec l'état demandé comme s'il était écrit.
        session.rollback()
        if claim_token:
            raise BailPerdu(artifact_id, claim_token)
        raise ArtefactAbsent(artifact_id)
    precedent = (getattr(artefact, "ingest_state", None) or "").strip().lower()
    precedent = precedent if precedent in ETATS else ETAT_INCONNU
    memorise = (getattr(artefact, "ingest_previous_state", None) or "").strip().lower()
    if memorise not in ETATS_RESTAURABLES_APRES_ECHEC:
        memorise = ""

    changement: dict[str, object] = {
        "ingest_state": etat,
        "ingest_reason": raison or None,
        "ingest_updated_at": utc_naif(),
    }

    if etat == ETAT_EN_COURS:
        # On mémorise l'état d'AVANT seulement s'il n'est pas déjà `en_cours`.
        # Un second signalement de démarrage ne doit pas écraser l'état d'origine :
        # c'est cet état-là que l'échec devra restaurer.
        if precedent != ETAT_EN_COURS:
            changement["ingest_previous_state"] = precedent
    elif etat == ETAT_ECHEC and memorise:
        # Un échec APRÈS un texte déjà extrait ne détruit pas ce texte : le
        # document reste lisible dans son dernier état utile. L'échec est inscrit
        # dans la raison, mais la valeur servie reste `extrait`/`tronque`/`partiel`.
        changement["ingest_state"] = memorise
        changement["ingest_previous_state"] = None
    else:
        changement["ingest_previous_state"] = None

    requete = session.query(_Art).filter(_Art.id == artifact_id)
    if claim_token:
        requete = requete.filter(_Art.ingest_claim_token == claim_token)
    lignes = requete.update(
        changement,
        synchronize_session=False,
    )
    if not lignes:
        # Le claim a été perdu entre la lecture et l'écriture : zéro ligne
        # conditionnelle. On annule la transaction et on signale la perte.
        session.rollback()
        if claim_token:
            raise BailPerdu(artifact_id, claim_token)
        raise ArtefactAbsent(artifact_id)
    session.commit()
    return str(changement["ingest_state"])


def marquer_une_fois(
    session,
    artifact_id: str,
    etat: str,
    raison: str = "",
    *,
    claim_token: str | None = None,
) -> str:
    """Comme `marquer`, mais journalise l'échec au lieu de le remonter.

    Pour les appelants dont le VRAI travail est déjà fait : l'extraction a réussi,
    son observation est écrite, et il ne reste que le relevé d'état. Perdre ce relevé
    doit se voir dans le journal, pas faire échouer une ingestion dont le résultat
    existe. La ligne de journal nomme l'artefact, sans quoi elle ne sert à rien.

    FENCING : `BailPerdu` n'est JAMAIS avalée. Une perte de claim doit arrêter le
    worker, pas se contenter d'un avertissement.
    """
    try:
        return marquer(session, artifact_id, etat, raison, claim_token=claim_token)
    except BailPerdu:
        raise
    except Exception as exc:
        log.warning(
            "[ingestion] état %s non écrit pour %s : %s: %s",
            etat,
            artifact_id,
            type(exc).__name__,
            exc,
        )
        return ""


def demarrer_tentative(
    session,
    artifact_id: str,
    *,
    maintenant: datetime | None = None,
    claim_token: str | None = None,
) -> int:
    """Compte une tentative et horodate son début. Committe, rend le numéro.

    L'incrément est fait AVANT le travail : une tentative interrompue par un
    crash doit avoir été comptée, sinon un artefact qui tue le processus
    bouclerait indéfiniment sans jamais atteindre le plafond.

    FENCING : avec `claim_token`, l'incrément n'est écrit que si le bail est
    encore détenu. Un worker expiré ne doit pas consommer le plafond d'essais
    d'un artefact repris par un autre.
    """
    from backend.db.models import Artifact as _Art

    requete = session.query(_Art).filter(_Art.id == artifact_id)
    if claim_token:
        requete = requete.filter(_Art.ingest_claim_token == claim_token)
    artefact = requete.first()
    if artefact is None:
        session.rollback()
        if claim_token:
            raise BailPerdu(artifact_id, claim_token)
        raise ArtefactAbsent(artifact_id)
    tentatives = int(getattr(artefact, "ingest_attempts", 0) or 0) + 1
    lignes = requete.update(
        {
            "ingest_attempts": tentatives,
            "ingest_last_attempt_at": utc_naif(maintenant),
            "ingest_heartbeat_at": utc_naif(maintenant),
        },
        synchronize_session=False,
    )
    if not lignes:
        session.rollback()
        if claim_token:
            raise BailPerdu(artifact_id, claim_token)
        raise ArtefactAbsent(artifact_id)
    session.commit()
    return tentatives


def planifier_tentative(
    session,
    artifact_id: str,
    tentatives: int,
    *,
    maintenant: datetime | None = None,
    claim_token: str | None = None,
) -> datetime | None:
    """Fixe `ingest_next_attempt_at` selon le backoff. Committe.

    Au plafond, la prochaine date est NULL et l'appelant doit avoir choisi un
    état TERMINAL : planifier sans état terminal rendrait la dette invisible
    au balayage, donc éternelle. Cette fonction ÉCRIT explicitement NULL au
    plafond — ne pas l'appeler laisserait une échéance antérieure en place.

    L'index est `tentatives - 1` : la première tentative échouée attend la
    première valeur du backoff (1 min), pas la deuxième.

    FENCING : avec `claim_token`, l'échéance n'est écrite que si le bail est
    encore détenu. Zéro ligne conditionnelle lève `BailPerdu`.
    """
    from backend.db.models import Artifact as _Art

    if tentatives >= PLAFOND_ESSAIS:
        prochaine: datetime | None = None
    else:
        minutes = delai_backoff(int(tentatives) - 1)
        prochaine = utc_naif(maintenant) + timedelta(minutes=minutes)
    requete = session.query(_Art).filter(_Art.id == artifact_id)
    if claim_token:
        requete = requete.filter(_Art.ingest_claim_token == claim_token)
    lignes = requete.update(
        {"ingest_next_attempt_at": prochaine},
        synchronize_session=False,
    )
    if not lignes and claim_token:
        session.rollback()
        raise BailPerdu(artifact_id, claim_token)
    session.commit()
    return prochaine


def annuler_tentative(
    session, artifact_id: str, *, claim_token: str | None = None
) -> int:
    """Annule une tentative qui n'a pas exécuté de travail réel. Committe.

    Un refus CONSENTI (`ModeleRequis`) ou une admission refusée (`ResourceBusy`)
    ne doit pas consommer le plafond : sans cela, cinq démarrages sans modèle
    transformeraient un PDF scanné jamais tenté en `echec` terminal. On défait
    donc le compteur posé avant le travail, et la date de dernière tentative.

    Rend le nombre de tentatives restantes. Plancher à zéro : deux annulations
    concurrentes ne doivent pas produire un compteur négatif.

    FENCING : avec `claim_token`, l'annulation n'est écrite que si le bail est
    encore détenu. Zéro ligne conditionnelle lève `BailPerdu`.
    """
    from backend.db.models import Artifact as _Art

    requete = session.query(_Art).filter(_Art.id == artifact_id)
    if claim_token:
        requete = requete.filter(_Art.ingest_claim_token == claim_token)
    artefact = requete.first()
    if artefact is None:
        session.rollback()
        if claim_token:
            raise BailPerdu(artifact_id, claim_token)
        raise ArtefactAbsent(artifact_id)
    tentatives = max(0, int(getattr(artefact, "ingest_attempts", 0) or 0) - 1)
    lignes = requete.update(
        {
            "ingest_attempts": tentatives,
            "ingest_last_attempt_at": None,
        },
        synchronize_session=False,
    )
    if not lignes:
        session.rollback()
        if claim_token:
            raise BailPerdu(artifact_id, claim_token)
        raise ArtefactAbsent(artifact_id)
    session.commit()
    return tentatives


def reinitialiser_essais(
    session, artifact_id: str, *, claim_token: str | None = None
) -> None:
    """Remet le compteur à zéro après un succès complet. Committe.

    FENCING : avec `claim_token`, le reset n'est écrit que si le bail est encore
    détenu. Zéro ligne conditionnelle lève `BailPerdu`.
    """
    from backend.db.models import Artifact as _Art

    requete = session.query(_Art).filter(_Art.id == artifact_id)
    if claim_token:
        requete = requete.filter(_Art.ingest_claim_token == claim_token)
    lignes = requete.update(
        {"ingest_attempts": 0, "ingest_next_attempt_at": None},
        synchronize_session=False,
    )
    if not lignes and claim_token:
        session.rollback()
        raise BailPerdu(artifact_id, claim_token)
    session.commit()


def battre_heartbeat(
    session,
    artifact_id: str,
    *,
    maintenant: datetime | None = None,
    claim_token: str | None = None,
) -> bool:
    """Horodate la vie de l'extraction en cours. Committe. Rend `True` si écrit.

    Une écriture de liveness n'est pas un changement d'état : elle ne touche
    pas `ingest_state` et ne doit donc pas passer par `marquer`.

    FENCING : avec `claim_token`, le battement n'est écrit que si le bail est
    encore détenu. Le TTL du claim est renouvelé en même temps (heartbeat 60 s,
    TTL 5 min). `False` signale une perte de bail — le worker doit s'arrêter.
    """
    from backend.db.models import Artifact as _Art

    requete = session.query(_Art).filter(_Art.id == artifact_id)
    if claim_token:
        requete = requete.filter(_Art.ingest_claim_token == claim_token)
    instant = utc_naif(maintenant)
    changement: dict[str, object] = {"ingest_heartbeat_at": instant}
    if claim_token:
        # Le renouvellement du TTL fait partie du même battement : deux écritures
        # séparées laisseraient une fenêtre où le heartbeat est frais mais le
        # claim est déjà expiré.
        changement["ingest_claim_at"] = instant
    lignes = requete.update(changement, synchronize_session=False)
    session.commit()
    return bool(lignes)


def phrase_sans_texte(artifact) -> str:
    """L'explication d'un artefact dont on n'a rien à lire. Ne lève jamais.

    Un état qui AFFIRME avoir extrait du texte alors qu'aucune observation ne le
    corrobore est traité comme une absence : on ne sert pas une promesse de texte
    qu'aucune donnée ne soutient.
    """
    etat = lire(artifact)
    if etat.a_texte:
        return PHRASE_DE_REPLI
    return etat.phrase


def texte_ingeste(
    session, artifact, *, limite: int = 5
) -> tuple[str | None, str | None]:
    """(texte, observation_id) de la DERNIÈRE extraction RÉUSSIE — lecture unique.

    Une seule implémentation de « ce fichier a-t-il été lu ». Elle existait trois
    fois, avec trois listes d'outils différentes : `reader` ignorait `read_text`,
    `chunks` ignorait l'ordre de tri d'un côté, `ask_files` prenait le dernier plutôt
    que le meilleur. Trois réponses à une question unique, donc trois verdicts
    possibles sur le même fichier — l'indécision qu'on supprime.

    On ne juge QUE les observations SUCCEITES et non vides : une extraction qui a
    échoué ou qui n'a rien reconnu ne laisse pas de texte, et le dire par son absence
    faisait perdre la raison de l'échec. C'est l'état porté (`phrase_sans_texte`) qui
    la rend, désormais.

    `limite` borne la lecture : on ne va pas remonter l'historique entier d'un
    artefact pour trouver un texte que cinq observations suffisent à caractériser.
    """
    from backend.db.models import Observation as _Obs
    from sqlalchemy import text as _sql_text

    if artifact is None:
        return None, None
    # La GARDE D'ÉTAT passe AVANT toute lecture d'observation : un artefact dont
    # la source est retirée (`obsolète`), dont le contenu n'est pas du texte
    # (`binaire_refuse`) ou dont l'extraction n'a rien reconnu (`vide`) ne doit
    # pas servir un ancien `completed` resté en base. `echec` et `partiel` sont
    # volontairement absents de cet ensemble : un échec peut suivre un succès,
    # et l'ancien texte doit rester lisible.
    if lire(artifact).etat in ETATS_SANS_TEXTE:
        return None, None
    try:
        lignes = (
            session.query(_Obs)
            .filter(
                _Obs.artifact_id == artifact.id,
                _Obs.status == "completed",
                _Obs.tool_name.in_(OUTILS_EXTRACTION),
            )
            # Ordre TOTAL : SQLite ne date qu'à la seconde, donc `created_at`
            # seul ne dit pas lequel est le dernier. `rowid` départage, et c'est
            # la même règle que la rétention.
            .order_by(_Obs.created_at.desc(), _sql_text("observations.rowid DESC"))
            .limit(max(1, int(limite)))
            .all()
        )
    except Exception as exc:
        log.warning(
            "[ingestion] texte de %s illisible : %s: %s",
            getattr(artifact, "id", "?"),
            type(exc).__name__,
            exc,
        )
        return None, None
    for observation in lignes:
        texte = (observation.payload_ref or "").strip()
        if texte:
            return texte, observation.id
    return None, None


def verdict_vers_etat(verdict: dict) -> str:
    """L'état d'ingestion qu'IMPLIQUE un verdict d'indexation (`fts.arbitrer_extrait`).

    Le juge des octets reste le juge des octets : il ne connaît ni l'historique du
    fichier ni les autres extractions, donc il ne décide d'aucun statut. Il rend un
    verdict, et cette fonction le traduit dans le vocabulaire du relevé — le seul
    endroit où la traduction a lieu, pour qu'il n'y ait jamais deux tables de
    correspondance concurrentes.
    """
    from backend.search import fts as _fts

    extract = str((verdict or {}).get("extract") or "")
    correspondance = {
        _fts.EXTRACT_INDEXE: ETAT_EXTRAIT,
        _fts.EXTRACT_TRONQUE: ETAT_TRONQUE,
        _fts.EXTRACT_BINAIRE: ETAT_BINAIRE,
    }
    etat = correspondance.get(extract)
    if etat is None:
        # Un verdict inconnu ne doit pas devenir l'état le plus favorable
        # (`extrait`) : l'absence de traduction est un bug d'appelant, pas une
        # extraction complète.
        raise VerdictInconnu(verdict)
    return etat


def utc_naif(maintenant: datetime | None = None) -> datetime:
    """UTC NAÏF. Le format que `func.now()` produit, donc le seul comparable."""
    base = maintenant or datetime.now(timezone.utc)
    if base.tzinfo is None:
        return base
    return base.astimezone(timezone.utc).replace(tzinfo=None)


def age_secondes(horodatage, *, maintenant: datetime | None = None) -> float | None:
    """Secondes écoulées depuis `horodatage`, ou `None` si on ne peut pas le dire.

    `None` — et non 0 — parce que 0 dirait « ça vient de commencer » alors qu'on ne
    sait rien du tout. C'est la différence entre un état mesuré et un état supposé, et
    le balayage de reprise en dépend : un âge qu'il ne sait pas mesurer doit le dire.

    UNE CHAÎNE EST ACCEPTÉE, et c'est nécessaire : une requête écrite en SQL brut ne
    reçoit pas le type `DateTime` de la colonne, et SQLite rend alors du `str`. Sans
    cette conversion, tout `en_cours` lui semblerait sans date — donc jamais périmé,
    donc jamais repris, c'est-à-dire jamais rattrapé.
    """
    instant = horodatage
    if isinstance(instant, str):
        try:
            instant = datetime.fromisoformat(instant.strip())
        except ValueError:
            return None
    if not isinstance(instant, datetime):
        return None
    if instant.tzinfo is not None:
        instant = instant.astimezone(timezone.utc).replace(tzinfo=None)
    return (utc_naif(maintenant) - instant).total_seconds()


__all__ = [
    "BACKOFF_MINUTES",
    "PLAFOND_ESSAIS",
    "ETATS",
    "ETATS_EXTRACTION_NULLE",
    "ETATS_NON_TERMINAUX",
    "ETATS_REPRENABLES",
    "ETATS_RESTAURABLES_APRES_ECHEC",
    "ETATS_SANS_TEXTE",
    "ETATS_TERMINAUX",
    "TRANSITIONS",
    "ETAT_BINAIRE",
    "ETAT_DEPOSE",
    "ETAT_ECHEC",
    "ETAT_EN_COURS",
    "ETAT_EXTRAIT",
    "ETAT_INCONNU",
    "ETAT_OBSOLETE",
    "ETAT_PARTIEL",
    "ETAT_TRONQUE",
    "ETAT_VIDE",
    "ArtefactAbsent",
    "Etat",
    "EtatInconnu",
    "annuler_tentative",
    "delai_backoff",
    "OUTILS_EXTRACTION",
    "VerdictInconnu",
    "age_secondes",
    "battre_heartbeat",
    "demarrer_tentative",
    "lire",
    "marquer",
    "marquer_une_fois",
    "phrase_sans_texte",
    "planifier_tentative",
    "reinitialiser_essais",
    "texte_ingeste",
    "utc_naif",
    "verdict_vers_etat",
]
