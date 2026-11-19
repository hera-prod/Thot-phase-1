"""Le filet de résolution de référence de `find_files` nomme les pannes (lot 4, phase 3).

La contrevérification APPROVE (ref:ses_eb14dd99affeOfRiOSzRVAhIF5) a relevé l'écart :
l'ancien `except Exception` de `_resoudre_reference` renommait TOUTE panne interne —
`KeyError`, erreur DB, `AttributeError` — en `[BAD_ARGS] « reformule »`. Un motif FAUX :
la boucle y lit un problème de FORMULATION, relance l'appel, et le même crash se rejoue.

Le filet corrigé ne garde `[BAD_ARGS]` que pour les échecs de RÉSOLUTION de la
référence (`ValueError`, `TypeError`) ; toute autre exception atteint le filet honnête
de `BaseTool.call`, qui écrit `[INTERNAL_ERROR] {type}` — la même mécanique que les
précédents `ask_files.py` et `search_info.py` (commits `b813004`, `7d31509`).

Ces tests injectent la panne au unitur de la résolution (`_origines`,
`_pieces_de_la_discussion`) : la nature du filet se vérifie avec la panne la plus
petite possible, pas avec un scénario d'ingestion complet.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Base, Conversation
from backend.tools import find_files as FF
from backend.tools.base import ToolContext
from backend.tools.find_files import FindFilesTool

LA_REFERENCE = {"query": "la dernière image que je t'ai envoyée"}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Base neuve et jetable, `SessionLocal` du projet redirigée (montage minimal)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'filets.db'}")
    Base.metadata.create_all(bind=engine)
    fabrique = sessionmaker(bind=engine, autoflush=False,
                            autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", fabrique)
    s = fabrique()
    try:
        s.add(Conversation(id="conv_1", title="Filets", mode="chat"))
        s.commit()
    finally:
        s.close()
    return fabrique


def _appeler(db, arguments: dict):
    """Un appel AUTHENTIQUE de l'outil, en mode référence (Bible §2.5 l. 153)."""
    ctx = ToolContext(run_id="r1", request_id="conv_1", conv_id="conv_1")
    return asyncio.run(FindFilesTool().call(dict(arguments), ctx))


def _code(resultat) -> str:
    """Le `motif` premier mot : `INTERNAL_ERROR`, `BAD_ARGS`, `TIMEOUT`…"""
    charge = json.loads(resultat.content or "{}")
    return (charge.get("error") or resultat.error or "")[:20]


def test_une_panne_interne_atteint_le_filet_honnete(db, monkeypatch):
    """Une `KeyError` dans la résolution est une panne : `[INTERNAL_ERROR]`.

    Avant le correctif, le même crash se lisait « référence illisible » — un
    motif FAUX que la boucle traitait en reformulation, rétorsion à chaque tour.
    """
    def _désamorcer(db, conv_id, sent_by_user):
        raise KeyError("artifact_id inexplicable")

    monkeypatch.setattr(FF, "_pieces_de_la_discussion", _désamorcer)
    resultat = _appeler(db, LA_REFERENCE)
    assert resultat.ok is False
    assert "INTERNAL_ERROR" in (resultat.error or ""), \
        f"la panne interne se nomme : {resultat.error!r}"
    assert "BAD_ARGS" not in (resultat.error or ""), \
        "une panne interne n'est pas une formulation maladroite"
    assert "référence illisible" not in (resultat.display_summary or "")


def test_une_valueerror_de_resolution_reste_un_bad_args(db, monkeypatch):
    """`ValueError` = la référence elle-même est mal formée : motif utilisateur.

    Le filet garde `[BAD_ARGS]` pour ce qu'il a toujours voulu dire — une
    formulation à reformuler — même quand l'échec survient en plein calcul.
    """
    def _désamorcer(db, conv_id, pieces_ids):
        raise ValueError("ordinal sans registre")

    monkeypatch.setattr(FF, "_origines", _désamorcer)
    resultat = _appeler(db, LA_REFERENCE)
    assert resultat.ok is False
    assert "BAD_ARGS" in (resultat.error or ""), \
        f"un échec de référence se lit comme tel : {resultat.error!r}"
    assert "INTERNAL_ERROR" not in (resultat.error or "")


def test_le_desamorçage_ne_recouvre_pas_la_session(db, monkeypatch):
    """Après une panne interne, la session est bien fermée au passage.

    Le `finally` de `_resoudre_reference` dispose la session MÊME quand
    l'exception remonte — c'est ce qui rend le filet de `BaseTool.call` sûr à
    son tour, et ce n'est pas négociable.
    """
    fermées = []

    def _désamorcer(db_local, conv_id, sent_by_user):
        raise RuntimeError("panne interne gênée")

    monkeypatch.setattr(FF, "_pieces_de_la_discussion", _désamorcer)
    original_session = database_module.SessionLocal

    def _tracée():
        session = original_session()
        _originale_close = session.close

        def close_observé():
            fermées.append(True)
            _originale_close()

        session.close = close_observé
        return session

    monkeypatch.setattr(database_module, "SessionLocal", _tracée)
    from backend.inference.media_policy import reference_fichier

    with pytest.raises(RuntimeError):
        # La panne remonte : `call` l'attrapera ; au niveau du module de test le
        # filet interne de `BaseTool.call` n'entre pas — on trace la session avant.
        asyncio.run(FF._resoudre_reference(
            reference_fichier("la dernière image que je t'ai envoyée"),
            FF.FindFilesArgs(query=LA_REFERENCE["query"]),
            "conv_1", 0))
    assert fermées, "la session se ferme malgré la panne interne"
