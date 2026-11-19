"""Régression : le générateur ne doit jamais nier une image déjà publiée.

q-r.md : l'utilisateur lisait « Je ne peux pas générer d'image » alors que la
diffusion venait de publier l'artefact. Le brief porte le fait established, mais
LFM2.5-8B le supplantait par son propre priori « je suis un assistant textuel ».
Ce test verrouille l'INSTRUCTION qui corrige ce priori : sans elle, le fait du
brief se fait écraser et l'utilisateur relit exactement le bug d'origine.

Ce test ne prétend PAS prouver l'obéissance du modèle (elle est mesurée en E2E
sur le vrai poids) : il garantit que l'instruction prioritaire reste présente et
non contredite par le reste du prompt.
"""
from __future__ import annotations

import re

from backend.inference.loop import _IMAGE_STATUS_FACT, _build_generator_messages
from backend.prompts.generator import SYSTEM_GENERATOR

# Les DEUX refus réellement observés sur le terrain, verbatim : q-r.md:32
# (« Je ne peux pas générer d'image ») et la capture E2E de ce tour
# (« Je suis incapable de générer d'image »). On ne verrouille pas des
# variantes imaginaires : la règle doit couvrir ce que le 8B a vraiment dit.
_REFUSALS = (
    "je ne peux pas générer d'image",
    "je suis incapable de générer d'image",
)


def _section(heading: str) -> str:
    """Le corps d'une section du system prompt, du titre à la suivante."""
    start = SYSTEM_GENERATOR.index(heading)
    rest = SYSTEM_GENERATOR[start + len(heading):]
    nxt = re.search(r"^##+ ", rest, flags=re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def test_image_published_rule_outranks_the_assistant_textual_prior():
    # Une section dédiée, AVANT la hiérarchie des sources : le 8B pondère le
    # début du system prompt, une ligne isolée en fin de section ne suffit pas.
    assert SYSTEM_GENERATOR.index("## Images déjà publiées") < \
        SYSTEM_GENERATOR.index("## Hiérarchie des sources")
    assert _IMAGE_STATUS_FACT in SYSTEM_GENERATOR or "a été générée et publiée" in SYSTEM_GENERATOR


def test_published_image_rule_forbids_the_denial_verbatim():
    lowered = SYSTEM_GENERATOR.lower()
    for refusal in _REFUSALS:
        # La règle doit nommer le refus tel qu'il est RÉELLEMENT prononcé
        # (1re personne), sinon le 8B ne le reconnaît pas : c'est l'occurrence
        # littérale, pas une reformulation générique.
        assert refusal in lowered, f"la règle image ne nomme pas le refus « {refusal} »"


def test_generator_messages_carry_the_published_image_fact():
    brief_json = (
        '{"language":"fr","complete":true,"facts":[{"text":"'
        + _IMAGE_STATUS_FACT
        + '","evidence_ids":[],"confidence":"established"}]}'
    )
    messages = _build_generator_messages([], brief_json, "Génère moi une image de papillon")
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == SYSTEM_GENERATOR
    assert _IMAGE_STATUS_FACT in messages[1]["content"], "le fait image absent du brief du générateur"


def test_no_other_prompt_section_tells_the_generator_it_cannot_produce_images():
    # Une contradiction plus loin annulerait la règle prioritaire : le modèle
    # lit « tu n'es pas un générateur d'images » et refuse malgré le brief.
    for match in re.finditer(r"^##+ .*$", SYSTEM_GENERATOR, flags=re.MULTILINE):
        heading = match.group(0)
        if heading.startswith("## Images déjà publiées"):
            continue
        start = match.end()
        nxt = re.search(r"^##+ ", SYSTEM_GENERATOR[start:], flags=re.MULTILINE)
        body = SYSTEM_GENERATOR[start:start + nxt.start()] if nxt else SYSTEM_GENERATOR[start:]
        for denial in ("pas un générateur d'image", "ne peux pas créer d'image",
                       "incapable de générer"):
            assert denial not in body.lower(), f"contradiction avec la règle image dans « {heading} »"


# --- Le fait publié doit être ANNONCÉ, pas seulement autorisé -----------------
# Mesuré en E2E : image réelle générée ET attachée, réponse = une description de
# chat en prose, zéro mot sur l'image. Ce n'est pas un mensonge, c'est un
# sous-claim : l'utilisateur ne sait pas qu'une image a été faite pour lui.

def test_l_image_publiee_est_une_OBLIGATION_pas_une_permission():
    section = _section("## Images déjà publiées")
    assert "tu le dis" in section.lower(), \
        "confirmer en option laisse le 8B décrire le sujet et oublier l'image"
    assert "ne réponds pas seulement sur le sujet" in section.lower(), \
        "le défaut mesuré (décrire à la place de livrer) doit être nommé"


def test_le_prompt_donne_un_exemple_image_qui_mentionne_l_image():
    section = _section("## Images déjà publiées")
    assert "Rends-moi un portrait de chat" in section, \
        "l'exemple doit être la formulation MESURÉE, pas une invention"
    assert _IMAGE_STATUS_FACT in section


def test_l_obligation_reste_conditionnee_au_fait_reserve():
    """L'obligation d'annoncer ne doit JAMAIS devenir une invitation à inventer."""
    section = _section("## Images déjà publiées").lower()
    assert "cette phrase absente" in section
    assert "n'affirmes pas qu'une image a été créée" in section

