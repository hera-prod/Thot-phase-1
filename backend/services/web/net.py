"""Recherche web — couche 1 : la prise de parole réseau.

Ce module est le **seul** endroit du projet qui ouvre une socket sortante
pour autre chose que le serveur llama local. Tout ce qui suit passe par
ici, y compris les appels aux API des moteurs. C'est volontaire : un moteur
de recherche qui aurait sa propre pile HTTP pourrait contourner les bornes,
et l'audit de la politesse deviendrait impossible.

Ce qu'il garantit, et qui est vérifiable par test :

* **Une identification, pas une usurpation.** Un `User-Agent` qui nomme le
  logiciel, sa version et le contact. Jamais un nom de navigateur. La règle
  vient de RFC 9309 §2.2.1 et de l'étiquette MediaWiki (« un User-Agent
  informatif avec coordonnées, ou vous pouvez être bloqué sans avis ») ; le
  refus de l'usurpation vient de la posture Jina Reader, qu'on s'est donnée
  pour règle.
* **Zéro redirection suivie** (`web_redirects_max = 0`). Ce n'est pas une
  préférence : c'est la seule façon de ne pas avoir à revalider une
  destination à chaque saut, et une redirection vers une adresse privée est
  le contournement le plus commun du SSRF.
* **Épinglage de l'adresse résolue.** L'hôte est résolu une fois, chaque
  adresse doit être `is_global`, puis la connexion part vers l'adresse
  validée alors que le SNI et le certificat restent vérifiés contre le nom
  réel. Sans ça, il reste une fenêtre entre la validation et la connexion.
* **Des bornes qu'on ne peut pas contourner par défaut** : octets, délai,
  requêtes par tour, requêtes par hôte, intervalle minimum entre deux
  appels au même hôte.

Une seule dérogation existe, et elle est écrite dans le code plutôt que
laissée à l'interprétation : `http_get(..., allow_private=True)` accepte
une adresse **non globale** si — et seulement si — elle est loopback. Elle
sert à joindre l'instance SearXNG que l'utilisateur a lui-même lancée sur
son poste. Elle n'ouvre aucun autre hôte, aucun autre port, aucun autre
schéma, et le chemin par défaut est inchangé.
"""
from __future__ import annotations

import http.client
import re
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from backend.config.defaults import TOOL_LIMITS
from backend.services.web.types import CONSENT_REQUIRED, NETWORK_BLOCKED, WebError

# --------------------------------------------------------------------------
# Identification
# --------------------------------------------------------------------------
# RFC 9309 §2.2.1 : le jeton de produit doit décrire le but du client, et
# l'étiquette MediaWiki ajoute qu'un User-Agent sans coordonnées expose à un
# blocage sans avis. Le contact est donc un emplacement à remplir, pas une
# chaîne décorative : tant qu'il est vide, on le DIT dans l'agent.
PROJECT_VERSION = "1.0"
# Contact public : lisible par les opérateurs des sites que nous visitons, et
# exigé par RFC 9309 §2.2.1. Le format est celui de `Claude-User
# (claude-code/<version>; +<contact>)` — un agent nommé, une version, un
# contact — parce que c'est la convention qui rend un blocage discutable : on
# peut nous écrire. Sans contact, MediaWiki et d'autres registries bloquent sans
# avis, et un blocage silencieux est impossible à diagnostiquer.
#
# Il se règle par variable d'environnement pour qu'un distributeur n'ait pas à
# recompiler le sidecar, et reste VISIBLE quand il manque : s'identifier
# imparfaitement en le disant vaut mieux que de s'identifier en mentant.
CONTACT_ENV = "THOT_CONTACT_URL"


def _contact() -> str:
    import os

    return (os.environ.get(CONTACT_ENV) or PROJECT_CONTACT).strip()


PROJECT_CONTACT = ""  # valeur par défaut volontairement vide ; THOT_CONTACT_URL prime


def user_agent() -> str:
    """L'agent par défaut est nom + version, comme la norme de ce genre d'outil.

    Mesuré sur trois projets de référence : AnythingLLM envoie
    `AnythingLLM/<version>` — sans contact ; son plugin web envoie
    `anything-llm`, sans version non plus. Claude Code est l'exception :
    `Claude-User (claude-code/<version>; +https://support.anthropic.com/)`,
    parce qu'il y a une entreprise et un service d'assupport derrière.

    Thot est dans le premier cas. Un logiciel que l'utilisateur installe chez
    lui n'a pas d'assistance à offrir, alors annoncer un contact serait
    annoncer une boîte aux lettres qui n'existe pas. Le contact reste donc
    OPTIONNEL, pour le distributeur qui en a un : c'est lui qui décide, et
    nous ne prétendons rien à sa place.
    """
    contact = _contact()
    if contact:
        return f"Thot/{PROJECT_VERSION} (outil local ; +{contact})"
    return f"Thot/{PROJECT_VERSION} (outil local)"


def identify_gap() -> str | None:
    """Ce qui manque à notre identification, en clair. `None` si complète."""
    return None if _contact() else (
        "aucun contact de distribution — conforme à la norme de ce genre d\'outil, "
        "et à compléter seulement par un distributeur qui en a un"
    )


# --------------------------------------------------------------------------
# Limites (lues une seule fois, au point unique)
# --------------------------------------------------------------------------
URL_MAX_CHARS = int(TOOL_LIMITS["web_url_max_chars"])
# Plafond par DÉFAUT d'une lecture. Le fetcher passe le sien, plus large :
# voir le commentaire de `fetcher.py` sur la différence entre octets reçus et
# texte entrant.
PAGE_BYTES_MAX = int(TOOL_LIMITS["web_page_bytes_max"])
CALL_BYTES_MAX = int(TOOL_LIMITS["web_bytes_max"])
TIMEOUT_S = float(TOOL_LIMITS["web_timeout_s"])
HOST_MIN_INTERVAL_S = float(TOOL_LIMITS["web_host_min_interval_s"])
HOST_MAX_PER_TURN = int(TOOL_LIMITS["web_host_max"])
TURN_QUERIES_MAX = int(TOOL_LIMITS["web_turn_queries_max"])
PAGES_MAX = int(TOOL_LIMITS["web_pages_max"])

ALLOWED_SCHEMES = ("http", "https")
# SSRF ne se limite pas à HTTP : `file:`, `data:`, `gopher:`, `dict:` sont
# refusés par la liste de schémas, pas par un filtrage a posteriori.
_READ_CHUNK = 65536


# --------------------------------------------------------------------------
# Budget : un objet, une vérité
# --------------------------------------------------------------------------
@dataclass
class NetworkBudget:
    """Ce qu'un appel de `web_research` a le droit de faire.

    Créé par `research.py` au début de l'appel, passé à tout le monde. Les
    compteurs sont ici plutôt que dans le rendu parce qu'ils doivent être
    vrai AVANT l'écriture du rendu : un compteur calculé à la fin peut
    mentir après un échec.
    """
    deadline: float
    max_pages: int = PAGES_MAX
    max_queries: int = TURN_QUERIES_MAX
    max_bytes: int = CALL_BYTES_MAX

    queries: int = 0
    pages: int = 0
    bytes_read: int = 0
    hosts: set[str] = field(default_factory=set)
    # Requêtes RÉELLEMENT parties sur le réseau, comptées à l'endroit unique où
    # elles partent : `http_get`, juste avant l'écriture de la requête. C'est la
    # seule mesure qui ne mente pas. Compter les appels à un moteur mentait par
    # omission (robots.txt et pages ne sont pas des appels moteur) et par
    # excès (une tentative refusée par le SSRF n'est pas une requête partie).
    requests_out: int = 0

    def remaining_s(self) -> float:
        return self.deadline - time.monotonic()

    def remaining_bytes(self) -> int:
        return max(0, self.max_bytes - self.bytes_read)

    def time_left(self, wanted: float) -> float:
        return max(0.1, min(wanted, self.remaining_s()))

    def queries_left(self) -> int:
        return max(0, self.max_queries - self.queries)

    def pages_left(self) -> int:
        return max(0, self.max_pages - self.pages)

    def take_query(self) -> bool:
        if self.queries >= self.max_queries:
            return False
        self.queries += 1
        return True

    def take_page(self) -> bool:
        if self.pages >= self.max_pages:
            return False
        self.pages += 1
        return True

    def note_bytes(self, count: int) -> None:
        self.bytes_read += count


# --------------------------------------------------------------------------
# Politesse par hôte : sérialisation + intervalle (processus entier)
# --------------------------------------------------------------------------
# MediaWiki : « Make your requests in series rather than in parallel ».
# L'état est global au processus parce que la règle porte sur l'hôte, pas
# sur l'appel : deux conversations simultanées partagent la même prudence.
_GATE_LOCK = threading.Lock()
_GATE_LAST: dict[str, float] = {}
_GATE_COUNT: dict[str, int] = {}
# Requêtes parties depuis le DÉBUT du processus, tous budgets confondus. Les
# moteurs construisent chacun leur `NetworkBudget` (chacun plafonné, chacun
# borné dans le temps) : un compteur par budget ne verrait donc que les pages,
# et le rendu annoncerait « 2 » pour trois requêtes réellement envoyées. Un seul
# compteur, au point de sortie, est la seule mesure qui ne mente pas.
_REQUESTS_LOCK = threading.Lock()
_REQUESTS_OUT = 0


def requests_out_total() -> int:
    """Requêtes parties depuis le début du processus."""
    with _REQUESTS_LOCK:
        return _REQUESTS_OUT


def note_request_out() -> None:
    """Marque une requête comme partie. Appelé au seul endroit d'écriture."""
    global _REQUESTS_OUT
    with _REQUESTS_LOCK:
        _REQUESTS_OUT += 1


def reset_host_gate() -> None:
    """Ouvre un nouveau tour : les compteurs par hôte repartent à zéro."""
    with _GATE_LOCK:
        _GATE_COUNT.clear()


def host_slots_left(host: str) -> int:
    with _GATE_LOCK:
        return max(0, HOST_MAX_PER_TURN - _GATE_COUNT.get(host, 0))


def wait_for_host(host: str, budget: NetworkBudget, *, page: bool = True) -> None:
    """Sérialise les appels à un hôte et tient l'intervalle minimum.

    `page=True` compte cette requête dans le plafond par tour. La règle de la
    conception porte sur « deux PAGES au maximum par hôte et par tour » : elle
    borne ce qu'on DOWNLOAD, pas ce qu'on interroge. Sans cette distinction, un
    moteur hébergé sur le même domaine que ses résultats — Wikipédia est
    exactement ce cas — épuiserait le plafond avant la première page, et la
    politesse deviendrait une panne.

    Les requêtes de contrôle (`robots.txt`, mises en cache 24 h) et les appels
    d'API documentés d'un moteur restent donc sérialisés et espacés d'une
    seconde, mais ne consomment pas le plafond de pages.

    Lève `WebError(NETWORK_BLOCKED)` si le plafond est atteint : la politesse
    n'est pas un paramètre, c'est un refus.
    """
    now = time.monotonic()
    with _GATE_LOCK:
        used = _GATE_COUNT.get(host, 0)
        if page and used >= HOST_MAX_PER_TURN:
            raise WebError(
                NETWORK_BLOCKED,
                f"plafond de politesse atteint pour {host} ({HOST_MAX_PER_TURN} pages par tour) — "
                f"aucune autre page n'est lue sur cet hôte dans ce tour",
            )
        last = _GATE_LAST.get(host)
        wait = 0.0 if last is None else max(0.0, (last + HOST_MIN_INTERVAL_S) - now)
        if page:
            _GATE_COUNT[host] = used + 1
        _GATE_LAST[host] = now + wait
    if wait > 0:
        # On attend DANS le budget du tour : la politesse ne doit jamais faire
        # déborder le délai imparti au run.
        time.sleep(min(wait, max(0.0, budget.remaining_s())))


# --------------------------------------------------------------------------
# Validation d'URL
# --------------------------------------------------------------------------
def validate_url(url: str) -> urllib.parse.SplitResult:
    """Valide la forme de l'URL. Ne résout rien — voir `resolve_global`.

    Le désaccord de parseur est traité par le refus, jamais par la
    réconciliation : `http://exemple\\@malveillant.example` n'est pas lu de
    même par `urllib.parse` et par la grammaire RFC 3986, alors on refuse
    tout hôte que les deux lectures ne partagent pas.
    """
    raw = (url or "").strip()
    if not raw:
        raise WebError(NETWORK_BLOCKED, "URL vide")
    if len(raw) > URL_MAX_CHARS:
        raise WebError(NETWORK_BLOCKED, f"URL trop longue ({len(raw)} > {URL_MAX_CHARS} caractères)")
    if any(char.isspace() or ord(char) < 32 for char in raw):
        raise WebError(NETWORK_BLOCKED, "URL contenant un espace ou un caractère de contrôle")
    if "\\" in raw:
        raise WebError(NETWORK_BLOCKED, "URL contenant une barre inverse : hôte ambigu entre parseurs")
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise WebError(NETWORK_BLOCKED, f"URL illisible : {exc}") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise WebError(NETWORK_BLOCKED, f"schéma interdit : {parts.scheme or 'aucun'} (autorisés : http, https)")
    if parts.username or parts.password or "@" in parts.netloc:
        raise WebError(NETWORK_BLOCKED, "URL avec identifiants : refusé")
    try:
        host = parts.hostname
    except ValueError as exc:
        raise WebError(NETWORK_BLOCKED, f"hôte illisible : {exc}") from exc
    if not host:
        raise WebError(NETWORK_BLOCKED, "URL sans hôte")
    if "%" in parts.netloc:
        raise WebError(NETWORK_BLOCKED, "URL avec échappement dans l'hôte : hôte ambigu entre parseurs")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise WebError(NETWORK_BLOCKED, f"port illisible : {exc}") from exc
    if not (0 < port < 65536):
        raise WebError(NETWORK_BLOCKED, f"port hors plage : {port}")
    return parts


def resolve_global(host: str, port: int) -> list[str]:
    """Résout et exige que TOUTES les adresses soient publiques.

    Une seule adresse privée suffit à refuser : une liste qui contient
    `127.0.0.1` est une liste d'attaque, pas une liste de serveurs.
    C'est la règle par défaut, et elle ne s'assouplit sur aucun chemin.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise WebError(NETWORK_BLOCKED, f"hôte introuvable : {host}") from exc
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        candidate = str(sockaddr[0])
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not address.is_global or address.is_private or address.is_loopback \
                or address.is_link_local or address.is_reserved or address.is_multicast \
                or address.is_unspecified:
            raise WebError(
                NETWORK_BLOCKED,
                f"refus de {address} : adresse non publique (réseau interne, loopback ou métadonnées)",
            )
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise WebError(NETWORK_BLOCKED, f"hôte sans adresse publique : {host}")
    return addresses


# Paramètres et segments dont la valeur ressemble à un secret. Un secret dans
# une URL est le pire accident possible ici : il est stocké, rendu au modèle,
# recopié dans les journaux, et parfois renvoyé par un tiers. On ne tente pas
# de deviner la forme de tous les secrets — on refuse quand la ressemblance est
# forte, et on laisse passer le reste.
_SEGRETS_RE = re.compile(
    r"(?i)(?:^|[._-])(?:api[_-]?key|apikey|key|token|secret|password|passwd|pwd|"
    r"access[_-]?token|auth|signature|sig|bearer|session|credential)s?(?:[._-]|$)"
)
_FORMES_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})"
)


def url_porte_un_secret(url: str) -> str | None:
    """Le nom du paramètre ou le motif qui trahit un secret dans cette URL.

    Renvoie `None` si l'URL ne porte rien de tel. La fonction ne juge pas le
    contenu de la page, seulement l'adresse qu'on s'apprête à visiter — parce
    qu'une adresse est le seul endroit où l'appelant a lui-même écrit le
    secret, et donc le seul endroit où on peut le refuse avant qu'il parte.
    """
    brut = (url or "").strip()
    forme = _FORMES_SECRET_RE.search(urllib.parse.unquote(brut))
    if forme is not None:
        return f"une valeur de la forme « {forme.group(0)[:4]}… »"
    parties = urllib.parse.urlsplit(brut)
    for nom, _valeur in urllib.parse.parse_qsl(parties.query, keep_blank_values=False):
        if _SEGRETS_RE.search(nom):
            return f"le paramètre « {nom} »"
    # Le chemin aussi : `/api_key=…` est une façon courante de porter un secret
    # sans passer par la chaîne de requête, et une URL de rappel d'API en fait
    # souvent usage.
    for segment in re.split(r"[/?&=]", parties.path):
        if segment and _SEGRETS_RE.search(segment):
            return f"le segment « {segment} »"
    return None


def assert_readable_destination(url: str) -> tuple[urllib.parse.SplitResult, list[str]]:
    """Vérifie qu'une URL est VISIBLE avant d'aller la chercher — et dis pourquoi.

    On sépare cette question de la lecture elle-même, parce que l'ordre
    change tout ce que l'utilisateur voit. Demandé après `robots`, un refus
    d'adresse interne remontait en « robots nous a bloqués » : une panne
    clinique masquée par une explication plausible, et le pire endroit pour
    perdre une alerte. Demandé avant, le refus porte son vrai motif et ne
    coûte pas un créneau de page.

    Renvoie la forme analysée et les adresses retenues, pour que l'appelant
    puisse réutiliser la résolution sans la refaire.
    """
    parts = validate_url(url)
    secret = url_porte_un_secret(url)
    if secret is not None:
        raise WebError(
            CONSENT_REQUIRED,
            f"cette URL porte {secret} : la lecture est refusée, parce qu'un secret "
            f"dans une adresse finit en base, dans le contexte du modèle et dans les journaux",
        )
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    return parts, resolve_global(parts.hostname or "", port)


def resolve_loopback(host: str, port: int) -> list[str]:
    """Résolution pour un service de l'utilisateur, sur sa propre machine.

    La seule dérogation à `resolve_global`, et elle est étroite par
    construction : **uniquement le loopback** (`127.0.0.0/8`, `::1`). Le
    réseau local d'un bureau, un pont Docker, un lien d'amis — tout cela
    reste refusé, parce que la justification de l'exception (« c'est la
    machine de l'utilisateur ») ne vaut pas pour une adresse que le projet
    ne connaît pas. Un seul appelant dans l'arbre : l'instance SearXNG
    que l'utilisateur a lui-même lancée (`providers/searxng.py`).

    Le filtrage de TOUTES les adresses est ici aussi obligatoire qu'ailleurs :
    une liste de résolution qui contient `127.0.0.1` et une adresse publique
    est une liste d'attaque, pas une liste de serveurs.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise WebError(NETWORK_BLOCKED, f"hôte introuvable : {host}") from exc
    addresses: list[str] = []
    for info in infos:
        candidate = str(info[4][0])
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not address.is_loopback:
            raise WebError(
                NETWORK_BLOCKED,
                f"refus de {address} : la voie loopback n'admet que la machine locale, "
                f"pas {candidate}",
            )
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise WebError(
            NETWORK_BLOCKED,
            f"{host} ne résout pas sur la machine locale : l'exception loopback ne sert "
            f"qu'un service lancé par l'utilisateur sur son propre poste",
        )
    return addresses


# --------------------------------------------------------------------------
# Connexion épinglée
# --------------------------------------------------------------------------
class _PinnedHTTP(http.client.HTTPConnection):
    """HTTP en clair vers l'adresse validée, en-tête ``Host`` porte le NOM.

    Sans cette classe, il faudrait passer l'adresse épinglée au constructeur —
    et `putrequest` enverrait alors `Host: <adresse IP>`, pas le nom de domaine.
    La réponse d'un serveur dépend de cet en-tête (virtual hosts, CDN), et
    arXiv est justement servi en HTTP clair : le construire ainsi aurait
    faussé silencieusement l'en-tête pour tout le trafic non-TLS. On sépare donc
    les deux rôles : `host` porte le nom (SNI, `Host`), le socket vise l'adresse
    déjà validée.
    """

    def __init__(self, ip: str, port: int, *, server_hostname: str, timeout: float) -> None:
        super().__init__(server_hostname, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:  # pragma: no cover - chemin réseau, testé par stub
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connexion vers l'adresse validée, certificat vérifié contre le nom réel.

    C'est la réponse au DNS rebinding : le nom sert au SNI et à la
    vérification, l'adresse sert au socket. Les deux ne peuvent pas diverger.
    """

    def __init__(self, ip: str, port: int, *, server_hostname: str, timeout: float) -> None:
        self._pinned_context = ssl.create_default_context()
        super().__init__(server_hostname, port, timeout=timeout, context=self._pinned_context)
        self._pinned_ip = ip

    def connect(self) -> None:  # pragma: no cover - chemin réseau, testé par stub
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._pinned_context.wrap_socket(raw, server_hostname=self.host)


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    content_type: str = ""

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


def http_get(
    url: str,
    *,
    budget: NetworkBudget,
    max_bytes: int | None = None,
    accept: str = "*/*",
    deadline: float | None = None,
    allow_private: bool = False,
    page: bool = True,
) -> HttpResponse:
    """Un GET, et rien d'autre. Jamais de redirection suivie.

    Ne lève pas sur 404 ou 500 : un moteur qui répond 429 doit pouvoir dire
    « je suis limité » plutôt que « j'ai échoué ». Les refus, eux, lèvent.

    `allow_private=True` est l'unique porte qui échappe à l'exigence
    d'adresse publique, et elle ne s'ouvre que sur le **loopback** — voir
    `resolve_loopback`. Elle existe pour l'instance SearXNG que l'utilisateur
    lance sur sa machine ; elle ne rend aucun autre hôte admissible, et le
    chemin par défaut est inchangé. Ne pas l'élargir : c'est le seul endroit
    du projet où l'on accepte de joindre une adresse non globale.
    """
    limit = PAGE_BYTES_MAX if max_bytes is None else max_bytes
    limit = min(limit, budget.remaining_bytes())
    if limit <= 0:
        raise WebError(NETWORK_BLOCKED, "budget d'octets du tour épuisé")
    limit = min(limit, CALL_BYTES_MAX)

    parts = validate_url(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    scheme = parts.scheme.lower()
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"

    addresses = resolve_loopback(host, port) if allow_private else resolve_global(host, port)
    wait_for_host(host, budget, page=page)

    stop_at = min(budget.deadline, deadline if deadline is not None else budget.deadline)
    # `stop_at` est un INSTANT (`time.monotonic`), pas une durée : c'est
    # ce que `_read_capped` compare, et c'est pour cela qu'il faut le
    # garder entier. `remaining` est la seule durée, et c'est elle que
    # `http.client` attend en `timeout`.
    remaining = stop_at - time.monotonic()
    if remaining <= 0:
        raise WebError(NETWORK_BLOCKED, "délai du tour épuisé avant la requête")

    last_error: Exception | None = None
    for address in addresses:
        connection: http.client.HTTPConnection
        if scheme == "https":
            connection = _PinnedHTTPS(address, port, server_hostname=host, timeout=remaining)
        else:
            connection = _PinnedHTTP(address, port, server_hostname=host, timeout=remaining)
        try:
            note_request_out()
            budget.requests_out += 1
            connection.request("GET", target, headers={
                "User-Agent": user_agent(),
                "Accept": accept,
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = connection.getresponse()
            if 300 <= response.status < 400:
                # Zéro redirection suivie : la destination n'est pas
                # revalidée parce qu'elle n'est jamais atteinte.
                raise WebError(
                    NETWORK_BLOCKED,
                    f"redirection non suivie ({response.status}) : l'arrêt ici est volontaire, "
                    f"une destination n'est jamais présumée sûre",
                )
            body = _read_capped(response, limit=limit, budget=budget, stop_at=stop_at)
            headers = {key.lower(): value for key, value in response.getheaders()}
            return HttpResponse(
                status=response.status,
                headers=headers,
                body=body,
                url=url,
                content_type=headers.get("content-type", ""),
            )
        except WebError:
            raise
        except socket.timeout as exc:
            raise WebError(NETWORK_BLOCKED, f"délai dépassé pour {host}") from exc
        except (http.client.HTTPException, OSError) as exc:
            last_error = exc
        finally:
            try:
                connection.close()
            except Exception:
                pass
    raise WebError(NETWORK_BLOCKED, f"connexion impossible vers {host} : {type(last_error).__name__}")


def _read_capped(
    response: http.client.HTTPResponse,
    *,
    limit: int,
    budget: NetworkBudget,
    stop_at: float,
) -> bytes:
    """Lecture par morceaux : plafond d'octets ET de délai, à chaque tour."""
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > stop_at:
            raise WebError(NETWORK_BLOCKED, "délai dépassé pendant la lecture")
        chunk = response.read(min(_READ_CHUNK, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise WebError(
                NETWORK_BLOCKED,
                f"réponse trop volumineuse (plus de {limit} octets) : lecture interrompue, rien n'est gardé",
            )
        chunks.append(chunk)
    budget.note_bytes(total)
    return b"".join(chunks)
