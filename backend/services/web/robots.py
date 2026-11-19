"""Recherche web — couche 2 : `robots.txt`, et la politesse rendue vérifiable.

Une seule question est posée ici, et elle a une seule source de vérité :
**cette URL est-elle permise à notre jeton ?** Ce module ne télécharge pas de
page, il télécharge des règles, et il répond par un verdict qui se lit à voix
haute. Trois décisions structurantes, toutes tirées de RFC 9309 :

1. **Le code de réponse décide, mais seulement de 4xx et 5xx** (§2.3.1.2 et
   §2.3.1.3). Un `404` autorise — le fichier est *indisponible*, pas
   restrictif ; un `503` interdit tout — le fichier est *injoignable*, et une
   règle qu'on n'a pas pu lire ne se devine pas. Aucun autre code ne tranche :
   c'est le contenu qui parle.
2. **Le contenu est une donnée non fiable** (section « security
   considerations »). Il est lu par `urllib.robotparser`, jamais évalué,
   jamais interprété au-delà de la grammaire. Un `robots.txt` qui contient du
   Javascript reste du texte : il ne gagne aucun pouvoir.
3. **Conservation 24 h, parsing 500 KiB** (§2.4, §2.5) : ce sont des
   plafonds de norme, pas des réglages de confort. D'où un cache borné par
   origine, avec expiration, et un refus — pas un silence — quand le fichier
   dépasse le plancher de parsing.

**Un `robots.txt` qui échoue ne fait jamais échouer l'appel.** Timeout, hôte
introuvable, fichier tronqué, connexion coupée : tous rendent un verdict avec
son motif. La seule hypothèse qui puisse nous coûter une page, c'est de ne pas
avoir su lire les règles ; l'inverse — inventer une permission — n'existe pas.

Trois écarts assumés vis-à-vis de la bibliothèque standard, tous trois
documentés sur place :

* `RobotFileParser.can_fetch` retient le **premier** groupe applicable ; la RFC
  9309 §2.2.2 impose le groupe **le plus spécifique**. On réordonne donc les
  groupes par spécificité décroissante avant de décider, ce qui rend la
  bibliothèque conforme sans réécrire son analyse.
* `Entry.allowance` retient la **première** ligne de règle qui matche, alors
  que la RFC demande le préfixe le plus long : un `Disallow: /docs` l'emporte
  donc sur un `Allow: /docs/public` déclaré après. L'écart joue dans le sens
  prudent — nous lisons au plus ce que l'éditeur autorisait — et nous le laissons
  visible plutôt que d'écrire un second moteur de règles.
* La stub stdlib n'expose que `can_fetch`. Pour réordonner et pour nommer le
  groupe qui a décidé, on déclare ici le strict nécessaire (`_ParserView`) :
  la DÉCISION reste entièrement dans `can_fetch`, l'accès typé ne sert qu'à
  l'ordonner et à l'expliquer.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass
from typing import Protocol, cast

from backend.config.defaults import TOOL_LIMITS
from backend.services.web import net
from backend.services.web.types import WebError

# --------------------------------------------------------------------------
# Norme, lue une seule fois au point unique
# --------------------------------------------------------------------------
ROBOTS_TTL_S = float(TOOL_LIMITS["web_robots_ttl_s"])          # §2.4 : 24 h, plafond
ROBOTS_MAX_BYTES = int(TOOL_LIMITS["web_robots_max_bytes"])      # §2.5 : 500 KiB, plancher
# Cache borné : 64 origines suffisent largement à un tour, et la borne empêche
# qu'un crawl long transforme le processus en mémoire de règles.
CACHE_MAX_ORIGINS = 64
DEFAULT_TOKEN = "Thot"
ROBOTS_ACCEPT = "text/plain, text/html;q=0.5, */*;q=0.1"


@dataclass(frozen=True)
class RobotsVerdict:
    """La réponse à « cette URL est-elle permise ? », et pourquoi.

    `reason` est un texte destiné au modèle comme à l'écran : un refus qui ne
    dit pas pourquoi est un refus que l'utilisateur irait contester. `status`
    est le code HTTP lu, ou `None` si le fichier n'a pas été obtenu du tout.
    """
    allowed: bool
    reason: str
    status: int | None = None


# --------------------------------------------------------------------------
# Cache des règles, par origine
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _Rules:
    """Ce qu'une origine a appris de son `robots.txt`, et jusqu'à quand.

    `parser is None` ne veut pas dire « pas de règles » : avec `status` dans
    les 4xx, c'est « le fichier est indisponible », qui vaut autorisation. La
    distinction est dans `note`, lisible par un humain.
    """
    parser: urllib.robotparser.RobotFileParser | None
    status: int | None
    note: str
    expires_at: float


_CACHE: dict[str, _Rules] = {}
_CACHE_LOCK = threading.Lock()


def reset_cache() -> None:
    """Ouvre un nouveau tour : les règles apprises ne survivent pas au test."""
    with _CACHE_LOCK:
        _CACHE.clear()


def cached_origins() -> int:
    """Nombre d'origines en cache — pour vérifier une expiration sans l'horloge."""
    with _CACHE_LOCK:
        return len(_CACHE)


# --------------------------------------------------------------------------
# Décision
# --------------------------------------------------------------------------
def is_allowed(
    url: str,
    *,
    user_agent_token: str = DEFAULT_TOKEN,
    budget: net.NetworkBudget | None = None,
) -> RobotsVerdict:
    """Dit si `url` est permise à notre jeton, et le dit en français.

    Le budget est celui du tour : la lecture de `robots.txt` passe par
    `net.http_get`, donc par les mêmes bornes et la même file par hôte qu'une
    page. Sans budget, on en crée un d'une requête, ce qui permet d'appeler
    cette fonction seule (diagnostic, test) sans ouvrir de fenêtre réseau
    incontrôlée.
    """
    if budget is None:
        budget = net.NetworkBudget(deadline=time.monotonic() + net.TIMEOUT_S)
    token = (user_agent_token or DEFAULT_TOKEN).strip() or DEFAULT_TOKEN

    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        return RobotsVerdict(False, f"URL illisible ({exc}) : aucune règle applicable", None)

    scheme = parts.scheme.lower()
    if scheme not in net.ALLOWED_SCHEMES:
        return RobotsVerdict(
            False, f"schéma « {scheme or 'aucun'} » : robots.txt ne se consulte pas", None,
        )
    try:
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        return RobotsVerdict(False, f"hôte illisible ({exc}) : aucune règle applicable", None)
    if not host:
        return RobotsVerdict(False, "URL sans hôte : aucune règle applicable", None)

    origin = f"{scheme}://{_authority(host, port)}"
    rules = _rules_for(origin, budget)
    path = parts.path or "/"

    if rules.parser is None:
        # §2.3.1.2 / §2.3.1.3 : ici, et seulement ici, le code de réponse décide.
        allowed = rules.status is not None and 400 <= rules.status < 500
        return RobotsVerdict(allowed, f"{rules.note} : {path}", rules.status)

    # Le parseur est rangé UNE fois, à sa lecture : décider ne fait plus que lire.
    allowed = rules.parser.can_fetch(token, url)
    group = _deciding_group(rules.parser, token)
    return RobotsVerdict(
        allowed,
        f"robots.txt lu ({rules.status}) : {path} {'autorisé' if allowed else 'interdit'} — {group}",
        rules.status,
    )


def _authority(host: str, port: int) -> str:
    """Authority RFC 3986 : un IPv6 littéral doit rester entre crochets."""
    literal = f"[{host}]" if ":" in host else host
    return f"{literal}:{port}"


# --------------------------------------------------------------------------
# Lecture du fichier
# --------------------------------------------------------------------------
def _rules_for(origin: str, budget: net.NetworkBudget) -> _Rules:
    """Les règles de l'origine, du cache si elles sont encore valides."""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(origin)
    if cached is not None and cached.expires_at > now:
        return cached

    rules = _download_rules(origin, budget)
    with _CACHE_LOCK:
        _CACHE[origin] = rules
        while len(_CACHE) > CACHE_MAX_ORIGINS:
            # dict = ordre d'insertion : on évince l'origine la plus ancienne,
            # pas la plus utilisée. La borne est une sécurité mémoire, pas une
            # politique de remplacement.
            _CACHE.pop(next(iter(_CACHE)))
    return rules


def _download_rules(origin: str, budget: net.NetworkBudget) -> _Rules:
    """Un `robots.txt`, et seulement les deux décisions que la norme autorise."""
    expires_at = time.monotonic() + ROBOTS_TTL_S
    robots_url = f"{origin}/robots.txt"
    try:
        response = net.http_get(
            robots_url, budget=budget, max_bytes=ROBOTS_MAX_BYTES, accept=ROBOTS_ACCEPT,
            page=False,
        )
    except WebError as exc:
        # §2.3.1.4 : un fichier injoignable vaut interdiction complète. Le
        # message d'origine est gardé : il nomme le plafond, le délai ou l'hôte.
        return _Rules(
            None, None,
            f"robots.txt illisible ({exc.message}) : interdiction complète, règle inconnue",
            expires_at,
        )

    status = response.status
    if 400 <= status < 500:
        return _Rules(
            None, status,
            f"robots.txt absent ({status}) : le fichier est indisponible, pas restrictif",
            expires_at,
        )
    if status >= 500:
        return _Rules(
            None, status,
            f"robots.txt indisponible ({status}) : le fichier est injoignable, interdiction complète",
            expires_at,
        )

    parser = urllib.robotparser.RobotFileParser(robots_url)
    # On appelle `parse()` nous-mêmes avec le contenu déjà lu et borné : le
    # cache interne du parseur ne doit pas décider quand on rafraîchit, et
    # `parse()` est le seul appel qui renseigne `last_checked`, sans lequel
    # `can_fetch` répondrait « rien n'est encore permis » par prudence.
    parser.parse(response.text().splitlines())
    _order_by_specificity(parser)
    return _Rules(parser, status, f"robots.txt lu ({status})", expires_at)


# --------------------------------------------------------------------------
# Spécificité (RFC 9309 §2.2.2) et explication
# --------------------------------------------------------------------------
class _RuleLine(Protocol):
    """Une ligne de règle. On ne lit que sa représentation textuelle."""

    def __str__(self) -> str: ...


class _Entry(Protocol):
    """Un groupe de règles. Seuls le jeton et les lignes nous intéressent."""

    useragents: list[str]
    rulelines: list[_RuleLine]

    def applies_to(self, useragent: str) -> bool: ...


class _ParserView(Protocol):
    """Le strict nécessaire du parseur que la stub stdlib ne déclare pas."""

    entries: list[_Entry]
    default_entry: _Entry | None


def _view(parser: urllib.robotparser.RobotFileParser) -> _ParserView:
    return cast(_ParserView, parser)


def _specificity(entry: _Entry) -> int:
    """Longueur du plus long jeton du groupe : le plus spécifique gagne (§2.2.2)."""
    return max((len(agent) for agent in entry.useragents), default=0)


def _order_by_specificity(parser: urllib.robotparser.RobotFileParser) -> None:
    """Range les groupes du plus spécifique au plus général, une fois pour toutes.

    `can_fetch` s'arrête au PREMIER groupe applicable ; sans ce tri, un
    `User-agent: Thot` déclaré après un `User-agent: t` serait ignoré, et la
    RFC 9309 §2.2.2 impose l'inverse. Le groupe `*` vit dans `default_entry`,
    déjà consulté en dernier : il n'est pas touché. Le tri se fait à la LECTURE
    du fichier, jamais au moment de décider : décider reste une lecture.
    """
    view = _view(parser)
    view.entries.sort(key=_specificity, reverse=True)


def _deciding_group(parser: urllib.robotparser.RobotFileParser, token: str) -> str:
    """Le groupe qui a décidé, cité tel quel. Pour l'explication, pas pour la décision."""
    view = _view(parser)
    for entry in (*view.entries, view.default_entry):
        if entry is None or not entry.applies_to(token):
            continue
        agents = " / ".join(entry.useragents)
        rules = " ; ".join(str(line) for line in entry.rulelines)
        return f"groupe « {agents} » → « {rules or 'aucune règle'} »"
    return "aucun groupe applicable"
