"""Contrat de charge utile de `web.settings` — la frontière entre l'interface et le moteur.

Une seule méthode, deux usages : **lire** (params vides) et **écrire** (params
partiels). La réponse porte toujours l'état **relu**, jamais l'état demandé —
une écriture qui échoue doit se voir.

Deux raisons de faire ainsi plutôt qu'un `get` et un `set` séparés : la
réponse d'un `set` peut alors mentir exactement comme celle d'un `get`, et
l'interface n'a qu'un aller-retour à écrire.

Ce fichier ne contient aucun format : c'est la description exécutable de ce que
les deux camps doivent tenir chacun. Les clés sont figées ici et testées des
deux côtés (`test_web_settings.py` côté Python, `settingsPage.test.tsx` côté
front), parce qu'une clé dupliquée dans deux langages sans test est une clé qui
divergera.
"""
from __future__ import annotations

from typing import Any

# Clés acceptées EN ENTRÉE. Une clé absente = « ne touche pas à ce réglage ».
PARAM_ENABLED = "enabled"
PARAM_PROVIDER = "provider"
PARAM_SEARXNG_URL = "searxng_url"
PARAM_DOMAINS = "allowed_domains"
WRITE_PARAMS = (PARAM_ENABLED, PARAM_PROVIDER, PARAM_SEARXNG_URL, PARAM_DOMAINS)

# Clues rendues EN SORTIE. `provider_status` est la liste déjà produite par le
# registre des moteurs, enrichie ici d'un libellé lisible et de l'état
# « sélectionné » — l'interface n'a rien à recalculer, donc rien à se tromper.
OUT_ENABLED = "enabled"
OUT_EFFECTIVE_ENABLED = "effective_enabled"
OUT_PROVIDER = "provider"
OUT_PROVIDER_OPTIONS = "provider_options"
OUT_SEARXNG_URL = "searxng_url"
OUT_DOMAINS = "allowed_domains"
OUT_DOMAINS_FREE = "domains_unrestricted"
OUT_SOURCE = "source"
OUT_MODE = "mode"
OUT_WRITABLE = "writable"
OUT_WRITTEN = "written"
OUT_ERROR = "error"

PROVIDER_FIELDS = ("name", "label", "covers", "coverage", "configured", "requires", "selected")

# Libellés : l'interface affiche une phrase, pas un nom de moteur. Le mot
# « navigateur » serait un mensonge — aucun de ces moteurs n'est un navigateur,
# ce sont des index ; le libellé dit ce que la source EST.
PROVIDER_LABELS: dict[str, str] = {
    "auto": "Automatique — encyclopédie puis articles scientifiques",
    "wikipedia": "Wikipédia — encyclopédie générale",
    "arxiv": "arXiv — pré-publications scientifiques",
    "openalex": "OpenAlex — notices et résumés d'articles",
    "searxng": "SearXNG (auto-hébergé) — web général",
}

# `auto` est le seul choix qui ne restreint rien : il laisse le registre choisir
# dans l'ordre de préférence. Les autres nomment UN moteur, et le registre ne
# rend alors que celui-là — y compris s'il est le moins couvrant, parce que
# c'est ce que l'utilisateur a demandé.
DEFAULT_PROVIDER_OPTION = "auto"


def sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Ne laisse passer que les trois clés attendues, aux bons types.

    Un `enabled` qui arrive en chaîne « oui » n'est pas un booléen : on le
    refuse, on ne devine pas. L'interface envoie du JSON typé, donc un refus
    ici signale un vrai bug de câblage, pas une entrée d'utilisateur à
    adoucir.
    """
    nets: dict[str, Any] = {}
    for cle in WRITE_PARAMS:
        if cle not in params:
            continue
        valeur = params[cle]
        if cle == PARAM_ENABLED:
            if isinstance(valeur, bool):
                nets[cle] = valeur
            else:
                raise ValueError("enabled doit être un booléen")
        elif cle == PARAM_PROVIDER:
            if isinstance(valeur, str) and valeur.strip():
                nets[cle] = valeur.strip().lower()
            else:
                raise ValueError("provider doit être un nom de moteur non vide")
        else:  # searxng_url ou allowed_domains
            if not isinstance(valeur, str):
                raise ValueError(f"{cle} doit être une chaîne")
            texte = valeur.strip()
            # La validation de l'adresse est celle du fetcher, pas d'ici : une
            # instance en `http://` sur le loopback est légitime, et la
            # refuser ici reviendrait à interdire l'option tout court.
            nets[cle] = texte[:2000]
    return nets


def apply_and_describe(params: dict[str, Any]) -> dict[str, Any]:
    """Écrit ce qu'on demande (si quelque chose), relit, et décrit l'état.

    Trois garanties dans cet ordre, et l'ordre est le contrat :

    1. une entrée invalide **n'écrit rien** et se rend en `error` — une
       écriture partielle sur une entrée malformée laisserait un réglage à
       moitié changé, ce qu'aucune case à cocher ne sait représenter ;
    2. l'état rendu est **relu depuis le disque**, jamais celui qu'on voulait
       écrire ;
    3. l'environnement est signalé par `source`, pour que l'interface puisse
       dire « une variable d'environnement l'emporte » au lieu de laisser
       l'utilisateur changer une case qui ne bouge rien.
    """
    from backend.services.web import settings as _settings
    from backend.services.web.providers import registry as _registry

    nets: dict[str, Any] = {}
    erreur = ""
    try:
        nets = sanitize_params(params)
    except ValueError as exc:
        erreur = str(exc)

    ecrit = False
    if not erreur and nets:
        ecrit, _ = _settings.write(
            enabled=nets.get(PARAM_ENABLED),
            provider=nets.get(PARAM_PROVIDER),
            searxng_url=nets.get(PARAM_SEARXNG_URL),
            allowed_domains=nets.get(PARAM_DOMAINS),
        )
    elif not erreur and not nets:
        ecrit = True  # lecture seule : rien à écrire n'est un succès

    etat = _settings.read()
    options = []
    choisi = etat.provider or _settings.DEFAULT_PROVIDER
    for entree in _registry.provider_status():
        nom = str(entree.get("name") or "")
        options.append({
            "name": nom,
            "label": PROVIDER_LABELS.get(nom, nom),
            "covers": list(entree.get("covers") or []),
            "coverage": str(entree.get("coverage") or ""),
            "configured": bool(entree.get("configured")),
            "requires": entree.get("requires") or "",
            "selected": nom == choisi,
        })
    options.insert(0, {
        "name": DEFAULT_PROVIDER_OPTION,
        "label": PROVIDER_LABELS[DEFAULT_PROVIDER_OPTION],
        "covers": [],
        "coverage": "laisse le registre choisir dans son ordre de préférence",
        "configured": True,
        "requires": "",
        "selected": choisi == DEFAULT_PROVIDER_OPTION,
    })

    return {
        OUT_ENABLED: etat.enabled,
        OUT_EFFECTIVE_ENABLED: etat.enabled,
        OUT_PROVIDER: choisi,
        OUT_PROVIDER_OPTIONS: options,
        OUT_SEARXNG_URL: etat.searxng_url,
        OUT_DOMAINS: ", ".join(_settings.domaines_admis()),
        OUT_DOMAINS_FREE: _settings.liste_domaines_libre(),
        OUT_SOURCE: etat.source,
        OUT_MODE: _settings.mode_texte(etat),
        # Un fichier écrit par l'environnement prime sur tout : l'interface doit
        # le dire, sinon elle promet un réglage qu'elle ne peut pas tenir.
        OUT_WRITABLE: etat.source != "env",
        OUT_WRITTEN: bool(ecrit),
        OUT_ERROR: erreur,
    }
