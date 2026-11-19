"""L'orchestrateur est le SEUL point de décision de `generate_image` (chemin Phase 1).

Ce fichier remplace l'ancienne suite d'ADMISSION DÉTERMINISTE : celle-ci testait un
filet de Phase 0 (`plan_media_tools` + `is_image_generation_request`) que le
propriétaire a supprimé. Un mot-clé sur un job GPU de 60 à 600 s avait mesuré
4/11 de faux positifs et 10/12 de faux négatifs sur du français ordinaire : la
décision appartient à l'orchestrateur, donc ce sont SES behaviour que l'on verrouille.

Ce qui est pinné ici, et ce qui casse si on revert chaque garde :
  * un pseudo-appel textuel enveloppé dans un <think> (la forme EXACTE de q-r.md)
    devient un VRAI tool_call natif et s'exécute ;
  * le prompt exécuté est celui que le MODÈLE a produit, jamais le message utilisateur ;
  * le re-ask natif forcé ne se déclenche que s'il n'y a pas déjà d'appel natif
    (sinon deux diffusions pour une image) ;
  * un tour sans appel et sans pseudo-appel ne produit NI image NI promesse d'image ;
  * `format` / `seed` du modèle sont honorés, les valeurs invalides refusées ;
  * garde anti-double, exemption de retry, relais de progression, rejeu d'effet.

Aucun modèle, aucun GPU : le runtime et la diffusion sont simulés, l'horloge aussi.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base, ToolCall
from backend.inference import loop as L
from backend.inference.loop import TOTAL_TIMEOUT


ARTIFACT_ID = "art_generated0001"
CONV_ID = "conv_image"
REQUEST_ID = "req_image"
IMAGE_REQUEST = "Génère moi une image de papillon photo réaliste en macro"
# Le prompt que le MODÈLE réécrit, distinct du message utilisateur (c'est tout l'objet
# du contrat « l'orchestrateur réécrit le prompt »).
MODEL_PROMPT = (
    "A hyper-realistic macro photograph of a butterfly perched on a delicate flower, "
    "intricate wing patterns, vivid colors, shallow depth of field, natural lighting"
)
# La forme EXACTE observée en E2E (q-r.md) : réflexion + pseudo-appel JSON textuel.
QR_PSEUDO_CALL_THINK = (
    "<think>L'utilisateur demande une image. J'appelle l'outil de création.</think>\n"
    '{"name": "generate_image", "arguments": {"prompt": "'
    + MODEL_PROMPT
    + '", "aspect_ratio": "1:1"}}'
)
VALID_BRIEF = {
    "brief": "Une demande de création d'image. Le générateur décrit l'image créée. "
    "Provenance : donnée du backend.",
}
# Dialecte 3, mesuré le 28/09 sur « Génère une image d'un marché de fruits, format 16:9 » :
# l'appel est CORRECT et COMPLET, la clé `name` manque. Aucun nom n'est lisible, donc
# le re-ask nommé n'a rien à demander et la décision était perdue.
FRUIT_MARKET_REQUEST = "Génère une image d'un marché de fruits, format 16:9"
FRUIT_MARKET_ENVELOPE_ARGUMENTS = (
    "A vibrant fruit market with stalls selling fresh fruits, warm morning light"
)
FRUIT_MARKET_ENVELOPE = (
    "<think>The user wants a 16:9 image. The image tool takes a prompt and a format.</think>\n"
    '{"prompt": "' + FRUIT_MARKET_ENVELOPE_ARGUMENTS + '", "aspect_ratio": "16:9"}'
)


class _FakeClock:
    """Horloge simulée : 400 s de diffusion ne doivent pas coûter 400 s de test."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def perf_counter(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedRuntime:
    """LFM2.5 simulé : découverte (appels ou texte) → brief forcé → générateur.

    `recovery_answer` est ce que le modèle RÉPOND au re-ask natif forcé : c'est la
    seconde passe du modèle, et ses arguments sont ce qui part au moteur.
    """

    runtime_id = "llama_cpp"

    def __init__(
        self, answer: str = "Voici l'image créée.", reasoning_s: float = 0.0
    ) -> None:
        self.answer = answer
        self.reasoning_s = reasoning_s
        self.discovery_rounds: list[dict | None] = []
        self.discovery_seen = 0
        self.recovery_answer: dict | None = None
        # Les arguments de CHAQUE submit_brief, par tentative. Un script d'un seul
        # élément est rejoué tel quel : c'est exactement ce que fait un modèle qui
        # repose la même question et reçoit la même réponse.
        self.brief_rounds: list[dict] = []
        self.brief_seen = 0
        self.clock: _FakeClock | None = None
        # Réponse au re-ask NON contraint (tool_choice="required") : là, le modèle
        # choisit LUI-MÊME l'outil et ses arguments — le backend n'en impose aucun.
        self.unconstrained_answer: dict | None = None
        self.unconstrained_requests: list[dict] = []
        # (outil nommé ou None, budget transmis) : le budget est une décision de
        # production, elle doit être observable depuis les tests.
        self.seen: list[tuple[str | None, float]] = []
        # Les requêtes COMPLETES vues par le re-ask (messages + tool_choice) : c'est
        # là que se lit « le modèle a régénéré ses arguments ».
        self.recovery_requests: list[dict] = []

    @property
    def runtime_fingerprint(self) -> str:
        return "llama_cpp:LFM2.5-8B-simule"

    def complete_chat(self, request):
        if self.clock is not None:
            self.clock.advance(self.reasoning_s)
        choice = request.tool_choice
        named = choice["function"]["name"] if isinstance(choice, dict) else None
        self.seen.append((named, request.timeout_s))
        if named == "submit_brief":
            index = self.brief_seen
            self.brief_seen += 1
            script = (
                self.brief_rounds[index] if index < len(self.brief_rounds) else None
            )
            arguments = script if script is not None else VALID_BRIEF
            message = {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_brief_{index}",
                        "function": {
                            "name": "submit_brief",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
        elif choice == "required":
            # Re-ask NON contraint : le modèle choisit LUI-MÊME l'outil et ses
            # arguments. Aucun nom n'est proposé par le backend — c'est tout le
            # sujet de ce canal.
            self.unconstrained_requests.append(
                {
                    "tool_choice": request.tool_choice,
                    "messages": [dict(m) for m in request.messages],
                    "tools": [dict(t) for t in request.tools],
                }
            )
            message = self.unconstrained_answer or _native_round(
                "generate_image",
                "call_recovery_0001",
                {
                    "prompt": MODEL_PROMPT,
                    "aspect_ratio": "1:1",
                },
            )
        elif named is not None:
            # Re-ask natif forcé : le SEUL canal qui autorise un choix nommé ici.
            self.recovery_requests.append(
                {
                    "tool_choice": request.tool_choice,
                    "messages": [dict(m) for m in request.messages],
                    "tools": [dict(t) for t in request.tools],
                }
            )
            message = self.recovery_answer or {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_recovery_0001",
                        "function": {
                            "name": named,
                            "arguments": json.dumps(
                                {"prompt": MODEL_PROMPT, "aspect_ratio": "1:1"}
                            ),
                        },
                    }
                ],
            }
        elif not request.tools:
            message = {"content": self.answer}
        else:
            index = self.discovery_seen
            self.discovery_seen += 1
            script = (
                self.discovery_rounds[index]
                if index < len(self.discovery_rounds)
                else None
            )
            message = script or {"content": "Je regarde la demande."}
        return SimpleNamespace(raw={"choices": [{"message": message}]})


def _native_round(name: str, call_id: str, arguments: dict) -> dict:
    """Réponse d'une ronde de découverte : un tool_call natif (jamais textuel)."""
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


@pytest.fixture()
def turn(tmp_path, monkeypatch):
    """Tour de chat complet : DB temporaire, diffusion simulée, horloge simulée."""
    engine = create_engine(f"sqlite:///{tmp_path / 'image_turn.db'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", sessions)
    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    clock = _FakeClock()
    monkeypatch.setattr(L, "time", clock)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        L, "emit", lambda event, payload, **kwargs: events.append((event, payload))
    )
    diffusion_calls: list[dict] = []

    def fake_diffusion(
        tool_call,
        manager=None,
        conv_id=None,
        request_id="",
        run_id="",
        cancel_flag=None,
        on_stage=None,
    ):
        diffusion_calls.append(dict(tool_call.get("args") or {}))
        if on_stage is not None:
            on_stage({"id": "diffusion", "label": "pas 1 / 4", "step": 1, "total": 4})
        return {
            "ok": True,
            "image_published": True,
            "artifact_id": ARTIFACT_ID,
            "result": f"[generate_image] {ARTIFACT_ID} (1024x1024, seed 7, 88.0s, backend flux2, ingestion ok)",
            "latency_ms": 88_000,
        }

    monkeypatch.setattr(L, "_execute_generate_image", fake_diffusion)
    runtime = _ScriptedRuntime()
    runtime.clock = clock

    def run(message: str = IMAGE_REQUEST, **kwargs):
        collected, trace = L.run_agent_loop(
            None,
            conv_id=CONV_ID,
            request_id=REQUEST_ID,
            message=message,
            image_paths=None,
            history_shared=[],
            runtime=runtime,
            **kwargs,
        )
        return collected, trace

    return SimpleNamespace(
        run=run,
        runtime=runtime,
        clock=clock,
        events=events,
        sessions=sessions,
        diffusion_calls=diffusion_calls,
    )


def _payloads(events, event):
    return [payload for name, payload in events if name == event]


def _tool_calls(turn, tool):
    return [
        payload
        for payload in _payloads(turn.events, "tool_call")
        if payload["tool"] == tool
    ]


# ---------- 1. Le pseudo-appel textuel de q-r.md devient un VRAI appel ----------


def test_pseudo_appel_textuel_de_qr_devient_un_appel_natif_execute(turn):
    """Le 8B écrit un pseudo-appel dans sa réflexion ; il doit être RÉCUPÉRÉ, pas perdu.

    Sans la chaîne de récupération, ce tour ne produirait AUCUNE image et le
    générateur raconterait que l'image a été faite (constaté en E2E le 27/09) :
    c'est le défaut historique de ce chemin.
    """
    turn.runtime.discovery_rounds = [{"content": QR_PSEUDO_CALL_THINK}, None]

    collected, trace = turn.run()

    # UN appel natif, exécuté : le pseudo-appel n'est jamais exécuté comme du texte.
    calls = _tool_calls(turn, "generate_image")
    assert len(calls) == 1
    assert calls[0]["tool_call_id"] == "call_recovery_0001"
    assert len(turn.diffusion_calls) == 1
    assert trace["image_published"] is True
    assert trace["image_artifact_id"] == ARTIFACT_ID
    db = turn.sessions()
    try:
        row = (
            db.query(ToolCall)
            .filter(ToolCall.run_id == REQUEST_ID, ToolCall.tool == "generate_image")
            .one()
        )
        assert row.ok is True
    finally:
        db.close()
    assert collected == "Voici l'image créée."


# ---------- 2. Le prompt exécuté est celui du MODÈLE, pas celui de l'utilisateur ----


def test_le_prompt_execute_est_celui_du_modele_pas_le_message_utilisateur(turn):
    """L'orchestrateur RÉÉCRIT le prompt. Le moteur ne voit jamais la phrase de l'utilisateur.

    Pinned parce que l'ancien chemin déterministe faisait exactement l'inverse : il
    passait `message` au moteur. Ici la seule source du prompt est l'argument que le
    MODÈLE a produit dans son appel.
    """
    turn.runtime.discovery_rounds = [
        _native_round(
            "generate_image",
            "call_orch_image",
            {"prompt": MODEL_PROMPT, "aspect_ratio": "1:1"},
        ),
        None,
    ]

    turn.run()

    assert turn.diffusion_calls[0]["prompt"] == MODEL_PROMPT
    assert turn.diffusion_calls[0]["prompt"] != IMAGE_REQUEST
    # Et ce prompt est persisté : un rechargement relit la décision du modèle.
    db = turn.sessions()
    try:
        row = (
            db.query(ToolCall)
            .filter(ToolCall.run_id == REQUEST_ID, ToolCall.tool == "generate_image")
            .one()
        )
        assert row.args["prompt"] == MODEL_PROMPT
    finally:
        db.close()


def test_le_reask_recoit_la_conversation_complete_et_le_choix_nomme(turn):
    """Le re-ask forcé ne se limite pas à une question nue : système + historique + tool_choice.

    Le modèle doit pouvoir régénérer ses arguments lui-même ; un re-ask sans le
    système produirait un prompt vide ou hors sujet.
    """
    turn.runtime.discovery_rounds = [{"content": QR_PSEUDO_CALL_THINK}, None]

    turn.run()

    assert len(turn.runtime.recovery_requests) == 1
    request = turn.runtime.recovery_requests[0]
    assert request["tool_choice"] == {
        "type": "function",
        "function": {"name": "generate_image"},
    }
    roles = [message["role"] for message in request["messages"]]
    assert (
        roles[0] == "system"
        and L.SYSTEM_ORCHESTRATOR in request["messages"][0]["content"]
    )
    assert roles[-1] == "user" and IMAGE_REQUEST in request["messages"][-1]["content"]
    assert [tool["function"]["name"] for tool in request["tools"]] == ["generate_image"]


def test_format_et_seed_du_modele_sont_honores(turn):
    turn.runtime.discovery_rounds = [
        _native_round(
            "generate_image",
            "call_seed",
            {"prompt": MODEL_PROMPT, "aspect_ratio": "16:9", "seed": 4242},
        ),
        None,
    ]

    turn.run()

    assert turn.diffusion_calls[0]["aspect_ratio"] == "16:9"
    assert turn.diffusion_calls[0]["seed"] == 4242


@pytest.mark.parametrize(
    "bad_argument, expected_code",
    [
        ({"aspect_ratio": "carré"}, "ENUM_INVALID"),
        ({"seed": -1}, "BAD_ARGS"),
        ({"prompt": "   "}, "EMPTY_QUERY"),
    ],
)
def test_valeurs_invalides_refusees_avec_le_code_existant(
    turn, bad_argument, expected_code
):
    """Un `format`/`seed`/`prompt` hors contrat est refusé AVANT le moteur, avec SON code.

    Le refus porte le code du service (ENUM_INVALID / BAD_ARGS / EMPTY_QUERY) : c'est
    ce code que `_image_failure_limit` lit pour choisir la limite à dire.
    """

    def refusing(tool_call, manager=None, **kwargs):
        from backend.services import image_service

        with pytest.raises(ValueError) as caught:
            image_service.validate_request(
                prompt=tool_call["args"].get("prompt", ""),
                aspect_ratio=tool_call["args"].get("aspect_ratio", "1:1"),
                reference_ids=tool_call["args"].get("reference_ids") or [],
                seed=tool_call["args"].get("seed"),
            )
        return {
            "ok": False,
            "image_published": False,
            "latency_ms": 1,
            "result": str(caught.value),
        }

    turn.runtime.discovery_rounds = [
        _native_round(
            "generate_image", "call_bad", {"prompt": MODEL_PROMPT, **bad_argument}
        ),
        None,
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(L, "_execute_generate_image", refusing)
        _collected, trace = turn.run()

    assert trace["image_published"] is False
    assert turn.diffusion_calls == []
    db = turn.sessions()
    try:
        row = (
            db.query(ToolCall)
            .filter(ToolCall.run_id == REQUEST_ID, ToolCall.tool == "generate_image")
            .one()
        )
        assert expected_code in row.result
    finally:
        db.close()


# ---------- 3. Le re-ask ne se déclenche QUE s'il n'y a pas déjà d'appel natif ----------


def test_reask_declenche_quand_aucun_appel_natif_nest_present(turn):
    turn.runtime.discovery_rounds = [
        {"content": '{"name":"generate_image","arguments":{"prompt":"papillon"}}'},
        None,
    ]

    turn.run()

    assert [name for name, _ in turn.runtime.seen].count("generate_image") == 1
    assert len(turn.diffusion_calls) == 1


def test_pas_de_reask_quand_un_appel_natif_est_deja_la(turn):
    """Un appel natif déjà là ne doit jamais déclencher un second appel modèle.

    Sans ce court-circuit, le tour ferait 2 requêtes 8B et could diffuser deux fois.
    """
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_native_only", {"prompt": MODEL_PROMPT}),
        None,
    ]

    turn.run()

    assert turn.runtime.recovery_requests == []
    assert len(turn.diffusion_calls) == 1


def test_reask_refuse_un_pseudo_appel_autre_que_generate_image(turn):
    """Le canal de récupération ne sert QUE generate_image : un autre nom textuel n'ouvre rien."""
    turn.runtime.discovery_rounds = [
        {"content": '{"name":"search_info","arguments":{"queries":["papillon"]}}'},
        None,
    ]

    _collected, _trace = turn.run()

    assert [name for name, _ in turn.runtime.seen].count("search_info") == 1
    assert turn.diffusion_calls == []


# ---------- 3bis. Le troisième dialecte : enveloppe d'arguments SANS nom ----------
#
# NOM DU TEST DE RÉVERSION : `test_enveloppe_sans_nom_declenche_un_reask_non_contraint_et_produit_l_image`.
# C'est CELUI-LÀ qui échoue si on revert la production (retrait de
# `carries_nameless_envelope` + `_reask_unconstrained_tool_call`) : sans eux, ce tour
# tombe sur `if not records: break`, aucune diffusion n'est appelée, et l'image
# réelle de la formulation « format 16:9 » est perdue — le 4/5 mesuré.


def test_enveloppe_sans_nom_declenche_un_reask_non_contraint_et_produit_l_image(turn):
    """L'enveloppe nue déclenche le re-ask ; le MODÈLE y nomme l'outil et ses arguments."""
    turn.runtime.discovery_rounds = [{"content": FRUIT_MARKET_ENVELOPE}, None]
    turn.runtime.unconstrained_answer = _native_round(
        "generate_image",
        "call_unconstrained_1",
        {"prompt": MODEL_PROMPT, "aspect_ratio": "21:9", "seed": 77},
    )

    _collected, trace = turn.run(FRUIT_MARKET_REQUEST)

    # L'image est EXÉCUTÉE, et par l'appel que le modèle a nommé lui-même.
    calls = _tool_calls(turn, "generate_image")
    assert len(calls) == 1
    assert calls[0]["tool_call_id"] == "call_unconstrained_1"
    assert len(turn.diffusion_calls) == 1
    assert trace["image_published"] is True
    assert trace["image_artifact_id"] == ARTIFACT_ID
    # C04 : le prompt exécuté est celui du modèle, JAMAIS celui de l'enveloppe.
    assert turn.diffusion_calls[0]["prompt"] == MODEL_PROMPT
    assert turn.diffusion_calls[0]["prompt"] != FRUIT_MARKET_ENVELOPE_ARGUMENTS
    assert turn.diffusion_calls[0]["aspect_ratio"] == "21:9", (
        "l'aspect_ratio vient du re-ask (21:9), pas de l'enveloppe (16:9)"
    )
    # Le canal NOMMÉ n'a pas été emprunté : aucun nom n'était lisible dans l'enveloppe.
    assert turn.runtime.recovery_requests == []
    assert len(turn.runtime.unconstrained_requests) == 1
    assert turn.runtime.unconstrained_requests[0]["tool_choice"] == "required"
    # L'ordre est celui du REGISTRE, pas une convention : `_reask_unconstrained_tool_call`
    # propose `tool_defs` tel quel, et `tool_defs` suit l'ordre d'insertion.
    assert [
        tool["function"]["name"]
        for tool in turn.runtime.unconstrained_requests[0]["tools"]
    ] == [
        "search_info",
        "find_files",
        "ask_files",
        "generate_image",
        "web_research",
    ]


def test_le_reask_non_contraint_ne_sempile_pas_avec_le_reask_nomme(turn):
    """Un tour = au plus UN re-ask. Deux canaux empilés = deux occasions de diffuser.

    Le re-ask nommé part dès qu'un nom est lisible ; sinon le non contraint prend
    le relais. Jamais les deux : un refus du modèle ne doit pas être transformé en
    seconde demande d'appel.
    """
    turn.runtime.discovery_rounds = [
        {"content": '{"name":"generate_image","arguments":{"prompt":"papillon"}}'},
        None,
    ]

    turn.run()

    assert turn.runtime.recovery_requests != []
    assert turn.runtime.unconstrained_requests == []


def test_un_objet_json_ambigu_ne_declenche_aucun_reask(turn):
    """`recency`+`types` sont publiés par deux outils : le signal est refusé, rien ne part.

    Départager serait choisir l'outil à la place du modèle. Le refus est le seul
    comportement honnête, et il coûte zéro appel modèle.
    """
    turn.runtime.discovery_rounds = [
        {"content": '{"recency": "any", "types": "file"}'},
        None,
    ]

    turn.run("Cherche les documents récents")

    assert turn.runtime.unconstrained_requests == []
    assert turn.diffusion_calls == []


def test_une_question_texte_ne_declenche_aucun_reask(turn):
    """Contrôle négatif : une question qui contient un objet JSON quelconque.

    Un objet JSON en prose qui ne correspond à AUCUN schéma d'outil n'est pas une
    tentative d'appel : aucun re-ask, donc aucun appel modèle inutile.
    """
    turn.runtime.discovery_rounds = [
        {"content": 'Voici le relevé : {"ventes": 120, "trimestre": "T3"}'},
        None,
    ]

    _collected, trace = turn.run("Fais un tableau visuel des ventes du trimestre")

    assert turn.runtime.unconstrained_requests == []
    assert turn.diffusion_calls == []
    assert trace["image_published"] is False


def test_un_reask_non_contraint_qui_ne_nomme_rien_ne_produit_ni_appel_ni_image(turn):
    """Le modèle peut refuser ou se taire au re-ask : c'est un résultat, pas une panne.

    Rien n'est enregistré, aucune diffusion n'est lancée, et le tour continue vers la
    phase brief forcée — l'image n'est ni inventée ni promise.
    """
    turn.runtime.discovery_rounds = [{"content": FRUIT_MARKET_ENVELOPE}, None]
    turn.runtime.unconstrained_answer = {"content": "Je ne peux pas créer d'image."}

    _collected, trace = turn.run(FRUIT_MARKET_REQUEST)

    assert turn.runtime.unconstrained_requests != [], "le re-ask a bien été tenté"
    assert _tool_calls(turn, "generate_image") == []
    assert turn.diffusion_calls == []
    assert trace["image_published"] is False


# ---------- 4. Aucun appel, aucun pseudo-appel : ni image, ni mensonge ----------


def test_tour_sans_appel_et_sans_pseudo_appel_ne_produit_ni_image_ni_promesse(turn):
    """Le 8B refuse en prose (« je ne peux pas générer d'image ») : la vérité est « pas d'image ».

    Observé en E2E le 27/09 : le backend laissait passer « l'image a bien été générée
    et publiée » alors qu'aucun pixel n'existait. Aucun mot-clé ne doit rattraper ça —
    l'honnêteté vient de l'absence de publication, pas d'une détection d'intention.
    """
    turn.runtime.discovery_rounds = [
        {"content": "Je ne peux pas générer d'image, mais voici une description."},
        None,
    ]
    turn.runtime.answer = "L'image d'un papillon en macro, réalisée de façon réaliste, a bien été générée et publiée."

    collected, trace = turn.run()

    assert turn.diffusion_calls == []
    assert trace["image_published"] is False
    assert not L._IMAGE_SUCCESS_CLAIM_RE.search(collected), collected


# ---------- 5. Les garde-fous du chemin image, sur le chemin de l'orchestrateur ----


def test_annonce_le_brief_de_l_image(turn):
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_annonce", {"prompt": MODEL_PROMPT}),
        None,
    ]
    _collected, trace = turn.run()
    brief = json.loads(trace["brief_json"])["brief"]
    assert brief.startswith(L._IMAGE_STATUS_FACT), brief


def test_annonce_la_progression_avant_la_premiere_tic(turn):
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_stages", {"prompt": MODEL_PROMPT}),
        None,
    ]
    turn.run()
    stages = [(p["stage"], p["status"]) for p in _payloads(turn.events, "run_stage")]
    assert ("production", "started") in stages
    assert ("admission", "Attente du moteur image (GPU partagé)") in stages
    # Le relais du worker garde les mêmes clés que le studio.
    assert any(
        p["stage"] == "diffusion" and p["step"] == 1 and p["total"] == 4
        for p in _payloads(turn.events, "run_stage")
    )


def test_moteur_coupe_annonce_une_limite_dans_le_brief(turn, monkeypatch):
    def unavailable(tool_call, manager=None, **kwargs):
        return {
            "ok": False,
            "image_published": False,
            "result": "[generate_image: SERVICE_UNAVAILABLE] moteur coupé",
            "latency_ms": 3,
        }

    monkeypatch.setattr(L, "_execute_generate_image", unavailable)
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_off", {"prompt": MODEL_PROMPT}),
        None,
    ]
    _collected, trace = turn.run()
    brief = json.loads(trace["brief_json"])["brief"]
    assert trace["image_published"] is False
    assert "partielle" in brief, (
        f"l'échec image doit annoncer une couverture partielle :\n{brief}"
    )
    # Le SERVICE_UNAVAILABLE porte SA limite spécifique (décision d'émission) :
    # la limite générique n'apparaît que si rien de mieux n'est enregistré.
    assert "La génération d’image n’est pas disponible dans cet environnement." in brief
    assert "La génération d’image n’a pas pu être terminée." not in brief


def test_anti_double_une_seule_diffusion_par_tour(turn):
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_1", {"prompt": MODEL_PROMPT}),
        _native_round("generate_image", "call_2", {"prompt": MODEL_PROMPT, "seed": 9}),
    ]
    _collected, trace = turn.run()
    assert len(turn.diffusion_calls) == 1  # le second appel est dédupliqué
    results = [
        p
        for p in _payloads(turn.events, "tool_result")
        if p["tool_call_id"] == "call_2"
    ]
    assert results and "déjà publiée ce tour" in results[0]["result"]
    brief = json.loads(trace["brief_json"])["brief"]
    assert brief.count("générée et publiée") == 1, (
        f"la promesse image n'apparaît jamais deux fois :\n{brief}"
    )
    assert "Une image a été générée et publiée pour cette demande." in brief


def test_timeout_de_diffusion_nest_pas_retente(turn, monkeypatch):
    calls: list[str] = []

    def timing_out(tool_call, manager=None, **kwargs):
        calls.append("diffusion")
        return {
            "ok": False,
            "image_published": False,
            "result": "[generate_image: [TIMEOUT] Génération image dépassant le délai autorisé.]",
            "latency_ms": 600_000,
        }

    monkeypatch.setattr(L, "_execute_generate_image", timing_out)
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_image_timeout", {"prompt": "un phare"}),
        None,
    ]
    turn.run("Cherche des infos sur les phares")
    assert calls == ["diffusion"]  # un seul job : le retry doublait 20 min


def test_outils_lourds_exclus_du_retry():
    assert L.RETRY_EXEMPT_TOOLS == frozenset({"generate_image"})
    assert L.RETRY_EXEMPT_TOOLS <= L.DISCOVERY_TOOL_NAMES


# ---------- 6. Le refus du générateur ne peut pas fabriquer une image ----------


def test_reponse_qui_annonce_une_image_inexistante_est_retiree(turn, monkeypatch):
    def unavailable(tool_call, manager=None, **kwargs):
        return {
            "ok": False,
            "image_published": False,
            "result": "[generate_image: SERVICE_UNAVAILABLE] moteur coupé",
            "latency_ms": 3,
        }

    monkeypatch.setattr(L, "_execute_generate_image", unavailable)
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_refus", {"prompt": MODEL_PROMPT}),
        None,
    ]
    turn.runtime.answer = (
        "Voici l'image que j'ai créée. Le papillon a des ailes bleues."
    )

    collected, trace = turn.run()

    assert trace["image_published"] is False
    assert not L._IMAGE_SUCCESS_CLAIM_RE.search(collected), collected
    assert collected == "Le papillon a des ailes bleues.", (
        "le reste de la réponse doit survivre"
    )
    streamed = "".join(payload["text"] for payload in _payloads(turn.events, "answer"))
    assert streamed == collected


def test_reponse_entierement_fabriquee_devient_la_limite_du_backend(turn, monkeypatch):
    def unavailable(tool_call, manager=None, **kwargs):
        return {
            "ok": False,
            "image_published": False,
            "result": "[generate_image: SERVICE_UNAVAILABLE] moteur coupé",
            "latency_ms": 3,
        }

    monkeypatch.setattr(L, "_execute_generate_image", unavailable)
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_fabrique", {"prompt": MODEL_PROMPT}),
        None,
    ]
    turn.runtime.answer = "Voici l'image que j'ai créée."

    collected, _trace = turn.run()

    # Le backend écrit la limite ENREGISTRÉE, jamais une phrase de modèle.
    assert (
        collected
        == "La génération d’image n’est pas disponible dans cet environnement."
    )
    assert "j'ai créée" not in collected


def test_limite_specifique_survit_a_la_reconciliation(turn, monkeypatch):
    def refusing(tool_call, manager=None, **kwargs):
        return {
            "ok": False,
            "image_published": False,
            "latency_ms": 3,
            "user_limit": "Le moteur image est désactivé dans les réglages.",
            "result": "[generate_image: SERVICE_UNAVAILABLE] moteur coupé",
        }

    monkeypatch.setattr(L, "_execute_generate_image", refusing)
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_limite", {"prompt": MODEL_PROMPT}),
        None,
    ]
    _collected, trace = turn.run()
    brief_text = json.loads(trace["brief_json"])["brief"]
    assert "Le moteur image est désactivé dans les réglages." in brief_text
    assert "La génération d’image n’a pas pu être terminée." not in brief_text, (
        "la limite SPÉCIFIQUE du tour remporte la sortie, jamais le générique"
    )


def test_statut_image_ne_compte_qu_une_seule_fois_le_meme_artefact():
    status = L._record_image_status(
        None, {"ok": True, "artifact_id": "art_x", "result": "ok"}
    )
    status = L._record_image_status(
        status, {"ok": True, "artifact_id": "art_x", "result": "rejeu"}
    )
    brief = L._with_image_status(None, status)
    assert brief is not None
    assert brief["brief"].count(L._IMAGE_STATUS_FACT) == 1


# ---------- 9. Le coût d'un brief invalide : un verdict ne se retente pas ----


def _brief_invalide() -> dict:
    """Un handoff qui refuse le schéma : une rubrique révoquée de l'ancienne forme."""
    return {**VALID_BRIEF, "facts": []}


def _brief_turns(turn) -> int:
    return [name for name, _ in turn.runtime.seen].count("submit_brief")


def test_un_brief_invalide_se_reste_trois_fois_puis_tombe_sur_le_repli(turn):
    """Une erreur de FORME est retentable (fiche v1.2 : la signature se corrige
    entièrement en réécrivant l'appel) — mais pas indéfiniment : trois tentatives
    puis le repli déterministe §4.1, jamais un blocage silencieux.
    """
    # Le modèle repose la même forme fausse à chaque tentative (aucun verdict
    # figé depuis le lot C : tout code est retentable dans la limite des trois).
    turn.runtime.brief_rounds = [
        _brief_invalide(),
        _brief_invalide(),
        _brief_invalide(),
    ]
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_storm", {"prompt": MODEL_PROMPT}),
        None,
    ]

    collected, trace = turn.run()

    assert _brief_turns(turn) == 3, "une forme retentable brûle les trois tentatives"
    # Le repli déterministe garantit une réponse — et le fait image réservé vient
    # s'y greffer, il est la seule vérité du tour.
    brief = json.loads(trace["brief_json"])
    # build_fallback_brief porte désormais le fait de publication d'image.
    assert (
        "Pour la réponse : restituer ce qui a été obtenu et expliquer ce qui manque."
        in brief["brief"]
    ), (
        "le repli déterministe doit porter ses cinq éléments, même en présence de l'image publiée"
    )
    assert L._IMAGE_STATUS_FACT in brief["brief"], (
        "même en repli, la promesse image réellement publiée reste dans le brief"
    )
    assert collected  # l'utilisateur reçoit une réponse


def test_un_champ_manquant_se_corrige_au_tour_suivant(turn):
    """Le contraire exact : une erreur de FORME se corrige, elle vaut un retry.

    Sans ce test, « retente » se lirait comme « n'essaie jamais » et la correction
    (message d'erreur → nouvel appel) deviendrait morte.
    """
    vide = {"brief": ""}
    turn.runtime.brief_rounds = [
        vide,
        {"brief": "Une préparation recevable pour ce tour."},
    ]
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_repair", {"prompt": MODEL_PROMPT}),
        None,
    ]

    _collected, trace = turn.run()

    assert _brief_turns(turn) == 2, "une forme corrigeable doit avoir droit au retry"
    assert (
        "Une préparation recevable pour ce tour."
        in json.loads(trace["brief_json"])["brief"]
    )


def test_chaque_echec_brief_est_trace_meme_entre_les_retries(turn):
    """Le refus reste VISIBLE (outil, ok=false, code) à CHAQUE tentative, même
    quand on retente : depuis le lot C, aucun code n'est figé non-retentable
    (fiche v1.2 : une forme se corrige), donc le coût d'un échec n'exonère
    jamais la trace — trois tentatives, trois refus traçés.
    """
    turn.runtime.brief_rounds = [
        _brief_invalide(),
        _brief_invalide(),
        _brief_invalide(),
    ]
    turn.runtime.discovery_rounds = [None]
    turn.run()

    results = _tool_calls(turn, "submit_brief")
    assert len(results) == 3
    for result in results:
        failed = [
            payload
            for payload in _payloads(turn.events, "tool_result")
            if payload["tool_call_id"] == result["tool_call_id"]
        ]
        assert failed and failed[0]["ok"] is False
        assert "UNKNOWN_ARG" in str(failed[0]["result"])


# ---------- 7. Budget de texte : le temps d'une diffusion n'est pas du raisonnement ----


def test_reask_textuel_recoit_le_budget_restant(turn):
    """Le re-ask natif forcé est un appel de PLUS, il doit être budgété.

    Sans budget transmis, `RuntimeRequest` prendrait son défaut de 300 s — un budget
    neuf au milieu d'un tour qui en a déjà brûlé un : exactement le tour image.
    """
    turn.runtime.reasoning_s = 100.0
    turn.runtime.discovery_rounds = [
        {"content": '{"name":"ask_files","arguments":{"questions":["q"]}}'},
    ]

    turn.run("Le papillon est mignon, dis-moi ce que tu en penses")

    reask = [entry for entry in turn.runtime.seen if entry[0] == "ask_files"]
    assert reask, "le pseudo-appel textuel doit déclencher un re-ask nommé"
    assert reask[0][1] == pytest.approx(TOTAL_TIMEOUT - 100.0)
    assert reask[0][1] < TOTAL_TIMEOUT, "aucun appel ne peut disposer d'un budget neuf"


def test_temps_d_un_outil_lourd_hors_budget_de_texte(turn):
    started = turn.clock.perf_counter()
    turn.clock.advance(400.0)
    assert L._text_budget_spent(started, heavy_tool_wall_s=400.0) is False
    assert L._remaining_run_timeout(started, 400.0) == pytest.approx(
        TOTAL_TIMEOUT, abs=1.0
    )


def test_raisonnement_de_400s_bloque_la_reponse(turn):
    turn.runtime.reasoning_s = 400.0
    collected, _trace = turn.run("Explique la photosynthèse")
    assert (
        collected
        == "La réponse complète n'a pas pu être terminée dans le délai imparti."
    )
    assert [
        p for p in _payloads(turn.events, "error") if p.get("code") == "total_timeout"
    ]


def test_diffusion_de_400s_laisse_la_reponse_du_generateur(turn, monkeypatch):
    def slow_diffusion(tool_call, manager=None, **kwargs):
        turn.clock.advance(400.0)
        return {
            "ok": True,
            "image_published": True,
            "artifact_id": ARTIFACT_ID,
            "result": f"[generate_image] {ARTIFACT_ID}",
            "latency_ms": 400_000,
        }

    monkeypatch.setattr(L, "_execute_generate_image", slow_diffusion)
    turn.runtime.discovery_rounds = [
        _native_round("generate_image", "call_lent", {"prompt": MODEL_PROMPT}),
        None,
    ]
    collected, trace = turn.run()
    assert trace["image_published"] is True
    assert collected == "Voici l'image créée."  # pas « délai imparti »
    assert not [
        p for p in _payloads(turn.events, "error") if p.get("code") == "total_timeout"
    ]


def test_budget_de_texte_reste_unique_partout():
    started = time.perf_counter()
    assert L._remaining_run_timeout(started, 0.0) == pytest.approx(
        max(0.1, L._text_budget_remaining_s(started, 0.0))
    )
    assert L._text_budget_spent(started, 0.0) is False  # tour qui démarre
    assert L._text_budget_spent(started - 400.0, 0.0) is True  # 400 s de raisonnement
    assert L._text_budget_spent(started - 400.0, 400.0) is False  # 400 s de diffusion
    assert (
        L._remaining_run_timeout(started - 10_000.0, 0.0) == 0.1
    )  # jamais un délai négatif


# ---------- 8. La Phase 0 n'ouvre plus de job image ----------


def test_la_phase0_ne_planifie_plus_de_generate_image():
    """Le plan déterministe reste un plan d'OBSERVATION : aucun mot-clé ne job image.

    Un plan vide, quelle que soit la formulation, y compris l'impératif exact de q-r.md.
    """
    for message in (
        IMAGE_REQUEST,
        "Je voudrais une photo de papillon en macro",
        "Rends-moi un portrait de chat",
        "Dessine une affiche de festival",
    ):
        assert L.plan_media_tools(message, []) == []


def test_plan_media_tools_observe_toujours_un_artefact():
    entry = {
        "id": "art_abc123def456",
        "kind": "image",
        "name": "photo.png",
        "available": True,
    }
    assert L.plan_media_tools("Décris cette image", [entry]) == [
        {"tool": "vision", "artifact_id": "art_abc123def456", "name": "photo.png"},
    ]


def test_le_predicat_d_admission_n_existe_plus():
    """Pas de raccourci mot-clé à réintroduire : l'API n'expose plus de prédicat."""
    from backend.services import image_service

    assert not hasattr(image_service, "is_image_generation_request")
    assert not hasattr(L, "_asks_image_creation")


def test_rejeu_d_effet_ne_redemande_pas_de_diffusion(turn):
    """Le registre d'effets retrouve une publication résolue au lieu de la refaire.

    C'est ce qui rend la reprise après crash possible, et c'est ce qui garantit
    qu'un rejeu est GRATUIT : le chemin passe par la clé d'événement, il ne
    redemande jamais une diffusion.
    """
    from backend.db.models import Artifact, ArtifactLink, Blob
    from backend.services import effect_ledger

    db = turn.sessions()
    db.add(
        Blob(
            id=f"sha_{ARTIFACT_ID}",
            sha256=f"sha_{ARTIFACT_ID}",
            relative_path=f"blobs/sha256/aa/sha_{ARTIFACT_ID}.bin",
            size_bytes=10,
            mime_detected="image/png",
            state="available",
        )
    )
    db.add(
        Artifact(
            id=ARTIFACT_ID,
            blob_id=f"sha_{ARTIFACT_ID}",
            kind="image",
            original_filename="papillon.png",
            display_name="papillon.png",
        )
    )
    db.flush()
    db.add(
        ArtifactLink(
            id="lnk_rejeu",
            artifact_id=ARTIFACT_ID,
            scope_kind="conversation",
            scope_id=CONV_ID,
            role="generated",
        )
    )
    db.commit()

    # L'évènement « artefact créé » du run est résolu par sa clé déterministe.
    event_key = effect_ledger.artifact_event_key(REQUEST_ID, ARTIFACT_ID)
    effect_ledger.enqueue_event(
        db,
        event_key=event_key,
        run_id=REQUEST_ID,
        event_type="artifact_created",
        payload={"artifact_id": ARTIFACT_ID},
    )
    db.commit()
    assert effect_ledger.mark_event_published_by_key(db, event_key) is True
    assert effect_ledger.is_artifact_replayable(db, ARTIFACT_ID, CONV_ID) is True
    # Aucune diffusion n'a été demandée par ce chemin : le registre suffit.
    assert turn.diffusion_calls == []
