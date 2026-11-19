"""Moteur arXiv — pré-publications scientifiques, API Atom officielle.

`http://export.arxiv.org/api/query?search_query=…&start=0&max_results=N`.
Pas de clé, pas de scraping, pas de compte : l'API publique prévue pour
cela. Le `<id>` que l'API rend est en `http://`, et il est converti en
`https://` avant d'être proposé comme adresse à lire — on ne propose
jamais à l'utilisateur une adresse qui redirige, et le fetcher ne suit
aucune redirection.

Politesse : les conditions d'utilisation d'arXiv imposent **une requête
toutes les trois secondes et une seule connexion à la fois**. Le champ
`net.HOST_MIN_INTERVAL_S` vaut 1 s pour tous les hôtes : cette porte est
donc propre à arXiv, et c'est la seule raison de son existence.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ElementTree

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

# Namespace Atom déclaré par l'API. On le pose en constante : le préfixe
# `{ns}` est la seule façon d'atteindre un élément d'un document namespacé.
ATOM_NS = "http://www.w3.org/2005/Atom"

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_ACCEPT = "application/atom+xml"

# Les conditions d'utilisation : « no more than one request every three
# seconds, and no more than one connection at a time ». La seconde phrase
# est tenue par la sérialisation de `net.wait_for_host` ; la première par
# la porte ci-dessous.
ARXIV_MIN_INTERVAL_S = 3.0

# Plafond de l'API elle-même ; au-delà, arXiv renvoie une erreur.
ARXIV_MAX_RESULTS = 200
SUMMARY_MAX_CHARS = 600

COVERAGE = (
    "articles scientifiques (arXiv) : pré-publications de toutes disciplines, "
    "pas de peer review — un texte arXiv peut avoir été refusé depuis"
)

_WHITESPACE = re.compile(r"\s+")
_ARXIV_SLOT_LOCK = threading.Lock()
_ARXIV_LAST_CALL: float | None = None


def reset_arxiv_gate() -> None:
    """Ouvre la porte à un appel suivant immédiat.

    Appelé par les tests, et en début de tour par `research.py` le jour où
    il existera : l'intervalle de trois secondes est une contrainte de
    politesse *entre* les requêtes, pas une contrainte de durée d'un tour.
    """
    global _ARXIV_LAST_CALL
    with _ARXIV_SLOT_LOCK:
        _ARXIV_LAST_CALL = None


def wait_arxiv_slot(deadline: float) -> None:
    """Tient les trois secondes d'arXiv, sans jamais déborder le tour.

    Même forme que `net.wait_for_host` : l'attente est calculée sous un
    verrou, puis déduite du temps qui reste. Si le tour expire pendant
    l'attente, la requête part quand même et c'est `http_get` qui refuse,
    avec le code de délai — on ne retient pas l'information pour rien.
    """
    global _ARXIV_LAST_CALL
    now = time.monotonic()
    with _ARXIV_SLOT_LOCK:
        last = _ARXIV_LAST_CALL
        wait = 0.0 if last is None else max(0.0, (last + ARXIV_MIN_INTERVAL_S) - now)
        _ARXIV_LAST_CALL = now + wait
    if wait > 0:
        time.sleep(min(wait, max(0.0, deadline - now)))


def collapse(text: str) -> str:
    """Un titre arXiv peut être coupé en plusieurs lignes : il devient une phrase.

    L'API rend `<title>` et `<summary>` avec des retours à la ligne et des
    indentations internes (visible dans les résumés longs, où chaque
    paragraphe est indenté). Un titre lu tel quel afficherait trois lignes
    dans l'interface et une coupure au milieu d'un mot à l'impression.
    """
    return _WHITESPACE.sub(" ", text).strip()


def truncate(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """Coupe sur une frontière de mot, sinon au caractère.

    Une coupure au milieu d'un mot se voit dans le rendu ; une coupure
    avant l'espace se voit pas. La marque de troncature reste visible pour
    que le modèle sache qu'il ne lit pas la fin.
    """
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip()
    return f"{head}…"


def _entry_hits(feed: ElementTree.Element) -> tuple[list[WebHit], list[SkippedItem]]:
    """Les entrées d'un flux Atom arXiv, lues sans rien exécuter."""
    hits: list[WebHit] = []
    skipped: list[SkippedItem] = []
    for rank, entry in enumerate(feed.findall(f"{{{ATOM_NS}}}entry"), start=1):
        raw_id = entry.findtext(f"{{{ATOM_NS}}}id", default="").strip()
        if not raw_id:
            skipped.append(SkippedItem(None, SKIP_NOT_SELECTED, "entrée arXiv sans identifiant"))
            continue
        title = collapse(entry.findtext(f"{{{ATOM_NS}}}title", default=""))
        summary = truncate(collapse(entry.findtext(f"{{{ATOM_NS}}}summary", default="")))
        published = entry.findtext(f"{{{ATOM_NS}}}published", default="").strip() or None
        hits.append(WebHit(
            # arXiv rend ses identifiants en http:// ; https:// parle la
            # même ressource et évite de proposer une adresse qui redirige.
            url=raw_id.replace("http://arxiv.org/", "https://arxiv.org/"),
            title=title,
            snippet=summary,
            provider="arxiv",
            coverage=COVERAGE,
            rank=rank,
            published_at=published,
        ))
    return hits, skipped


def parse_feed(payload: str) -> tuple[tuple[WebHit, ...], tuple[SkippedItem, ...]]:
    """Du XML Atom aux `WebHit`. Lève `WebError` si le document n'est pas lisible.

    Le flux est vérifié avant d'être parcouru : une réponse qui n'est pas un
    `<feed>` Atom — une page d'erreur, un corps tronqué par le plafond
    d'octets — doit être un échec nommé, pas une liste de résultats vide.
    Une liste vide se lit « rien trouvé », et c'est un mensonge de plus.
    """
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise WebError(
            PROVIDER_MALFORMED,
            f"réponse arXiv illisible ({exc}) : le XML Atom n'a pas pu être analysé",
        ) from exc
    if root.tag != f"{{{ATOM_NS}}}feed":
        raise WebError(
            PROVIDER_MALFORMED,
            f"réponse arXiv inattendue : élément racine <{root.tag}> au lieu d'un flux Atom",
        )
    hits, skipped = _entry_hits(root)
    return tuple(hits), tuple(skipped)


class ArxivProvider:
    """Pré-publications arXiv. `academic` uniquement."""

    name = "arxiv"
    covers = frozenset({"academic"})
    coverage = COVERAGE

    def __init__(self, api_url: str = ARXIV_API_URL) -> None:
        # L'URL est injectable pour que les tests puissent viser un serveur
        # figé. Elle n'ouvre aucun accès : le refus du loopback reste actif,
        # arXiv ne passe jamais par `allow_private`.
        self.api_url = api_url

    def _url(self, text: str, max_results: int) -> str:
        wanted = max(1, min(int(max_results), ARXIV_MAX_RESULTS))
        query = urllib.parse.urlencode({"search_query": text, "start": 0, "max_results": wanted})
        return f"{self.api_url}?{query}"

    def search(self, query: WebQuery, *, max_results: int, deadline: float) -> ProviderAnswer:
        budget = NetworkBudget(deadline=deadline)
        if not budget.take_query():
            return answer_error(
                self.name, self.coverage, NETWORK_BLOCKED,
                f"budget de requêtes du tour épuisé avant l'appel arXiv ({max_results} résultats demandés)",
            )
        wait_arxiv_slot(deadline)
        try:
            response = net.http_get(
                self._url(query.text, max_results),
                budget=budget,
                page=False,
                accept=ARXIV_ACCEPT,
                deadline=deadline,
            )
            if response.status == 429:
                raise WebError(
                    NETWORK_BLOCKED,
                    "arXiv a refusé la requête (429) : ses conditions imposent une requête "
                    "toutes les 3 s ; attendre avant de relancer",
                )
            if response.status != 200:
                raise WebError(
                    NETWORK_BLOCKED,
                    f"arXiv a répondu {response.status} : source indisponible pour le moment, "
                    f"relancer plus tard ou s'appuyer sur OpenAlex",
                )
            hits, skipped = parse_feed(response.text())
        except WebError as exc:
            return answer_error(self.name, self.coverage, exc.code, exc.message)
        return ProviderAnswer(
            provider=self.name, coverage=self.coverage, hits=hits, skipped=skipped
        )
