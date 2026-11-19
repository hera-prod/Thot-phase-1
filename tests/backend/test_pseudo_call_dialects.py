"""Régression q-r.md:8 — « Génère moi une image de papillon photo réaliste en macro ».

Le 8B répond à la découverte par un PSEUDO-APPEL en texte, dans deux dialectes
alternativement (mesuré, pas supposé) :

- ``tests/fixtures/captures/papillon_pseudo_call_json.txt``    — enveloppe JSON
- ``tests/fixtures/captures/papillon_pseudo_call_python.txt``  — appel ``fn(...)``

Seul le premier était lu. Le second passait inaperçu : aucun re-ask, aucune
image, un refus honnête à l'utilisateur. Ces tests épinglent les DEUX dialectes
sur la capture réelle, et prouvent que les arguments exécutés sont ceux du
modèle sur son canal natif — jamais ceux, textuels, du pseudo-appel (C04).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.inference.loop import _recover_named_tool_call
from backend.inference.tool_calls import textual_tool_intent

RACINE = Path(__file__).resolve().parents[2]
CAPTURES = RACINE / "tests" / "fixtures" / "captures"
REFERENCE_PROMPT = "Génère moi une image de papillon photo réaliste en macro"

# (fichier, prompt porté par le TEXTE du pseudo-appel) : le second élément est
# l'argument que C04 interdit d'exécuter.
PSEUDO_CALLS = [
    (
        "papillon_pseudo_call_json.txt",
        "A highly detailed macro photograph of a butterfly, wings with intricate patterns",
    ),
    (
        "papillon_pseudo_call_python.txt",
        "Photographie macro réaliste d'un papillon posé sur une fleur, ailes détaillées",
    ),
]
# Argument que le MODÈLE a produit sur le canal natif lors du re-ask forcé
# (tests/fixtures/captures/papillon_reask_native.json), relu tel quel par le test.
NATIVE_PROMPT = (
    "Photographie macro réaliste d'un papillon posé sur une fleur, ailes détaillées, "
    "profondeur de champ courte, lumière naturelle douce, couleurs vives, texture des "
    "ailes, détail des motifs, fond flou, style hyperréaliste, macro‑photographie, 1:1"
)
GENERATE_IMAGE_DEF = {
    "type": "function",
    "function": {"name": "generate_image", "parameters": {"type": "object"}},
}
MESSAGES = [
    {"role": "system", "content": "orchestrateur"},
    {"role": "user", "content": REFERENCE_PROMPT},
]


def captured_reask_response() -> dict:
    return json.loads((CAPTURES / "papillon_reask_native.json").read_text(encoding="utf-8"))


def named_intent(content: str) -> str:
    intent = textual_tool_intent(content)
    assert intent is not None, "un pseudo-appel capturé doit nommer un outil"
    return intent


class ReplayRuntime:
    """Rejoue une réponse réelle du 8B et note la contrainte qu'il a reçue."""

    runtime_id = "llama_cpp"

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.seen: list[object] = []

    def complete_chat(self, request):
        self.seen.append(request.tool_choice)
        return SimpleNamespace(raw=self.raw)


@pytest.mark.parametrize("filename,textual_prompt", PSEUDO_CALLS)
def test_captured_pseudo_call_names_generate_image(filename: str, textual_prompt: str) -> None:
    content = (CAPTURES / filename).read_text(encoding="utf-8")
    assert REFERENCE_PROMPT in content, "la capture doit être celle du prompt de référence"
    assert textual_prompt in content
    assert textual_tool_intent(content) == "generate_image"


@pytest.mark.parametrize("filename,_textual_prompt", PSEUDO_CALLS)
def test_forced_reask_answers_natively_for_every_captured_dialect(filename: str, _textual_prompt: str) -> None:
    """Le re-ask forcé est la seule voie d'exécution : elle aboutit sur le canal natif."""
    content = (CAPTURES / filename).read_text(encoding="utf-8")
    runtime = ReplayRuntime(captured_reask_response())

    records = _recover_named_tool_call(
        runtime,
        MESSAGES,
        [GENERATE_IMAGE_DEF],
        named_intent(content),
        run_id="run_qr",
        round_index=0,
        timeout_s=30.0,
    )

    assert len(records) == 1
    assert records[0].name == "generate_image"
    assert records[0].provenance == "native"
    assert runtime.seen == [{"type": "function", "function": {"name": "generate_image"}}], \
        "le re-ask doit imposer l'appel nommé, pas le laisser au choix du modèle"


@pytest.mark.parametrize("filename,textual_prompt", PSEUDO_CALLS)
def test_executed_arguments_are_the_model_ones_and_never_the_textual_ones(
    filename: str,
    textual_prompt: str,
) -> None:
    """C04 : le texte choisit l'OUTIL, le modèle écrit les ARGUMENTS."""
    content = (CAPTURES / filename).read_text(encoding="utf-8")
    runtime = ReplayRuntime(captured_reask_response())

    records = _recover_named_tool_call(
        runtime,
        MESSAGES,
        [GENERATE_IMAGE_DEF],
        named_intent(content),
        run_id="run_qr",
        round_index=0,
        timeout_s=30.0,
    )

    assert records[0].arguments["prompt"] == NATIVE_PROMPT
    assert records[0].arguments["prompt"] != textual_prompt, \
        "un argument textuel ne doit jamais atteindre l'exécution"


def test_a_refused_reask_produces_no_call_at_all() -> None:
    """Un refus du modèle forcé est un résultat légitime : pas d'appel, pas d'image."""
    refusal = {
        "choices": [{
            "message": {"role": "assistant", "content": "Je ne peux pas créer d'image."},
            "finish_reason": "stop",
        }],
    }
    runtime = ReplayRuntime(refusal)

    records = _recover_named_tool_call(
        runtime,
        MESSAGES,
        [GENERATE_IMAGE_DEF],
        named_intent((CAPTURES / "papillon_pseudo_call_python.txt").read_text(encoding="utf-8")),
        run_id="run_qr",
        round_index=0,
        timeout_s=30.0,
    )

    assert records == ()


def test_two_dialects_naming_two_tools_stay_ambiguous() -> None:
    content = (
        '{"name":"generate_image","arguments":{"prompt":"papillon"}} '
        "[ask_files(questions=['quels documents ?'])]"
    )
    assert textual_tool_intent(content) is None
    assert textual_tool_intent("[generate_image(prompt='a')] [generate_image(prompt='b')]") == "generate_image", \
        "le même nom dans les deux dialectes reste univoque"
