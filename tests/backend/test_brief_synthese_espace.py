"""Synthèse d'espace — tâche 5.4 : le début d'une discussion d'espace transmet à
l'orchestrateur une synthèse courte de l'espace.

Fiches : orchestration.md §2.1 (synthèse courte de l'espace, contexte documentaire)
et contexte_et_brief.md §2.1 (les éléments de synthèse sélectionnés par l'orchestrateur
transmettent dans le brief ; la synthèse n'est pas un bloc spécial du générateur).

Le texte est un gabarit déterministe : il ne fabrique aucune conclusion, ne traite un
fait comme une instruction, et dit l'absence quand le registre d'espace est vide. C'est
le même caractère que le brief de secours (tâche 5.3) : l'état réellement connu est rendu
tel quel, l'inconnu est dit, jamais deviné.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base, Conversation, MemoryEntry, MemorySynthesis
from backend.inference import loop as L
from backend.inference.brief import build_space_synthesis
from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR
from backend.services import espaces as service

LIGNE_SYNTHESE = "Synthèse d'espace :"
DEMANDE = "Quel est l'orientation de ce projet ?"


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Base temporaire, racines de données jetables, émissions neutralisées."""
    engine = create_engine(f"sqlite:///{tmp_path / 'synthese-espace.db'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", sessions)
    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(L, "emit", lambda event, payload, **kwargs: None)
    return sessions


def _discussion(db, conv_id: str, workspace_id: str | None = None) -> str:
    db.add(
        Conversation(
            id=conv_id, title="Discussion", mode="chat", workspace_id=workspace_id
        )
    )
    db.commit()
    return conv_id


def _orientation(
    db, workspace_id: str, index: int, contenu: str, statut: str, origine: str
) -> None:
    """Une fiche de mémoire d'espace, au format que la synthèse consomme."""
    db.add(
        MemoryEntry(
            id=f"mem_synthese_{index}",
            entry_key=f"memk_synthese_{index}",
            workspace_id=workspace_id,
            version=1,
            is_current=1,
            content=contenu,
            status=statut,
            source_artifact_ids=[],
            origin_context=origine,
        )
    )


class RuntimeFaux:
    """Le strict nécessaire à `run_agent_loop` : une réponse scriptée par appel."""

    runtime_id = "faux"
    runtime_fingerprint = "faux"

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.requests: list = []

    def complete_chat(self, request):
        self.requests.append(request)
        message = self._script.pop(0) if self._script else {"content": "Réponse."}
        return SimpleNamespace(
            raw={"choices": [{"message": message, "finish_reason": "stop"}]}
        )


def _appel(name: str, call_id: str, arguments: dict) -> dict:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


TOUR_SANS_OUTIL = [
    {"content": "Je regarde ce que contient cet espace."},
    _appel(
        "submit_brief",
        "call_brief",
        {"brief": "Le projet utilise SQLite comme base de données."},
    ),
    {"content": "J'ai regardé."},
]


def _contexte_orchestrateur(runtime: RuntimeFaux) -> str:
    """Le contexte du tour tel que le modèle le reçoit (premier appel : la découverte)."""
    for request in runtime.requests:
        contenu = "\n".join(
            str(message.get("content") or "") for message in request.messages
        )
        if "<REGISTRE_NON_FIABLE>" in contenu:
            return contenu
    raise AssertionError("l'orchestrateur n'a jamais reçu de registre")


def _tour(conv_id: str, script: list[dict] | None = None) -> tuple[RuntimeFaux, dict]:
    runtime = RuntimeFaux(script or TOUR_SANS_OUTIL)
    _, trace = L.run_agent_loop(
        None,
        conv_id=conv_id,
        request_id=f"req_{conv_id}",
        message=DEMANDE,
        image_paths=None,
        history_shared=[],
        runtime=runtime,
    )
    return runtime, trace


# ---------- 1-3. Le GABARIT DE LA SYNTHÈSE : CE QU'IL EN FAIT ----------


def test_la_synthèse_rend_les_orientations_avec_statut_et_origine():
    """La synthèse condense les orientations connues, avec leur état et leur origine."""
    faits = [
        {
            "content": "Le projet utilise SQLite comme base de données.",
            "status": "décision sur le projet",
            "origin": "note utilisateur",
        },
        {
            "content": "Les préavis de résiliation sont de trente jours.",
            "status": "observation",
            "origin": "doc_42:0",
        },
    ]
    out = build_space_synthesis(faits)

    # Forme mono-champ : le texte porte la synthèse, jamais un dict de faits.
    assert set(out) == {"synthese"}
    texte = out["synthese"]
    assert texte.startswith("Synthèse d'espace :")
    assert "SQLite" in texte
    assert "décision sur le projet" in texte
    assert "note utilisateur" in texte
    assert "doc_42:0" in texte


def test_la_synthèse_sans_fait_annonce_l_absence():
    """Sans orientation connue, la synthèse dit l'absence — jamais un silence ni un fait inventé."""
    out = build_space_synthesis()
    assert set(out) == {"synthese"}
    assert out["synthese"]
    assert "aucune orientation" in out["synthese"].lower()


def test_la_synthèse_d_une_fait_unique_retient_statut_et_fraîcheur():
    """Une seule orientation : le texte reste court et garde le contexte de fraîcheur."""
    out = build_space_synthesis(
        {
            "content": "L'API repose sur FastAPI.",
            "status": "hypothèse",
            "freshness": "2026-10-25",
        }
    )
    texte = out["synthese"]
    assert "FastAPI" in texte
    assert "hypothèse" in texte
    assert "2026-10-25" in texte


# ---------- 4-6. LA TRANSMISSION : LE DÉBUT D'UNE DISCUSSION D'ESPACE ----------


def test_un_chat_simple_n_entend_pas_de_synthèse(base):
    """Un chat n'appartient à aucun espace : il n'a pas de synthèse à recevoir."""
    db = base()
    try:
        _discussion(db, "conv_simple")
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_simple")[0])
    assert LIGNE_SYNTHESE not in contexte
    assert DEMANDE in contexte, "le test ne prouve rien si la demande n'est plus là"


def test_un_espace_vide_reçoit_l_absence_de_synthèse(base):
    """Un espace sans orientation reçoit la synthèse honnête : l'absence, pas un vide."""
    db = base()
    try:
        workspace_id = str(service.create_workspace(db, "Vide").id)
        _discussion(db, "conv_vide", workspace_id)
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_vide")[0])
    assert LIGNE_SYNTHESE in contexte
    assert "aucune orientation" in contexte.lower()


def test_un_espace_avec_des_orientations_transmet_la_synthèse(base):
    """Un espace avec des orientations connues : la synthèse y est et les éléments y sont
    conservés (contenu, statut, origine) — le texte que l'orchestrateur reçoit est le
    texte construit, rien d'autre."""
    db = base()
    try:
        workspace_id = str(service.create_workspace(db, "Projet").id)
        _discussion(db, "conv_synthese", workspace_id)
        _orientation(
            db,
            workspace_id,
            1,
            "Le projet utilise SQLite comme base de données.",
            "accepted",
            "note utilisateur",
        )
        _orientation(
            db,
            workspace_id,
            2,
            "Les préavis de résiliation sont de trente jours.",
            "stale",
            "doc_42:0",
        )
        db.commit()
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_synthese")[0])
    assert LIGNE_SYNTHESE in contexte
    assert "SQLite" in contexte
    assert "note utilisateur" in contexte
    assert "préavis de résiliation" in contexte
    assert "doc_42:0" in contexte


def test_la_synthese_courante_est_rendue_telle_quelle_sans_double_prefixe(base):
    """La synthèse déjà construite et fraîche est servie telle quelle : la
    repasser dans le gabarit ajouterait un second préfixe « Synthèse d'espace »."""
    db = base()
    try:
        workspace_id = str(service.create_workspace(db, "Projet").id)
        _discussion(db, "conv_synthese_courante", workspace_id)
        db.add(
            MemoryEntry(
                id="mem_courante_1",
                entry_key="memk_courante_1",
                workspace_id=workspace_id,
                version=1,
                is_current=1,
                content="Le projet utilise SQLite comme base de données.",
                status="accepted",
                source_artifact_ids=[],
                origin_context="note utilisateur",
                created_at=datetime(2026, 10, 24, 12, 0, 0),
            )
        )
        contenu = (
            "Synthèse d'espace :\n"
            "- Le projet utilise SQLite comme base de données.\n"
            "  Statut : accepted."
        )
        db.add(
            MemorySynthesis(
                id="syn_courante_1",
                workspace_id=workspace_id,
                version=1,
                is_current=1,
                content=contenu,
                source_entry_ids=["mem_courante_1"],
                created_at=datetime(2026, 10, 25, 12, 0, 0),
            )
        )
        db.commit()
    finally:
        db.close()

    rendu = L._synthese_espace("conv_synthese_courante")

    assert rendu == contenu
    assert rendu.count("Synthèse d'espace :") == 1


def test_la_synthèse_n_est_pas_un_bloc_du_generateur():
    """La synthèse reste un contexte de l'orchestrateur : le générateur ne la reçoit pas
    en bloc. Elle ne fait que transiter par le brief, sélectionné par l'orchestrateur."""
    msgs = L._build_generator_messages(
        [{"role": "user", "content": "Quel est le projet ?"}],
        '{"brief": "Le projet utilise SQLite comme base de données."}',
        "Quel est le projet ?",
    )
    contenu = "\n".join(str(message.get("content") or "") for message in msgs)
    assert "Synthèse d'espace" not in contenu


# ---------- 7. LE PROMPT ORCHESTRATEUR DÉCRIT CE QUE LE MODÈLE REÇOIT ----------


def test_le_prompt_annonce_la_synthèse_que_le_modele_recoit():
    """La rubrique « Contexte que tu reçois » doit décrire ce qui arrive réellement."""
    section = SYSTEM_ORCHESTRATOR.split("## Contexte que tu reçois", 1)[1].split(
        "\n## ", 1
    )[0]
    assert "espace" in section
    assert "synthèse" in section.lower()
    assert "dépôt" in section


class RuntimeContextTooSmall:
    """Faux runtime dont ``complete_chat`` lève une erreur de fenêtre de contexte."""

    runtime_id = "faux"
    runtime_fingerprint = "faux"

    def __init__(self) -> None:
        self.calls = 0

    def complete_chat(self, request):
        self.calls += 1
        from backend.inference.runtime import RuntimeProtocolError

        raise RuntimeProtocolError(
            "CONTEXT_WINDOW_TOO_SMALL: prompt≈30000 tokens pour une fenêtre 32768."
        )


def test_context_window_too_small_emet_code_distinct(base, monkeypatch):
    from backend.inference.runtime import RuntimeProtocolError  # noqa: F401

    runtime = RuntimeContextTooSmall()
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        L,
        "emit",
        lambda event, payload, **kwargs: events.append((event, dict(payload))) or True,
    )
    conv_id = "conv_context_window_too_small"
    db = base()
    db.add(Conversation(id=conv_id, title="Discussion", mode="chat"))
    db.commit()
    db.close()

    L.run_agent_loop(
        None,
        conv_id=conv_id,
        request_id=f"req_{conv_id}",
        message="Explique l'architecture du projet.",
        image_paths=None,
        history_shared=[],
        runtime=runtime,
    )

    assert runtime.calls == 1
    errors = [payload for (event, payload) in events if event == "error"]
    codes = [payload.get("code") for payload in errors]
    assert "context_window_too_small" in codes
    message = next(
        payload["message"]
        for payload in errors
        if payload.get("code") == "context_window_too_small"
    )
    assert "prompt≈" not in message
    assert "CONTEXT_WINDOW_TOO_SMALL" not in message
