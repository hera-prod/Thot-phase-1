"""Moteur Wikipédia — encyclopédie générale, API MediaWiki officielle.

`https://<lang>.wikipedia.org/w/api.php?action=query&list=search&…`, sans
clé. L'étiquette MediaWiki demande un `User-Agent` informatif avec
coordonnées : c'est celui de `net.py`, qui nomme le logiciel et dit
franchement, tant que le contact n'est pas renseigné, qu'il manque.

Ce moteur ne couvre **que** `general`, et c'est un choix, pas un oubli.
`list=search` classe les articles par pertinence pour la requête, sans
filtre de récence et sans promesse de fraîcheur : un article sur un
événement vieux de dix ans sort au même rang qu'un article d'aujourd'hui.
L'annoncer comme couverture de `news` ferait lire « j'ai cherché
l'actualité » dans un index encyclopédique, ce qui est le mensonge que la
fiche interdit. `news` revient à un moteur qui interroge réellement des
sources d'actualité — et qui dit, dans sa `coverage`, que son ampleur
dépend des moteurs que l'utilisateur a activés.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse

from backend.services.web import net
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

WIKIPEDIA_ACCEPT = "application/json"
DEFAULT_LANG = "fr"

# `lang` entre dans le NOM D'HÔTE. Sans cette contrainte, une chaîne
# comme `fr.m.wikipedia.org.evil.example` suffirait à rediriger la
# requête vers un hôte choisi par le texte de la requête. La règle est
# « un code wiki a cette forme », pas « un code wiki est dans une liste » :
# la liste exhaustive vit chez MediaWiki, se met à jour sans préavis, et
# un code inconnu ne rend qu'un 404 — ce n'est pas un risque. Ce qu'il
# faut empêcher, c'est le `/`, le `@`, le `:` et la casse.
_LANG_PATTERN = re.compile(r"^[a-z]{2,8}(-[a-z0-9]{2,8})*$")

# Un `snippet` MediaWiki contient du HTML : MediaWiki entoure les termes
# trouvés de `<span class="searchmatch">`. On retire les balises, on
# n'exécute JAMAIS le HTML — ni navigateur, ni moteur de rendu, ni
# `HTMLParser` qui reconstruirait des attributs.
_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")

COVERAGE = (
    "encyclopédie générale Wikipédia : articles de référence, datés mais sans "
    "garantie de fraîcheur — ce n'est PAS le web, ni l'actualité, ni les prix, "
    "ni les documents privés"
)


def clean_snippet(raw: str) -> str:
    """Du HTML de MediaWiki au texte nu, sans jamais l'exécuter.

    L'ordre est délibéré et c'est le seul qui tienne : **d'abord** les
    entités, **ensuite** les balises. Dans l'autre sens, une entité
    `&lt;b&gt;` deviendrait `<b>` au passage suivant — c'est-à-dire une
    balise que l'on aurait fait apparaître en croyant supprimer du HTML.
    L'invariant obtenu est vérifiable : après cet appel, la chaîne ne
    contient plus ni `<` ni `>`, quelles que soient les entités d'origine.

    Le prix est qu'un article qui parle de HTML perd, dans l'extrait, le
    texte de ses balises. Un extrait de recherche n'en montrait de toute
    façon pas la structure.
    """
    return _WHITESPACE.sub(" ", _TAG.sub(" ", html.unescape(raw))).strip()


def article_url(lang: str, title: str) -> str:
    """L'adresse d'un article, bâtie depuis son titre canonique.

    MediaWiki se prononce sur les espaces (underscore) et sur les
    caractères que le titre peut contenir ; `quote` s'en charge, donc on
    ne devine pas. Les parenthèses restent littérales parce que c'est la
    forme que Wikipédia elle-même rend dans ses liens.
    """
    quoted = urllib.parse.quote(title.replace(" ", "_"), safe="()")
    return f"https://{lang}.wikipedia.org/wiki/{quoted}"


def parse_results(payload: str, lang: str, provider: str, coverage: str) -> tuple[tuple[WebHit, ...], tuple[SkippedItem, ...]]:
    """Du JSON MediaWiki aux `WebHit`. Lève `WebError` si la charge est illisible.

    Une recherche sans résultat fait disparaître la clé `query` : l'absence
    est un résultat vide, pas une erreur. Ce qui est illisible, en
    revanche, doit l'être nommé.
    """
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WebError(
            PROVIDER_MALFORMED,
            f"réponse Wikipédia illisible ({exc}) : le JSON de l'API MediaWiki n'a pas pu être analysé",
        ) from exc
    if not isinstance(document, dict):
        raise WebError(PROVIDER_MALFORMED, "réponse Wikipédia inattendue : la charge JSON n'est pas un objet")
    query = document.get("query")
    if not isinstance(query, dict):
        return (), ()
    entries = query.get("search")
    if not isinstance(entries, list):
        raise WebError(PROVIDER_MALFORMED, "réponse Wikipédia inattendue : `query.search` n'est pas une liste")

    hits: list[WebHit] = []
    skipped: list[SkippedItem] = []
    for rank, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            skipped.append(SkippedItem(None, SKIP_NOT_SELECTED, "résultat Wikipédia hors objet JSON"))
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            skipped.append(SkippedItem(None, SKIP_NOT_SELECTED, "résultat Wikipédia sans titre"))
            continue
        hits.append(WebHit(
            url=article_url(lang, title.strip()),
            title=title.strip(),
            snippet=clean_snippet(entry.get("snippet") or ""),
            provider=provider,
            coverage=coverage,
            rank=rank,
        ))
    return tuple(hits), tuple(skipped)


class WikipediaProvider:
    """Encyclopédie générale. `general` seulement — pas `news` (voir le module)."""

    name = "wikipedia"
    covers = frozenset({"general"})
    coverage = COVERAGE

    def __init__(self, lang: str = DEFAULT_LANG, api_url: str | None = None) -> None:
        if not _LANG_PATTERN.match(lang):
            # Fail Fast : une langue illisible est une faute de programmation,
            # pas une situation de runtime. Elle n'est jamais « corrigée ».
            raise ValueError(
                f"code de langue Wikipédia illisible : {lang!r} "
                f"(attendu : deux ou trois lettres, éventuellement suivies d'un code de région)"
            )
        self.lang = lang
        # Injectable pour les tests ; en usage normal, l'API suit la langue.
        self.api_url = api_url or f"https://{lang}.wikipedia.org/w/api.php"

    def _url(self, text: str, max_results: int) -> str:
        wanted = max(1, min(int(max_results), 500))
        query = urllib.parse.urlencode({
            "action": "query",
            "list": "search",
            "srsearch": text,
            "format": "json",
            "srlimit": wanted,
            "srprop": "snippet",
        })
        return f"{self.api_url}?{query}"

    def search(self, query: WebQuery, *, max_results: int, deadline: float) -> ProviderAnswer:
        budget = NetworkBudget(deadline=deadline)
        if not budget.take_query():
            return answer_error(
                self.name, self.coverage, NETWORK_BLOCKED,
                f"budget de requêtes du tour épuisé avant l'appel Wikipédia ({max_results} résultats demandés)",
            )
        try:
            response = net.http_get(
                self._url(query.text, max_results),
                budget=budget,
                page=False,
                accept=WIKIPEDIA_ACCEPT,
                deadline=deadline,
            )
            if response.status == 429:
                raise WebError(
                    NETWORK_BLOCKED,
                    "Wikipédia a refusé la requête (429) : attendre avant de relancer, "
                    "l'étiquette demande des requêtes en série et espacées",
                )
            if response.status != 200:
                raise WebError(
                    NETWORK_BLOCKED,
                    f"Wikipédia a répondu {response.status} : encyclopédie indisponible pour le moment",
                )
            hits, skipped = parse_results(response.text(), self.lang, self.name, self.coverage)
        except WebError as exc:
            return answer_error(self.name, self.coverage, exc.code, exc.message)
        return ProviderAnswer(
            provider=self.name, coverage=self.coverage, hits=hits, skipped=skipped
        )
