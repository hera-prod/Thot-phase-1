"""Le dépôt ne doit pas avaler ses anomalies : il les NOMME.

Deux mensonges cohabitaient dans `services/espaces.py:depot()`. Le premier : un
artefact supprimé, ou privé de nom affichable, sortait de `files` sans log, sans
compteur et sans un mot — l'utilisateur lisait un dépôt plus court, sans savoir
qu'il l'était. Le second : une ligne `blobs` introuvable rendait « 0 o », donc un
fichier vide qui ne l'est pas.

Le dépôt est un INVENTAIRE, pas une vitrine : ce qui n'y figure pas doit se lire.
D'où les deux clés qui manquaient — `hidden` pour l'écart, `size_known` pour la
taille — et l'onglet Dépôt qui les consomme
(`frontend/src/pages/espaces/EspacePage.tsx`).

Aucune de ces anomalies n'est atteignable par un versement CORRECT : les
fabricer par le staging ne donnerait qu'un dépôt sain. Elles sont donc posées en
base, seule façon de les voir. Le blob manquant, lui, n'est pas une fiction : une
connexion tierce n'est pas garantie `PRAGMA foreign_keys = ON` — `purge_workspace`
le dit déjà — et c'est là qu'un artefact peut référencer un blob absent.

Base temporaire, racines jetables : aucune ligne du projet n'est touchée.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Artifact, ArtifactLink, Base, Blob
from backend.services import espaces as service

# Une date posée à la main : `created_at` par défaut vient de `func.now()`, donc
# à la seconde, et trois insertions d'une même seconde ne seraient pas ordonnables.
VERSE_LE = datetime(2026, 5, 20, 9, 0, 0)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Base temporaire et racines jetables, comme `test_depot_espace.py`."""
    engine = create_engine(f"sqlite:///{tmp_path / 'depot-cache.db'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    session = sessions()
    try:
        yield session
    finally:
        session.close()


def _espace(db, nom: str = "Anomalies") -> str:
    return str(service.create_workspace(db, nom).id)


def _verser(db, workspace_id: str, artifact_id: str, *, nom: str = "note.txt",
            octets: int = 120, vers_le: datetime = VERSE_LE,
            supprime: bool = False, sans_blob: bool = False) -> None:
    """Un artefact VERSÉ dans l'espace, avec son blob — sauf si on le veut absent."""
    blob_id = f"blob_{artifact_id}"
    if not sans_blob:
        db.add(Blob(id=blob_id, sha256=blob_id, relative_path=f"blobs/{blob_id}.bin",
                    size_bytes=octets, mime_detected="text/plain"))
    db.add(Artifact(id=artifact_id, blob_id=blob_id, kind="text",
                    original_filename=nom, display_name=nom, created_at=vers_le,
                    deleted_at=VERSE_LE if supprime else None))
    db.add(ArtifactLink(id=f"lnk_{artifact_id}", artifact_id=artifact_id,
                        scope_kind="workspace", scope_id=workspace_id, role="shared"))
    db.commit()


def _masques(payload: dict) -> dict[str, str]:
    """`hidden` réduit au couple qui compte : quel artefact, pour quel motif."""
    return {item["artifact_id"]: item["reason"] for item in payload["hidden"]}


# --- Un dépôt sain ne parle de rien -----------------------------------------

def test_un_depot_sain_n_eccarte_personne(db):
    """Trois fichiers versés, aucun écart : `hidden` est vide, chaque taille est connue.

    C'est le contrepoint de tous les autres tests : une clé `hidden` qui se remplit
    pour rien ferait crier l'écran sur un dépôt intact.
    """
    ws = _espace(db, "Sain")
    _verser(db, ws, "art_a", nom="un.pdf", octets=10, vers_le=datetime(2026, 5, 18, 9, 0))
    _verser(db, ws, "art_b", nom="deux.pdf", octets=0, vers_le=datetime(2026, 5, 19, 9, 0))
    _verser(db, ws, "art_c", nom="trois.pdf", octets=2048, vers_le=VERSE_LE)

    payload = service.depot(db, ws)

    assert [f["artifact_id"] for f in payload["files"]] == ["art_c", "art_b", "art_a"]
    assert payload["hidden"] == []
    assert all(f["size_known"] for f in payload["files"])
    assert [f["size_bytes"] for f in payload["files"]] == [2048, 0, 10]
    assert payload["conversation_count"] == 0
    assert payload["is_physical_folder"] is False


# --- Ce qui n'est pas affiché se signale -------------------------------------

def test_un_artefact_sans_nom_est_signale_et_non_present(db):
    """L'écart se voit : il est dans `hidden`, avec son motif, et PAS dans `files`.

    Le dépôt affichait un fichier pour deux liens versés. L'utilisateur ne pouvait
    ni voir le second, ni savoir qu'il existait.
    """
    ws = _espace(db, "Sans nom")
    _verser(db, ws, "art_nomme", nom="lisible.txt", octets=120)
    _verser(db, ws, "art_sans_nom", nom="", octets=120)

    payload = service.depot(db, ws)

    assert [f["artifact_id"] for f in payload["files"]] == ["art_nomme"]
    assert _masques(payload) == {"art_sans_nom": "sans_nom"}
    entree = payload["hidden"][0]
    # Le motif est une VALEUR stable, relisible sans le service : c'est lui que
    # l'écran compte. Aucun identifiant technique ne doit sortir de là.
    assert set(entree) == {"artifact_id", "display_name", "reason"}
    assert entree["reason"] == "sans_nom"


def test_un_artefact_supprime_est_signale_et_non_present(db):
    """Un lien résiduel vers un artefact supprimé ne fait plus partie DU SILENCE.

    Le dépôt ne présente pas un document en corbeille — c'est la règle. Ce qu'il ne
    peut plus faire, c'est le retirer sans rien dire.
    """
    ws = _espace(db, "Corbeille")
    _verser(db, ws, "art_vivant", nom="vivant.txt", octets=120)
    _verser(db, ws, "art_mort", nom="mort.txt", octets=120, supprime=True)

    payload = service.depot(db, ws)

    assert [f["artifact_id"] for f in payload["files"]] == ["art_vivant"]
    assert _masques(payload) == {"art_mort": "supprime"}


def test_un_lien_sans_artefact_a_son_propre_motif(db):
    """Le confondre avec « sans nom » dirait qu'il manque une étiquette à un
    document qui n'a plus de ligne : deux anomalies, deux causes, deux motifs.
    """
    ws = _espace(db, "Orphelin")
    _verser(db, ws, "art_ok", nom="present.txt", octets=120)
    db.add(ArtifactLink(id="lnk_orphelin", artifact_id="art_absent",
                        scope_kind="workspace", scope_id=ws, role="shared"))
    db.commit()

    payload = service.depot(db, ws)

    assert [f["artifact_id"] for f in payload["files"]] == ["art_ok"]
    assert _masques(payload) == {"art_absent": "lien_orphelin"}


# --- Une taille inconnue n'est pas une taille nulle -------------------------

def test_un_blob_absent_donne_une_taille_inconnue_pas_zero(db):
    """Le poids affiché doit pouvoir dire « inconnu », et non « 0 o ».

    Le couple (`size_bytes`, `size_known`) est ce qui rend la phrase possible :
    `size_bytes` reste 0 pour ne rien casser, et l'indicateur empêche l'écran de
    présenter ce 0 comme une mesure.
    """
    ws = _espace(db, "Poids")
    _verser(db, ws, "art_sans_blob", nom="sans-poids.txt", sans_blob=True)

    ligne = service.depot(db, ws)["files"][0]

    assert ligne["artifact_id"] == "art_sans_blob"
    assert ligne["size_known"] is False
    assert ligne["size_bytes"] == 0


def test_un_blob_de_taille_nulle_reste_une_taille_connue(db):
    """Le faux positif de l'indicateur : un fichier vraiment vide.

    Écrire « taille inconnue » pour un fichier de 0 o serait le même mensonge,
    inversé : l'écran dirait ignorer ce qu'il sait.
    """
    ws = _espace(db, "Vide")
    _verser(db, ws, "art_vide", nom="vide.txt", octets=0)

    ligne = service.depot(db, ws)["files"][0]

    assert (ligne["size_known"], ligne["size_bytes"]) == (True, 0)


def test_un_blob_manquant_ne_contamine_pas_la_taille_des_autres(db):
    """Un blob manquant ne contamine pas la taille de ses voisins.

    Le repli `tailles.get(..., 0)` s'appliquait par artefact : il fallait que
    l'indicateur reste attaché à SON fichier, sinon « taille inconnue » se
    propagerait à tout le dépôt.
    """
    ws = _espace(db, "Mesure")
    _verser(db, ws, "art_sans_blob", nom="sans-poids.txt", sans_blob=True)
    _verser(db, ws, "art_avec_poids", nom="avec-poids.txt", octets=2048)

    lignes = {f["artifact_id"]: f for f in service.depot(db, ws)["files"]}

    assert lignes["art_avec_poids"]["size_known"] is True
    assert lignes["art_avec_poids"]["size_bytes"] == 2048
    assert lignes["art_sans_blob"]["size_known"] is False


# --- Le compteur : ce qui est rattaché, ou ce qui est présentable ? ----------

def test_stats_compte_les_fichiers_presentables_et_non_les_liens(db):
    """Trois liens versés, un seul fichier affichable : `fichiers` vaut 1.

    Compter les liens donnerait « 3 fichiers » là où l'écran en montre un, et le
    même dépôt aurait deux chiffres selon l'endroit où l'on regarde. La réserve de
    `depot()` tranche : le dépôt présente des documents vivants et nommés, donc
    les compteurs comptent ce que le dépôt présente. Ce qui n'est pas présenté n'est
    pas perdu pour autant — `hidden` le nomme, l'écran le dit.
    """
    ws = _espace(db, "Compteurs")
    _verser(db, ws, "art_present", nom="present.txt", octets=120)
    _verser(db, ws, "art_sans_nom", nom="", octets=999)
    _verser(db, ws, "art_mort", nom="mort.txt", octets=999, supprime=True)

    liens = db.query(ArtifactLink).filter(
        ArtifactLink.scope_kind == "workspace",
        ArtifactLink.scope_id == ws,
        ArtifactLink.removed_at.is_(None),
    ).count()
    payload = service.depot(db, ws)
    compteurs = service.stats(db, ws)

    assert liens == 3
    assert len(payload["files"]) == 1
    assert len(payload["hidden"]) == 2
    # La décision, verrouillée : les compteurs suivent `files`, pas les liens.
    assert compteurs["fichiers"] == len(payload["files"]) == 1
    assert compteurs["fichiers"] != liens
    # `octets` suit la même règle : on ne pèse pas des fichiers qu'on n'affiche pas.
    assert compteurs["octets"] == 120