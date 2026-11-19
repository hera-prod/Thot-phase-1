"""2.15 — Porte de non-régression : aucun outil exposé ne détermine le périmètre.

Le périmètre — la conversation en cours (`conv_id`), l'espace de travail et la
destination de livraison des résultats — est une décision du BACKEND, déduite
du contexte serveur, jamais une donnée que le modèle choisirait par argument
d'outil. Ce fichier est une PORTE : si un jour un argument d'outil exposé à
l'orchestrateur se met à fixer le périmètre, la suite échoue ici et il faut
une décision explicite, pas un contournement.

L'exception interne `tools/search_documents.py` (champ
`scope: Literal["conversation", "workspace"]`) est ACTÉE ci-dessous : ce champ
réduit la portée de la lecture, il ne définit pas un périmètre. L'espace y est
déduit côté serveur de la conversation (`search_documents.py`,
`conversation_workspace(db0, conv_id)` — « le modèle ne choisit pas son
périmètre »), l'identifiant d'espace n'existe dans aucune signature exposée,
et la branche espace ne rend aucun `artifact_id` (un extrait n'est pas une
poignée sur le fichier). Le droit — ce que le modèle peut toucher — est
inchangé.
"""
from __future__ import annotations

import inspect
import typing

from backend.inference import loop
from backend.tools.registry import get_default_registry
from backend.tools.search_documents import SearchDocumentsTool, SearchDocumentsArgs


# Le vocabulaire du périmètre : un argument d'outil qui porterait l'une de
# ces clés déciderait DE QUOI le modèle parle et OÙ les résultats vont —
# c'est le droit, pas la recherche. Il ne doit jamais paraître dans une
# signature exposée.
PERIMETRE_VOCABULAIRE = frozenset({
    "conv_id",
    "conversation_id",
    "conversation",
    "id_conversation",
    "workspace",
    "workspace_id",
    "workspace_name",
    "espace",
    "space",
    "destination",
    "destination_id",
    "target_conversation",
})


def test_la_decouverte_expose_exactement_le_contrat():
    """Ce que l'orchestrateur expose = DISCOVERY_TOOL_NAMES, épinglé ici aussi."""
    registry = get_default_registry()
    exposes = {
        definition["function"]["name"]
        for definition in registry.definitions()
        if definition["function"]["name"] in loop.DISCOVERY_TOOL_NAMES
    }
    assert exposes == loop.DISCOVERY_TOOL_NAMES, (
        "les définitions du registre et la grille de découverte ont cessé de coïncider."
    )
    assert loop.DISCOVERY_TOOL_NAMES == frozenset({
        "search_info", "find_files", "ask_files", "generate_image", "web_research",
    })


def test_aucune_signature_exposee_ne_porte_le_vocabulaire_du_perimetre():
    registry = get_default_registry()
    for nom in sorted(loop.DISCOVERY_TOOL_NAMES):
        outil = registry.get(nom)
        arguments = set(outil.input_model.model_fields)
        intrus = sorted(arguments & PERIMETRE_VOCABULAIRE)
        assert not intrus, (
            f"{nom} publie {intrus} au modèle : un argument d'outil qui fixe le "
            "périmètre déplace la frontière — décision requise, pas contournement."
        )


def test_le_ciblage_ask_files_est_intra_document_pas_un_perimetre():
    """Le `scope` optionnel d'ask_files est un ciblage de LECTURE au sein des
    ressources déjà identifiées (pages, feuilles, plages, zones) — il réduit,
    il n'ouvre ni ne renomme rien."""
    registry = get_default_registry()
    args = registry.get("ask_files").input_model
    arguments = set(args.model_fields)
    assert "scope" in arguments
    assert not (arguments & PERIMETRE_VOCABULAIRE), (
        "ask_files porte en plus du ciblage une clé de périmètre — divergence réelle."
    )
    scope = args.model_fields["scope"].annotation
    sous_cles = set(getattr(scope, "model_fields", {}))
    assert sous_cles <= {"pages", "sheets", "ranges", "regions"}, (
        f"ask_files.scope a gagné des clés hors ciblage intra-document : {sous_cles}"
    )


def test_search_documents_exception_interne_actee():
    """`search_documents` n'est PAS exposé à l'orchestrateur. Son champ
    `scope: Literal["conversation", "workspace"]` redimensionne la lecture,
    pas le droit : l'espace est déduit CÔTÉ SERVEUR de la conversation, il
    n'existe aucun argument qui permette de nommer un espace, et la branche
    espace ne rend aucune poignée sur fichier."""
    if "search_documents" in loop.DISCOVERY_TOOL_NAMES:
        raise AssertionError(
            "search_documents est redevenu un outil du modèle : porte levée, décision requise."
        )
    # Ce que la boucle publie au modèle est exactement la grille (épinglé
    # par le premier test de ce fichier) : hors grille = jamais publié.
    # `search_documents` reste au registre comme moteur FTS interne des
    # exécuteurs brouillon — interne uniquement.
    arguments = set(SearchDocumentsArgs.model_fields)
    assert not (arguments & PERIMETRE_VOCABULAIRE), (
        "search_documents porte une clé de périmètre dans sa signature : divergence réelle."
    )
    # La valeur "workspace" du champ scope n'est qu'un sélecteur de registre
    # de lecture (Literal fermé), pas un identifiant choisi par le modèle.
    annotation = SearchDocumentsArgs.model_fields["scope"].annotation
    assert typing.get_args(annotation) == ("conversation", "workspace")
    # L'espace est résolu serveur-side depuis la conversation, jamais depuis
    # un argument : `conversation_workspace(db0, conv_id)`.
    source = inspect.getsource(SearchDocumentsTool.execute)
    assert "conversation_workspace(db0, conv_id)" in source, (
        "la réduction d'espace de search_documents n'est plus déduite côté serveur "
        "de la conversation : le modèle choisirait son périmètre — divergence réelle."
    )
