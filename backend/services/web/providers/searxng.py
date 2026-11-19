"""Moteur SearXNG — web général par une instance que l'utilisateur héberge.

SearXNG n'est pas un service public : c'est un logiciel que l'utilisateur
installe et lance **sur sa machine**. Le projet ne peut donc pas en
attendre un par défaut, et ne le contacte que si l'utilisateur a
déclaré où le trouver — par l'environnement :

    THOT_WEB_SEARXNG_URL=http://127.0.0.1:8080

ou par le fichier de réglages, que l'interface écrit :

    searxng_url=http://127.0.0.1:8080

Sans l'une des deux déclarations, ce moteur n'est **pas construit** — voir
`SearxngProvider.from_environment`, qui ordonne les deux portes. Aucune
instance publique, aucun service de recherche loué, aucune clé.

C'est aussi le seul moteur du projet autorisé à joindre une adresse non
globale, et c'est un choix assumé : c'est la machine de l'utilisateur.
Le refus par défaut de `net.http_get` reste intact pour tous les autres
moteurs, et l'exception n'admet que le **loopback** (voir
`net.resolve_loopback`) — un réseau local ou un pont Docker restent
refusés.

**Honnêteté de la couverture.** Ce que SearXNG rend dépend des moteurs
que l'utilisateur a activés dans son `settings.yml`. `academic` et
`news` ne sont donc pas garantis : ils le sont si, et seulement si, les
moteurs correspondants sont activés côté instance. La phrase de couverture
le dit au lieu de le promettre.
"""
from __future__ import annotations

import json
import urllib.parse

from backend.services.web import net
from backend.services.web import settings as _settings
from backend.services.web.net import NetworkBudget
from backend.services.web.providers import PROVIDER_MALFORMED, answer_error
from backend.services.web.types import (
    NETWORK_BLOCKED,
    SKIP_NOT_SELECTED,
    ProviderAnswer,
    SkippedItem,
    WebError,
    WebHit,
    WebQuery,
)

SEARXNG_URL_ENV = "THOT_WEB_SEARXNG_URL"
SEARXNG_ACCEPT = "application/json"
SEARXNG_MAX_RESULTS = 50

COVERAGE = (
    "web général via une instance SearXNG auto-hébergée par l'utilisateur ; "
    "l'ampleur réelle dépend des moteurs activés dans son instance — "
    "l'actualité et le domaine académique n'y figurent que si ces moteurs y sont activés"
)


def parse_results(
    payload: str,
    provider: str,
    coverage: str,
    limit: int,
) -> tuple[tuple[WebHit, ...], tuple[SkippedItem, ...]]:
    """Du JSON SearXNG aux `WebHit`. Lève `WebError` si la charge est illisible."""
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WebError(
            PROVIDER_MALFORMED,
            f"réponse SearXNG illisible ({exc}) : le JSON de l'instance n'a pas pu être analysé "
            f"(le format JSON est-il activé dans settings.yml ?)",
        ) from exc
    if not isinstance(document, dict):
        raise WebError(PROVIDER_MALFORMED, "réponse SearXNG inattendue : la charge JSON n'est pas un objet")
    results = document.get("results")
    if not isinstance(results, list):
        raise WebError(PROVIDER_MALFORMED, "réponse SearXNG inattendue : `results` n'est pas une liste")

    hits: list[WebHit] = []
    skipped: list[SkippedItem] = []
    for rank, entry in enumerate(results, start=1):
        if rank > limit:
            break
        if not isinstance(entry, dict):
            skipped.append(SkippedItem(None, SKIP_NOT_SELECTED, "résultat SearXNG hors objet JSON"))
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            skipped.append(SkippedItem(None, SKIP_NOT_SELECTED, "résultat SearXNG sans URL : rien à proposer"))
            continue
        # `engine` est le nom du moteur d'origine côté instance. Ce n'est
        # pas une certification : SearXNG ne l'a pas vérifié, il l'a
        # ajouté. Il va dans l'extrait, jamais dans le nom du moteur.
        engine = entry.get("engine")
        content = entry.get("content")
        snippet = content.strip() if isinstance(content, str) else ""
        if isinstance(engine, str) and engine.strip():
            marker = f"[{engine.strip()}] "
            snippet = marker + snippet if snippet else marker.rstrip()
        hits.append(WebHit(
            url=url.strip(),
            title=(entry.get("title") or "").strip() if isinstance(entry.get("title"), str) else "",
            snippet=snippet,
            provider=provider,
            coverage=coverage,
            rank=rank,
        ))
    return tuple(hits), tuple(skipped)


class SearxngProvider:
    """Web général, par l'instance SearXNG que l'utilisateur a déclarée."""

    name = "searxng"
    covers = frozenset({"general", "news", "academic"})
    coverage = COVERAGE

    def __init__(self, base_url: str) -> None:
        root = base_url.strip().rstrip("/")
        if not root:
            raise ValueError("URL de l'instance SearXNG vide")
        # L'URL vient de l'environnement : c'est une entrée de programme.
        # `validate_url` (via `http_get`) refusera de toute façon un schéma
        # interdit ou un hôte ambigu ; on ne fait ici que rejeter le vide
        # et les schémas que SearXNG ne peut pas servir.
        parts = urllib.parse.urlsplit(root)
        if parts.scheme not in ("http", "https"):
            raise ValueError(
                f"URL d'instance SearXNG illisible : schéma {parts.scheme or 'absent'!r} "
                f"(attendu : http ou https)"
            )
        if not parts.netloc:
            raise ValueError(f"URL d'instance SearXNG sans hôte : {base_url!r}")
        self.base_url = root

    @classmethod
    def from_environment(cls) -> "SearxngProvider | None":
        """Construit le moteur si — et seulement si — l'utilisateur l'a déclaré.

        La déclaration a deux portes, et le réglage les ordonne lui-même :
        `settings.searxng_url()` lit d'abord `THOT_WEB_SEARXNG_URL`, puis le
        fichier de réglages. Un déploiement impose donc l'adresse par
        l'environnement sans dépendre d'un fichier écrit par un utilisateur.
        Rien de déclaré rend `""` — donc `None` ici, et jamais une exception.

        `None` n'est pas une erreur : c'est la réponse normale sur une
        installation où l'utilisateur n'héberge pas SearXNG. C'est
        `registry.py` qui décide de l'annoncer ou non.
        """
        declare = _settings.searxng_url()
        if not declare:
            return None
        return cls(declare)

    def _url(self, text: str) -> str:
        # `format=json` n'est pas une préférence : sans lui, SearXNG rend
        # du HTML, et le fetcher n'a pas de page à faire d'un moteur.
        query = urllib.parse.urlencode({"q": text, "format": "json"})
        return f"{self.base_url}/search?{query}"

    @staticmethod
    def _limit(max_results: int) -> int:
        """Le nombre de résultats retenus, borné.

        SearXNG n'expose pas de paramètre de taille dans cet appel : la
        borne est donc appliquée après coup, sur la liste rendue.
        """
        return max(1, min(int(max_results), SEARXNG_MAX_RESULTS))

    def search(self, query: WebQuery, *, max_results: int, deadline: float) -> ProviderAnswer:
        budget = NetworkBudget(deadline=deadline)
        if not budget.take_query():
            return answer_error(
                self.name, self.coverage, NETWORK_BLOCKED,
                f"budget de requêtes du tour épuisé avant l'appel SearXNG ({max_results} résultats demandés)",
            )
        try:
            response = net.http_get(
                self._url(query.text),
                budget=budget,
                page=False,
                accept=SEARXNG_ACCEPT,
                deadline=deadline,
                # La seule dérogation de tout le projet, et elle est
                # restreinte au loopback par `net.resolve_loopback`.
                allow_private=True,
            )
            if response.status == 403:
                # Réponse caractéristique d'une instance dont le format
                # JSON est désactivé : SearXNG rend alors une page HTML.
                raise WebError(
                    NETWORK_BLOCKED,
                    "l'instance SearXNG a refusé la requête (403) : le format JSON est "
                    "désactivé côté instance — ajouter `formats: [html, json]` dans "
                    "settings.yml, puis recharger",
                )
            if response.status != 200:
                raise WebError(
                    NETWORK_BLOCKED,
                    f"l'instance SearXNG a répondu {response.status} : service local "
                    f"injoignable — vérifier que {self.base_url} est bien démarré",
                )
            hits, skipped = parse_results(
                response.text(), self.name, self.coverage, self._limit(max_results)
            )
        except WebError as exc:
            return answer_error(self.name, self.coverage, exc.code, exc.message)
        return ProviderAnswer(
            provider=self.name, coverage=self.coverage, hits=hits, skipped=skipped
        )