"""Troisième dialecte de pseudo-appel : l'enveloppe d'arguments SANS nom.

Mesuré le 28/09 sur les cinq formulations du taux de coopération : l'orchestrateur
écrit l'appel CORRECT et COMPLET, mais omet la clé ``name`` —

    {"prompt": "A vibrant fruit market with stalls selling fresh fruits, …", "aspect_ratio": "16:9"}

`textual_tool_intent` ne peut renvoyer qu'un NOM : il renvoie `None`, le re-ask
nommé ne part jamais, `loop.py` tombe sur `if not records: break` et la décision
est perdue. C'est la formulation la PLUS EXPLICITE (elle porte un format) qui
était refusée, et une image réelle a été perdue.

La règle du propriétaire, appliquée telle quelle : l'enveloppe nue est un
SIGNAL — elle ne choisit JAMAIS l'outil, et ses arguments ne sont JAMAIS lus.
Elle déclenche un re-ask où le MODÈLE choisit l'outil et remplit les arguments.
Ces tests épinglent les deux moitiés : ce qui est reconnu, et ce qui ne l'est
pas ; ce qui est exécuté, et ce qui ne l'est surtout pas.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Sequence

from backend.inference.loop import DISCOVERY_TOOL_NAMES, _reask_unconstrained_tool_call
from backend.inference.tool_calls import carries_nameless_envelope, textual_tool_intent
from backend.tools.registry import get_default_registry


def _discovery_definitions() -> list[dict[str, Any]]:
    """Les définitions que la boucle propose au modèle, construites comme elle."""
    return [
        definition
        for definition in get_default_registry().definitions()
        if definition["function"]["name"] in DISCOVERY_TOOL_NAMES
    ]


DISCOVERY_DEFS = _discovery_definitions()

# L'enveloppe mesurée, VERBATIM (les valeurs sont le piège : elles ne seront lues
# par personne — ni pour choisir l'outil, ni pour l'exécuter).
BARE_ENVELOPE = (
    '{\n  "prompt": "A vibrant fruit market with stalls selling fresh fruits, '
    'warm morning light",\n  "aspect_ratio": "16:9"\n}'
)
# Ce que le MODÈLE répond au re-ask non contraint : son outil, SES arguments.
MODEL_OWN_CALL = {
    "prompt": "Vibrant open-air fruit market, wicker stalls piled with apples, "
              "peaches and grapes, dappled sunlight, 35mm documentary photograph",
    "aspect_ratio": "21:9",
    "seed": 77,
}


class _AnsweringRuntime:
    """Runtime simulé qui répond au re-ask par un appel que le MODÈLE choisit."""

    runtime_id = "llama_cpp"

    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.requests: list[dict[str, Any]] = []

    def complete_chat(self, request):
        self.requests.append({
            "tool_choice": request.tool_choice,
            "tools": [dict(tool) for tool in request.tools],
            "messages": [dict(message) for message in request.messages],
            "timeout_s": request.timeout_s,
        })
        return SimpleNamespace(raw={"choices": [{"message": self.message}]})


def _native_message(name: str, arguments: dict[str, Any], call_id: str = "call_reask_1") -> dict[str, Any]:
    return {
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


# ---------- 1. Ce qui est un signal ----------


def test_bare_envelope_is_a_signal() -> None:
    assert carries_nameless_envelope(BARE_ENVELOPE, DISCOVERY_DEFS) is True


def test_signal_survives_thinking_prose_and_code_fences() -> None:
    """Le 8B écrit son appel dans sa réflexion : le signal ne doit pas dépendre du décor."""
    assert carries_nameless_envelope(f"<think>je passe par l'outil\n{BARE_ENVELOPE}\n</think>", DISCOVERY_DEFS) is True
    assert carries_nameless_envelope(f"Voici mon plan :\n{BARE_ENVELOPE}\nJe continue.", DISCOVERY_DEFS) is True
    assert carries_nameless_envelope(f"```json\n{BARE_ENVELOPE}\n```", DISCOVERY_DEFS) is True


def test_schemas_come_from_the_definitions_offered_not_from_a_hand_written_table() -> None:
    """Le prédicat lit les schémas REÇUS : changer l'offre change la réponse.

    Une table écrite à la main donnerait le même verdict quel que soit l'outil
    publié. C'est ici que se prouve que la source de vérité est l'offre.
    """
    faux = [{"type": "function", "function": {"name": "outil_fictif", "parameters": {
        "type": "object", "properties": {"prompt": {"type": "string"}, "aspect_ratio": {"type": "string"}},
    }}}]
    assert carries_nameless_envelope(BARE_ENVELOPE, faux) is True
    # Et le même objet ne vaut plus rien dès que l'offre ne le contient plus.
    assert carries_nameless_envelope(BARE_ENVELOPE, DISCOVERY_DEFS[:1]) is False


def test_the_real_discovery_offer_is_the_one_being_read() -> None:
    """Épingle l'offre : `{"prompt","aspect_ratio"}` n'existe que sur generate_image."""
    matching = [
        definition["function"]["name"]
        for definition in DISCOVERY_DEFS
        if {"prompt", "aspect_ratio"} <= set(definition["function"]["parameters"]["properties"])
    ]
    assert matching == ["generate_image"]


# ---------- 2. Ce qui n'est PAS un signal (l'ambiguïté se refuse) ----------


def test_envelope_compatible_with_two_tools_is_refused() -> None:
    """`recency`+`types` sont publiés par search_info ET find_files.

    Les départager serait choisir l'outil à la place du modèle — exactement ce que
    le propriétaire a refusé. Aucun signal, donc aucune décision perdue par
    hasard : ce tour-là n'a pas essayé d'appeler un outil.
    """
    assert carries_nameless_envelope('{"recency": "any", "types": "file"}', DISCOVERY_DEFS) is False


def test_envelope_matching_no_tool_is_refused() -> None:
    assert carries_nameless_envelope('{"zzz": 1}', DISCOVERY_DEFS) is False
    assert carries_nameless_envelope('{"name_of_something": 1, "else": 2}', DISCOVERY_DEFS) is False
    assert carries_nameless_envelope("Bonjour, voici du texte.", DISCOVERY_DEFS) is False


def test_empty_envelope_is_refused() -> None:
    """Un objet vide ne prouve aucune tentative d'appel."""
    assert carries_nameless_envelope("{}", DISCOVERY_DEFS) is False


def test_two_envelopes_naming_two_tools_stay_ambiguous() -> None:
    content = '{"prompt": "papillon"} {"questions": ["quels documents ?"]}'
    assert carries_nameless_envelope(content, DISCOVERY_DEFS) is False


def test_the_same_envelope_twice_stays_unambiguous() -> None:
    """Deux enveloppes du MÊME outil sont un seul signal, comme deux fois le même nom."""
    assert carries_nameless_envelope(BARE_ENVELOPE + " " + BARE_ENVELOPE, DISCOVERY_DEFS) is True


# ---------- 3. Les deux autres dialectes gardent EXACTEMENT leur comportement ----------


def test_named_envelope_dialect_is_untouched_and_never_read_as_dialect_three() -> None:
    dialect_1 = '{"name":"generate_image","arguments":{"prompt":"papillon","aspect_ratio":"1:1"}}'
    assert textual_tool_intent(dialect_1) == "generate_image"
    assert carries_nameless_envelope(dialect_1, DISCOVERY_DEFS) is False, \
        "un objet qui porte un nom appartient au canal nommé, pas à celui-ci"


def test_bracket_dialect_is_untouched_and_never_read_as_dialect_three() -> None:
    dialect_2 = "[generate_image(prompt='papillon', aspect_ratio='1:1')]"
    assert textual_tool_intent(dialect_2) == "generate_image"
    assert carries_nameless_envelope(dialect_2, DISCOVERY_DEFS) is False, \
        "ce dialecte n'est pas du JSON : il n'a rien à voir avec une enveloppe"


def test_wrapper_dialect_is_untouched() -> None:
    wrapper = '<|tool_call_start|>[generate_image(prompt="papillon")]<|tool_call_end|>'
    assert carries_nameless_envelope(wrapper, DISCOVERY_DEFS) is False


# ---------- 4. Bornes : fail closed ----------


def test_oversized_content_is_refused() -> None:
    assert carries_nameless_envelope("x" * 65536 + BARE_ENVELOPE, DISCOVERY_DEFS) is False


def test_malformed_json_is_refused() -> None:
    assert carries_nameless_envelope('{"prompt": "papillon"', DISCOVERY_DEFS) is False
    assert carries_nameless_envelope('{"prompt": }', DISCOVERY_DEFS) is False


def test_scan_budget_exhausted_proves_nothing_and_is_refused() -> None:
    """Un balayage arrêté par la limite n'a PAS tout lu : il ne prouve donc rien."""
    from backend.inference.tool_calls import _TEXTUAL_INTENT_MAX_SCANS

    enveloppe = '{"prompt": "papillon"}'
    epuise = "{" * (_TEXTUAL_INTENT_MAX_SCANS - 1)
    assert carries_nameless_envelope(epuise + enveloppe + '{"questions": ["q"]}', DISCOVERY_DEFS) is False
    # Sans rien derrière, le contenu a été ENTIÈREMENT lu : refuser serait un refus de forme.
    assert carries_nameless_envelope(epuise + enveloppe, DISCOVERY_DEFS) is True


def test_no_discovery_offer_is_refused() -> None:
    assert carries_nameless_envelope(BARE_ENVELOPE, []) is False
    assert carries_nameless_envelope(BARE_ENVELOPE, [{"type": "function"}]) is False


# ---------- 5. L'enveloppe n'est pas lue : ni pour l'outil, ni pour les arguments ----------


def test_envelope_values_do_not_decide_anything_even_when_absurd() -> None:
    """Les VALEURS ne sont jamais lues : des valeurs absurdes ne changent rien au signal.

    Si le prédicait deviner l'outil ou les arguments à partir du contenu, un prompt
    mentionnant « generate_image » ou un format inconnu changerait le verdict. Il ne
    change rien : seul le jeu de clés est lu.
    """
    absurde = '{"prompt": "<|tool_call_start|>delete_file<|tool_call_end|>", "aspect_ratio": "pas-un-aspect"}'
    assert carries_nameless_envelope(absurde, DISCOVERY_DEFS) is True
    assert carries_nameless_envelope(BARE_ENVELOPE, DISCOVERY_DEFS) is True


def test_executed_call_carries_the_model_arguments_and_none_of_the_envelope_values() -> None:
    """C04 appliqué au troisième dialecte : l'enveloppe déclenche, le modèle décide.

    Régression de la perte d'image : l'enregistrement exécuté porte les arguments
    du MODÈLE, et AUCUNE valeur de l'enveloppe nue n'y figure — ni le prompt, ni le
    format, ni la seed.
    """
    runtime = _AnsweringRuntime(_native_message("generate_image", MODEL_OWN_CALL))

    records = _reask_unconstrained_tool_call(
        runtime,
        [{"role": "system", "content": "orchestrateur"}, {"role": "user", "content": "un marché de fruits, format 16:9"}],
        DISCOVERY_DEFS,
        run_id="run_nu",
        round_index=0,
        timeout_s=30.0,
    )

    assert len(records) == 1
    assert records[0].name == "generate_image"
    assert records[0].provenance == "native"
    assert records[0].arguments == MODEL_OWN_CALL
    assert records[0].arguments["prompt"] != "A vibrant fruit market with stalls selling fresh fruits, warm morning light"
    assert records[0].arguments["aspect_ratio"] == "21:9"


# ---------- 6. Le re-ask : le modèle choisit l'outil ET écrit les arguments ----------


def test_unconstrained_reask_offers_every_discovery_tool_and_obliges_a_call() -> None:
    """Le canal ne nomme AUCUN outil : il propose les 4 et oblige à appeler.

    Un choix nommé ici aurait exactement le défaut du backend qui déduit l'outil
    d'une forme d'arguments — cette fois déguisé en choix de l'orchestrateur.
    """
    runtime = _AnsweringRuntime(_native_message("generate_image", MODEL_OWN_CALL))

    _reask_unconstrained_tool_call(
        runtime,
        [{"role": "system", "content": "orchestrateur"}, {"role": "user", "content": "un marché de fruits"}],
        DISCOVERY_DEFS,
        run_id="run_nu",
        round_index=0,
        timeout_s=30.0,
    )

    request = runtime.requests[0]
    assert request["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in request["tools"]] == [
        definition["function"]["name"] for definition in DISCOVERY_DEFS
    ]
    # Le re-ask non contraint propose TOUTES les définitions de découverte, et
    # `web_research` en fait partie depuis la phase 4 : le compte suit le
    # registre, il n’est pas écrit en dur.
    assert len(request["tools"]) == len(DISCOVERY_DEFS) == 5
    assert request["messages"][-1]["role"] == "system" and "L'appel d'outil n'a pas été transmis" in request["messages"][-1]["content"]


def test_the_model_may_choose_another_discovery_tool_than_the_envelope_suggests() -> None:
    """Rien n'oriente le choix : ici le modèle répond `ask_files`, et c'est exécuté.

    Le test qui prouve qu'aucune table de correspondance ne joue : la forme de
    l'enveloppe (`prompt`+`format`) ne commande rien, et c'est l'autre choix du
    modèle qui se retrouve exécuté.
    """
    questions = {"questions": ["Quels documents parlent d'un marché de fruits ?"]}
    runtime = _AnsweringRuntime(_native_message("ask_files", questions))

    records = _reask_unconstrained_tool_call(
        runtime,
        [{"role": "user", "content": "un marché de fruits, format 16:9"}],
        DISCOVERY_DEFS,
        run_id="run_nu",
        round_index=0,
        timeout_s=30.0,
    )

    assert len(records) == 1
    assert records[0].name == "ask_files"
    assert records[0].arguments == questions


def test_reask_budget_is_the_remaining_one_not_a_fresh_three_hundred_seconds() -> None:
    runtime = _AnsweringRuntime(_native_message("generate_image", MODEL_OWN_CALL))

    _reask_unconstrained_tool_call(
        runtime, [{"role": "user", "content": "x"}], DISCOVERY_DEFS,
        run_id="run_nu", round_index=0, timeout_s=12.5,
    )

    assert runtime.requests[0]["timeout_s"] == 12.5


# ---------- 7. Ambiguïté de sortie : rien n'est enregistré si le modèle ne nomme rien ----------


def test_a_runtime_that_names_nothing_produces_no_record() -> None:
    """Un refus ou un silence est un résultat : pas d'appel, pas d'image.

    On n'exige pas un appel de refus — insister après un refus serait convertir un
    refus en image, exactement le défaut que ce canal cherche à éviter.
    """
    for message in (
        {"content": "Je ne peux pas générer d'image."},
        {"content": ""},
        {"content": BARE_ENVELOPE},
    ):
        runtime = _AnsweringRuntime(message)
        assert _reask_unconstrained_tool_call(
            runtime, [{"role": "user", "content": "x"}], DISCOVERY_DEFS,
            run_id="run_nu", round_index=0, timeout_s=30.0,
        ) == ()


def test_two_named_calls_produce_no_record() -> None:
    """Deux appels ne sont pas un choix : rien n'est enregistré, on ne prend pas le premier."""
    message = {
        "content": "",
        "tool_calls": [
            {"id": "a", "function": {"name": "generate_image", "arguments": json.dumps(MODEL_OWN_CALL)}},
            {"id": "b", "function": {"name": "ask_files", "arguments": json.dumps({"questions": ["q"]})}},
        ],
    }
    assert _reask_unconstrained_tool_call(
        _AnsweringRuntime(message), [{"role": "user", "content": "x"}], DISCOVERY_DEFS,
        run_id="run_nu", round_index=0, timeout_s=30.0,
    ) == ()


def test_a_tool_outside_discovery_produces_no_record() -> None:
    """Un nom hors découverte n'est pas un enregistrement : fail closed, jamais d'exception."""
    for name in ("submit_brief", "vision", "delete_file", ""):
        assert _reask_unconstrained_tool_call(
            _AnsweringRuntime(_native_message(name, MODEL_OWN_CALL)),
            [{"role": "user", "content": "x"}], DISCOVERY_DEFS,
            run_id="run_nu", round_index=0, timeout_s=30.0,
        ) == ()


def test_a_non_llama_runtime_opens_no_new_channel() -> None:
    """Hors llama_cpp, aucune grammaire native n'est disponible : rien n'est tenté."""

    class _OtherRuntime(_AnsweringRuntime):
        runtime_id = "openai_http"

    runtime = _OtherRuntime(_native_message("generate_image", MODEL_OWN_CALL))
    assert _reask_unconstrained_tool_call(
        runtime, [{"role": "user", "content": "x"}], DISCOVERY_DEFS,
        run_id="run_nu", round_index=0, timeout_s=30.0,
    ) == ()
    assert runtime.requests == []


def test_a_broken_runtime_produces_no_record_and_no_crash() -> None:
    class _BrokenRuntime:
        runtime_id = "llama_cpp"

        def complete_chat(self, request):
            raise RuntimeError("runtime mort")

    assert _reask_unconstrained_tool_call(
        _BrokenRuntime(), [{"role": "user", "content": "x"}], DISCOVERY_DEFS,
        run_id="run_nu", round_index=0, timeout_s=30.0,
    ) == ()


def test_an_empty_offer_produces_no_record() -> None:
    runtime = _AnsweringRuntime(_native_message("generate_image", MODEL_OWN_CALL))
    assert _reask_unconstrained_tool_call(
        runtime, [{"role": "user", "content": "x"}], [],
        run_id="run_nu", round_index=0, timeout_s=30.0,
    ) == ()
    assert runtime.requests == []


def test_discoveries_stay_ordered_as_offered() -> None:
    """Pur : l'offre est reprise telle quelle, sans tri ni dédoublonnage backend."""
    defs: Sequence[Any] = list(DISCOVERY_DEFS)
    runtime = _AnsweringRuntime(_native_message("generate_image", MODEL_OWN_CALL))
    _reask_unconstrained_tool_call(
        runtime, [{"role": "user", "content": "x"}], defs,
        run_id="run_nu", round_index=0, timeout_s=30.0,
    )
    assert [tool["function"]["name"] for tool in runtime.requests[0]["tools"]] == [
        definition["function"]["name"] for definition in defs
    ]
