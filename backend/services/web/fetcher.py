"""Recherche web — couche 4 : le téléchargement fermé.

C'est ici que la recherche web touche le monde. Tout ce qui précède
(`types`, `net`, `robots`, `extract`) prépare ce moment, et ce moment est une
suite de refus. Le fetcher ne « tente » rien : il applique, dans l'ordre, cinq
verdicts dont chacun porte un code local de `types.py`, et il ne rend une
`WebPage` que si les cinq passent.

L'ordre n'est pas un style, c'est la sécurité :

1. **Forme** — `net.validate_url` : schéma, longueur, ambiguïté d'hôte.
2. **Provenance** — l'URL figure-t-elle dans ce qu'on a le droit de lire ? C'est
   la fermeture du canal d'exfiltration, et la règle est celle de l'outil
   `web_fetch` d'Anthropic : une adresse que le modèle invente n'est pas une
   source, c'est une invention, et une invention n'est jamais téléchargée.
   Sans cette porte, `web_research` devient un proxy vers n'importe quelle
   adresse, y compris une    adresse interne que le modèle aurait devinée.
3. **Politesse** — `robots.is_allowed` : un refus ici n'est jamais un échec, il
   est une réponse, et son motif est transmis tel quel au rendu.
4. **Téléchargement** — `net.http_get` : résolution vérifiée, IP épinglée,
   plafond d'octets, zéro redirection suivie, file par hôte.
5. **Nature de la réponse** — type de contenu puis statut HTTP. L'ordre
   compte : un `429` en `text/html` doit se lire comme « je suis limité, voici
   son `Retry-After` », et non comme « type refusé ».

Ce que le fetcher **ne** fait pas, volontairement : suivre une redirection,
rendre du JavaScript, extraire un PDF, réessayer. Chaque « je pourrais »
serait une porte que quelqu'un d'autre décidera d'ouvrir un jour.
"""
from __future__ import annotations

import email.utils
import hashlib
import urllib.parse
from datetime import datetime, timezone

from backend.config.defaults import TOOL_LIMITS
from backend.services.web import extract, net, robots
from backend.services.web.types import NETWORK_BLOCKED, ROBOTS_DENIED, URL_NOT_ADMITTED, WebError, WebPage

# Le plafond de la conception (512 KiB) porte sur ce qui ENTRE, pas sur ce qui
# arrive. Une page Wikipédia fait 600 ko de balisage pour 40 ko de texte : la
# refuser au octet près revient à refuser l'internet réel, pas à protéger qui
# que ce soit. Le risque qu'un plafond d'octets couvre réellement est la
# décompression (une bombe) — d'où le plafond de TÉLÉCHARGEMENT ci-dessous, plus
# large, et le plafond de TEXTE, qui lui est bien celui du contrat.
TEXT_BYTES_MAX = int(TOOL_LIMITS["web_page_bytes_max"])
RAW_BYTES_MAX = int(TOOL_LIMITS["web_bytes_max"])
# Deux pages par tour (C15) : on borne le texte rendu bien en deçà de la
# fenêtre de contexte, pour que la citation d'une page ne mange pas le tour.
TEXT_CHARS_MAX = 40_000
ACCEPT = "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.5"

# Types acceptés. `text/*` est accepté en bloc : un `text/csv` reste du texte
# lisible, et le refus d'un type nommé vaut mieux qu'un charabia d'octets.
_ACCEPTED_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "text/plain", "application/pdf",
    "application/json", "application/xml",
})
_ACCEPTED_PREFIXES = ("text/",)


def _media_type(content_type: str) -> str:
    """Le type MIME nu, minuscules et sans son paramètre.

    `text/html; charset=utf-8` devient `text/html` : la décision de type se prend
    sur le type, et le paramètre sert ailleurs — à l'encodage.
    """
    return content_type.split(";", 1)[0].strip().lower()


def _is_accepted(media: str) -> bool:
    return media in _ACCEPTED_TYPES or media.startswith(_ACCEPTED_PREFIXES)


def _admission_key(parts: urllib.parse.SplitResult) -> str:
    """La forme canonique d'une URL, pour comparer à ce qui a été admis.

    Le fragment est retiré (le serveur ne le voit jamais) et le port implicite
    est écrit, mais le chemin et la requête restent intacts : on tolère une
    différence de frappe, jamais une différence d'adresse.
    """
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)
    host = parts.hostname or ""
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return urllib.parse.urlunsplit((scheme, authority, parts.path, parts.query, ""))


def _admission_keys(admitted: frozenset[str]) -> frozenset[str]:
    """Les mêmes clés, calculées sur ce qui a été admis.

    Comparer des chaînes brutes punirait une différence de frappe — un
    fragment, un port implicite — comme si c'était une autre adresse. Une entrée
    illisible n'admet rien et n'interrompt pas la lecture : c'est le refus par
    défaut qui reste vrai.
    """
    cles: set[str] = set()
    for candidate in admitted:
        try:
            cles.add(_admission_key(urllib.parse.urlsplit(candidate.strip())))
        except ValueError:
            continue
    return frozenset(cles)


class Fetcher:
    """Le seul chemin du projet qui transforme une URL en `WebPage`."""

    def __init__(self, *, max_chars: int = TEXT_CHARS_MAX) -> None:
        self._max_chars = max_chars

    def fetch(
        self,
        url: str,
        *,
        deadline: float,
        admitted: frozenset[str] | None = None,
        source_type: str = "general",
        proposed_by: str = "",
        budget: net.NetworkBudget | None = None,
    ) -> WebPage:
        """Lit `url`, ou refuse en nommant pourquoi.

        `deadline` est un `time.monotonic()` : il part du même cadran que
        `NetworkBudget.remaining_s()`, donc les deux ne peuvent pas se contredire.
        `admitted`, quand il est fourni, est un ensemble **fermé** : toute URL
        absente de cet ensemble est refusée avant toute socket. C'est la règle
        de l'outil `web_fetch` d'Anthropic, appliquée ici comme un contrôle
        d'accès et non comme une intention : le modèle peut citer une URL,
        jamais décider qu'on la visite. Sans `admitted` (usage interne, cache,
        diagnostic), la provenance n'est pas évaluée — c'est alors
        l'appelant, seul, qui porte cette responsabilité.
        """
        if budget is None:
            budget = net.NetworkBudget(deadline=deadline)

        parts = net.validate_url(url)
        self._require_admitted(url, parts, admitted)
        # La destination est validée AVANT `robots`, et pour une raison
        # diagnostique autant que de sécurité : une adresse interne refusée
        # ici porte son vrai motif, au lieu de ressortir « robots nous a
        # bloqués » — une panne clinique sous une explication plausible.
        net.assert_readable_destination(url)
        verdict = robots.is_allowed(url, budget=budget)
        if not verdict.allowed:
            raise WebError(ROBOTS_DENIED, verdict.reason)

        response = net.http_get(url, budget=budget, max_bytes=RAW_BYTES_MAX, accept=ACCEPT)
        media = _media_type(response.content_type)
        if not _is_accepted(media):
            raise WebError(
                NETWORK_BLOCKED,
                f"type de contenu non lu : « {response.content_type or 'absent'} » "
                f"n'est pas du texte ni un format que ce chemin sait rendre",
            )
        if media == "application/pdf":
            # Le contrat l'admet dans l'`Accept` — c'est une réponse
            # respectable — mais ce chemin n'a pas d'extracteur PDF. On le dit
            # en clair plutôt que de renvoyer des octets illisibles au modèle.
            raise WebError(
                NETWORK_BLOCKED,
                "PDF non extrait par ce chemin : aucune dépendance PDF ici, "
                "le document n'est donc pas cité comme s'il avait été lu",
            )
        if response.status >= 400:
            raise WebError(NETWORK_BLOCKED, _http_error_message(response.status, response.headers))

        body = response.body
        rendered = extract.extract(
            body.decode(_charset(response.content_type), errors="replace"),
            url=url,
            max_chars=self._max_chars,
        )
        return WebPage(
            url=url,
            final_url=url,  # zéro redirection suivie (C18) : la destination est l'origine
            status=response.status,
            content_type=media,
            content_sha256=hashlib.sha256(body).hexdigest(),
            text=rendered.text,
            extractor_version=rendered.extractor_version,
            fetched_at=datetime.now(),
            published_at=_published_at(response.headers),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            title=rendered.title,
            source_type=source_type,
            proposed_by=proposed_by,
        )

    # -- Garde-fous internes -------------------------------------------------
    def _require_admitted(
        self, url: str, parts: urllib.parse.SplitResult, admitted: frozenset[str] | None,
    ) -> None:
        """Provenance : l'URL a-t-elle été rendue à l'utilisateur ou proposée par un moteur ?"""
        if admitted is None:
            return
        if url.strip() in admitted or _admission_key(parts) in _admission_keys(admitted):
            return
        raise WebError(
            URL_NOT_ADMITTED,
            f"URL absente de ce qui a été proposé : {url} — "
            f"une adresse n'est lue que si l'utilisateur l'a donnée ou si un moteur l'a trouvée",
        )


_DEFAULT_FETCHER = Fetcher()


def fetch(
    url: str,
    *,
    deadline: float,
    admitted: frozenset[str] | None = None,
    source_type: str = "general",
    proposed_by: str = "",
    budget: net.NetworkBudget | None = None,
) -> WebPage:
    """Raccourci de module : le fetcher unique, et un budget né du délai."""
    return _DEFAULT_FETCHER.fetch(
        url, deadline=deadline, admitted=admitted, source_type=source_type,
        proposed_by=proposed_by, budget=budget,
    )


# --------------------------------------------------------------------------
# Détails d'en-têtes
# --------------------------------------------------------------------------
def _charset(content_type: str) -> str:
    """L'encodage annoncé, ou l'UTF-8 par défaut — jamais une exception."""
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"')
    return "utf-8"


def _published_at(headers: dict[str, str]) -> str | None:
    """`Last-Modified` en ISO 8601 — une date de publication, pas de visite.

    Un en-tête absent ou illisible ne bloque pas la page : il rend `None`, et le
    rendu dira alors « date inconnue » au lieu d'inventer une fraîcheur.
    """
    raw = headers.get("last-modified")
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _http_error_message(status: int, headers: dict[str, str]) -> str:
    """Un refus d'état HTTP qui se comprend sans le journal : le code, et la suite à donner."""
    if status == 429:
        delay = headers.get("retry-after")
        if delay:
            return f"HTTP 429 : trop de requêtes, le serveur demande d'attendre (Retry-After: {delay})"
        return "HTTP 429 : trop de requêtes, le serveur n'annonce pas de Retry-After"
    if status == 503:
        delay = headers.get("retry-after")
        if delay:
            return f"HTTP 503 : service indisponible, à réessayer après {delay} s (Retry-After)"
        return "HTTP 503 : service indisponible, à réessayer plus tard"
    return f"HTTP {status} : la page n'a pas été renvoyée, rien n'est lu ni cité"
