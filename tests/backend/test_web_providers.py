"""Moteurs de recherche web — parsing sur réponses FIGÉES, zéro accès réseau.

Un seul petit serveur HTTP local sert des charges utiles **recopiées des
API officielles** (arXiv, Wikipédia, OpenAlex) et une charge SearXNG de
forme réaliste. Rien ne sort de la machine : la fixture neutralise la
**seule** résolution DNS (`net.resolve_global`) et l'intervalle d'un
seconde entre deux appels au même hôte, parce qu'un test ne peut pas
faire résoudre `127.0.0.1` vers une adresse publique. Tout le reste est
le vrai code de `net.py` — vraie socket, vrais plafonds d'octets, vrai
budget de tour, vraie porte de politesse.

Ce qui n'est pas figé, et qui ne doit pas l'être : la politesse d'arXiv.
Une porte à trois secondes testée à trois secondes ferait perdre trois
secondes à la suite ; elle est donc testée à cinquante millisecondes, et
la constante elle-même est vérifiée séparément.

Deux tests annulent volontairement la neutralisation DNS (ils utilisent
`monkeypatch.undo()`) : ce sont ceux qui prouvent que `127.0.0.1` reste
refusé pour arXiv, Wikipédia et OpenAlex, et que la voie privée
n'ouvre que le loopback. Eux, ils n'ouvrent aucune connexion.

Le seul test qui touche le réseau est `test_e2e_arxiv_live`, marqué
`slow` et conditionné à `THOT_E2E_WEB=1`. Il est exclu par le
`-k "not slow"` que le dépôt utilise déjà.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, cast

import pytest

from backend.services.web import net
from backend.services.web.providers import PROVIDER_MALFORMED, arxiv as arxiv_module, registry
from backend.services.web.providers.arxiv import (
    ARXIV_API_URL,
    ARXIV_MIN_INTERVAL_S,
    ArxivProvider,
    reset_arxiv_gate,
)
from backend.services.web.providers.openalex import OpenAlexProvider
from backend.services.web.providers.searxng import SEARXNG_URL_ENV, SearxngProvider
from backend.services.web.providers.wikipedia import WikipediaProvider
from backend.services.web.settings import SETTINGS_ENV, SETTINGS_FILENAME
from backend.services.web.types import NETWORK_BLOCKED, NO_PROVIDER, WebError, WebQuery

# --------------------------------------------------------------------------
# Charges utiles figées
# --------------------------------------------------------------------------
# Captures réelles du 2026-09-28, recopiées telles quelles, pour que le
# parseur soit éprouvé contre ce que les API rendent vraiment et non
# contre un gabarit de notre propre invention.

# http://export.arxiv.org/api/query?search_query=deep+research+agents&start=0&max_results=2
# Le premier résumé porte des retours à la ligne et une indentation : c'est
# exactement ce que l'API rend pour les résumés longs.
ARXIV_ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns="http://www.w3.org/2005/Atom">
  <id>https://arxiv.org/api/anL+O5a1aUXxpnXTjZ6HAy3nYt4</id>
  <title>arXiv Query: search_query=all:deep OR all:research OR all:agents&amp;id_list=&amp;start=0&amp;max_results=2</title>
  <updated>2026-09-28T22:31:27Z</updated>
  <link href="https://arxiv.org/api/query?search_query=all:deep+OR+(all:research+OR+all:agents)&amp;start=0&amp;max_results=2&amp;id_list=" type="application/atom+xml"/>
  <opensearch:itemsPerPage>2</opensearch:itemsPerPage>
  <opensearch:totalResults>416111</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <entry>
    <id>http://arxiv.org/abs/2301.00942v1</id>
    <title>Deep Learning and Computational Physics (Lecture Notes)</title>
    <updated>2023-01-03T03:56:19Z</updated>
    <link href="https://arxiv.org/abs/2301.00942v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2301.00942v1" rel="related" type="application/pdf" title="pdf"/>
    <summary>These notes were compiled as lecture notes for a course developed and taught at the University of the Southern California. They should be accessible to a typical engineering graduate student with a strong background in Applied Mathematics.
  The main objective of these notes is to introduce a student who is familiar with concepts in linear algebra and partial differential equations to select topics in deep learning. These lecture notes exploit the strong connections between deep learning algorithms and the more conventional techniques of computational physics to achieve two goals. First, they use concepts from computational physics to develop an understanding of deep learning algorithms. Not surprisingly, many concepts in deep learning can be connected to similar concepts in computational physics, and one can utilize this connection to better understand these algorithms. Second, several novel deep learning algorithms can be used to solve challenging problems in computational physics. Thus, they offer someone who is interested in modeling a physical phenomena with a complementary set of tools.</summary>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="math-ph" scheme="http://arxiv.org/schemas/atom"/>
    <published>2023-01-03T03:56:19Z</published>
    <arxiv:comment>7 chapters</arxiv:comment>
    <arxiv:primary_category term="cs.LG"/>
    <author>
      <name>Deep Ray</name>
    </author>
    <author>
      <name>Orazio Pinti</name>
    </author>
    <author>
      <name>Assad A. Oberai</name>
    </author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1912.06732v2</id>
    <title>On the approximation of rough functions with deep neural networks</title>
    <updated>2020-10-08T06:27:30Z</updated>
    <link href="https://arxiv.org/abs/1912.06732v2" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/1912.06732v2" rel="related" type="application/pdf" title="pdf"/>
    <summary>Deep neural networks and the ENO procedure are both efficient frameworks for approximating rough functions. We prove that at any order, the ENO interpolation procedure can be cast as a deep ReLU neural network. This surprising fact enables the transfer of several desirable properties of the ENO procedure to deep neural networks, including its high-order accuracy at approximating Lipschitz functions. Numerical tests for the resulting neural networks show excellent performance for approximating solutions of nonlinear conservation laws and at data compression.</summary>
    <category term="math.NA" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="stat.ML" scheme="http://arxiv.org/schemas/atom"/>
    <published>2019-12-13T22:48:36Z</published>
    <arxiv:comment>39 pages</arxiv:comment>
    <arxiv:primary_category term="math.NA"/>
    <author>
      <name>Tim De Ryck</name>
    </author>
    <author>
      <name>Siddhartha Mishra</name>
    </author>
    <author>
      <name>Deep Ray</name>
    </author>
  </entry>
</feed>
"""

# VARIANTE SYNTHÉTIQUE, pas une capture. arXiv a longtemps coupé les titres
# longs sur plusieurs lignes, avec une indentation de continuation ; l'API
# d'aujourd'hui ne le fait plus sur les titres courts, mais les archives
# anciennes en gardent. La capture ci-dessus ne prouve que la forme
# actuelle : celle-ci couvre la forme que le parseur doit aussi tenir.
ARXIV_ATOM_TITRE_COUPE = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2506.18096v2</id>
    <title>Deep Research Agents: A Systematic
  Examination And Roadmap</title>
    <summary>Résumé court.
  Seconde ligne du résumé.</summary>
    <published>2025-06-22T16:52:48Z</published>
  </entry>
</feed>
"""

# https://fr.wikipedia.org/w/api.php?action=query&list=search&srsearch=transformeur+de+tension&format=json&srlimit=3&srprop=snippet
# Les extraits contiennent `<span class="searchmatch">` et l'entité
# `&#039;` : les deux obstacles réels du nettoyage.
WIKIPEDIA_JSON: dict[str, Any] = {
    "batchcomplete": "",
    "continue": {"sroffset": 3, "continue": "-||"},
    "query": {
        "searchinfo": {"totalhits": 7},
        "search": [
            {
                "ns": 0,
                "title": "La Compagnie noire (roman)",
                "pageid": 3640661,
                "snippet": "les <span class=\"searchmatch\">tensions</span> entre Temp\u00eate et <span class=\"searchmatch\">Transformeur</span>, et se retrouve t\u00e9moin du meurtre du Pendu. Les discordes persistent m\u00eame lors <span class=\"searchmatch\">de</span> la bataille finale <span class=\"searchmatch\">de</span> Charme",
            },
            {
                "ns": 0,
                "title": "Mesut \u00d6zil",
                "pageid": 2745457,
                "snippet": "son meilleur \u00ab\u00a0<span class=\"searchmatch\">transformeur</span>\u00a0\u00bb, Olivier Giroud et la seconde pour Tom\u00e1\u0161 Rosick\u00fd (2-0). Apr\u00e8s une d\u00e9faite 1-3 en huiti\u00e8mes <span class=\"searchmatch\">de</span> finale aller <span class=\"searchmatch\">de</span> Ligue des champions",
            },
            {
                "ns": 0,
                "title": "Intelligence artificielle",
                "pageid": 1548,
                "snippet": "sont des mod\u00e8les <span class=\"searchmatch\">de</span> langage ayant des milliards <span class=\"searchmatch\">de</span> param\u00e8tres. Ils reposent tr\u00e8s souvent sur l&#039;architecture <span class=\"searchmatch\">transformeur</span>. Les <span class=\"searchmatch\">transformeurs</span> g\u00e9n\u00e8rent pr\u00e8s des",
            },
        ],
    },
}

# https://api.openalex.org/works?search=transformer+attention&per-page=2
# Capture réelle, allégée aux champs que le parseur lit. L'index inversé
# est celui de l'API, mot pour mot et position pour position : c'est lui
# qui éprouve la reconstruction.
# Les deux index ci-dessous sont recopiés du texte JSON que l'API rend,
# position pour position. Ils sont stockés en TEXTE et non en littéral
# Python : un littéral ferait que deux écritures de la même clé — cas réel
# dans un index inversé, où « a » revient dix fois — s'écraseraient
# silencieusement, et le jeu de tests testerait autre chose que l'API.
# Les deux index ci-dessous sont recopiés du texte JSON que l'API rend,
# position pour position, sans une seule retouche. Ils sont stockés en TEXTE
# et non en littéral Python : dans un littéral, deux écritures de la même clé
# s'écraseraient silencieusement — cas réel dans un index inversé, où « a »
# revient dix fois — et le jeu de tests éprouverait autre chose que l'API.
OPENALEX_ABSTRACT_SWIN = (
    '{"This": [0, 95], "paper": [1], "presents": [2], "a": [3, 14, 64, 125, 172], '
    '"new": [4], "vision": [5, 27, 129, 197], "Transformer,": [6, 9], "called": [7], '
    '"Swin": [8, 119], "that": [10], "capably": [11], "serves": [12], '
    '"as": [13, 36, 144, 196], "general-purpose": [15], "backbone": [16], '
    '"for": [17, 92, 210], "computer": [18], "vision.": [19], "Challenges": [20], '
    '"in": [21, 39, 51, 56], "adapting": [22], "Transformer": [23, 66, 120], '
    '"from": [24, 29], "language": [25], "to": [26, 54, 85, 101, 113], "arise": [28], '
    '"differences": [30], "between": [31], "the": [32, 40, 46, 99, 168, 191, 203], '
    '"two": [33], "domains,": [34], "such": [35, 143], "large": [37, 173], '
    '"variations": [38], "scale": [41], "of": [42, 49, 118, 128, 175, 193], '
    '"visual": [43], "entities": [44], '
    '"and": [45, 106, 139, 150, 157, 179, 185, 202, 215], "high": [47], '
    '"resolution": [48], "pixels": [50], "images": [52], "compared": [53], "words": [55], '
    '"text.": [57], "To": [58], "address": [59], "these": [60], "differences,": [61], '
    '"we": [62], "propose": [63], "hierarchical": [65, 96, 200], "whose": [67], '
    '"representation": [68], "is": [69], "computed": [70], "with": [71, 111, 124], '
    '"Shifted": [72], "windows.": [73], "The": [74, 199, 213], "shifted": [75, 204], '
    '"windowing": [76], "scheme": [77], "brings": [78], "greater": [79], '
    '"efficiency": [80], "by": [81, 171], "limiting": [82], "self-attention": [83], '
    '"computation": [84], "non-overlapping": [86], "local": [87], "windows": [88], '
    '"while": [89], "also": [90, 207], "allowing": [91], "cross-window": [93], '
    '"connection.": [94], "architecture": [97], "has": [98, 107], "flexibility": [100], '
    '"model": [102], "at": [103, 220], "various": [104], "scales": [105], "linear": [108], '
    '"computational": [109], "complexity": [110], "respect": [112], "image": [114, 132], '
    '"size.": [115], "These": [116], "qualities": [117], "make": [121], "it": [122], '
    '"compatible": [123], "broad": [126], "range": [127], "tasks,": [130], '
    '"including": [131], "classification": [133], "(87.3": [134], "top-1": [135], '
    '"accuracy": [136], "on": [137, 154, 162, 183, 188], "ImageNet-1K)": [138], '
    '"dense": [140], "prediction": [141], "tasks": [142], "object": [145], '
    '"detection": [146], "(58.7": [147], "box": [148, 177], "AP": [149, 153, 178, 182], '
    '"51.1": [151], "mask": [152, 181], "COCO": [155], "test-dev)": [156], '
    '"semantic": [158], "segmentation": [159], "(53.5": [160], "mIoU": [161, 187], '
    '"ADE20K": [163], "val).": [164], "Its": [165], "performance": [166], '
    '"surpasses": [167], "previous": [169], "state-of-the-art": [170], "margin": [174], '
    '"+2.7": [176], "+2.6": [180], "COCO,": [184], "+3.2": [186], "ADE20K,": [189], '
    '"demonstrating": [190], "potential": [192], "Transformer-based": [194], '
    '"models": [195, 216], "backbones.": [198], "design": [201], "window": [205], '
    '"approach": [206], "prove": [208], "beneficial": [209], "all-MLP": [211], '
    '"architectures.": [212], "code": [214], "are": [217], "publicly": [218], '
    '"available": [219], "https://github.com/microsoft/Swin-Transformer.": [221]}'
)

OPENALEX_ABSTRACT_MASK2FORMER = (
    '{"Image": [0], "segmentation": [1, 52, 110, 116, 123], "groups": [2], "pixels": [3], '
    '"with": [4], "different": [5], "semantics,": [6], "e.g.,": [7], "category": [8], '
    '"or": [9, 56], "instance": [10, 55, 115], "membership.": [11], "Each": [12], '
    '"choice": [13], "of": [14, 23, 48], "semantics": [15, 22], "defines": [16], '
    '"a": [17, 44, 94, 105], "task.": [18, 36], "While": [19], "only": [20], '
    '"the": [21, 79, 89], "each": [24, 35], "task": [25, 53], "differ,": [26], '
    '"current": [27], "research": [28, 80], "focuses": [29], "on": [30, 97, 113, 119], '
    '"designing": [31], "spe-cialized": [32], "architectures": [33, 92], "for": [34, 108], '
    '"We": [37], "present": [38], "Masked-": [39], "attention": [40], "Mask": [41], '
    '"Transformer": [42], "(Mask2Former),": [43], "new": [45, 106], "archi-tecture": [46], '
    '"capable": [47], "addressing": [49], "any": [50], "image": [51], "(panoptic,": [54], '
    '"semantic).": [57], "Its": [58], "key": [59], "components": [60], "in-clude": [61], '
    '"masked": [62], "attention,": [63], "which": [64], "extracts": [65], '
    '"localized": [66], "features": [67], "by": [68, 82, 93], "constraining": [69], '
    '"cross-attention": [70], "within": [71], "predicted": [72], "mask": [73], '
    '"regions.": [74], "In": [75], "addition": [76], "to": [77], "reducing": [78], '
    '"effort": [81], "at": [83], "least": [84], "three": [85], "times,": [86], "it": [87], '
    '"outperforms": [88], "best": [90], "specialized": [91], "significant": [95], '
    '"margin": [96], "four": [98], "popular": [99], "datasets.": [100], "Most": [101], '
    '"no-tably,": [102], "Mask2Former": [103], "sets": [104], "state-of-the-art": [107], '
    '"panoptic": [109], "(57.8": [111], "PQ": [112], "COCO),": [114], "(50.1": [117], '
    '"AP": [118], "COCO)": [120], "and": [121], "semantic": [122], "(57.7": [124], '
    '"mIoU": [125], "onADE20K).": [126]}'
)

OPENALEX_JSON: dict[str, Any] = {
    "meta": {"count": 597203, "page": 1, "per_page": 2},
    "results": [
        {
            "id": "https://openalex.org/W3138516171",
            "doi": "https://doi.org/10.1109/iccv48922.2021.00986",
            "title": "Swin Transformer: Hierarchical Vision Transformer using Shifted Windows",
            "display_name": "Swin Transformer: Hierarchical Vision Transformer using Shifted Windows",
            "publication_year": 2021,
            "publication_date": "2021-10-01",
            "type": "conference-paper",
            "cited_by_count": 32965,
            "abstract_inverted_index": json.loads(OPENALEX_ABSTRACT_SWIN),
        },
        {
            "id": "https://openalex.org/W4226069476",
            "doi": "https://doi.org/10.1109/cvpr52688.2022.00135",
            "title": "Masked-attention Mask Transformer for Universal Image Segmentation",
            "display_name": "Masked-attention Mask Transformer for Universal Image Segmentation",
            "publication_year": 2022,
            "publication_date": "2022-06-01",
            "type": "conference-paper",
            "cited_by_count": 3260,
            "abstract_inverted_index": json.loads(OPENALEX_ABSTRACT_MASK2FORMER),
        },
    ],
}

# Forme réelle d'une réponse `format=json` de SearXNG. Aucune instance ne
# peut être jointe depuis les tests : le jeu reproduit la forme, pas un
# service. Les domaines sont en `.test`, réservé et non résolvable.
SEARXNG_JSON: dict[str, Any] = {
    "query": "éthernet",
    "number_of_results": 2,
    "results": [
        {
            "url": "https://exemple.test/ethernet",
            "title": "Ethernet — Wikipédia",
            "content": "Ethernet est une technologie de communication de données.",
            "engine": "wikipedia",
            "score": 4.0,
            "positions": [1],
        },
        {
            "url": "https://exemple.test/ethernet-02",
            "title": "Ethernet",
            "content": "Description de la norme IEEE 802.3.",
            "engine": "qwant",
            "score": 3.1,
            "positions": [2],
        },
    ],
    "answers": [],
    "corrections": [],
    "infoboxes": [],
    "suggestions": [],
    "unresponsive_engines": [],
}


# --------------------------------------------------------------------------
# Serveur local figé
# --------------------------------------------------------------------------
class _Routes:
    """Ce que le serveur a reçu. Sert à prouver ce qui a été demandé."""

    def __init__(self) -> None:
        self.requests: list[str] = []


class _Server(ThreadingHTTPServer):
    # `daemon_threads` : la route « lente » dort plus longtemps que le
    # test, et un thread de serveur ne doit pas empêcher la sortie.
    daemon_threads = True
    routes = _Routes()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Un client qui raccroche pendant qu'on lui écrit (`/boom/slow`)
        # est un comportement normal de test, pas une erreur à afficher.
        return


class _Handler(BaseHTTPRequestHandler):
    """Répond par des charges constantes. Jamais de réseau sortant."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature stdlib
        return  # un test qui écrit sur stderr n'est pas un test lisible

    def _send(self, status: int, body: str, content_type: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - nom imposé par BaseHTTPRequestHandler
        server = cast(_Server, self.server)
        server.routes.requests.append(self.path)
        path = urllib.parse.urlsplit(self.path).path

        # Les routes s'appliquent par PRÉFIXE : un moteur ajoute toujours
        # son propre suffixe à l'URL qu'on lui donne, donc `/boom/500` sert
        # aussi bien `/boom/500/api/query` que `/boom/500/search`.
        if path.startswith("/api/query"):
            self._send(200, ARXIV_ATOM, "application/atom+xml")
        elif path.startswith("/api/titre-coupe"):
            self._send(200, ARXIV_ATOM_TITRE_COUPE, "application/atom+xml")
        elif path.startswith("/w/api.php"):
            self._send(200, json.dumps(WIKIPEDIA_JSON, ensure_ascii=False), "application/json")
        elif path.startswith("/works"):
            self._send(200, json.dumps(OPENALEX_JSON, ensure_ascii=False), "application/json")
        elif path.startswith("/search"):
            self._send(200, json.dumps(SEARXNG_JSON, ensure_ascii=False), "application/json")
        elif path.startswith("/casse/xml"):
            self._send(200, "<feed><entry><title>coupé", "application/atom+xml")
        elif path.startswith("/casse/json"):
            self._send(200, '{"results": [', "application/json")
        elif path.startswith("/casse/page-d-erreur"):
            self._send(200, "<html><body>page d'erreur</body></html>", "text/html")
        elif path.startswith("/boom/500"):
            self._send(500, "erreur interne du serveur de test", "text/plain")
        elif path.startswith("/boom/503"):
            self._send(503, "service indisponible", "text/plain")
        elif path.startswith("/boom/429"):
            self._send(429, "trop de requêtes", "text/plain")
        elif path.startswith("/boom/403"):
            self._send(403, "<html><body>Forbidden</body></html>", "text/html")
        elif path.startswith("/boom/lent"):
            time.sleep(0.8)  # plus long que le délai imparti par les tests
            return
        else:
            self._send(404, "route inconnue", "text/plain")


class _LocalApi:
    """Adresse du serveur figé, et ce qu'il a reçu."""

    def __init__(self, base: str) -> None:
        self.base = base

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    @property
    def last_request(self) -> str:
        return _Server.routes.requests[-1]


@pytest.fixture(scope="module")
def local_api() -> Iterator[_LocalApi]:
    """Un serveur HTTP unique pour tout le module."""
    _Server.routes = _Routes()
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield _LocalApi(f"http://{host}:{port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Le test ne sort pas de la machine ; le reste de `net.py` reste réel.

    Deux choses seulement sont neutralisées. La **résolution DNS**, parce
    que `127.0.0.1` n'est pas une adresse globale et que `net.resolve_global`
    la refuse à raison : un test ne peut pas faire autrement. Et
    l'**intervalle d'une seconde** entre deux appels au même hôte, qui
    appartient au serveur de test et non aux moteurs — la porte propre à
    arXiv, elle, est testée pour elle-même.

    La socket, les plafonds d'octets, le budget de tour et la porte de
    politesse restent, eux, le vrai code.

    L'installation est aussi rendue NUE : ni la variable ni le fichier de
    réglages ne déclarent d'instance SearXNG. Le fichier est donc pointé sur
    un chemin temporaire VIDE — sans cela, un `web_search.settings` laissé sur
    la machine du développeur ferait apparaître le moteur dans tous les tests
    du module, et les tests d'ordre de résolution mesureraient SA machine.
    """
    monkeypatch.setattr(net, "resolve_global", lambda host, port: ["127.0.0.1"])
    monkeypatch.setattr(net, "HOST_MIN_INTERVAL_S", 0.0)
    net.reset_host_gate()
    reset_arxiv_gate()
    monkeypatch.delenv(SEARXNG_URL_ENV, raising=False)
    monkeypatch.setenv(SETTINGS_ENV, str(tmp_path / SETTINGS_FILENAME))


def _query(text: str = "transformateur de tension") -> WebQuery:
    return WebQuery(text=text, source_type="general", freshness="cached")


def _deadline(seconds: float = 5.0) -> float:
    return time.monotonic() + seconds


# ==========================================================================
# arXiv
# ==========================================================================
def test_arxiv_parses_the_two_entries_of_the_frozen_feed(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/api/query"))
    answer = provider.search(_query("deep research agents"), max_results=2, deadline=_deadline())

    assert answer.ok, answer.error
    assert len(answer.hits) == 2
    assert answer.hits[0].title == "Deep Learning and Computational Physics (Lecture Notes)"
    assert answer.hits[1].title == "On the approximation of rough functions with deep neural networks"
    assert answer.hits[0].rank == 1 and answer.hits[1].rank == 2
    assert answer.hits[0].published_at == "2023-01-03T03:56:19Z"
    assert answer.hits[1].published_at == "2019-12-13T22:48:36Z"


def test_arxiv_rewrites_the_http_identifier_to_https(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/api/query"))
    answer = provider.search(_query("deep research agents"), max_results=2, deadline=_deadline())

    # L'API rend `http://arxiv.org/abs/…` : proposer cette adresse, c'est
    # proposer une redirection, et le fetcher ne suit aucune redirection.
    assert [hit.url for hit in answer.hits] == [
        "https://arxiv.org/abs/2301.00942v1",
        "https://arxiv.org/abs/1912.06732v2",
    ]


def test_arxiv_collapses_the_newlines_the_api_really_sends(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/api/query"))
    answer = provider.search(_query("deep research agents"), max_results=2, deadline=_deadline())

    # Le résumé capturé est indenté et coupé en deux paragraphes ; lu tel
    # quel il s'afficherait sur quatre lignes.
    assert "\n" not in answer.hits[0].snippet
    assert "  " not in answer.hits[0].snippet
    assert answer.hits[0].snippet.startswith("These notes were compiled as lecture notes")
    assert "Applied Mathematics. The main objective" in answer.hits[0].snippet


def test_arxiv_truncates_the_long_summary(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/api/query"))
    answer = provider.search(_query("deep research agents"), max_results=2, deadline=_deadline())

    # 600 caractères de texte plus la marque de troncature, et une coupe
    # qui ne coupe jamais un mot : la longueur réelle dépend donc de la
    # position du dernier espace, d'où la borne plutôt qu'une égalité.
    assert 595 <= len(answer.hits[0].snippet) <= 601
    assert answer.hits[0].snippet.endswith("…")
    assert len(answer.hits[1].snippet) < len(answer.hits[0].snippet)
    # Le résumé complet, lui, ne tient pas dans l'extrait rendu.
    assert len(answer.hits[0].snippet) < 1100


def test_arxiv_collapses_a_title_split_over_two_lines(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/api/titre-coupe"))
    answer = provider.search(_query("deep research agents"), max_results=1, deadline=_deadline())

    assert answer.ok, answer.error
    # Un titre coupé s'afficherait sur deux lignes et se couperait au
    # milieu d'une ligne à l'impression.
    assert answer.hits[0].title == "Deep Research Agents: A Systematic Examination And Roadmap"
    assert "\n" not in answer.hits[0].snippet
    assert answer.hits[0].snippet == "Résumé court. Seconde ligne du résumé."


def test_arxiv_encodes_the_query_in_the_request(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/api/query"))
    provider.search(_query("transformateur de tension"), max_results=3, deadline=_deadline())

    sent = urllib.parse.urlsplit(local_api.last_request)
    params = urllib.parse.parse_qs(sent.query)
    assert params["search_query"] == ["transformateur de tension"]
    assert params["start"] == ["0"]
    assert params["max_results"] == ["3"]
    # Une espace ne doit jamais atteindre l'URL telle quelle : `validate_url`
    # refuse les URL qui en contiennent, et ce serait un refus pour la
    # mauvaise raison.
    assert " " not in local_api.last_request


def test_arxiv_default_api_url_is_the_official_one() -> None:
    assert ArxivProvider().api_url == ARXIV_API_URL
    assert ARXIV_API_URL == "http://export.arxiv.org/api/query"


def test_arxiv_keeps_the_three_second_rule(local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch) -> None:
    # La règle des conditions d'utilisation est une constante, pas un
    # réglage : elle se vérifie sans lancer de requête.
    assert ARXIV_MIN_INTERVAL_S == 3.0
    assert arxiv_module.ARXIV_MIN_INTERVAL_S == 3.0

    # Et la porte est réelle : ramenée à 50 ms pour ne pas faire perdre
    # trois secondes à la suite, elle retient quand même le second appel.
    monkeypatch.setattr(arxiv_module, "ARXIV_MIN_INTERVAL_S", 0.05)
    provider = ArxivProvider(api_url=local_api.url("/api/query"))
    provider.search(_query("a"), max_results=1, deadline=_deadline())
    started = time.monotonic()
    provider.search(_query("b"), max_results=1, deadline=_deadline())
    assert time.monotonic() - started >= 0.04


def test_arxiv_reset_lets_the_next_call_go_out_immediately(local_api: _LocalApi) -> None:
    # La porte d'arXiv est une contrainte ENTRE les requêtes, pas une
    # contrainte de durée : un tour begun après une pause ne doit pas
    # attendre trois secondes pour rien.
    monkeypatched = arxiv_module.ARXIV_MIN_INTERVAL_S
    arxiv_module.ARXIV_MIN_INTERVAL_S = 0.2
    try:
        provider = ArxivProvider(api_url=local_api.url("/api/query"))
        provider.search(_query("a"), max_results=1, deadline=_deadline())
        reset_arxiv_gate()
        started = time.monotonic()
        provider.search(_query("b"), max_results=1, deadline=_deadline())
        assert time.monotonic() - started < 0.15
    finally:
        arxiv_module.ARXIV_MIN_INTERVAL_S = monkeypatched


def test_arxiv_malformed_xml_is_an_error_not_an_exception(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/casse/xml"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert answer.error.startswith(f"[{PROVIDER_MALFORMED}]")
    assert answer.hits == ()


def test_arxiv_a_response_that_is_not_a_feed_is_named(local_api: _LocalApi) -> None:
    # Une page d'erreur servie avec un 200 : une liste vide se lirait
    # « rien trouvé », et ce serait faux.
    provider = ArxivProvider(api_url=local_api.url("/casse/page-d-erreur"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert PROVIDER_MALFORMED in answer.error
    assert "racine" in answer.error


def test_arxiv_http_500_gives_an_actionable_message(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/boom/500"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert f"[{NETWORK_BLOCKED}]" in answer.error
    assert "500" in answer.error
    # Le repli existe : le message nomme l'autre moteur plutôt que de
    # laisser le modèle se demander s'il doit réessayer indéfiniment.
    assert "OpenAlex" in answer.error


def test_arxiv_http_429_names_the_three_second_rule(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/boom/429"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert "429" in answer.error
    assert "3 s" in answer.error


def test_arxiv_timeout_gives_a_deadline_error(local_api: _LocalApi) -> None:
    provider = ArxivProvider(api_url=local_api.url("/boom/lent"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline(0.15))

    assert not answer.ok
    assert answer.error is not None
    assert f"[{NETWORK_BLOCKED}]" in answer.error
    assert "délai" in answer.error


def test_arxiv_refuses_a_loopback_address(local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch) -> None:
    # arXiv ne passe jamais par la voie privée : une instance locale
    # d'arXiv n'est pas une source qu'on doit admettre. On annule la
    # neutralisation DNS de la fixture, qui rendrait ce test absurde.
    monkeypatch.undo()
    provider = ArxivProvider(api_url=local_api.url("/api/query"))
    answer = provider.search(_query("x"), max_results=1, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert f"[{NETWORK_BLOCKED}]" in answer.error
    assert "127.0.0.1" in answer.error


# ==========================================================================
# Wikipédia
# ==========================================================================
def test_wikipedia_strips_the_searchmatch_spans(local_api: _LocalApi) -> None:
    provider = WikipediaProvider(lang="fr", api_url=local_api.url("/w/api.php"))
    answer = provider.search(_query("transformateur de tension"), max_results=3, deadline=_deadline())

    assert answer.ok, answer.error
    assert len(answer.hits) == 3
    first = answer.hits[0].snippet
    assert "<span" not in first
    assert "class=" not in first
    assert "<" not in first and ">" not in first
    # Le texte reste, l'habillage disparaît.
    assert first.startswith("les tensions entre Tempête et Transformeur")
    assert "Charme" in first


def test_wikipedia_unescapes_entities_after_removing_the_tags(local_api: _LocalApi) -> None:
    provider = WikipediaProvider(lang="fr", api_url=local_api.url("/w/api.php"))
    answer = provider.search(_query("transformateur de tension"), max_results=3, deadline=_deadline())

    # `l&#039;architecture` est une apostrophe, pas du HTML : les entités
    # sont décodées, puis les balises retirées — dans cet ordre, pour qu'une
    # entité ne puisse pas fabriquer une balise.
    assert "l'architecture transformeur" in answer.hits[2].snippet
    assert "&#039;" not in answer.hits[2].snippet
    assert "« transformeur »" in answer.hits[1].snippet
    assert "é" in answer.hits[1].snippet


def test_wikipedia_never_renders_the_html_it_strips() -> None:
    from backend.services.web.providers.wikipedia import clean_snippet

    # Le HTML est enlevé par une expression régulière, jamais interprété.
    # L'invariant vérifié : plus un seul chevron ne survit, même quand il
    # est venu d'une entité et non d'une balise réelle. Ce qui reste est
    # du texte, jamais une balise.
    hostile = '<span class="searchmatch">a</span><script>alert(1)</script>&lt;b&gt;gras&lt;/b&gt;'
    cleaned = clean_snippet(hostile)
    assert "<" not in cleaned and ">" not in cleaned
    assert "&lt;" not in cleaned
    # Le texte nu subsiste : on efface le balisage, on n'efface pas le propos.
    assert "alert(1)" in cleaned
    assert "gras" in cleaned


def test_wikipedia_builds_the_article_url_from_the_canonical_title(local_api: _LocalApi) -> None:
    provider = WikipediaProvider(lang="fr", api_url=local_api.url("/w/api.php"))
    answer = provider.search(_query("transformateur de tension"), max_results=3, deadline=_deadline())

    assert answer.hits[0].url == "https://fr.wikipedia.org/wiki/La_Compagnie_noire_(roman)"
    assert answer.hits[2].url == "https://fr.wikipedia.org/wiki/Intelligence_artificielle"


def test_wikipedia_sends_the_documented_api_parameters(local_api: _LocalApi) -> None:
    provider = WikipediaProvider(lang="fr", api_url=local_api.url("/w/api.php"))
    provider.search(_query("transformateur de tension"), max_results=3, deadline=_deadline())

    sent = urllib.parse.urlsplit(local_api.last_request)
    params = urllib.parse.parse_qs(sent.query)
    assert params["action"] == ["query"]
    assert params["list"] == ["search"]
    assert params["format"] == ["json"]
    assert params["srprop"] == ["snippet"]
    assert params["srlimit"] == ["3"]
    assert params["srsearch"] == ["transformateur de tension"]


def test_wikipedia_defaults_to_french() -> None:
    assert WikipediaProvider().lang == "fr"
    assert WikipediaProvider().api_url == "https://fr.wikipedia.org/w/api.php"


@pytest.mark.parametrize("lang", ["fr", "en", "pt", "zh-min-nan", "fr-be", "simple"])
def test_wikipedia_accepts_the_documented_language_codes(lang: str) -> None:
    assert WikipediaProvider(lang=lang).lang == lang


@pytest.mark.parametrize(
    "lang",
    ["", "FR", "fr/../../x", "fr@evil.example", "f", "fr.", "-fr", "fr--be", "fr:80", "fr "],
)
def test_wikipedia_refuses_a_language_code_it_cannot_validate(lang: str) -> None:
    # `lang` entre dans le NOM D'HÔTE : un code non validé est un hôte
    # choisi par le texte. Fail Fast, pas de correction silencieuse.
    with pytest.raises(ValueError, match="langue"):
        WikipediaProvider(lang=lang)


def test_wikipedia_malformed_json_is_an_error_not_an_exception(local_api: _LocalApi) -> None:
    provider = WikipediaProvider(lang="fr", api_url=local_api.url("/casse/json"))
    answer = provider.search(_query("x"), max_results=3, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert answer.error.startswith(f"[{PROVIDER_MALFORMED}]")
    assert answer.hits == ()


def test_wikipedia_a_search_without_results_is_empty_not_an_error() -> None:
    from backend.services.web.providers.wikipedia import parse_results

    # Une recherche sans résultat fait disparaître la clé `query` : c'est
    # un résultat vide, pas une panne.
    hits, skipped = parse_results('{"batchcomplete":""}', "fr", "wikipedia", "encyclopédie")
    assert hits == ()
    assert skipped == ()


def test_wikipedia_http_503_gives_an_actionable_message(local_api: _LocalApi) -> None:
    provider = WikipediaProvider(lang="fr", api_url=local_api.url("/boom/503"))
    answer = provider.search(_query("x"), max_results=3, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert "503" in answer.error
    assert "Wikipédia" in answer.error


def test_wikipedia_timeout_gives_a_deadline_error(local_api: _LocalApi) -> None:
    provider = WikipediaProvider(lang="fr", api_url=local_api.url("/boom/lent"))
    answer = provider.search(_query("x"), max_results=3, deadline=_deadline(0.15))

    assert not answer.ok
    assert answer.error is not None
    assert "délai" in answer.error


def test_wikipedia_skips_a_result_with_no_title() -> None:
    from backend.services.web.providers.wikipedia import parse_results

    payload = json.dumps({"query": {"search": [{"snippet": "sans titre"}, "pas un objet"]}})
    hits, skipped = parse_results(payload, "fr", "wikipedia", "encyclopédie")
    assert hits == ()
    assert len(skipped) == 2
    assert all(item.reason == "not_selected" for item in skipped)


# ==========================================================================
# OpenAlex
# ==========================================================================
def test_openalex_rebuilds_the_abstract_in_reading_order(local_api: _LocalApi) -> None:
    provider = OpenAlexProvider(api_url=local_api.url("/works"))
    answer = provider.search(_query("transformer attention"), max_results=2, deadline=_deadline())

    assert answer.ok, answer.error
    assert len(answer.hits) == 2
    # L'index est un INDEX, pas un texte : « Transformer, » est aux
    # positions 6 ET 9, et il faut le remettre à sa place.
    assert answer.hits[0].snippet.startswith(
        "This paper presents a new vision Transformer, called Swin Transformer, that "
        "capably serves as a general-purpose backbone for computer vision."
    )
    assert answer.hits[1].snippet.startswith("Image segmentation groups pixels with different semantics")


def test_openalex_truncates_the_rebuilt_abstract(local_api: _LocalApi) -> None:
    provider = OpenAlexProvider(api_url=local_api.url("/works"))
    answer = provider.search(_query("transformer attention"), max_results=2, deadline=_deadline())

    # 600 caractères de texte plus la marque de troncature, et une coupe
    # qui ne coupe jamais un mot : la longueur réelle dépend donc de la
    # position du dernier espace, d'où la borne plutôt qu'une égalité.
    assert 595 <= len(answer.hits[0].snippet) <= 601
    assert answer.hits[0].snippet.endswith("…")
    # Les deux résumés capturés dépassent la borne : les deux sont tronqués,
    # et aucun n'est complété par du texte qui n'existait pas.
    assert answer.hits[1].snippet.endswith("…")
    assert answer.hits[0].snippet.rstrip("…").endswith("computation to")


def test_openalex_does_not_pad_a_short_abstract() -> None:
    from backend.services.web.providers.openalex import parse_results

    payload = json.dumps({"results": [
        {"id": "https://openalex.org/W3", "title": "Court",
         "abstract_inverted_index": {"Un": [0], "résumé": [1], "court.": [2]}},
    ]})
    hits, _ = parse_results(payload, "openalex", "notices")
    assert hits[0].snippet == "Un résumé court."


def test_openalex_prefers_the_doi_over_the_openalex_identifier(local_api: _LocalApi) -> None:
    provider = OpenAlexProvider(api_url=local_api.url("/works"))
    answer = provider.search(_query("transformer attention"), max_results=2, deadline=_deadline())

    assert answer.hits[0].url == "https://doi.org/10.1109/iccv48922.2021.00986"
    assert answer.hits[1].url == "https://doi.org/10.1109/cvpr52688.2022.00135"
    assert answer.hits[0].published_at == "2021-10-01"
    assert answer.hits[0].title.startswith("Swin Transformer")
    assert answer.hits[0].rank == 1


def test_openalex_keeps_the_openalex_id_when_there_is_no_doi() -> None:
    from backend.services.web.providers.openalex import parse_results

    payload = json.dumps({"results": [
        {"id": "https://openalex.org/W1", "title": "Sans DOI", "publication_date": "2020-01-01"},
    ]})
    hits, _ = parse_results(payload, "openalex", "notices")
    assert hits[0].url == "https://openalex.org/W1"
    assert hits[0].published_at == "2020-01-01"


def test_openalex_falls_back_on_display_name_and_tolerates_a_null_date() -> None:
    from backend.services.web.providers.openalex import parse_results

    payload = json.dumps({"results": [
        {"id": "https://openalex.org/W2", "title": None, "display_name": "Titre de repli",
         "publication_date": None},
    ]})
    hits, _ = parse_results(payload, "openalex", "notices")
    assert hits[0].title == "Titre de repli"
    assert hits[0].published_at is None


def test_openalex_says_so_when_a_notice_has_no_address() -> None:
    from backend.services.web.providers.openalex import parse_results

    payload = json.dumps({"results": [{"title": "Sans DOI ni identifiant"}]})
    hits, skipped = parse_results(payload, "openalex", "notices")
    assert hits == ()
    assert len(skipped) == 1
    assert skipped[0].reason == "not_selected"
    assert "sans DOI" in skipped[0].detail


def test_openalex_omits_mailto_when_the_project_has_no_contact(local_api: _LocalApi) -> None:
    # `net.PROJECT_CONTACT` est vide tant que le propriétaire ne l'a pas
    # rempli. On n'invente pas d'adresse : un contact fabriqué est une
    # adresse qui n'existe pas, vers laquelle on dirige le trafic d'un tiers.
    assert net.PROJECT_CONTACT == ""
    provider = OpenAlexProvider(api_url=local_api.url("/works"))
    provider.search(_query("transformer attention"), max_results=2, deadline=_deadline())

    params = urllib.parse.parse_qs(urllib.parse.urlsplit(local_api.last_request).query)
    assert "mailto" not in params
    assert params["search"] == ["transformer attention"]
    assert params["per-page"] == ["2"]


def test_openalex_sends_mailto_when_the_project_has_a_contact(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(net, "PROJECT_CONTACT", "https://thot.example/contact")
    provider = OpenAlexProvider(api_url=local_api.url("/works"))
    provider.search(_query("transformer attention"), max_results=2, deadline=_deadline())

    params = urllib.parse.parse_qs(urllib.parse.urlsplit(local_api.last_request).query)
    assert params["mailto"] == ["https://thot.example/contact"]


def test_openalex_malformed_json_is_an_error_not_an_exception(local_api: _LocalApi) -> None:
    provider = OpenAlexProvider(api_url=local_api.url("/casse/json"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert answer.error.startswith(f"[{PROVIDER_MALFORMED}]")
    assert answer.hits == ()


def test_openalex_refuses_a_payload_whose_results_are_not_a_list() -> None:
    from backend.services.web.providers.openalex import parse_results

    with pytest.raises(WebError) as raised:
        parse_results('{"results": {"unexpected": true}}', "openalex", "notices")
    assert raised.value.code == PROVIDER_MALFORMED


def test_openalex_rebuilds_an_empty_index_as_empty_text() -> None:
    from backend.services.web.providers.openalex import rebuild_abstract

    assert rebuild_abstract(None) == ""
    assert rebuild_abstract({}) == ""
    assert rebuild_abstract({"mot": "pas une liste"}) == ""


def test_openalex_http_500_gives_an_actionable_message(local_api: _LocalApi) -> None:
    provider = OpenAlexProvider(api_url=local_api.url("/boom/500"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert "500" in answer.error


def test_openalex_http_429_points_at_the_missing_contact(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 403/429 chez OpenAlex, c'est le « polite pool » qui dit non. La
    # cause est invisible dans la réponse : le message doit la nommer.
    provider = OpenAlexProvider(api_url=local_api.url("/boom/429"))
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())
    assert answer.error is not None
    assert "PROJECT_CONTACT" in answer.error

    monkeypatch.setattr(net, "PROJECT_CONTACT", "contact@thot.example")
    answer = provider.search(_query("x"), max_results=2, deadline=_deadline())
    assert answer.error is not None
    assert "PROJECT_CONTACT" not in answer.error
    assert "attendre" in answer.error


# ==========================================================================
# SearXNG
# ==========================================================================
def test_searxng_is_not_built_without_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cette assertion porte sur l'ABSENCE TOTALE de déclaration : ni la
    # variable, ni le fichier de réglages. Le test construit donc lui-même
    # cette absence — le fichier est pointé sur un chemin temporaire vide —
    # au lieu d'hériter de ce que la machine du développeur a laissé là.
    monkeypatch.delenv(SEARXNG_URL_ENV, raising=False)
    monkeypatch.setenv(SETTINGS_ENV, str(tmp_path / SETTINGS_FILENAME))

    assert SEARXNG_URL_ENV not in os.environ
    assert not (tmp_path / SETTINGS_FILENAME).exists()
    assert SearxngProvider.from_environment() is None
    assert "searxng" not in [p.name for p in registry.applicable_providers("general")]


def test_searxng_is_built_when_the_user_declares_an_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, "http://127.0.0.1:8080")
    provider = SearxngProvider.from_environment()
    assert provider is not None
    assert provider.base_url == "http://127.0.0.1:8080"
    # La barre oblique finale est mangée : l'URL rendue ne doit pas
    # contenir le `//` qu'un serveur ne comprendrait pas comme un chemin.
    assert SearxngProvider("http://127.0.0.1:8080/").base_url == "http://127.0.0.1:8080"


@pytest.mark.parametrize("declared", ["127.0.0.1:8080", "file:///tmp/x", "ftp://h/x", "http://"])
def test_searxng_refuses_a_declaration_it_cannot_use(
    declared: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, declared)
    with pytest.raises(ValueError):
        SearxngProvider.from_environment()


@pytest.mark.parametrize("declared", ["", "   "])
def test_searxng_treats_a_blank_declaration_as_absent(
    declared: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Une variable vide n'est pas une déclaration fausse, c'est l'absence
    # de déclaration : le moteur n'existe pas, et rien ne lève.
    monkeypatch.setenv(SEARXNG_URL_ENV, declared)
    assert SearxngProvider.from_environment() is None


def test_searxng_asks_the_instance_for_json_with_an_encoded_query(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, local_api.base)
    provider = SearxngProvider.from_environment()
    assert provider is not None

    answer = provider.search(_query("réseau éthernet & câblage"), max_results=5, deadline=_deadline())

    assert answer.ok, answer.error
    sent = urllib.parse.urlsplit(local_api.last_request)
    assert sent.path == "/search"
    params = urllib.parse.parse_qs(sent.query)
    # Sans `format=json`, SearXNG rend du HTML : la requête n'aboutirait
    # à rien de lisible.
    assert params["format"] == ["json"]
    assert params["q"] == ["réseau éthernet & câblage"]
    # Le `&` de la requête ne doit pas couper les paramètres en deux.
    assert "%26" in local_api.last_request
    assert "r%C3%A9seau" in local_api.last_request


def test_searxng_says_which_instance_engine_proposed_each_result(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, local_api.base)
    provider = SearxngProvider.from_environment()
    assert provider is not None

    answer = provider.search(_query("éthernet"), max_results=5, deadline=_deadline())

    assert len(answer.hits) == 2
    # Le nom du moteur d'origine est une donnée de l'instance, pas une
    # certification : il va dans l'extrait, jamais dans `provider`.
    assert answer.hits[0].snippet.startswith("[wikipedia] ")
    assert answer.provider == "searxng"
    assert answer.hits[0].url == "https://exemple.test/ethernet"


def test_searxng_keeps_only_the_requested_number_of_results(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, local_api.base)
    provider = SearxngProvider.from_environment()
    assert provider is not None

    answer = provider.search(_query("éthernet"), max_results=1, deadline=_deadline())
    assert len(answer.hits) == 1


def test_searxng_403_points_at_the_settings_file(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, f"{local_api.base}/boom/403")
    provider = SearxngProvider.from_environment()
    assert provider is not None

    answer = provider.search(_query("éthernet"), max_results=5, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert "403" in answer.error
    assert "settings.yml" in answer.error


def test_searxng_malformed_json_names_the_format_setting(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, f"{local_api.base}/casse/json")
    provider = SearxngProvider.from_environment()
    assert provider is not None

    answer = provider.search(_query("éthernet"), max_results=5, deadline=_deadline())

    assert not answer.ok
    assert answer.error is not None
    assert PROVIDER_MALFORMED in answer.error
    assert "settings.yml" in answer.error


def test_searxng_skips_a_result_with_no_address() -> None:
    from backend.services.web.providers.searxng import parse_results

    payload = json.dumps({"results": [
        {"title": "Sans URL", "content": "rien à proposer"},
        {"url": "https://exemple.test/ok", "title": "Avec URL"},
    ]})
    hits, skipped = parse_results(payload, "searxng", "web", 10)
    assert len(hits) == 1
    assert len(skipped) == 1
    assert skipped[0].reason == "not_selected"


def test_searxng_is_the_only_engine_allowed_on_a_private_address(
    local_api: _LocalApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Les trois autres moteurs passent par `resolve_global`, qui refuse le
    # loopback ; seul SearXNG demande la voie privée. Cette comparaison
    # est la garantie que l'exception n'a pas fui.
    monkeypatch.undo()
    for provider in (
        ArxivProvider(api_url=local_api.url("/api/query")),
        WikipediaProvider(lang="fr", api_url=local_api.url("/w/api.php")),
        OpenAlexProvider(api_url=local_api.url("/works")),
    ):
        answer = provider.search(_query("x"), max_results=1, deadline=_deadline())
        assert not answer.ok
        assert answer.error is not None
        assert f"[{NETWORK_BLOCKED}]" in answer.error


def test_the_private_way_opens_only_on_the_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # `allow_private=True` n'est pas une clé passe-partout : il ouvre la
    # machine locale et rien d'autre. Une adresse littérale ne déclenche
    # aucune résolution DNS, donc ce test n'ouvre aucune connexion.
    monkeypatch.undo()
    with pytest.raises(WebError) as raised:
        net.http_get(
            "http://192.168.1.10:8080/search",
            budget=net.NetworkBudget(deadline=time.monotonic() + 2),
            allow_private=True,
        )
    assert raised.value.code == NETWORK_BLOCKED
    assert "loopback" in raised.value.message


def test_the_private_way_refuses_a_public_address_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    with pytest.raises(WebError) as raised:
        net.http_get(
            "http://93.184.216.34/",
            budget=net.NetworkBudget(deadline=time.monotonic() + 2),
            allow_private=True,
        )
    assert raised.value.code == NETWORK_BLOCKED
    assert "loopback" in raised.value.message


def test_the_loopback_way_does_not_trust_the_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Un nom vide ne résout pas : la voie loopback commence par résoudre,
    # elle ne commence pas par croire. Et surtout, elle ne débranche
    # jamais la résolution, qui reste celle du système.
    monkeypatch.undo()
    with pytest.raises(WebError) as raised:
        net.resolve_loopback("", 8080)
    assert raised.value.code == NETWORK_BLOCKED


def test_the_loopback_way_accepts_the_local_machine() -> None:
    assert net.resolve_loopback("127.0.0.1", 8080) == ["127.0.0.1"]


# ==========================================================================
# Résolution
# ==========================================================================
def test_no_engine_covers_news_without_a_configured_instance() -> None:
    assert registry.applicable_providers("news") == ()
    answer = registry.no_provider_answer("news")
    assert not answer.ok
    assert answer.error is not None
    assert f"[{NO_PROVIDER}]" in answer.error
    assert "news" in answer.error
    # Le message dit ce qu'il faudrait, pour que le rendu puisse le dire.
    assert SEARXNG_URL_ENV in answer.error


def test_academic_starts_with_arxiv_then_openalex() -> None:
    chosen = registry.applicable_providers("academic")
    assert [provider.name for provider in chosen] == ["arxiv", "openalex"]


def test_general_prefers_the_local_instance_then_wikipedia(monkeypatch: pytest.MonkeyPatch) -> None:
    without = [p.name for p in registry.applicable_providers("general")]
    assert without == ["wikipedia"]

    monkeypatch.setenv(SEARXNG_URL_ENV, "http://127.0.0.1:8080")
    with_instance = [p.name for p in registry.applicable_providers("general")]
    # SearXNG passe devant Wikipédia parce que c'est la SEULE couverture
    # du web général : une encyclopédie n'est pas le web.
    assert with_instance == ["searxng", "wikipedia"]


def test_news_is_covered_only_by_the_local_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEARXNG_URL_ENV, "http://127.0.0.1:8080")
    chosen = registry.applicable_providers("news")
    assert [provider.name for provider in chosen] == ["searxng"]


def test_official_has_no_engine_at_all() -> None:
    # « Officiel » est une exigence de provenance, pas une classe de
    # moteur : elle se sert par le chemin URL.
    assert registry.applicable_providers("official") == ()
    answer = registry.no_provider_answer("official")
    assert answer.error is not None
    assert f"[{NO_PROVIDER}]" in answer.error


def test_an_unknown_source_type_is_not_silently_emptied() -> None:
    assert registry.applicable_providers("inconnu") == ()


def test_an_engine_that_does_not_declare_the_type_is_never_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # L'ordre ne classe que des moteurs retenus : un moteur qui ne déclare
    # pas le type demandé n'est jamais servi, quoi qu'on ait écrit à côté.
    monkeypatch.setattr(
        registry, "_PREFERRED_ORDER", dict(registry._PREFERRED_ORDER, academic=("wikipedia",))
    )
    assert registry.applicable_providers("academic") == ()


def test_provider_status_triggers_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("http_get")
        raise AssertionError("provider_status() ne doit jamais appeler le réseau")

    monkeypatch.setattr(net, "http_get", _forbidden)
    status = registry.provider_status()

    assert calls == []
    assert [entry["name"] for entry in status] == ["arxiv", "openalex", "wikipedia", "searxng"]


def test_provider_status_announces_what_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {entry["name"]: entry for entry in registry.provider_status()}
    assert before["searxng"]["configured"] is False
    assert before["searxng"]["requires"] is not None
    assert before["searxng"]["covers"] == []
    assert before["arxiv"]["configured"] is True
    assert before["arxiv"]["requires"] is None
    assert before["arxiv"]["covers"] == ["academic"]

    monkeypatch.setenv(SEARXNG_URL_ENV, "http://127.0.0.1:8080")
    after = {entry["name"]: entry for entry in registry.provider_status()}
    assert after["searxng"]["configured"] is True
    assert sorted(after["searxng"]["covers"]) == ["academic", "general", "news"]
    assert after["searxng"]["requires"] is None


# ==========================================================================
# Honnêteté des couvertures
# ==========================================================================
@pytest.mark.parametrize("provider", [
    ArxivProvider(),
    OpenAlexProvider(),
    WikipediaProvider(),
    SearxngProvider("http://127.0.0.1:8080"),
])
def test_every_coverage_is_a_sentence_that_says_what_it_is_not(provider: Any) -> None:
    coverage = provider.coverage
    assert isinstance(coverage, str)
    assert len(coverage.strip()) > 40, "une couverture se dit en une phrase, pas en un mot"
    assert isinstance(provider.name, str) and provider.name
    assert provider.covers
    assert provider.covers <= {"general", "news", "academic", "official"}


def test_wikipedia_announces_itself_as_an_encyclopedia() -> None:
    # Le test de l'honnêteté : un résultat encyclopédique ne doit jamais
    # pouvoir se lire comme « le web ».
    coverage = WikipediaProvider().coverage
    assert "encyclopédie" in coverage
    assert "PAS le web" in coverage
    assert "l'actualité" in coverage
    assert "news" not in WikipediaProvider().covers


def test_arxiv_announces_itself_as_preprints_without_peer_review() -> None:
    coverage = ArxivProvider().coverage
    assert "arXiv" in coverage
    assert "pas de peer review" in coverage
    assert ArxivProvider().covers == frozenset({"academic"})


def test_openalex_announces_itself_as_bibliographic_records() -> None:
    coverage = OpenAlexProvider().coverage
    assert "notices bibliographiques" in coverage
    assert OpenAlexProvider().covers == frozenset({"academic"})


def test_searxng_does_not_promise_more_than_the_instance_provides() -> None:
    coverage = SearxngProvider("http://127.0.0.1:8080").coverage
    assert "auto-hébergée" in coverage
    # L'ampleur dépend des moteurs activés côté instance : le dire est la
    # seule façon de ne pas promettre une couverture qu'on ne contrôle pas.
    assert "dépend des moteurs activés" in coverage


def test_every_hit_carries_the_coverage_of_its_engine(local_api: _LocalApi) -> None:
    provider = OpenAlexProvider(api_url=local_api.url("/works"))
    answer = provider.search(_query("transformer attention"), max_results=2, deadline=_deadline())
    assert answer.hits
    assert all(hit.coverage == provider.coverage for hit in answer.hits)
    assert all(hit.provider == provider.name for hit in answer.hits)


# ==========================================================================
# Le seul test qui touche le réseau
# ==========================================================================
@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("THOT_E2E_WEB") != "1",
    reason="THOT_E2E_WEB=1 pour ouvrir le réseau",
)
def test_e2e_arxiv_live() -> None:
    """Vérifie que l'API réelle rend encore la forme que le parseur attend.

    Inactif par défaut, et exclu par le `-k "not slow"` du dépôt. Il est
    là parce qu'une API peut changer de forme sans prévenir : un parseur
    qui n'est confronté au vrai document que le jour où l'utilisateur
    cherche n'est pas un parseur testé.
    """
    reset_arxiv_gate()
    provider = ArxivProvider()
    answer = provider.search(
        WebQuery(text="deep research agents", source_type="academic"),
        max_results=2,
        deadline=_deadline(15.0),
    )
    assert answer.ok, answer.error
    assert 0 < len(answer.hits) <= 2
    assert all(hit.url.startswith("https://arxiv.org/abs/") for hit in answer.hits)
