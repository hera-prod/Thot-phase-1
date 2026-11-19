from __future__ import annotations

import json
from typing import Any

import llama_cpp
import llama_cpp._internals as internals
from llama_cpp.llama_grammar import LlamaGrammar

from backend.services.image_service import FORMATS
from backend.tools.generate_image import GenerateImageArgs, GenerateImageTool
from backend.tools.registry import get_default_registry


def test_generate_image_schema_requires_prompt_and_lists_all_ratios() -> None:
    """La fiche `generate_image.md` fixe ``aspect_ratio`` : le nom « format »
    est révoqué au schéma — deux noms, un seul champ c'est un double.

    Ce fichier est l'un des deux endroits (avec la suite backend) qui
    vérifient que le schéma publié au modèle reste un JSON-schema que
    llama.cpp accepte : renommer ici sans renommer là laisserait un
    SIGSEGV passer.
    """
    schema = GenerateImageArgs.model_json_schema()
    assert "prompt" in schema["required"]
    assert set(schema["properties"]["aspect_ratio"]["enum"]) == set(FORMATS)
    # `aspect_ratio` reste un Literal simple : pas d'anyOf publié au parseur.
    assert "anyOf" not in schema["properties"]["aspect_ratio"]
    assert set(schema["properties"]["operation"]["enum"]) == {
        "create",
        "edit",
        "outpaint",
    }
    assert set(schema["properties"]) == {
        "operation",
        "prompt",
        "aspect_ratio",
        "seed",
        "source_image_id",
        "reference_ids",
    }
    assert "idempotency_key" not in schema["properties"]


def test_generate_image_accepts_every_service_ratio() -> None:
    for ratio in FORMATS:
        args = GenerateImageArgs.model_validate(
            {"prompt": "chat", "aspect_ratio": ratio}
        )
        assert args.aspect_ratio == ratio


class _VocablessModel:
    """Le parseur de grammaire de llama.cpp ignore le vocabulaire, et le sampler
    n'est jamais appliqué ici : on veut savoir si le parseur ACCEPTE la grammaire."""

    vocab = None


def test_schemas_publies_acceptees_par_le_parseur_de_grammaire() -> None:
    """Tout schéma refusé par llama.cpp tue le backend en SIGSEGV.

    Le convertisseur JSON-schema → GBNF déplie une ``maxLength`` en une
    imbrication par unité (``maxLength: 2000`` → 2000 niveaux) que llama.cpp
    refuse ("exceeds sane defaults"). ``LlamaSampler.add_grammar`` ajoute alors
    le sampler NULL renvoyé par llama.cpp à la chaîne d'échantillonnage sans
    vérifier, et le premier token échantillonné déréférence NULL : SIGSEGV
    innocçable, que ``try/except`` ne peut pas rattraper.

    Ce test passe par le chemin EXACT du runtime — ``LlamaGrammar.from_json_schema``
    (appelé par llama_chat_format quand ``tool_choice`` nomme une fonction) puis
    ``internals.LlamaSampler.add_grammar`` (appelé par ``Llama._create_completion``)
    — et couvre les 11 définitions publiées. Aucun modèle, aucun GPU, quelques ms.
    """
    # Les 11 définitions du registre, PLUS ``SUBMIT_BRIEF_TOOL`` : c'est le seul
    # schéma écrit à la main (backend/prompts/orchestrator.py) qui n'est PAS
    # passé par ``_publishable_schema``, et c'est le SEUL envoyé avec un
    # ``tool_choice`` NOMMÉ — donc le seul qui emprunte le chemin qui plante. Il
    # était hors de la boucle parce que la boucle lisait le registre.
    for definition in _schemas_a_tester():
        name = definition["function"]["name"]
        grammar = LlamaGrammar.from_json_schema(
            json.dumps(definition["function"]["parameters"]), verbose=False
        )
        chain = internals.LlamaSampler()
        try:
            chain.add_grammar(_VocablessModel(), grammar)  # type: ignore[arg-type]
            assert llama_cpp.llama_sampler_chain_get(chain.sampler, 0), (
                f"llama.cpp refuse la grammaire de {name} : la chaîne d'échantillonnage "
                "contiendrait un sampler NULL et le processus mourrait en SIGSEGV."
            )
        finally:
            chain.close()


def _schemas_a_tester() -> list[dict[str, Any]]:
    from backend.prompts.orchestrator import SUBMIT_BRIEF_TOOL

    noms = [
        definition["function"]["name"]
        for definition in get_default_registry().definitions()
    ]
    assert "submit_brief" not in noms, (
        "si submit_brief entre au registre, ce test le couvre deux fois — "
        "enlever l'ajout manuel plutôt que de dupliquer la grammaire"
    )
    return [*get_default_registry().definitions(), SUBMIT_BRIEF_TOOL]


def test_generate_image_description_interdit_le_texte_non_demande() -> None:
    """Le modèle lit la description publiée, pas le prompt orchestrateur seul.

    `prompt` décrit le contenu visible et renvoie le format à `aspect_ratio` ;
    l'outil rappelle qu'aucun texte non demandé ne s'ajoute. Ce test lit la
    définition réellement publiée, pas le modèle d'arguments.
    """
    definition = GenerateImageTool().definition()["function"]
    prompt = definition["parameters"]["properties"]["prompt"]["description"]
    outil = definition["description"]

    assert "aspect_ratio" in prompt
    assert "contenu visible" in prompt
    assert "aucun texte" in outil.casefold()
    assert "aspect_ratio" in outil


def test_generate_image_description_explique_que_la_source_implique_edit() -> None:
    """Une image existante se passe en `source_image_id` et devient `edit`.

    La description publiée au modèle doit le dire : c'est elle qu'il lit pour
    choisir `operation`, et une source avec `create` n'est jamais refusée.
    """
    definition = GenerateImageTool().definition()["function"]
    source = definition["parameters"]["properties"]["source_image_id"]["description"]
    operation = definition["parameters"]["properties"]["operation"]["description"]

    assert "edit" in source
    assert "create" in source
    assert "implique" in operation.casefold() or "devient" in operation.casefold()
    assert "source_image_id" in operation
    assert "edit" in definition["description"]


def test_generate_image_description_renvoie_le_changement_de_format_vers_outpaint() -> (
    None
):
    """q-r.md : un edit avec un ratio cible ne peut pas changer le format.

    La description publiée doit dire que `outpaint` est le changement de format,
    et que `aspect_ratio` est ignoré en edit.
    """
    definition = GenerateImageTool().definition()["function"]
    outil = definition["description"]
    properties = definition["parameters"]["properties"]

    assert "outpaint" in outil.casefold()
    assert "changer le format" in outil.casefold()
    assert "pas edit" in outil.casefold()
    assert "aucune référence" in outil.casefold()
    assert "outpaint" in properties["operation"]["description"].casefold()
    assert "changement de format" in properties["operation"]["description"].casefold()
    assert "ignoré en edit" in properties["aspect_ratio"]["description"].casefold()
    assert (
        "traité comme outpaint" in properties["aspect_ratio"]["description"].casefold()
    )


def test_bornes_de_longueur_publiees_en_prose_seulement() -> None:
    """La borne part dans la description (le modèle la lit) et reste appliquée
    par ``input_model`` — jamais publiée comme contrainte de grammaire."""
    for definition in get_default_registry().definitions():
        published: dict[str, Any] = definition["function"]["parameters"]
        assert "maxLength" not in json.dumps(published)
        assert "maxItems" not in json.dumps(published)
    prompt: dict[str, Any] = GenerateImageTool().definition()["function"]["parameters"][
        "properties"
    ]["prompt"]
    assert "Maximum 2000 caractères." in prompt["description"]
