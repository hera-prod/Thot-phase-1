"""`search_info` remonte la mémoire d'un espace depuis la discussion qui la porte.

`search_info` interroge deux registres sur la même question : le contenu indexé
(observation, markdown, caption, schéma) et le registre de faits révisables de
l'espace (`backend.services.memoire`). Le second ne porte pas de `doc_N` — il
désigne une fiche — et c'est précisément la promesse de phase 6 : la mémoire est
recherchable depuis une discussion de son espace, sans que l'utilisateur ait à
penser à un fichier.

Montage : base temporaire, `SessionLocal` du projet redirigée, racines de données
jetables, ingestion neutralisée. Le service mémoire est un MOCK déterministe : la
décision humaine (proposition/acceptation) y vit déjà, les tests portent ici le
rendu du câblage, pas la pertinence lexicale.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.database import _ensure_fts
from backend.db.models import Artifact, ArtifactLink, Base, Blob, Conversation
from backend.ingestion import attachment_ingest
from backend.services import espaces as service
from backend.tools.base import ToolContext
from backend.tools.search_info import SearchInfoTool


@pytest.fixture()
def base_temporaire(tmp_path, monkeypatch):
    """Base neuve et jetable, index FTS créé, ingestion neutralisée, racines isolées."""
    engine = create_engine(f"sqlite:///{tmp_path / 'search-info-memoire.db'}")
    Base.metadata.create_all(bind=engine)
    _ensure_fts(engine)  # sinon la fiche n'a nulle part où s'indexer
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda artifact_ids: None
    )
    return sessions


def _appeler(arguments: dict, conv_id: str):
    """Le pont `call()` de la classe — le chemin que la boucle utilise aussi."""
    ctx = ToolContext(run_id="r_search_memoire", request_id=conv_id, conv_id=conv_id)
    return asyncio.run(SearchInfoTool().call(arguments, ctx))


def _page_memoire(
    entry_id: str = "mem_9f2c1d77aabc",
    contenu: str = "Le projet utilise SQLite pour le registre local.",
) -> dict:
    """Une page de recherche mémoire, dans le vocabulaire que `memoire.search` rend."""
    return {
        "ok": True,
        "workspace_id": "ws_le_test",
        "total": 1,
        "returned_count": 1,
        "cursor": 0,
        "next_cursor": None,
        "has_more": False,
        "page_size": 5,
        "match_reasons": {
            "relevance": "recouvrement lexical avec la question",
            "usage": "signal secondaire, jamais une validation de vérité (C17)",
            "recency": "signal secondaire",
        },
        "dropped_unreachable_source": 0,
        "results": [
            {
                "entry_id": entry_id,
                "version": 2,
                "status": "accepted",
                "is_candidate": False,
                "is_stale": False,
                "content": contenu,
                "relevance": 0.75,
                "usage_count": 0,
                "sources_count": 1,
                "citation_hint": "la synthèse ne remplace pas la preuve d'origine : cite la source",
            }
        ],
    }


def _source_partagee(db, ws_id: str) -> None:
    """Un artefact réel, partagé au dépôt de l'espace : le périmètre qui autorise."""
    art_id = f"src_{str(ws_id)[:6]}"
    db.add(
        Blob(
            id=f"sha_{art_id}",
            sha256=f"sha_{art_id}",
            relative_path=f"blobs/sha_{art_id}.bin",
            size_bytes=12,
            mime_detected="text/plain",
            state="available",
        )
    )
    db.add(
        Artifact(
            id=art_id,
            blob_id=f"sha_{art_id}",
            kind="text",
            original_filename="note.txt",
            display_name="note.txt",
            ingest_state="indexed",
        )
    )
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}",
            artifact_id=art_id,
            scope_kind="workspace",
            scope_id=ws_id,
            role="shared",
        )
    )


def _espace_et_discussion(sessions, nom: str = "Memoire") -> tuple[str, str]:
    """Un espace, une discussion dedans, un fichier partagé au dépôt."""
    db = sessions()
    try:
        espace = str(service.create_workspace(db, nom).id)
        conversation = "conv_memoire_e2e"
        db.add(
            Conversation(
                id=conversation, title="Discussion", mode="chat", workspace_id=espace
            )
        )
        _source_partagee(db, espace)
        db.commit()
        return espace, conversation
    finally:
        db.close()


def test_search_info_recherche_la_memoire_du_espace(base_temporaire, monkeypatch):
    """`search_info` pose la question à la mémoire du DÉPÔT, avec le périmètre serveur."""
    espace, conversation = _espace_et_discussion(base_temporaire)
    page = _page_memoire()
    appels: list[dict] = []

    def _fake_search(
        db, workspace_id, query, *, limit=5, cursor=None, include_stale=True
    ):
        appels.append(
            {
                "workspace_id": workspace_id,
                "query": query,
                "limit": limit,
                "include_stale": include_stale,
            }
        )
        return page

    monkeypatch.setattr("backend.services.memoire.search", _fake_search)

    resultat = _appeler({"queries": ["protocole sauvegarde"]}, conversation)
    charge = json.loads(resultat.content)

    assert resultat.ok and charge["ok"] is True
    (entree,) = charge["by_query"]
    (fiche,) = entree["memoire"]
    assert fiche["text"] == "Le projet utilise SQLite pour le registre local."
    assert fiche["statut"] == "accepted"
    assert fiche["entry_id"] == "mem_9f2c1d77aabc"
    assert fiche["relevance"] == pytest.approx(0.75)
    assert fiche["sources_count"] == 1
    # Le registre mémoire a bien été interrogé dans l'espace de la discussion,
    # avec l'inclusion des fiches révisables et une page bornée.
    assert appels == [
        {
            "workspace_id": espace,
            "query": "protocole sauvegarde",
            "limit": 5,
            "include_stale": True,
        }
    ]
    # Le rendu parle de la fiche : extrait, statut, entrée.
    assert (
        "1. Mémoire — Le projet utilise SQLite pour le registre local. — statut accepted "
        f"(entry mem_9f2c1d77aabc)" in resultat.display_summary
    )


def test_search_info_sans_fiche_memoire_rend_niets(base_temporaire, monkeypatch):
    """Sans fiche pertinente : mémoire vide, jamais un échec ni un faux « aucune chose »."""
    _espace_et_discussion(base_temporaire)
    monkeypatch.setattr(
        "backend.services.memoire.search",
        lambda db, workspace_id, query, **kw: {"ok": True, "results": []},
    )

    resultat = _appeler({"queries": ["protocole sauvegarde"]}, "conv_memoire_e2e")
    charge = json.loads(resultat.content)

    assert resultat.ok and charge["ok"] is True
    (entree,) = charge["by_query"]
    assert entree["memoire"] == []
    assert "Mémoire" not in resultat.display_summary


def test_search_info_memoire_en_panne_ne_casse_pas_la_recherche(
    base_temporaire, monkeypatch
):
    """Une panne du registre mémoire n'empêche pas la recherche de fichiers."""

    def _explose(db, workspace_id, query, **kw):
        raise RuntimeError("registre mémoire indisponible")

    monkeypatch.setattr("backend.services.memoire.search", _explose)
    resultat = _appeler({"queries": ["protocole sauvegarde"]}, "conv_memoire_e2e")
    charge = json.loads(resultat.content)

    assert resultat.ok and charge["ok"] is True
    assert charge["by_query"] == []
