"""Résolution : quelle source répond à quel type de question.

Une seule question ici, et elle est embarrassante : **par quel moteur
commencer ?** Un registre mal écrit ferait passer une recherche
scientifique à l'encyclopédie, ou une question d'actualité à un index
d'articles, et le modèle citerait alors une source qui ne couvre pas ce
qu'il a demandé. L'ordre ci-dessous est donc explicite, motivé, et
testé — pas une itération d'un dictionnaire.

Trois règles qui gouvernent le reste :

1. **Un moteur n'est retenu que s'il déclare le type demandé.** La
   déclaration est `provider.covers`, lisible dans la fiche du moteur. Le
   registre ne l'adapte pas, ne l'élargit pas, ne suppose rien.
2. **Aucun moteur applicable n'est une réponse, pas une panne.** La
   fonction rend une `ProviderAnswer` en erreur `NO_PROVIDER` au lieu de
   lever : le rendu doit pouvoir écrire « aucune source applicable pour
   ce type », pas s'interrompre. Une exception ici ferait perdre au
   modèle l'explication de ce qu'il a demandé.
3. **`provider_status()` ne fait aucun I/O réseau.** Elle sert l'interface
   et le rendu ; interroger un moteur pour savoir s'il est là serait la
   façon la plus sûre de tomber sur un réseau lent au moment où
   l'utilisateur ouvre la fenêtre. Lire le fichier de réglages n'est pas
   un I/O réseau : c'est une ligne sur le disque de l'utilisateur, lue à
   chaque appel pour qu'un réglage écrit à chaud s'applique sans relance.

SearXNG est construit à la demande, à partir de la déclaration de
l'utilisateur : la variable `THOT_WEB_SEARXNG_URL` d'abord, le fichier de
réglages ensuite (`SearxngProvider.from_environment` ordonne les deux).
Sans déclaration, il n'existe pas. Aucune instance par défaut, aucun
service public loué — le projet ne peut pas dépendre d'un moteur qu'il ne
contrôle pas sur l'IP d'un particulier.
"""
from __future__ import annotations

from typing import Any, Callable

from backend.services.web import settings as _settings
from backend.services.web.providers.arxiv import ArxivProvider
from backend.services.web.providers.openalex import OpenAlexProvider
from backend.services.web.providers.searxng import SEARXNG_URL_ENV, SearxngProvider
from backend.services.web.providers.wikipedia import WikipediaProvider
from backend.services.web.types import (
    NO_PROVIDER,
    ProviderAnswer,
    WebProvider,
)

# --------------------------------------------------------------------------
# L'ordre, et pourquoi il est celui-là
# --------------------------------------------------------------------------
# `academic` — **arXiv d'abord**. arXiv donne le résumé intégral et
# couvre les disciplines récentes, ce qu'OpenAlex n'a pas toujours. Puis
# **OpenAlex**, pour ce qu'il indexe seul : les articles évalués par les
# pairs, toutes disciplines, avec un DOI. Ce n'est pas un repli, c'est
# la partie du domaine qu'arXiv ne voit pas.
#
# `general` — **SearXNG en premier, s'il existe**. C'est la seule
# couverture du web général du projet : Wikipédia n'est pas le web, et
# une page d'encyclopédie ne répond pas à « prix du transport aérien
# Lyon-Tokyo ». Le classement de Wikipédia vient ensuite, pour ce qu'il
# couvre réellement : des définitions et des références durables.
#
# `news` — **SearXNG seulement**. Aucun autre moteur ne sait ce qui est
# paru aujourd'hui. Wikipédia ne le déclare pas : `list=search` classe par
# pertinence, sans filtre de récence, donc un article de 2015 sort au
# même rang qu'un article de ce matin.
#
# `official` — **aucun moteur**. « Officiel » est une exigence de
# provenance — le site d'un ministère, d'un opérateur, d'un tribunal —,
# et un moteur de recherche rend des adresses sans garantir leur rang.
# Cette exigence se sert par le chemin URL, pas par un moteur de plus.
_PREFERRED_ORDER: dict[str, tuple[str, ...]] = {
    "academic": ("arxiv", "openalex"),
    "general": ("searxng", "wikipedia"),
    "news": ("searxng",),
    "official": (),
}

# Construits à chaque appel plutôt que mémorisés : la variable
# d'environnement peut changer pendant que le programme tourne (l'utilisateur
# démarre son instance SearXNG après Thot), et un cache introduirait un état
# caché que rien ne pourrait rafraîchir. Quatre objets sans état ne valent
# pas une variable globale à invalider.
_FACTORIES: dict[str, Callable[[], WebProvider | None]] = {
    "arxiv": ArxivProvider,
    "openalex": OpenAlexProvider,
    "wikipedia": WikipediaProvider,
    # `from_environment` rend `None` quand la variable n'est pas déclarée :
    # le moteur n'est alors pas construit du tout.
    "searxng": SearxngProvider.from_environment,
}

# Même table, dans l'ordre d'annonce de l'interface.
_ALL_NAMES: tuple[str, ...] = ("arxiv", "openalex", "wikipedia", "searxng")

# Ce qu'un moteur non configuré couvrirait, dit honnêtement : on ne peut
# pas afficher sa `coverage` — il n'existe pas — mais on ne peut pas non
# plus laisser une case vide qui laisserait croire à une couverture.
_UNCONFIGURED_COVERAGE: dict[str, str] = {
    "searxng": (
        "web général via une instance SearXNG auto-hébergée — non configuré : "
        f"aucune instance n'est déclarée ({SEARXNG_URL_ENV} est vide), donc ce type "
        "de source n'est couvert par aucun moteur"
    ),
}


def no_provider_answer(source_type: str) -> ProviderAnswer:
    """Ce qu'on rend quand aucun moteur ne couvre le type demandé.

    Le nom du moteur est vide et la couverture est vide : il n'y a pas de
    source, donc il n'y a rien à décrire. Le message, lui, dit le type
    demandé et ce qu'il faudrait — un moteur ne se débrouille pas
    silencieusement, il dit ce qui lui manque.
    """
    return ProviderAnswer(
        provider="",
        coverage="",
        error=(
            f"[{NO_PROVIDER}] aucun moteur ne couvre le type de source {source_type!r} — "
            f"pour `news`, hébergez une instance SearXNG et déclarez {SEARXNG_URL_ENV} ; "
            f"pour `official`, les sources se donnent par leur URL"
        ),
    )


def _candidats(source_type: str) -> tuple[str, ...]:
    """Les noms de moteur à essayer, dans l'ordre où il faut les essayer.

    Un choix de l'utilisateur REMPLACE l'ordre de préférence : il ne demande
    pas « celui-là en premier », il demande « celui-là, lui seul ». Un moteur
    choisi qui ne couvre pas le type demandé ne fait donc retomber la recherche
    sur aucun autre — l'appel rend `NO_PROVIDER` et le dit. Reprendre un
    autre moteur en silence servirait l'utilisateur à ses yeux tout en
    livrant au brief des sources d'une autre nature que celles qu'il a
    demandées.

    Un nom inconnu du registre n'est pas une erreur non plus : il ne produit
    aucun candidat, donc aucune source — ce que l'appelant rend honnêtement.
    """
    choisi = _settings.provider_choice()
    if choisi != _settings.PROVIDER_AUTO:
        return (choisi,)
    return _PREFERRED_ORDER.get(source_type, ())


def applicable_providers(source_type: str) -> tuple[WebProvider, ...]:
    """Les moteurs retenus pour un type de source, dans l'ordre motivé ci-dessus.

    Renvoie un tuple vide quand rien ne s'applique — c'est une réponse,
    pas une erreur : l'appelant en tire `no_provider_answer(source_type)`
    et le rendu l'explique. Un type hors `SOURCE_TYPES` tombe dans la même
    branche, ce qui est le comportement voulu : `SOURCE_TYPES` est fermé,
    et l'appelant a déjà refusé l'argument avant d'arriver ici.
    """
    providers: list[WebProvider] = []
    for name in _candidats(source_type):
        build = _FACTORIES.get(name)
        if build is None:
            continue
        provider = build()
        if provider is None:
            # Non configuré : il n'existe pas, donc il n'a pas d'ordre.
            continue
        # La déclaration du moteur reste la source de vérité : un moteur
        # qui ne couvre pas le type demandé n'est jamais retenu, quel que
        # soit l'ordre. L'ordre n'est qu'un classement parmi les retenus.
        if source_type not in provider.covers:
            continue
        providers.append(provider)
    return tuple(providers)


def provider_status() -> list[dict[str, Any]]:
    """La fiche de chaque moteur, pour l'interface et le rendu.

    Champs : `name`, `covers` (triée, donc affichable telle quelle),
    `coverage` (la phrase honnête), `configured` (l'utilisateur peut-il
    s'en servir ?), `requires` (ce qu'il faudrait pour l'activer, ou
    `None` quand c'est déjà là).

    **Aucun appel réseau**, et c'est vérifié par test : un objet sentinelle
    est posé sur `net.http_get` pendant l'appel, et l'on affirme qu'il n'a
    pas été sollicité. Interroger un moteur pour savoir s'il est disponible
    serait la manière la plus sûre de faire une découverte réseau au moment
    où l'utilisateur ouvre l'application.
    """
    status: list[dict[str, Any]] = []
    for name in _ALL_NAMES:
        build = _FACTORIES[name]
        provider = build()
        if provider is None:
            status.append({
                "name": name,
                "covers": [],
                "coverage": _UNCONFIGURED_COVERAGE[name],
                "configured": False,
                "requires": f"déclarer {SEARXNG_URL_ENV} (instance SearXNG à	auto-héberger)",
            })
            continue
        status.append({
            "name": name,
            "covers": sorted(provider.covers),
            "coverage": provider.coverage,
            "configured": True,
            "requires": None,
        })
    return status
