"""Recherche web — couche 2 : les moteurs.

Quatre moteurs, quatre APIs **officielles et sans clé**, appelées par la
couche réseau du projet (`net.http_get`). Aucun scraping de moteur de
recherche, aucune usurpation d'identité : chaque requête sortante porte le
`User-Agent` de Thot, passe par le fetcher fermé, et est donc bornée en
octets, en délai et en nombre.

| Moteur | API | Couvre |
|---|---|---|
| `ArxivProvider` | `export.arxiv.org/api/query` (Atom) | `academic` |
| `OpenAlexProvider` | `api.openalex.org/works` (JSON) | `academic` |
| `WikipediaProvider` | `<lang>.wikipedia.org/w/api.php` (JSON) | `general` |
| `SearxngProvider` | instance auto-hébergée (JSON) | `general`, `news`, `academic` |

Trois invariants, vérifiés par `tests/backend/test_web_providers.py` :

1. **Aucun moteur ne lève vers l'appelant.** Une API en panne, un 429, du
   XML coupé : le moteur rend une `ProviderAnswer` whose `error` porte le
   code, et le rendu peut dire ce qui s'est passé. Une exception qui
   remonterait ici interromprait le tour de recherche sans rien expliquer.
2. **Aucun moteur ne télécharge de page.** Il rend des `WebHit` — des
   adresses — et c'est le fetcher qui décide si une adresse est lue.
3. **Aucun moteur n'enjolive sa couverture.** `coverage` est une phrase qui
   dit ce que la source est *et ce qu'elle n'est pas*. Un résultat
   encyclopédique présenté comme « le web » est le mensonge que la fiche
   interdit ; c'est pourquoi Wikipédia se déclare sans `news` et pourquoi
   SearXNG dit que son ampleur dépend des moteurs activés dans l'instance.

Ce module ne fait aucun I/O. Il porte les deux seules primitives que les
quatre moteurs partagent, définies **avant** l'import des sous-modules :
les sous-modules font `from backend.services.web.providers import ...`,
ce qui résout parce que le paquet parent est déjà dans `sys.modules`.
"""
from __future__ import annotations

from backend.services.web.types import ProviderAnswer

# --------------------------------------------------------------------------
# Vocabulaire d'erreur des moteurs
# --------------------------------------------------------------------------
# ÉCART CONSIGNÉ (C01). `types.py` fixe un vocabulaire local fermé —
# `WEB_LOCAL_CODES` : `WEB_DISABLED`, `NO_PROVIDER`, `ROBOTS_DENIED`,
# `NETWORK_BLOCKED`, `CONSENT_REQUIRED`, `URL_NOT_ADMITTED` — et aucun de ces
# codes ne dit « le moteur a répondu, mais sa réponse est illisible ».
# Réutiliser `NETWORK_BLOCKED` serait un mensonge : le réseau a très bien
# fonctionné. On ajoute donc un code, et on le consigne ici pour qu'il soit
# replié dans `WEB_LOCAL_CODES` au moment de la révision du vocabulaire,
# plutôt que d'être perdu dans quatre copieux.
PROVIDER_MALFORMED = "PROVIDER_MALFORMED"  # réponse illisible ou inexploitable


def local_error(code: str, message: str) -> str:
    """Un code local voyage dans son texte, jamais seul.

    C'est la syntaxe que le projet a déjà choisie pour `generate_image` et
    que `WebError` applique : `[CODE] message`. Le rendu affiche la chaîne
    telle quelle, sans table à étendre.
    """
    return f"[{code}] {message}"


def answer_error(provider: str, coverage: str, code: str, message: str) -> ProviderAnswer:
    """Une réponse d'échec. Pas d'exception : le moteur a une voix, il l'utilise."""
    return ProviderAnswer(provider=provider, coverage=coverage, error=local_error(code, message))


# Les imports viennent APRÈS les primitives ci-dessus : c'est la raison
# d'être de cet ordre, et l'ordre du fichier n'est pas du style.
from backend.services.web.providers.arxiv import ArxivProvider  # noqa: E402
from backend.services.web.providers.openalex import OpenAlexProvider  # noqa: E402
from backend.services.web.providers.searxng import SearxngProvider  # noqa: E402
from backend.services.web.providers.wikipedia import WikipediaProvider  # noqa: E402
from backend.services.web.providers.registry import (  # noqa: E402
    applicable_providers,
    no_provider_answer,
    provider_status,
)

__all__ = [
    "PROVIDER_MALFORMED",
    "answer_error",
    "local_error",
    "applicable_providers",
    "no_provider_answer",
    "provider_status",
    "ArxivProvider",
    "OpenAlexProvider",
    "SearxngProvider",
    "WikipediaProvider",
]
