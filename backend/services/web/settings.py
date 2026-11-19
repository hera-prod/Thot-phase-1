"""Réglages de la recherche web — persistance et effet.

Deux réglages, un fichier. Le format est `clé=valeur`, une ligne par clé, parce
que l'écrivain n'est pas Python : c'est Electron, qui n'a rien de plus à faire
qu'écrire trois lignes (le même pragmatisme que l'opt-in image, dont le fichier
ne contient que `flux2`). Un fichier JSON aurait demandé à Electron de
sérialiser, donc d'avoir deux implémentations d'un même format — et deux
lectures, donc deux vérité.

Trois règles, et elles sont toutes des règles de sûreté :

1. **L'environnement prime sur le fichier.** Un déploiement et un E2E doivent
   pouvoir imposer un mode sans toucher au disque de l'utilisateur. Le fichier
   ne s'applique donc que là où aucune variable ne parle.
2. **Une lecture n'échoue jamais.** Un fichier absent, illisible, tronqué ou
   écrit par une version future : on rend le défaut, et c'est tout. Un réglage
   illisible ne peut pas faire tomber un tour de chat.
3. **Aucun chemin ne sort.** `describe()` ne rend que des noms de clés et des
   valeurs de réglages — jamais l'emplacement du fichier, qui est un chemin
   absolu de la machine de l'utilisateur (C08).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from backend.services.web.types import WEB_DISABLED

SETTINGS_ENV = "THOT_WEB_SETTINGS_PATH"
DOMAINS_ENV = "THOT_WEB_ALLOWED_DOMAINS"  # impose la liste, prime sur le fichier
SETTINGS_FILENAME = "web_search.settings"

KEY_ENABLED = "enabled"
KEY_PROVIDER = "provider"
KEY_SEARXNG_URL = "searxng_url"
KEY_DOMAINS = "allowed_domains"  # liste blanche, ou "*" pour tout autoriser

# `auto` = l'ordre de préférence du registre, celui d'aujourd'hui. Un nom de
# moteur = l'utilisateur a choisi, et le registre n'en retient qu'un.
PROVIDER_AUTO = "auto"
DEFAULT_PROVIDER = PROVIDER_AUTO

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def settings_path() -> Path:
    """L'emplacement du fichier, résolu comme `flux_spike._optin_path()`.

    La variable d'environnement d'abord, sinon **à côté de la base active** —
    jamais le répertoire courant, qui n'est pas la base active lors d'un
    lancement normal. C'est l'erreur déjà commise une fois avec l'opt-in image,
    et le test qui l'a attrapé existe toujours.
    """
    override = (os.environ.get(SETTINGS_ENV) or "").strip()
    if override:
        return Path(override)
    from backend.db.database import DB_PATH

    return Path(DB_PATH).parent / SETTINGS_FILENAME


def _read_raw() -> dict[str, str]:
    """Lit le fichier sans jamais lever. Un réglage illisible vaut absent."""
    try:
        contenu = Path(settings_path()).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return {}
    brut: dict[str, str] = {}
    for ligne in contenu.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, valeur = ligne.partition("=")
        brut[cle.strip().lower()] = valeur.strip()
    return brut


def _write_raw(valeurs: dict[str, str]) -> bool:
    """Écrit, ou supprime le fichier s'il ne reste plus rien à dire."""
    lignes = [f"{cle}={valeur}" for cle, valeur in valeurs.items() if valeur != ""]
    chemin = settings_path()
    try:
        if not lignes:
            chemin.unlink(missing_ok=True)
            return True
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def _env_enabled() -> bool | None:
    """Ce que la variable de mode dit, dans les DEUX vocabulaires.

     `THOT_WEB_MODE` s'écrit tantôt `web_allowed`/`local_only` (c'est le
     vocabulaire de `research`), tantôt `1`/`0` (c'est celui d'un drapeau).
     Ne pas accepter les deux aurait fait dépendre la page de réglages d'un
    auvais mode de lecture : elle aurait affiché le réglage du FICHIER alors
     que c'est la variable qui décide. Le nom vient de `research.MODE_ENV`,
     pas d'un littéral — deux places à changer pour un même réglage, c'est une
     variable qui finira par diverger.
    """
    from backend.services.web import research as _research

    brute = (os.environ.get(_research.MODE_ENV) or "").strip().lower()
    if not brute:
        return None
    if brute == _research.MODE_LOCAL_ONLY:
        return False
    if brute == _research.MODE_WEB_ALLOWED:
        return True
    if brute in _TRUE:
        return True
    if brute in _FALSE:
        return False
    return None


def _cles_knownes(brut: dict[str, str]) -> bool:
    """Le fichier contient-il une clé de CE fichier, et non du bruit ?

    `source == "fichier"` doit signifier « l'utilisateur a réglé quelque
    chose ». Une ligne de commentaire ou une clé d'une version future ne
    doivent pas faire croire qu'un réglage existe — sinon l'interface annonce
    un état que personne n'a choisi.
    """
    connues = {KEY_ENABLED, KEY_PROVIDER, KEY_SEARXNG_URL}
    return any(cle in connues and valeur != "" for cle, valeur in brut.items())


def env_searxng_url() -> str:
    """L'adresse imposée par l'environnement, ou vide. Point unique du nom."""
    from backend.services.web.providers.searxng import SEARXNG_URL_ENV

    return (os.environ.get(SEARXNG_URL_ENV) or "").strip()


@dataclass(frozen=True)
class WebSettings:
    """Les deux réglages, tels que le reste du code doit les lire.

    `source` dit d'où vient la valeur effective : `env`, `fichier` ou `defaut`.
    C'est ce qui permet à l'interface d'expliquer « ton réglage ne s'applique
    pas parce qu'une variable d'environnement l'impose », au lieu de laisser
    l'utilisateur changer une case qui ne bouge rien.
    """

    enabled: bool
    provider: str
    searxng_url: str
    source: str
    allowed_domains: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "searxng_url": self.searxng_url,
            "source": self.source,
            "allowed_domains": self.allowed_domains,
        }


def read() -> WebSettings:
    """Les réglages effectifs. Une seule lecture, une seule vérité."""
    brut = _read_raw()
    fichier_provider = (
        brut.get(KEY_PROVIDER) or ""
    ).strip().lower() or DEFAULT_PROVIDER
    fichier_url = (brut.get(KEY_SEARXNG_URL) or "").strip()
    mode_env = _env_enabled()
    source = "fichier" if _cles_knownes(brut) else "defaut"

    if mode_env is not None:
        # L'environnement décide du mode ET du moteur associé : on ne veut pas
        # `web_allowed` imposé par env avec un `local_only` choisi à la main
        # dans l'interface, qui laisserait croire que le réglage est pris.
        return WebSettings(
            enabled=mode_env,
            allowed_domains=(
                os.environ.get(DOMAINS_ENV) or brut.get(KEY_DOMAINS, "")
            ).strip(),
            provider=fichier_provider,
            searxng_url=env_searxng_url() or fichier_url,
            source="env",
        )

    # Une clé présente MAIS VIDE n'est pas un réglage : c'est une écriture
    # interrompue, et la lire comme « fermée » ferait fermer la recherche sans
    # que personne l'ait demandé. La valeur absente vaut le défaut.
    if (brut.get(KEY_ENABLED) or "").strip().lower() in _TRUE:
        enabled = True
    elif (brut.get(KEY_ENABLED) or "").strip().lower() in _FALSE:
        enabled = False
    else:
        enabled = True
    return WebSettings(
        enabled=enabled,
        allowed_domains=(
            os.environ.get(DOMAINS_ENV) or brut.get(KEY_DOMAINS, "")
        ).strip(),
        provider=fichier_provider,
        searxng_url=env_searxng_url() or fichier_url,
        source=source,
    )


def write(
    *,
    enabled: bool | None = None,
    provider: str | None = None,
    searxng_url: str | None = None,
    allowed_domains: str | None = None,
) -> tuple[bool, WebSettings]:
    """Écrit ce qu'on lui donne et rend l'état relu, jamais l'état espéré.

    Relire après avoir écrit est la seule façon d'être honnête : si l'écriture
    a échoué, l'interface doit le montrer, pas annoncer un réglage appliqué.
    """
    brut = _read_raw()
    if enabled is not None:
        brut[KEY_ENABLED] = "1" if enabled else "0"
    if provider is not None:
        brut[KEY_PROVIDER] = provider.strip().lower()
    if searxng_url is not None:
        brut[KEY_SEARXNG_URL] = searxng_url.strip()
    if allowed_domains is not None:
        brut[KEY_DOMAINS] = allowed_domains.strip()
    ok = _write_raw(brut)
    return ok, read()


# Domaines de référence, sur lesquels une citation vaut quelque chose. La liste
# est courte et argumentée : encyclopédies collaboratives, archives
# scientifiques, publications officielles. Elle ne prétend pas que ces sites
# sont « sûrs » — un Wikipédia est modifiable par chacun — mais qu'une source
# de cette catégorie est *citable*, ce qui est une autre question.
DOMAINES_PAR_DEFAUT: tuple[str, ...] = (
    "wikipedia.org",
    "wikidata.org",
    "wikisource.org",
    "wikiversity.org",
    "arxiv.org",
    "openalex.org",
    "crossref.org",
    "doi.org",
    "gouv.fr",
    "service-public.fr",
    "legifrance.gouv.fr",
    "insee.fr",
    "europa.eu",
    "huggingface.co",
    "github.com",
)
TOUS_DOMAINE = "*"  # liste désactivée : n'importe quel hôte public est admis


def _hote_de(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def domaine_autorise(url: str) -> bool:
    """L'hôte de cette URL est-il dans la liste blanche ?

    La correspondance se fait sur une **frontière de point** : `wikipedia.org`
    couvre `fr.wikipedia.org`, mais jamais `wikipedia.org.attaquant.example` ni
    `mawikipedia.org`. Un test de sous-chaîne naïf laisse passer les deux, et
    c'est exactement la faille qu'une liste blanche crée si on n'y fait pas
    attention.
    """
    brut = (os.environ.get(DOMAINS_ENV) or "").strip()
    if brut == TOUS_DOMAINE:
        return True
    liste = _liste_domaines() if not brut else _analyser(brut)
    if not liste:
        return True  # aucune liste : on ne restreint pas
    hote = _hote_de(url)
    if not hote:
        return False
    return any(hote == d or hote.endswith("." + d) for d in liste)


def _analyser(brut: str) -> tuple[str, ...]:
    """Découpe une liste de domaines écrite à la main.

    L'espace après la virgule est toléré — c'est la façon d'écrire
    spontanée — et `*.` aussi, parce qu'une liste de sous-domaines s'écrit
    souvent ainsi. Les deux sont normalisés ici, une fois, plutôt que
    re-normalisés à chaque comparaison.
    """
    sortie: list[str] = []
    for part in brut.replace(";", ",").replace("\n", ",").split(","):
        domaine = part.strip().lower().lstrip("*").lstrip(".").rstrip(".")
        if domaine and domaine != TOUS_DOMAINE:
            sortie.append(domaine)
    return tuple(sortie)


def _liste_domaines() -> tuple[str, ...]:
    return _analyser(_read_raw().get(KEY_DOMAINS, "")) or DOMAINES_PAR_DEFAUT


def domaines_admis() -> tuple[str, ...]:
    """La liste effective, telle que l'interface doit l'afficher."""
    return _liste_domaines()


def liste_domaines_libre() -> bool:
    """La liste est-elle désactivée (`*`) ?"""
    return (
        os.environ.get(DOMAINS_ENV) or ""
    ).strip() == TOUS_DOMAINE or not _liste_domaines()


def enabled_or_disabled() -> tuple[bool, str]:
    """`(est actif, source)` — le duo dont `research.current_mode()` a besoin."""
    etat = read()
    return etat.enabled, etat.source


def provider_choice() -> str:
    """Le moteur choisi par l'utilisateur, ou `auto`."""
    return read().provider or DEFAULT_PROVIDER


def searxng_url() -> str:
    """L'adresse de l'instance SearXNG : le fichier d'abord, l'env ensuite.

    L'ordre est l'inverse de celui de `enabled` parce que les deux réglages ne
    répondent pas à la même question : `enabled` décide si le réseau s'ouvre,
    l'adresse dit **où** envoyer. Un déploiement force l'adresse par env pour
    ne pas dépendre d'un fichier écrit par un utilisateur.
    """
    return env_searxng_url() or (read().searxng_url or "")


def mode_texte(etat: WebSettings | None = None) -> str:
    """Le mode que la boucle doit appliquer, en un mot."""
    courant = etat or read()
    if not courant.enabled:
        return WEB_DISABLED
    return "web_allowed"
