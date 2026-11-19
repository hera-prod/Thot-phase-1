"""Un tour à outil doit produire UNE seconde décision — pas un `llm_error`.

Défaut mesuré et non corrigé avant ce fichier : l'orchestrateur était plafonné à
UN tour à outil. `append_canonical_tool_round` écrit `function.arguments` comme une
CHAÎNE JSON — la forme OpenAI, épinglée par `test_tool_calls` — mais le template
Jinja embarqué dans le GGUF LFM2.5 fait l'inverse :

    {%- for arg_name, arg_value in func_args.items() -%}

Une chaîne y meurt sur ``'str object' has no attribute 'items'``, `create_chat_completion`
lève, la boucle émet `llm_error` et saute en brief forcé. Les trois retries du
brief meurent du même défaut, sur `loop.py:942` qui ré-appende le round canonique.

Ces tests rendent le VRAI template du VRAI modèle installé à travers le VRAI
`Jinja2ChatFormatter` de llama_cpp (le chemin exact que `llama.Llama` prend pour
LFM2.5-8B : `guess_chat_format_from_gguf_metadata` ne reconnaît pas LFM2.5, donc
`chat_template.default`). Revertir la traduction dans `runtime.py` fait
réapparaître l'`llm_error` exact dans
`test_un_tour_a_outil_donne_une_seconde_decision_sans_llm_error`.
"""

from __future__ import annotations

import json
import struct
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.defaults import MODEL_CONFIGS
from backend.db.models import Base
from backend.inference import loop as L
from backend.inference.llama_server import (
    LlamaServerClient,
    LlamaServerConfig,
    LlamaServerProcess,
    LlamaServerRuntime,
)
from backend.inference.runtime import (
    LlamaCppRuntime,
    RuntimeProtocolError,
    RuntimeRequest,
)
from backend.inference.tool_calls import (
    append_canonical_tool_round,
    as_openai_tool_call,
    normalize_native_tool_calls,
)

GGUF_TEMPLATE_KEY = b"tokenizer.chat_template"
# Le GGUF range ses métadonnées avant les tenseurs : lire l'en-tête suffit et évite
# de charger 5 Go de poids pour lire un template de 4 Ko.
GGUF_HEADER_BYTES = 8 << 20

CONV_ID = "conv_tool_round"
REQUEST_ID = "req_tool_round"
USER_QUESTION = "Que disent les documents sur le papillon ?"
VALID_BRIEF = {"brief": "Les documents décrivent un papillon."}


def _installed_lfm25_template() -> str | None:
    """Le template Jinja embarqué dans le GGUF installé, lu sans charger le modèle."""
    path = Path(MODEL_CONFIGS["LFM2.5-8B"].path)
    if not path.is_file():
        return None
    header = path.open("rb").read(GGUF_HEADER_BYTES)
    key_start = header.find(GGUF_TEMPLATE_KEY)
    if key_start < 0:
        return None
    # Après la clé : le type de la valeur (uint32) puis la longueur (uint64).
    value_start = key_start + len(GGUF_TEMPLATE_KEY) + 4
    size = struct.unpack_from("<Q", header, value_start)[0]
    return header[value_start + 8 : value_start + 8 + size].decode("utf-8")


@pytest.fixture(scope="module")
def lfm25_template() -> str:
    template = _installed_lfm25_template()
    if template is None:
        pytest.skip(
            "le GGUF LFM2.5-8B n'est pas présent : le template Jinja n'est pas observable"
        )
    return template


def _native_round(name: str, call_id: str, arguments: dict) -> dict:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class TemplateRenderingModel:
    """Ce que fait `llama.Llama.create_chat_completion` pour LFM2.5-8B, sans les 5 Go.

    Le vrai modèle rend `chat_template.default` du GGUF via `Jinja2ChatFormatter`
    et lève si le template ne sait pas rendre ce qu'on lui donne. Ce stub rend le
    MÊME template par le MÊME formateur, puis répond le script du tour.
    """

    def __init__(self, template: str, script: list[dict]) -> None:
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter

        self._formatter = Jinja2ChatFormatter(
            template=template,
            eos_token="<|im_end|>",
            bos_token="<|bos|>",
        )
        self._script = list(script)
        self.rendered_prompts: list[str] = []

    def create_chat_completion(self, **kwargs):
        prompt = self._formatter(
            messages=kwargs["messages"],
            functions=None,
            function_call=None,
            tools=kwargs.get("tools"),
            tool_choice=kwargs.get("tool_choice"),
        ).prompt
        self.rendered_prompts.append(prompt)
        message = (
            self._script.pop(0) if self._script else {"content": "Plus rien à dire."}
        )
        return {"choices": [{"message": message, "finish_reason": "stop"}]}


class SingleModelManager:
    def __init__(self, model) -> None:
        self.model = model

    @contextmanager
    def use_model(self, name, estimated_tokens=0, *, kind="interactive"):
        assert name == "LFM2.5-8B"
        yield self.model


class RecordingClient(LlamaServerClient):
    """Client llama-server qui garde la charge utile telle qu'elle part sur le fil."""

    def __init__(self, config: LlamaServerConfig) -> None:
        super().__init__(config)
        self.payloads: list[dict] = []

    def health(self) -> bool:
        return True

    def chat_completion(self, payload, timeout_s=None):
        self.payloads.append(dict(payload))
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


@pytest.fixture()
def turn(tmp_path, monkeypatch, lfm25_template):
    """Tour de chat complet : DB temporaire, VRAI LlamaCppRuntime, VRAI template du modèle."""
    engine = create_engine(f"sqlite:///{tmp_path / 'tool_round.db'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", sessions)
    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        L, "emit", lambda event, payload, **kwargs: events.append((event, payload))
    )

    def run(script: list[dict], message: str = USER_QUESTION):
        model = TemplateRenderingModel(lfm25_template, script)
        runtime = LlamaCppRuntime(SingleModelManager(model))
        collected, trace = L.run_agent_loop(
            None,
            conv_id=CONV_ID,
            request_id=REQUEST_ID,
            message=message,
            image_paths=None,
            history_shared=[],
            runtime=runtime,
        )
        return collected, trace, model

    return SimpleNamespace(run=run, events=events)


def _payloads(events, event: str) -> list[dict]:
    return [payload for name, payload in events if name == event]


# Les quatre appels modèle d'un tour texte réussi : découverte à outil, décision
# qui relit le résultat, handoff du brief, puis rédaction par le générateur. Les
# appels 2, 3 et 4 portent le round canonique dans leur historique.
TURN_SCRIPT = [
    _native_round("search_info", "call_search_1", {"queries": ["papillon"]}),
    {"content": "J'ai lu le résultat, je rédige."},
    _native_round("submit_brief", "call_brief_1", VALID_BRIEF),
    {"content": "Les documents décrivent un papillon."},
]


# ---------- 1. Le défaut lui-même : le round à outil ne tuerait plus le tour suivant --


def test_un_tour_a_outil_donne_une_seconde_decision_sans_llm_error(turn):
    """Après un résultat d'outil, l'orchestrateur DOIT pouvoir reconsulter le modèle.

    Le script du modèle imite le défaut mesuré : tour 1 = un `search_info` natif,
    tour 2 = la décision qui lit le résultat, puis le handoff `submit_brief`. Les
    tours 2, 3 et 4 portent tous le round canonique dans leur historique.
    """
    collected, _trace, model = turn.run(TURN_SCRIPT)

    assert [payload["code"] for payload in _payloads(turn.events, "error")] == [], (
        "aucun llm_error : un tour à outil doit survivre à son propre résultat"
    )
    assert len(model.rendered_prompts) == 4, (
        "les quatre appels modèle doivent avoir été rendus"
    )
    assert "papillon" in model.rendered_prompts[1], (
        "le second tour doit avoir vu la demande"
    )
    # La preuve que le résultat d'outil est lisible par l'orchestrateur au tour 2 :
    # le template n'a pas seulement rendu, il a rendu le RÉSULTAT.
    assert "<|im_start|>tool" in model.rendered_prompts[1]
    assert collected == "Les documents décrivent un papillon."


def test_le_round_a_outil_arrive_au_modele_dans_son_dialecte_natif(turn):
    """Les arguments sont rendus en `fn(valeur)` LFM, pas en JSON OpenAI.

    C'est la trace directe de la traduction : sans elle, le template meurt sur
    `.items()` ; avec elle, il rend la syntaxe sur laquelle le 8B a été entraîné.
    """
    _collected, _trace, model = turn.run(TURN_SCRIPT)

    assert (
        "<|tool_call_start|>[search_info(queries=['papillon'])]<|tool_call_end|>"
        in model.rendered_prompts[1]
    )


# ---------- 2. Le contrat OpenAI, lui, n'a pas bougé -------------------------------


def test_le_round_canonique_garde_la_chaine_openai():
    """`as_openai_tool_call` reste une chaîne JSON : la trace et le serveur la lisent ainsi."""
    records = normalize_native_tool_calls(
        [
            {
                "id": "call_1",
                "function": {"name": "find_files", "arguments": '{"query":"rapport"}'},
            }
        ],
        run_id="run_1",
        round_index=0,
    )
    emitted = as_openai_tool_call(records[0])["function"]["arguments"]
    assert isinstance(emitted, str)

    messages: list[dict] = []
    append_canonical_tool_round(
        messages, "raisonnement", records, {"call_1": "résultat 1"}
    )
    assert isinstance(messages[0]["tool_calls"][0]["function"]["arguments"], str)


def test_le_serveur_llama_recoit_encore_la_chaine_openai():
    """La traduction est locale à llama.cpp : le FIL OpenAI reste une chaîne.

    Le serveur llama rend le même template, mais derrière HTTP : c'est le client
    qui choisit la forme. Changer le fil casserait la spec OpenAI pour rien.
    """
    config = LlamaServerConfig(binary="/opt/llama-server", model="/models/text.gguf")
    client = RecordingClient(config)
    runtime = LlamaServerRuntime(LlamaServerProcess(config, client))
    messages: list[dict] = []
    records = normalize_native_tool_calls(
        [
            {
                "id": "call_1",
                "function": {"name": "find_files", "arguments": '{"query":"rapport"}'},
            }
        ],
        run_id="run_1",
        round_index=0,
    )
    append_canonical_tool_round(
        messages, "raisonnement", records, {"call_1": "résultat 1"}
    )

    runtime.complete_chat(
        RuntimeRequest(messages=tuple(messages), tools=(), tool_choice=None)
    )

    served = client.payloads[0]["messages"][-2]["tool_calls"][0]["function"][
        "arguments"
    ]
    assert isinstance(served, str)
    assert json.loads(served) == {"query": "rapport"}


def test_le_runtime_ne_mutte_pas_les_messages_de_l_appelant(lfm25_template):
    """La traduction est une COPIE : l'historique canonique reste réutilisable.

    Sans copie, le tour suivant verrait des arguments déjà dé-sérialisés et le
    contrat de `messages_orch` changerait de forme en cours de boucle.
    """
    model = TemplateRenderingModel(lfm25_template, [{"content": "ok"}])
    runtime = LlamaCppRuntime(SingleModelManager(model))
    messages: list[dict] = []
    records = normalize_native_tool_calls(
        [
            {
                "id": "call_1",
                "function": {"name": "find_files", "arguments": '{"query":"rapport"}'},
            }
        ],
        run_id="run_1",
        round_index=0,
    )
    append_canonical_tool_round(
        messages, "raisonnement", records, {"call_1": "résultat 1"}
    )
    before = json.dumps(messages, ensure_ascii=False, sort_keys=True)

    runtime.complete_chat(RuntimeRequest(messages=tuple(messages), tools=()))

    assert json.dumps(messages, ensure_ascii=False, sort_keys=True) == before
    assert isinstance(messages[0]["tool_calls"][0]["function"]["arguments"], str)


def test_des_arguments_illisibles_echouent_fort_et_sans_bruit(lfm25_template):
    """Un argument que le template ne saura pas rendre doit crier, pas rendre du vide."""
    model = TemplateRenderingModel(lfm25_template, [{"content": "ok"}])
    runtime = LlamaCppRuntime(SingleModelManager(model))

    with pytest.raises(RuntimeProtocolError, match="arguments illisibles"):
        runtime.complete_chat(
            RuntimeRequest(
                messages=(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "find_files",
                                    "arguments": "pas-du-json",
                                },
                            }
                        ],
                    },
                )
            )
        )
