"""Recherche web — couche 2 : `robots.txt` répond par des verdicts, pas par des exceptions.

Un vrai serveur HTTP sur la boucle locale sert les `robots.txt` : la question
posée ici — « ce code de réponse autorise-t-il ? » — mérite des réponses HTTP
réelles, pas une fonction simulée qui confirmerait nos propres suppositions. Le
seul point neutralisé est `resolve_global`, qu'un serveur de test ne peut pas
satisfaire sans éteindre le filtre SSRF ; ce filtre est testé ailleurs, sur ses
propres adresses (`test_web_fetcher.py`).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.services.web import net, robots

# ---------- Le serveur de test ----------

@dataclass
class _Route:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain"


@dataclass
class _Journal:
    hits: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)


class _Serveur:
    """Une table de routes et un journal des requêtes reçues."""

    def __init__(self) -> None:
        self.routes: dict[str, _Route] = {}
        self.journal = _Journal()
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            # `poll_interval` par défaut (0,5 s) dominerait le temps de chaque
            # test : ici on veut l'arrêt net, pas l'attente de la boucle.
            target=lambda: self._httpd.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def url(self, path: str) -> str:
        """L'adresse d'un chemin sur cette origine."""
        return f"{self.origin}{path}"

    def sous_hote(self, hote: str, path: str = "/") -> str:
        """La même origine servie sous un autre nom : c'est ainsi qu'on remplit le cache.

        `127.0.0.0/8` est entièrement en boucle locale, donc l'adresse ne change
        rien au dialogue — mais l'origine, elle, change, et c'est elle que le
        cache indexe.
        """
        return f"http://{hote}:{self._port}{path}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _handler_for(serv: _Serveur) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:  # noqa: N802 - nom imposé par la stdlib
            with serv._lock:
                serv.journal.hits.append(self.path)
                serv.journal.agents.append(self.headers.get("User-Agent", ""))
            route = serv.routes.get(self.path)
            status, body, content_type = (
                (route.status, route.body, route.content_type) if route
                else (404, b"", "text/plain")
            )
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Le journal du test EST le journal du serveur : celui de la stdlib est muet."""

    return _Handler


@pytest.fixture()
def serveur(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Serveur]:
    instance = _Serveur()
    # Le serveur est sur la boucle locale : `resolve_global` refuserait sa propre
    # adresse, sinon. Tout le reste — UA, bornes, file par hôte, lecture bornée —
    # reste le vrai chemin réseau, c'est le point du test.
    monkeypatch.setattr(net, "resolve_global", lambda host, port: ["127.0.0.1"])
    monkeypatch.setattr(net, "HOST_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(net, "HOST_MAX_PER_TURN", 64)
    net.reset_host_gate()
    robots.reset_cache()
    yield instance
    robots.reset_cache()
    net.reset_host_gate()
    instance.close()


# ---------- 4xx autorise, 5xx interdit (RFC 9309 §2.3.1.2 et §2.3.1.3) ----------

@pytest.mark.parametrize("status", [400, 404, 410, 451])
def test_robots_absent_autorise_la_page(serveur: _Serveur, status: int) -> None:
    serveur.routes["/robots.txt"] = _Route(status=status, body=b"")
    verdict = robots.is_allowed(serveur.url("/article"), budget=_budget())
    assert verdict.allowed is True
    assert verdict.status == status
    assert str(status) in verdict.reason


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_robots_injoignable_interdit_tout(serveur: _Serveur, status: int) -> None:
    serveur.routes["/robots.txt"] = _Route(status=status, body=b"")
    verdict = robots.is_allowed(serveur.url("/article"), budget=_budget())
    assert verdict.allowed is False
    assert verdict.status == status
    assert "interdiction" in verdict.reason


def test_robots_404_et_503_ne_produisent_pas_la_meme_verite(serveur: _Serveur) -> None:
    """Le même chemin, deux réponses opposées : c'est bien le statut qui tranche."""
    serveur.routes["/robots.txt"] = _Route(status=404)
    assert robots.is_allowed(serveur.url("/article"), budget=_budget()).allowed is True
    robots.reset_cache()
    serveur.routes["/robots.txt"] = _Route(status=503)
    assert robots.is_allowed(serveur.url("/article"), budget=_budget()).allowed is False


# ---------- Règles ----------

def test_groupe_etoile_interdit_une_seule_zone(serveur: _Serveur) -> None:
    serveur.routes["/robots.txt"] = _Route(body=b"User-agent: *\nDisallow: /prive\n")
    budget = _budget()
    assert robots.is_allowed(serveur.url("/prive/article"), budget=budget).allowed is False
    assert robots.is_allowed(serveur.url("/public/article"), budget=budget).allowed is True


def test_groupe_specifique_prime_sur_etoile_meme_declare_avant(serveur: _Serveur) -> None:
    """`Thot` interdit tout alors que `*` n'interdit rien : le groupe le plus spécifique gagne."""
    serveur.routes["/robots.txt"] = _Route(
        body=b"User-agent: *\nDisallow:\n\nUser-agent: Thot\nDisallow: /\n",
    )
    verdict = robots.is_allowed(serveur.url("/article"), budget=_budget())
    assert verdict.allowed is False
    assert "Thot" in verdict.reason


def test_groupe_le_plus_specifique_prime_meme_declare_en_dernier(serveur: _Serveur) -> None:
    """`User-agent: t` (large) déclaré d'abord ne doit pas écraser `Thot` (précis)."""
    serveur.routes["/robots.txt"] = _Route(
        body=b"User-agent: t\nDisallow: /\n\nUser-agent: Thot\nDisallow: /secret\n",
    )
    budget = _budget()
    assert robots.is_allowed(serveur.url("/secret"), budget=budget).allowed is False
    assert robots.is_allowed(serveur.url("/article"), budget=budget).allowed is True


def test_groupe_d_un_autre_agent_ne_sapplique_pas_a_nous(serveur: _Serveur) -> None:
    serveur.routes["/robots.txt"] = _Route(
        body=b"User-agent: Googlebot\nDisallow: /\n",
    )
    assert robots.is_allowed(serveur.url("/article"), budget=_budget()).allowed is True


def test_allow_plus_specifique_n_ouvre_pas_la_porte_avec_la_stdlib(serveur: _Serveur) -> None:
    """Écart connu et volontaire : le premier préfixe qui matche gagne.

    La RFC 9309 §2.2.2 demande le préfixe le plus long ; `urllib.robotparser`
    retient la première ligne qui matche, donc un `Disallow: /docs` l'emporte
    sur un `Allow: /docs/public` déclaré ensuite. Nous lisons donc au plus ce
    que l'éditeur autorisait — l'écart joue dans le sens prudent, et il se voit
    ici pour qu'on ne le prenne pas pour un choix.
    """
    serveur.routes["/robots.txt"] = _Route(
        body=b"User-agent: *\nDisallow: /docs\nAllow: /docs/public\n",
    )
    budget = _budget()
    assert robots.is_allowed(serveur.url("/docs/prive"), budget=budget).allowed is False
    assert robots.is_allowed(serveur.url("/docs/public/x"), budget=budget).allowed is False


def test_robots_vide_autorise_tout(serveur: _Serveur) -> None:
    serveur.routes["/robots.txt"] = _Route(body=b"")
    assert robots.is_allowed(serveur.url("/article"), budget=_budget()).allowed is True


# ---------- Cache et bornes de norme ----------

def test_ttl_de_24h_et_une_seule_requete_par_origine(serveur: _Serveur) -> None:
    """La RFC 9309 §2.4 plafonne la conservation à 24 h, et le cache évite de redemander."""
    assert robots.ROBOTS_TTL_S == 86_400
    serveur.routes["/robots.txt"] = _Route(status=404)
    budget = _budget()
    for _ in range(3):
        robots.is_allowed(serveur.url("/a"), budget=budget)
        robots.is_allowed(serveur.url("/b"), budget=budget)
    assert serveur.journal.hits.count("/robots.txt") == 1
    assert robots.cached_origins() == 1


def test_reset_cache_relit_le_fichier_modifie(serveur: _Serveur) -> None:
    serveur.routes["/robots.txt"] = _Route(body=b"User-agent: *\nDisallow: /a\n")
    assert robots.is_allowed(serveur.url("/a"), budget=_budget()).allowed is False
    robots.reset_cache()
    serveur.routes["/robots.txt"] = _Route(body=b"User-agent: *\nDisallow: /b\n")
    assert robots.is_allowed(serveur.url("/a"), budget=_budget()).allowed is True
    assert serveur.journal.hits.count("/robots.txt") == 2


def test_entree_expiree_n_est_plus_consideree(serveur: _Serveur) -> None:
    """Sans attendre 24 h : on avance l'horloge du cache au-delà de l'expiration."""
    serveur.routes["/robots.txt"] = _Route(status=404)
    budget = _budget()
    assert robots.is_allowed(serveur.url("/a"), budget=budget).allowed is True
    _vieillir_cache(robots.ROBOTS_TTL_S + 1)
    robots.is_allowed(serveur.url("/a"), budget=budget)
    assert serveur.journal.hits.count("/robots.txt") == 2


def test_cache_borne_a_64_origines(serveur: _Serveur) -> None:
    """Un cache de règles non borné est une fuite : la borne se vérifie par l'entrée."""
    assert robots.CACHE_MAX_ORIGINS == 64
    budget = _budget()
    for index in range(robots.CACHE_MAX_ORIGINS + 10):
        robots.is_allowed(serveur.sous_hote(f"127.0.0.{index + 1}", "/article"), budget=budget)
    assert robots.cached_origins() == robots.CACHE_MAX_ORIGINS


def test_fichier_au_dessus_du_plancher_de_parsing_refuse_sans_lever(serveur: _Serveur) -> None:
    """§2.5 : au-delà de 500 KiB le fichier n'est pas lu, et l'absence vaut refus."""
    assert robots.ROBOTS_MAX_BYTES == 524_288
    serveur.routes["/robots.txt"] = _Route(body=b"Disallow: /\n" + b"#" * robots.ROBOTS_MAX_BYTES)
    verdict = robots.is_allowed(serveur.url("/article"), budget=_budget())
    assert verdict.allowed is False
    assert "volumineuse" in verdict.reason


def test_fichier_a_la_limite_de_parsing_est_encore_lu(serveur: _Serveur) -> None:
    """Juste sous le plancher, le fichier vaut ce qu'il dit — refuser plus serait facile."""
    remplissage = robots.ROBOTS_MAX_BYTES - len("User-agent: *\nDisallow: /prive\n")
    serveur.routes["/robots.txt"] = _Route(
        body=b"User-agent: *\nDisallow: /prive\n" + b"#" * remplissage,
    )
    budget = _budget()
    assert robots.is_allowed(serveur.url("/prive"), budget=budget).allowed is False
    assert robots.is_allowed(serveur.url("/public"), budget=budget).allowed is True


# ---------- Un robots.txt qui échoue n'échoue jamais ----------

def test_origine_injoignable_rend_un_verdict_et_non_une_exception() -> None:
    verdict = robots.is_allowed("http://127.0.0.1:1/article", budget=_budget())
    assert verdict.allowed is False
    assert verdict.status is None
    assert "robots.txt illisible" in verdict.reason


def test_robots_non_lisible_reste_un_verdict_nomme(serveur: _Serveur) -> None:
    """Une connexion qui coupe ne remonte jamais en exception : le motif porte le détail."""
    serveur.routes["/robots.txt"] = _Route(status=200, body=b"", content_type="")
    verdict = robots.is_allowed(serveur.url("/article"), budget=_budget())
    assert verdict.allowed is True
    assert verdict.status == 200


def test_requete_robots_part_avec_le_user_agent_du_projet(serveur: _Serveur) -> None:
    """La politesse se prouve par l'identification : c'est le même UA que pour une page."""
    serveur.routes["/robots.txt"] = _Route(status=404)
    robots.is_allowed(serveur.url("/article"), budget=_budget())
    assert serveur.journal.agents == [net.user_agent()]
    assert serveur.journal.agents[0].startswith("Thot/")


def test_schema_non_http_refuse_sans_aller_chercher_les_regles() -> None:
    verdict = robots.is_allowed("file:///etc/passwd", budget=_budget())
    assert verdict.allowed is False
    assert "robots.txt ne se consulte pas" in verdict.reason


# ---------- Outils ----------

def _budget(seconds: float = 10.0) -> net.NetworkBudget:
    return net.NetworkBudget(deadline=time.monotonic() + seconds)


def _vieillir_cache(secondes: float) -> None:
    """Avance l'expiration de toutes les entrées, sans attendre l'horloge réelle."""
    with robots._CACHE_LOCK:
        rules = list(robots._CACHE.items())
        robots._CACHE.clear()
        for origin, entry in rules:
            robots._CACHE[origin] = robots._Rules(
                parser=entry.parser, status=entry.status, note=entry.note,
                expires_at=entry.expires_at - secondes,
            )
