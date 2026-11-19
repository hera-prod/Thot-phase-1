"""Un échantillon binaire qui regarde assez loin, et un vide qui n'est pas une panne.

Deux défauts verrouillés ici, même famille : une non-distinction qui se
lisait comme l'autre.

  1. L'ÉCHANTILLON DE 12 OCTETS. `sniff_mime` juge « texte ou binaire » sur
     ce que l'appelant lui passe — et les appelants de
     `backend/ipc/attachments.py` coupaient `data[:12]` : un `.txt`/.csv
     commençant par quelques octets textuels avant son binaire passait la
     porte de la seule en-tête, et son `kind` annonçait `text` en silence.
     La fenêtre de jugement est désormais TAILLE_ECHANTILLON, l'échantillon
     partagé de `backend/utils/binary_guard` — la signature prime toujours :
     elle est au tête du fichier, c'est elle qui juge avant le ratio ;
  2. LE VIDE LU, PRISE POUR UNE PANNE. Un lecteur de tableur qui a ABOUTI
     sans rien rendre (« le fichier est lisible mais vide », retour rendu
     par `représenter_feuillet`) était recueilli au panier des erreurs de
     lecteur et l'ingestion écrivait `ETAT_ECHEC` — accusant une panne qui
     n'a pas lieu. texte.md §2.3 : « Un fichier vide peut être une
     ressource valide sans passage indexable ; ce n'est pas nécessairement
     une panne. » Un fichier lisible et vide se dit désormais
     `FeuilletVide` → `ETAT_VIDE`, et les vraies défaites de lecteur
     restent des échecs.

Montage : base neuve et jetable, `SessionLocal` redirigée, racines de
données jetables, `_ingérer_un` piloté à la main — même outillage que
`test_depot_texte.py`. Aucun réseau, aucun modèle ; `openpyxl` est requis
pour fabriquer un classeur à en-têtes seuls.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import Artifact, Base, Observation
from backend.ingestion import attachment_ingest, etat
from backend.ipc import attachments as A
from backend.ipc import paths_bridge as paths_bridge_module
from backend.utils import files as F

# Un en-tête textuel ASSEZ LONG pour dépasser douze octets (l'ancienne
# fenêtre), suivi d'un binaire manifeste : sous l'ancien plan, la portion
# textuelle rentrait seule dans la fenêtre de jugement. Octets ASCII en
# tête (l'axe du test EST la fenêtre, pas l'encodage) et binaires
# contrôlés derrière, dits RLÈGLES de contrôle du guard.
OCTETS_PIEGE = b"Salut visite!\n" + b"\x00\x01\x02" * 40

ENTETE_CSV = b"Date;Client;Montant\n"


@contextmanager
def _session(sessions):
    db = sessions()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    """Base neuve, index FTS et fragments, racines de données jetables."""
    moteur = create_engine(f"sqlite:///{tmp_path / 'echantillon-vide.db'}")
    Base.metadata.create_all(moteur)
    from backend.db.database import _ensure_fts
    from backend.search.chunks import ensure_chunk_tables

    _ensure_fts(moteur)
    sessions = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "app"
    (racine / "blobs").mkdir(parents=True)
    (racine / "temp").mkdir(parents=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(
        paths_bridge_module, "blobs_root", lambda: racine / "blobs", raising=False
    )
    monkeypatch.setattr(
        paths_bridge_module, "temp_root", lambda: racine / "temp", raising=False
    )
    monkeypatch.setattr(
        paths_bridge_module,
        "data_dirs",
        lambda: (racine, racine / "temp"),
        raising=False,
    )
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None, raising=False)
    with _session(sessions) as db:
        ensure_chunk_tables(db)
        db.commit()
    return sessions


def _deposer(sessions, octets: bytes, nom: str) -> str:
    """Dépose par le VRAI chemin de dépôt. Renvoie l'identifiant d'artefact."""
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with _session(sessions) as db:
        db.execute(
            sql_text(
                "INSERT INTO conversations (id,title,mode,created_at,updated_at)"
                " VALUES (:c,'echantillon-vide','chat',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            {"c": conversation},
        )
        db.commit()
        item = A.stage_picker_bytes(
            db, {"requestId": "echantillon-vide", "filename": nom, "bytes": octets}
        )["items"][0]
        (artefact,) = A.attachments_prepare(
            db,
            {
                "conversationId": conversation,
                "stageItemIds": [item["stage_item_id"]],
                "names": [nom],
            },
        )["artifact_ids"]
    return artefact


def _etat_de(sessions, artefact: str) -> etat.Etat:
    with _session(sessions) as db:
        return etat.lire(db.query(Artifact).filter(Artifact.id == artefact).one())


def _observations(sessions, artefact: str) -> list[Observation]:
    with _session(sessions) as db:
        return (
            db.query(Observation)
            .filter(
                Observation.artifact_id == artefact,
                Observation.tool_name == "attachment_ingest",
            )
            .all()
        )


# ══════════════════════════════════════════════════════════════════════════════
# 1. L'ÉCHANTILLON : la fenêtre de jugement est celle du guard binaire
# ══════════════════════════════════════════════════════════════════════════════


def test_l_echantillon_est_celui_du_guard_binaire():
    """La fenêtre de jugement COMMANDE par la borne partagée de binary_guard.

    Le littéral 12 était une seconde vérité sur « où commence le binaire » :
    la signature vit au début du fichier (elle n'a pas besoin de plus), mais
    qui JUGE du ratio regarde la même fenêtre que le guard — sinon deux
    bornes pouvaient diverger indépendamment. La signature prime toujours.
    """
    from backend.utils.binary_guard import TAILLE_ECHANTILLON

    assert (
        A._octets_de_jugement(b"abc") == b"abc"
    )  # un fichier court est tout son jugement
    assert (
        len(A._octets_de_jugement(bytes(TAILLE_ECHANTILLON + 7))) == TAILLE_ECHANTILLON
    )
    assert A.TAILLE_ECHANTILLON == TAILLE_ECHANTILLON


def test_un_txt_petits_octets_textuels_puis_binaires_est_refuse_par_le_chemin_reel(
    monde,
):
    """Un `.txt` textuel sur ses douze premiers octets, binaire ensuite : refus.

    Sous l'ancien plan, `sniff_mime` recevait l'en-tête seul : le ratio sur
    douze octets textuels restait nul, `kind="text"` partait en silence, et
    le fichier aurait été ingéré comme du texte qu'il n'est pas. Au VRAI
    chemin de dépôt, le binaire est NOMMÉ — refuse, pas un kind muet.
    """
    with _session(monde) as db:
        with pytest.raises(ValueError) as attrape:
            A.stage_picker_bytes(
                db,
                {
                    "requestId": "echantillon",
                    "filename": "notes.txt",
                    "bytes": OCTETS_PIEGE,
                },
            )
    message = str(attrape.value)
    assert "binaire" in message, message
    assert ".txt" in message, message  # le refus NOMME le type fautif


def test_un_txt_textuel_intact_traverse_toujours_l_echantillon_elargi():
    """Symétrique : la fenêtre élargie ne refuse pas le vrai texte.

    La garde juge un RATIO ; un texte légitime — accents compris, les
    octets de contrôle exclus du décompte — n'a rien à craindre de elle.
    """
    texte = (
        "COMPTE RENDU - Le rayon pythagorique du depot de Beziers est complet : "
        "palettes comptees, casse inferieure a un pour cent, rotation assuree.\n"
    )
    mime, kind = A.sniff_mime(
        A._octets_de_jugement(texte.encode("utf-8") * 300), "notes.txt"
    )
    assert (mime, kind) == ("text/plain", "text")


def test_l_ecriture_legacy_juge_elle_aussi_sur_la_fenetre_partagee(
    tmp_path, monkeypatch
):
    """La voie legacy `files.save_upload` juge le MÊME échantillon que son appelant.

    `attachments.save_upload` appelle `files.save_upload` (importée en
    `_legacy_save`) qui, APRÈS le jugement élargi du chemin réel, re-validait
    le type à coup de `data[:12]` local : un littéral recalé dès qu'il existe
    — deux vérités sur « où commence le binaire » dans un même flux. La garde
    legacy regarde désormais TAILLE_ECHANTILLON : le .txt piégé est refusé
    nommément ici aussi, jamais un silence.
    """
    racine = tmp_path / "temp"
    monkeypatch.setattr(F, "_get_tmp_root", lambda: racine)
    with pytest.raises(ValueError) as attrape:
        F.save_upload("request-123", "notes.txt", OCTETS_PIEGE)
    message = str(attrape.value)
    assert "binaire" in message, message
    assert ".txt" in message, message  # le refus NOMME le type fautif
    F.cleanup_request("request-123")


def test_un_csv_reduit_a_son_en_tete_est_vide_pas_en_echec(monde):
    """CSV à en-têtes seuls : `vide`, la lecture a ABOUTI (texte.md §2.3).

    Avant ce verrou : `représenter_feuillet` rendait "" (en-tête sans ligne
    de données), ce retour était recueilli au panier des erreurs de lecteur
    (« le fichier est lisible mais vide »), et l'ingestion écrivait
    `ETAT_ECHEC` — un fichier valide accusé d'une panne qu'il n'a pas.
    """
    artefact = _deposer(monde, ENTETE_CSV, "ventes.csv")
    attachment_ingest._ingérer_un(artefact)

    etat_lu = _etat_de(monde, artefact)
    assert etat_lu.etat == etat.ETAT_VIDE, (
        f"état {etat_lu.etat!r} : un fichier lisible mais vide est un état du "
        "fichier, pas une panne de lecteur"
    )
    assert "lisible mais vide" in etat_lu.raison, etat_lu.raison
    # L'honnêteté du message : le lecteur qui a réussi est cité nommément.
    assert "lire_csv_feuillet" in etat_lu.raison, etat_lu.raison
    lignes = _observations(monde, artefact)
    assert len(lignes) == 1 and lignes[0].status != "completed", (
        "une lecture sans texte ne s'inscrit pas comme extraction réussie"
    )
    assert "lisible mais vide" in str(lignes[0].payload_ref or ""), lignes[
        0
    ].payload_ref


def test_un_classeur_sans_ligne_de_donnees_est_vide_pas_en_echec(monde):
    """Classeur (.xlsx) réduit à son en-tête : `vide`, même distinction.

    Le lecteur openpyxl A ABOUTI (double passage, formules comprises) —
    l'absence de ligne de données est l'état du fichier. Le lecteur CSV, en
    face, se heurte à la signature ZIP : une panne d'AUTRE voie, jamais une
    panne de LECTURE. L'ingestion tranche `vide` sur la lecture réussie.
    """
    openpyxl = pytest.importorskip("openpyxl")
    classeur = openpyxl.Workbook()
    classeur.active.append(["Date", "Client", "Montant"])
    tampon = io.BytesIO()
    classeur.save(tampon)
    artefact = _deposer(monde, tampon.getvalue(), "factures.xlsx")
    attachment_ingest._ingérer_un(artefact)

    etat_lu = _etat_de(monde, artefact)
    assert etat_lu.etat == etat.ETAT_VIDE, etat_lu.raison
    assert "lisible mais vide" in etat_lu.raison, etat_lu.raison


def test_un_vrai_echec_reste_un_echec(monde):
    """Symétrique : une vraie panne de lecteur reste `echec`.

    Un conteneur ZIP ni classeur lisible ni CSV texte — les deux lecteurs
    DÉFAILLENT : aucun n'a LU, il n'y a donc pas d'état « vide » à invoquer.
    Le nom `echec` reste réservé aux lectures qui n'ont pas eu lieu.
    """
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        zf.writestr("content.xml", "<office:document-content/>")
    artefact = _deposer(monde, tampon.getvalue(), "faux.xlsx")
    attachment_ingest._ingérer_un(artefact)

    etat_lu = _etat_de(monde, artefact)
    assert etat_lu.etat == etat.ETAT_ECHEC, etat_lu.raison
    assert "aucun lecteur de tableur n'a produit de contenu" in etat_lu.raison
    assert "lire_classeur_feuillet" in etat_lu.raison
    assert "lire_csv_feuillet" in etat_lu.raison
