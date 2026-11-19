"""Moteur OpenAlex — notices bibliographiques, toutes disciplines.

`https://api.openalex.org/works?search=…&per-page=N`, sans clé. OpenAlex
agrège les notices de Crossref, OpenAIRE et d'autres sources : elle
indexe des articles **évalués par les pairs** autant que des
pré-publications, là où arXiv ne voit que les pré-publications. C'est
exactement la raison de son rang dans la résolution de `academic` : arXiv
d'abord (résumé intégral, disciplines larges), OpenAlex ensuite (ce
qu'OpenAlex seul connaît, avec son DOI).

Le champ `abstract_inverted_index` est un dictionnaire mot → positions.
Il n'est pas dans l'ordre : c'est un index, pas un texte. On le
reconstruit en triant les positions — sinon le résumé se lirait
« deduction access interface embedding … » et le modèle citerait n'importe
quoi.

**Politesse et `mailto`.** OpenAlex demande une adresse de contact pour
accéder à son « polite pool ». `net.PROJECT_CONTACT` est vide tant que le
propriétaire ne l'a pas remplie. On ne l'invente pas : une adresse
fabriquée serait une adresse qui n'existe pas, vers laquelle on dirige le
trafic d'un tiers. Le paramètre n'est envoyé que si la constante est
renseignée, et l'écart est visible dans `net.identify_gap()`.
"""
from __future__ import annotations

import json
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

OPENALEX_API_URL = "https://api.openalex.org/works"
OPENALEX_ACCEPT = "application/json"
OPENALEX_MAX_PER_PAGE = 200
ABSTRACT_MAX_CHARS = 600

COVERAGE = (
    "notices bibliographiques et résumés d'articles (OpenAlex), toutes "
    "disciplines : articles évalués par les pairs comme pré-publications"
)


def rebuild_abstract(index: object) -> str:
    """Rend un `abstract_inverted_index` lisible, dans l'ordre du texte.

    L'API donne `{mot: [positions…]}` ; un mot réapparaît plus loin, donc
    on ne peut pas itérer le dictionnaire. On étale chaque mot sur ses
    positions, on trie, on recolle. Les trous éventuels — une position
    manquante, une fin de texte tronquée par OpenAlex — disparaissent
    silencieusement : ce qu'il reste est un texte, pas un index.
    """
    if not isinstance(index, dict):
        return ""
    placed: dict[int, str] = {}
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and position not in placed:
                placed[position] = word
    return " ".join(placed[position] for position in sorted(placed))


def truncate(text: str, limit: int = ABSTRACT_MAX_CHARS) -> str:
    """Coupe sur une frontière de mot, sinon au caractère."""
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip()
    return f"{head}…"


def _work_url(work: dict[str, object]) -> str:
    """L'adresse d'une notice : le DOI s'il y en a un, sinon l'identifiant.

    Le DOI est la adresse la plus durable et la plus universelle d'un
    article ; `openalex.org/W…` n'est qu'un identifiant interne. Quand il
    n'y a pas de DOI — notices de livres, de.datasets, de thèses — l'identifiant
    OpenAlex reste une adresse résoluble, donc on la garde.
    """
    doi = work.get("doi")
    if isinstance(doi, str) and doi.strip():
        return doi.strip()
    identifier = work.get("id")
    if isinstance(identifier, str) and identifier.strip():
        return identifier.strip()
    return ""


def parse_results(payload: str, provider: str, coverage: str) -> tuple[tuple[WebHit, ...], tuple[SkippedItem, ...]]:
    """Du JSON OpenAlex aux `WebHit`. Lève `WebError` si la charge est illisible."""
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WebError(
            PROVIDER_MALFORMED,
            f"réponse OpenAlex illisible ({exc}) : le JSON de l'API n'a pas pu être analysé",
        ) from exc
    if not isinstance(document, dict):
        raise WebError(PROVIDER_MALFORMED, "réponse OpenAlex inattendue : la charge JSON n'est pas un objet")
    results = document.get("results")
    if not isinstance(results, list):
        raise WebError(PROVIDER_MALFORMED, "réponse OpenAlex inattendue : `results` n'est pas une liste")

    hits: list[WebHit] = []
    skipped: list[SkippedItem] = []
    for rank, work in enumerate(results, start=1):
        if not isinstance(work, dict):
            skipped.append(SkippedItem(None, SKIP_NOT_SELECTED, "notice OpenAlex hors objet JSON"))
            continue
        url = _work_url(work)
        if not url:
            skipped.append(SkippedItem(None, SKIP_NOT_SELECTED, "notice OpenAlex sans DOI ni identifiant"))
            continue
        # `title` est absent sur certaines notices ; `display_name` en tient lieu.
        title = work.get("title") or work.get("display_name") or ""
        published = work.get("publication_date")
        hits.append(WebHit(
            url=url,
            title=str(title).strip(),
            snippet=truncate(rebuild_abstract(work.get("abstract_inverted_index"))),
            provider=provider,
            coverage=coverage,
            rank=rank,
            published_at=published.strip() if isinstance(published, str) and published.strip() else None,
        ))
    return tuple(hits), tuple(skipped)


class OpenAlexProvider:
    """Notices et résumés d'articles, toutes disciplines. `academic`."""

    name = "openalex"
    covers = frozenset({"academic"})
    coverage = COVERAGE

    def __init__(self, api_url: str = OPENALEX_API_URL) -> None:
        # Injectable pour les tests. Le refus du loopback reste actif : ce
        # moteur ne passe jamais par `allow_private`.
        self.api_url = api_url

    def _url(self, text: str, max_results: int) -> str:
        wanted = max(1, min(int(max_results), OPENALEX_MAX_PER_PAGE))
        params: dict[str, str | int] = {"search": text, "per-page": wanted}
        contact = net.PROJECT_CONTACT.strip()
        if contact:
            params["mailto"] = contact
        return f"{self.api_url}?{urllib.parse.urlencode(params)}"

    def search(self, query: WebQuery, *, max_results: int, deadline: float) -> ProviderAnswer:
        budget = NetworkBudget(deadline=deadline)
        if not budget.take_query():
            return answer_error(
                self.name, self.coverage, NETWORK_BLOCKED,
                f"budget de requêtes du tour épuisé avant l'appel OpenAlex ({max_results} résultats demandés)",
            )
        try:
            response = net.http_get(
                self._url(query.text, max_results),
                budget=budget,
                page=False,
                accept=OPENALEX_ACCEPT,
                deadline=deadline,
            )
            if response.status in (403, 429):
                # 403 est la réponse d'OpenAlex à un usage sans contact dans
                # le « polite pool ». Le message le dit, parce que la cause
                # est invisible depuis la réponse.
                hint = (
                    f"renseigner net.PROJECT_CONTACT ({net.identify_gap()})"
                    if not net.PROJECT_CONTACT.strip()
                    else "attendre avant de relancer"
                )
                raise WebError(
                    NETWORK_BLOCKED,
                    f"OpenAlex a refusé la requête ({response.status}) : {hint}",
                )
            if response.status != 200:
                raise WebError(
                    NETWORK_BLOCKED,
                    f"OpenAlex a répondu {response.status} : base bibliographique indisponible pour le moment",
                )
            hits, skipped = parse_results(response.text(), self.name, self.coverage)
        except WebError as exc:
            return answer_error(self.name, self.coverage, exc.code, exc.message)
        return ProviderAnswer(
            provider=self.name, coverage=self.coverage, hits=hits, skipped=skipped
        )
