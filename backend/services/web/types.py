"""Recherche web — couche 0 : types et codes.

Ce module ne fait **aucun I/O**. Il fixe la forme des données qui circulent
entre le fetcher (qui télécharge), les moteurs (qui cherchent) et
`research.py` (qui décide). Un seul propriétaire, donc un seul jeu de
noms : c'est la raison d'être du fichier.

Trois décisions structurantes viennent de la conception (C15, C16, C18) et
doivent rester vraies quelles que soient les implémentations :

1. **Le texte web est une donnée, jamais une consigne.** `WebPage.text` est
   destiné à être encadré comme non fiable par le rendu, jamais fusionné au
   prompt système. C'est pourquoi il voyage dans un objet à part, et non
   dans un `dict` libre que n'importe qui reformaterait.
2. **Le cache donne une économie de requêtes, pas une garantie de
   fraîcheur.** D'où `FetchedAt`/`PublishedAt` conservés séparément, et
   `WebPage.content_sha256` (hash du CONTENU, jamais de l'URL — C16).
3. **Ce qui n'a pas été lu se dit.** `SkippedItem` existe pour que le rendu
   puisse expliquer un trou, sinon le modèle refait la même recherche.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

# --------------------------------------------------------------------------
# Codes locaux
# --------------------------------------------------------------------------
# Les 14 codes du registre (`backend/tools/errors.py`) sont un vocabulaire
# FERMÉ, épinglé par `test_specs_01_02_03.py`. Les cas propres au web —
# réseau, robots, consentement, fournisseur — n'y ont pas de place, et les
# ajouter casserait un contrat d'outil local. On prend donc la voie des
# codes LOCAUX, comme `generate_image` l'a fait pour `SERVICE_UNAVAILABLE` :
# même syntaxe (`[CODE] message`), jamais de membre ajouté à l'enum.

WEB_DISABLED = "WEB_DISABLED"          # mode local_only : réseau fermé, cache rendu
NO_PROVIDER = "NO_PROVIDER"            # aucun moteur applicable : échec BRUYANT
ROBOTS_DENIED = "ROBOTS_DENIED"        # page interdite par robots.txt
NETWORK_BLOCKED = "NETWORK_BLOCKED"    # cible privée, schéma interdit, redirection refusée
CONSENT_REQUIRED = "CONSENT_REQUIRED"  # transfert de donnée privée nécessaire
URL_NOT_ADMITTED = "URL_NOT_ADMITTED"  # URL absente du message utilisateur ou d'un résultat antérieur

WEB_LOCAL_CODES: tuple[str, ...] = (
    WEB_DISABLED, NO_PROVIDER, ROBOTS_DENIED, NETWORK_BLOCKED,
    CONSENT_REQUIRED, URL_NOT_ADMITTED,
)

# Motifs de `SkippedItem.reason` — vocabulaire fermé, le rendu les affiche tels quels.
SKIP_ROBOTS = "robots"                 # robots.txt interdit
SKIP_NETWORK = "network_blocked"       # schéma / IP / redirection / non admis
SKIP_TOO_LARGE = "too_large"           # page au-delà du plafond
SKIP_TIMEOUT = "timeout"               # délai dépassé
SKIP_BUDGET = "budget"                 # plafond de pages ou d'octets du tour atteint
SKIP_DUPLICATE = "duplicate"           # même URL déjà lue dans cet appel
SKIP_NOT_SELECTED = "not_selected"     # résultat found, non retenu (budget de pages)
SKIP_NOT_APPLICABLE = "not_applicable" # aucun moteur ne couvre ce type de source
SKIP_DOMAIN = "domain_not_allowed"     # hôte hors de la liste blanche des domaines

SKIP_REASONS: tuple[str, ...] = (
    SKIP_ROBOTS, SKIP_NETWORK, SKIP_TOO_LARGE, SKIP_TIMEOUT,
    SKIP_BUDGET, SKIP_DUPLICATE, SKIP_NOT_SELECTED, SKIP_NOT_APPLICABLE,
    SKIP_DOMAIN,
)

# Types de source acceptés par l'argument `type` de l'outil. L'énumération est
# fermée : `ENUM_INVALID` hors liste. `official` n'est pas une classe de moteur
# mais une exigence de provenance, servie par le chemin URL.
SOURCE_TYPES: tuple[str, ...] = ("general", "news", "academic", "official")

# Le vocabulaire de fraîcheur vient de la fiche web_research §2.1 (v1.2) :
# `reuse_if_sufficient` — réutiliser un contenu conservé si suffisant — et
# `verify_current` — vérifier dehors, un refus réseau s'énonçant plutôt que
# d'être masqué par un contenu ancien. Les valeurs antérieures (`cached`,
# `recent`) ne sont reconduites pour personne : ce sont deux mots pour une
# distinction que la fiche ne reprend pas.
FRESHNESS_REUSE = "reuse_if_sufficient"
FRESHNESS_VERIFY = "verify_current"
FRESHNESS_VALUES: tuple[str, ...] = (FRESHNESS_REUSE, FRESHNESS_VERIFY)


# --------------------------------------------------------------------------
# Requête
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WebQuery:
    """Une requête normalisée, prête pour un moteur.

    `text` est déjà passée par la réduction C15 (« ne pas envoyer
    d'extrait privé ») : ce qui arrive ici est ce qui SORTIRA de la machine.
    """
    text: str
    source_type: str = "general"
    freshness: str = FRESHNESS_REUSE

    def cache_key(self) -> str:
        """La clé identifie la DEMANDE (type de source + texte), pas la politique de fraîcheur.

        La fraîcheur est réévaluée À CHAQUE ACCÈS par `cache.freshness_s`
        (RFC 9111 §4.2.2), et le choix « réutiliser / vérifier » est appliqué à
        la lecture, dans `research._traiter_requete` — jamais figé dans la clé.
        Encadrer la politique dans la clé ferait qu'un appel `verify_current`
        écrit sous une clé que `reuse_if_sufficient` ne relirait jamais : le
        cache de conversation perdrait très précisément son sens. Tradition
        conservée pour les valeurs antérieures : les anciens appels écrivaient
        sous d'autres clés (« general|cached|… ») ; la rétention de
        conversation les fait expirer naturellement, la translation valait
        purge douce.
        """
        return f"{self.source_type}|{self.text.strip().lower()}"


# --------------------------------------------------------------------------
# Résultat proposé par un moteur (page PAS encore téléchargée)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WebHit:
    """Un résultat de recherche. Ce n'est PAS une preuve : c'est une adresse.

    `provider` est le moteur qui a proposé l'URL, jamais celui qui l'a
    certifiée. `coverage` dit en clair ce que ce moteur couvre réellement
    (« encyclopédique », « articles scientifiques », « web général via
    instance auto-hébergée ») : sans cette phrase, un résultat
    encyclopédique se lirait comme « le web », et c'est le mensonge que la
    fiche interdit.
    """
    url: str
    title: str = ""
    snippet: str = ""
    provider: str = ""
    coverage: str = ""
    rank: int = 0
    published_at: str | None = None


# --------------------------------------------------------------------------
# Page effectivement téléchargée
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WebPage:
    """Une page lue, extraite, datée. Les champs suivent C16.

    `final_url == url` par construction : le fetcher ne suit AUCUNE
    redirection (C18), donc il n'y a pas de « destination » à revalider
    après coup. Le champ reste, parce que C16 exige les deux et qu'un jour
    une redirection autorisée deviendrait possible.
    """
    url: str
    final_url: str
    status: int
    content_type: str
    content_sha256: str
    text: str
    extractor_version: str
    fetched_at: datetime
    published_at: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    title: str = ""
    source_type: str = "general"
    proposed_by: str = ""      # moteur qui a proposé l'URL, pour la traçabilité

    @property
    def age_days(self) -> float:
        return max(0.0, (datetime.now() - self.fetched_at).total_seconds() / 86400.0)


# --------------------------------------------------------------------------
# Ce qui a été écarté, et pourquoi
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SkippedItem:
    url: str | None
    reason: str          # SKIP_REASONS
    detail: str = ""


# --------------------------------------------------------------------------
# Réponse d'un moteur
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderAnswer:
    provider: str
    coverage: str
    hits: tuple[WebHit, ...] = ()
    skipped: tuple[SkippedItem, ...] = ()
    error: str | None = None       # code local, jamais une exception

    @property
    def ok(self) -> bool:
        return self.error is None


# --------------------------------------------------------------------------
# Erreur portant un CODE
# --------------------------------------------------------------------------
class WebError(Exception):
    """Erreur dont le message porte déjà un `[CODE]` local.

    Le code voyage dans le texte, comme le veut le motif `generate_image` :
    `base.py` le ressort tel quel, sans table à étendre.
    """
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Protocoles
# --------------------------------------------------------------------------
class WebProvider(Protocol):
    """Un moteur de recherche. Implémenté par `providers/*`.

    Un moteur ne télécharge JAMAIS de page : il ne rend que des `WebHit`.
    C'est ce qui rend impossible pour un moteur de contourner le fetcher.
    """
    name: str
    covers: frozenset[str]        # parmi SOURCE_TYPES
    coverage: str                 # phrase honnête, rendue au modèle

    def search(
        self,
        query: WebQuery,
        *,
        max_results: int,
        deadline: float,
    ) -> ProviderAnswer:
        ...


class PageFetcher(Protocol):
    """Téléchargeur fermé (C18). Implémenté par `fetcher.Fetcher`."""
    def fetch(
        self,
        url: str,
        *,
        deadline: float,
        admitted: frozenset[str] | None = None,
        source_type: str = "general",
        proposed_by: str = "",
    ) -> WebPage:
        ...
