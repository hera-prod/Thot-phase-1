"""Le dépôt d'un espace : ce que l'UTILISATEUR y verse, et rien d'autre.

Un onglet « Dépôt » qui afficherait « les fichiers de cet espace » confondrait deux
choses : un fichier versé dans l'espace, et un fichier resté dans une discussion de
cet espace. Le second n'appartient pas au dépôt — il y reste, et la discussion est
le seul endroit où il se trouve. Cette frontière est une décision de produit, donc
elle se verrouille par les deux bouts : ce qui ENTRE (`attachments.prepare` avec
`workspaceId`, et ses refus avant toute écriture) et ce qui SORT (`depot()`).

Base temporaire, racines de données jetables, ingestion neutralisée : aucun octet
ni aucune ligne du projet n'est touché. Le passage par le staging est obligatoire —
un jeton fabriqué à la main ne prouverait rien, puisque `attachments_prepare`
refuserait de promotionner un fichier qu'il n'a pas vu passer.
"""

from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main as backend_main
from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    DocRef,
)
from backend.ingestion import attachment_ingest
from backend.ipc import attachments as A
from backend.ipc import paths_bridge
from backend.search import scope as S
from backend.services import espaces as service


def pdf_bytes(marqueur: str = "rapport") -> bytes:
    """PDF minimal mais structurallement valide (en-tête + catalogue + EOF)."""
    return (
        b"%PDF-1.7\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF " + marqueur.encode()
    )


@pytest.fixture()
def depot_espace(tmp_path, monkeypatch):
    """Base temporaire, `SessionLocal` du projet, racines jetables, émissions capturées."""
    engine = create_engine(f"sqlite:///{tmp_path / 'depot-espace.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", session_factory)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(
        attachment_ingest, "lancer_ingestion", lambda artifact_ids: None
    )
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        backend_main,
        "emit",
        lambda event, payload, **kwargs: emitted.append((event, payload)),
    )
    return session_factory, emitted


def _prepare(request_id: str, params: dict) -> dict:
    return {
        "request_id": request_id,
        "method": "attachments.prepare",
        "params": params,
        "protocol_version": 1,
    }


def _evenements(emitted: list[tuple[str, dict]], nom: str) -> list[dict]:
    return [payload for name, payload in emitted if name == nom]


def _espace(db, nom: str) -> str:
    return str(service.create_workspace(db, nom).id)


def _conversation(db, conv_id: str, workspace_id: str) -> str:
    db.add(
        Conversation(
            id=conv_id, title="Discussion", mode="chat", workspace_id=workspace_id
        )
    )
    db.commit()
    return conv_id


def _stager(db, request_id: str, data: bytes, nom: str) -> dict:
    return A.stage_picker_bytes(
        db, {"requestId": request_id, "filename": nom, "bytes": data}
    )["items"][0]


def _deposer_dans_espace(
    db, workspace_id: str, jetons: list[str], noms: list[str] | None = None
) -> dict:
    return A.attachments_prepare(
        db,
        {
            "workspaceId": workspace_id,
            "stageItemIds": jetons,
            "names": noms or [],
        },
    )


def _deposer_en_discussion(
    db, conversation_id: str, jetons: list[str], noms: list[str] | None = None
) -> dict:
    return A.attachments_prepare(
        db,
        {
            "conversationId": conversation_id,
            "stageItemIds": jetons,
            "names": noms or [],
        },
    )


def _blobs_sur_disque() -> list[str]:
    return sorted(p.name for p in paths_bridge.blobs_root().rglob("*.bin"))


def _aucune_ecriture(db) -> None:
    assert db.query(Blob).count() == 0
    assert db.query(Artifact).count() == 0
    assert db.query(ArtifactLink).count() == 0
    assert _blobs_sur_disque() == []


def test_un_pdf_verse_dans_un_espace_arrive_dans_son_depot(depot_espace):
    """Le versement direct crée un artefact, un lien d'espace, et l'inventaire le rend."""
    sessions, emitted = depot_espace
    donnees = pdf_bytes("versé")
    db = sessions()
    try:
        workspace_id = _espace(db, "Recherche")
        item = _stager(db, "depot-x1", donnees, "Notes de reunion.pdf")
    finally:
        db.close()

    backend_main.handle(
        _prepare(
            "versement-espace",
            {
                "workspaceId": workspace_id,
                "stageItemIds": [item["stage_item_id"]],
                "names": ["Notes de réunion.pdf"],
            },
        )
    )

    assert not _evenements(emitted, "error")
    reponses = _evenements(emitted, "attachments_prepare")
    assert reponses[0]["scope_kind"] == "workspace"
    assert reponses[0]["count"] == 1

    db = sessions()
    try:
        artefact_id = reponses[0]["artifact_ids"][0]
        assert db.query(Artifact).filter(Artifact.id == artefact_id).one().kind == "pdf"
        liens = (
            db.query(ArtifactLink).filter(ArtifactLink.artifact_id == artefact_id).all()
        )
        assert [(l.scope_kind, l.scope_id, l.role) for l in liens] == [
            ("workspace", workspace_id, "shared")
        ]
        contenu = service.depot(db, workspace_id)
        assert [f["artifact_id"] for f in contenu["files"]] == [artefact_id]
        assert contenu["files"][0]["kind"] == "pdf"
        assert contenu["files"][0]["size_bytes"] == len(donnees)
        assert contenu["is_physical_folder"] is False
    finally:
        db.close()


def test_le_nom_dorigine_survit_sans_toucher_l_adresse_sur_disque(depot_espace):
    """`names` est une ÉTIQUETTE : elle nomme l'artefact, jamais le blob.

    Le staging a perdu le nom de l'utilisateur (il n'a gardé que l'empreinte). Sans
    `names`, le dépôt afficherait un sha256 illisible. Mais l'étiquette ne doit pas
    non plus devenir une adresse : le blob reste sous son empreinte, sinon un nom
    arbitraire déciderait de l'emplacement des octets.
    """
    sessions, _ = depot_espace
    donnees = pdf_bytes("étiquette")
    db = sessions()
    try:
        workspace_id = _espace(db, "Documents")
        item = _stager(db, "depot-x2", donnees, "fichier.pdf")
        reponse = _deposer_dans_espace(
            db, workspace_id, [item["stage_item_id"]], ["Notes de réunion.pdf"]
        )
        artefact_id = reponse["artifact_ids"][0]

        artefact = db.query(Artifact).filter(Artifact.id == artefact_id).one()
        assert artefact.display_name == "Notes de réunion.pdf"
        assert artefact.display_name != item["sha256"]
        assert (
            service.depot(db, workspace_id)["files"][0]["display_name"]
            == "Notes de réunion.pdf"
        )

        blob = db.query(Blob).filter(Blob.id == artefact.blob_id).one()
        assert blob.relative_path == f"blobs/{item['sha256'][:2]}/{item['sha256']}.bin"
        sur_disque = (
            paths_bridge.blobs_root() / blob.relative_path.split("blobs/", 1)[1]
        )
        assert sur_disque.is_file() and sur_disque.read_bytes() == donnees
    finally:
        db.close()


def test_espace_inconnu_refuse_et_n_ecrit_rien(depot_espace):
    """Un `workspaceId` fantôme ne doit laisser ni ligne ni octet.

    Le contrôle précède la lecture du staging : un dépôt annoncé dans un espace qui
    n'existe pas laisserait un fichier que l'utilisateur ne saura jamais retrouver,
    et il croirait l'avoir versé dans cet espace.
    """
    sessions, _ = depot_espace
    donnees = pdf_bytes("fantome")
    db = sessions()
    try:
        item = _stager(db, "depot-x3", donnees, "orphelin.pdf")
        with pytest.raises(LookupError, match="WORKSPACE_NOT_FOUND"):
            _deposer_dans_espace(
                db, "ws_inexistant", [item["stage_item_id"]], ["orphelin.pdf"]
            )
        db.rollback()
        _aucune_ecriture(db)
    finally:
        db.close()


def test_espace_en_corbeille_refuse_et_n_ecrit_rien(depot_espace):
    """La corbeille est un état, pas une portée : un dépôt y est refusé aussi."""
    sessions, _ = depot_espace
    donnees = pdf_bytes("corbeille")
    db = sessions()
    try:
        workspace_id = _espace(db, "Vieux")
        service.trash_workspace(db, workspace_id)
        item = _stager(db, "depot-x4", donnees, "range.pdf")
        with pytest.raises(LookupError, match="WORKSPACE_NOT_FOUND"):
            _deposer_dans_espace(
                db, workspace_id, [item["stage_item_id"]], ["range.pdf"]
            )
        db.rollback()
        _aucune_ecriture(db)
    finally:
        db.close()


def test_une_conversation_ET_un_espace_ensemble_sont_refuses(depot_espace):
    """Deux destinations à la fois : le dépôt choisit, il ne devine pas.

    Un appel qui fournit les deux serait ambigu — et l'ambiguïté déciderait alors de
    l'endroit où l'utilisateur croit avoir versé son fichier.
    """
    sessions, _ = depot_espace
    donnees = pdf_bytes("ambigu")
    db = sessions()
    try:
        workspace_id = _espace(db, "Alpha")
        conversation_id = _conversation(db, "conv_ambigu", workspace_id)
        item = _stager(db, "depot-x5", donnees, "ambigu.pdf")
        with pytest.raises(ValueError, match="jamais aux deux"):
            A.attachments_prepare(
                db,
                {
                    "conversationId": conversation_id,
                    "workspaceId": workspace_id,
                    "stageItemIds": [item["stage_item_id"]],
                },
            )
        db.rollback()
        _aucune_ecriture(db)
    finally:
        db.close()


def test_sans_conversation_ni_espace_le_frontend_recoit_un_refus(depot_espace):
    """Aucune destination : refus lisible, pas une promotion orpheline.

    On passe par le handler, parce que ce que l'écran voit n'est pas l'exception
    Python mais l'événement `error`. Un refus sans code lisible se traduit à l'écran
    par un dépôt qui semble avoir réussi.
    """
    sessions, emitted = depot_espace
    db = sessions()
    try:
        item = _stager(db, "depot-x6", pdf_bytes("sans cible"), "orphelin.pdf")
    finally:
        db.close()

    backend_main.handle(
        _prepare(
            "sans-destination",
            {
                "stageItemIds": [item["stage_item_id"]],
            },
        )
    )

    erreurs = _evenements(emitted, "error")
    assert erreurs[0]["code"] == "bad_request"
    assert erreurs[0]["retryable"] is False
    assert not _evenements(emitted, "attachments_prepare")
    db = sessions()
    try:
        _aucune_ecriture(db)
    finally:
        db.close()


def test_depot_ignore_les_fichiers_restes_dans_une_discussion(depot_espace):
    """Un fichier versé dans une discussion n'entre PAS au dépôt de l'espace.

    Deux versements réels dans le même espace : l'un dans l'espace, l'autre dans une
    discussion de cet espace. Le second appartient à la discussion, et la discussion
    le voit toujours — il ne disparaît pas, il n'est juste pas dans le dépôt.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Alpha")
        conversation_id = _conversation(db, "conv_du_chat", workspace_id)

        de_discussion = _stager(
            db, "depot-x7a", pdf_bytes("discussion"), "dans-la-discussion.pdf"
        )
        de_espace = _stager(db, "depot-x7b", pdf_bytes("espace"), "dans-lespace.pdf")
        artefact_discussion = _deposer_en_discussion(
            db,
            conversation_id,
            [de_discussion["stage_item_id"]],
            ["dans-la-discussion.pdf"],
        )["artifact_ids"][0]
        artefact_espace = _deposer_dans_espace(
            db, workspace_id, [de_espace["stage_item_id"]], ["dans-lespace.pdf"]
        )["artifact_ids"][0]

        contenu = service.depot(db, workspace_id)
        assert [f["artifact_id"] for f in contenu["files"]] == [artefact_espace]
        assert {f["display_name"] for f in contenu["files"]} == {"dans-lespace.pdf"}
        assert artefact_discussion not in {f["artifact_id"] for f in contenu["files"]}

        lien_discussion = (
            db.query(ArtifactLink)
            .filter(ArtifactLink.artifact_id == artefact_discussion)
            .one()
        )
        assert (lien_discussion.scope_kind, lien_discussion.scope_id) == (
            "conversation",
            conversation_id,
        )
        assert lien_discussion.removed_at is None
        assert S.resolve_allowed_artifacts(conversation_id) == {
            artefact_discussion,
            artefact_espace,
        }
        assert contenu["conversation_count"] == 1
    finally:
        db.close()


def test_le_retrait_du_depot_conserve_le_blob(depot_espace):
    """Retirer un fichier du dépôt est un `removed_at` : le lien part, les octets restent.

    Le retrait est une décision d'interface, pas une purge : le blob peut encore être
    référencé ailleurs, et le supprimer laisserait une ligne orpheline.
    """
    sessions, _ = depot_espace
    donnees = pdf_bytes("retrait")
    db = sessions()
    try:
        workspace_id = _espace(db, "Documents")
        item = _stager(db, "depot-x8", donnees, "a-retirer.pdf")
        artefact_id = _deposer_dans_espace(
            db, workspace_id, [item["stage_item_id"]], ["a-retirer.pdf"]
        )["artifact_ids"][0]
        blob_id = db.query(Artifact).filter(Artifact.id == artefact_id).one().blob_id
        chemin = (
            paths_bridge.blobs_root() / item["sha256"][:2] / f"{item['sha256']}.bin"
        )

        avant = service.unshare_artifact(db, workspace_id, artefact_id)
        assert avant["already_removed"] is False

        lien = (
            db.query(ArtifactLink)
            .filter(
                ArtifactLink.artifact_id == artefact_id,
                ArtifactLink.scope_kind == "workspace",
            )
            .one()
        )
        assert lien.removed_at is not None
        assert service.depot(db, workspace_id)["files"] == []
        assert db.query(Blob).filter(Blob.id == blob_id).count() == 1
        assert chemin.is_file() and chemin.read_bytes() == donnees
    finally:
        db.close()


def test_le_versement_dans_un_espace_n_alloue_aucune_etiquette_doc_N(depot_espace):
    """Un `doc_N` désigne une pièce jointe du tour courant d'une discussion.

    Un fichier de dépôt n'a pas de tour : lui donner une étiquette le ferait passer
    pour ce qu'il n'est pas. La comparaison avec le même dépôt dans une discussion
    est ce qui rend cette assertion signifiante — sans elle, « aucun doc_refs »
    prouverait seulement que le test n'a rien préparé.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Alpha")
        conversation_id = _conversation(db, "conv_etiquette", workspace_id)

        en_discussion = _stager(db, "depot-x9a", pdf_bytes("chat"), "piece-du-chat.pdf")
        dans_espace = _stager(db, "depot-x9b", pdf_bytes("depot"), "piece-du-depot.pdf")
        artefact_chat = _deposer_en_discussion(
            db, conversation_id, [en_discussion["stage_item_id"]], ["piece-du-chat.pdf"]
        )["artifact_ids"][0]
        artefact_depot = _deposer_dans_espace(
            db, workspace_id, [dans_espace["stage_item_id"]], ["piece-du-depot.pdf"]
        )["artifact_ids"][0]

        etiquettes_chat = (
            db.query(DocRef).filter(DocRef.artifact_id == artefact_chat).all()
        )
        assert [(r.conv_id, r.doc_label) for r in etiquettes_chat] == [
            (conversation_id, "doc_1")
        ]
        assert (
            db.query(DocRef).filter(DocRef.artifact_id == artefact_depot).count() == 0
        )
    finally:
        db.close()


def test_la_reponse_ne_publie_que_des_cles_reellement_lues(depot_espace):
    """Pas de `items` dans la réponse, et c'est délibéré.

    Le chat fait `prep.items.map(i => i.artifactId)`. Un `items` aux clés
    snake_case y vaudrait `undefined` SANS lever : `.map` réussirait, sa longueur
    vaudrait 1, et le repli `image_paths` ne se déclencherait plus — le fichier
    joint disparaîtrait en silence. Le dépôt, lui, relit l'inventaire par
    `espace_depot`, qui rend le `display_name`. Aucune clé que personne ne lit.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Réponse")
        item = _stager(db, "depot-x12", pdf_bytes("cles"), "cles.pdf")
        reponse = _deposer_dans_espace(
            db, workspace_id, [item["stage_item_id"]], ["cles.pdf"]
        )
        assert set(reponse) == {"ok", "artifact_ids", "count", "scope_kind"}
    finally:
        db.close()


def test_names_desaligne_est_refuse_au_lieu_d_afficher_l_empreinte(depot_espace):
    """`names` plus court que les jetons : refus, pas une empreinte à l'écran.

    L'entrée manquante retomberait sur le nom dérivé du fingerprint, c'est-à-dire
    le sha256 complet dans l'inventaire — exactement ce que `names` existe pour
    empêcher. Aligner deux tableaux par position ne se devine pas : mieux vaut un
    refus qu'un dépôt dont un fichier est devenu illisible.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Étiquettes")
        premier = _stager(db, "depot-x13a", pdf_bytes("aligne-a"), "a.pdf")
        second = _stager(db, "depot-x13b", pdf_bytes("aligne-b"), "b.pdf")
        with pytest.raises(ValueError, match="un nom par pièce jointe"):
            _deposer_dans_espace(
                db,
                workspace_id,
                [premier["stage_item_id"], second["stage_item_id"]],
                ["a.pdf"],
            )
        db.rollback()
        _aucune_ecriture(db)
    finally:
        db.close()


def test_un_separateur_ne_tronque_pas_le_nom_affiche(depot_espace):
    """« notes du 12/03.txt » ne doit pas s'afficher « 03.txt ».

    Tronquer au dernier séparateur ne garderait que la date : deux fichiers datés
    deviennent la même ligne, et l'utilisateur ne sait plus lequel il ouvre. Le
    séparateur part, le nom reste.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Dates")
        item = _stager(db, "depot-x14", pdf_bytes("separateur"), "n'importe.pdf")
        artefact_id = _deposer_dans_espace(
            db, workspace_id, [item["stage_item_id"]], ["notes du 12/03.txt"]
        )["artifact_ids"][0]
        affiche = (
            db.query(Artifact).filter(Artifact.id == artefact_id).one().display_name
        )
        assert affiche == "notes du 1203.txt"
        assert service.depot(db, workspace_id)["files"][0]["display_name"] == affiche
    finally:
        db.close()


def test_un_nom_affiche_ne_ment_sur_sa_propre_forme(depot_espace):
    """Un saut de ligne déborde de la ligne ; U+202E affiche le nom à l'envers.

    `facture<U+202E>gpj.exe.pdf` se lit `facture.pdf` : l'extension montrée n'est
    pas celle du fichier, et rien ne signale la substitution. Le risque est
    l'AFFICHAGE — l'octet reste dans le blob, adressé par son empreinte, et ce nom
    ne touche jamais le disque.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Affichage")
        item = _stager(db, "depot-x15", pdf_bytes("controles"), "n'importe.pdf")
        artefact_id = _deposer_dans_espace(
            db, workspace_id, [item["stage_item_id"]], ["rap\nport‮gpj.exe.pdf"]
        )["artifact_ids"][0]
        affiche = (
            db.query(Artifact).filter(Artifact.id == artefact_id).one().display_name
        )
        assert affiche == "rapportgpj.exe.pdf"
        assert not set(affiche) & set("\n\r\0‮")
    finally:
        db.close()


def test_un_jeton_recu_est_purge_meme_si_l_appel_echoue(depot_espace):
    """Le staging est un tampon : un lot interrompu ne doit pas le laisser derrière lui.

    Le dépôt procède par lots. Un lot qui échoue arrête la promotion, et les
    fichiers des lots suivants restent en attente de promotion — donc personne ne
    les purgera jamais. C'est le `finally` de l'appel qui doit le faire, pour les
    jetons qu'il a reçus.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Lots")
        premier = _stager(db, "depot-x16a", pdf_bytes("lot-a"), "lot-a.pdf")
        with pytest.raises(ValueError, match="introuvable"):
            _deposer_dans_espace(
                db,
                workspace_id,
                [premier["stage_item_id"], "sti_deadbeefdeadbeef"],
                ["lot-a.pdf", "fantome.pdf"],
            )
        db.rollback()
        assert not list(A._staged_dir().glob(f"{premier['sha256'][:16]}*"))
        _aucune_ecriture(db)
    finally:
        db.close()


def test_un_jeton_recu_par_un_autre_appel_survit_a_un_echec(depot_espace):
    """Le nettoyage s'arrête aux jetons de l'appel qui échoue, et pas plus loin.

    Sans cette borne, purger un lot interrompu effacerait le staging du lot suivant
    — celui qui n'a pas encore eu l'occasion d'être promu. On verse donc l'autre
    fichier après l'échec, pour prouver qu'il était encore là.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Lots")
        casse = _stager(db, "depot-x17a", pdf_bytes("casse"), "casse.pdf")
        intact = _stager(db, "depot-x17b", pdf_bytes("intact"), "intact.pdf")

        with pytest.raises(ValueError, match="introuvable"):
            _deposer_dans_espace(
                db,
                workspace_id,
                [casse["stage_item_id"], "sti_deadbeefdeadbeef"],
                ["casse.pdf", "fantome.pdf"],
            )
        db.rollback()
        assert not list(A._staged_dir().glob(f"{casse['sha256'][:16]}*"))
        assert list(A._staged_dir().glob(f"{intact['sha256'][:16]}*"))

        artefact_id = _deposer_dans_espace(
            db, workspace_id, [intact["stage_item_id"]], ["intact.pdf"]
        )["artifact_ids"][0]
        assert service.depot(db, workspace_id)["files"][0]["artifact_id"] == artefact_id
    finally:
        db.close()


def test_un_fichier_partiel_existant_est_verifie_et_remplace(depot_espace):
    """Un fichier partiel laissé par un crash n'est adopté que si taille ET hash concordent.

    Le défaut d'origine était `if not cible.exists(): write_bytes` : un fichier
    tronqué au même chemin était réutilisé sans contrôle, et l'artefact pointait
    vers des octets incomplets. Ici, le fichier partiel est remplacé par une
    écriture atomique, et son contenu final est exactement celui versé.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Partiel")
        donnees = pdf_bytes("complet")
        item = _stager(db, "depot-partiel", donnees, "fichier.pdf")
        sha = item["sha256"]
        cible = paths_bridge.blobs_root() / sha[:2] / f"{sha}.bin"
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(b"partiel")  # taille et empreinte fausses

        _deposer_dans_espace(db, workspace_id, [item["stage_item_id"]], ["fichier.pdf"])

        assert cible.is_file() and cible.read_bytes() == donnees
        assert cible.stat().st_size == len(donnees)
        assert not list(cible.parent.glob("*.tmp"))
    finally:
        db.close()


def test_un_commit_echoue_ne_laisse_pas_de_blob_orphelin(depot_espace, monkeypatch):
    """Un blob écrit puis non commité est compensé, sans toucher un blob partagé.

    La compensation ne supprime un fichier que si aucune ligne `blobs` ne porte
    son empreinte APRÈS le rollback. Ici, la transaction n'a jamais commité :
    le blob temporaire final doit disparaître, et la base retrouver son vide.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Échec commit")
        donnees = pdf_bytes("echec-commit")
        item = _stager(db, "depot-echec", donnees, "echec.pdf")
        sha = item["sha256"]

        def _commit_refuse():
            raise RuntimeError("commit refusé")

        monkeypatch.setattr(db, "commit", _commit_refuse)
        with pytest.raises(RuntimeError, match="commit refusé"):
            _deposer_dans_espace(
                db, workspace_id, [item["stage_item_id"]], ["echec.pdf"]
            )

        assert db.query(Blob).count() == 0
        assert db.query(Artifact).count() == 0
        assert db.query(ArtifactLink).count() == 0
        cible = paths_bridge.blobs_root() / sha[:2] / f"{sha}.bin"
        assert not cible.exists()
        assert _blobs_sur_disque() == []
    finally:
        db.close()


def test_un_commit_echoue_ne_detruit_pas_un_blob_deja_partage(
    depot_espace, monkeypatch
):
    """Un blob déjà enregistré ne doit jamais être emporté par l'échec d'une promotion.

    On force la réécriture du fichier (il a été supprimé) pour que la
    compensation soit réellement exécutée : le blob est réécrit, la transaction
    échoue, mais la ligne `blobs` existante doit empêcher sa suppression.
    """
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Blob partagé")
        donnees = pdf_bytes("blob-partage")
        premier = _stager(db, "depot-partage-1", donnees, "premier.pdf")
        _deposer_dans_espace(
            db, workspace_id, [premier["stage_item_id"]], ["premier.pdf"]
        )
        sha = premier["sha256"]
        cible = paths_bridge.blobs_root() / sha[:2] / f"{sha}.bin"
        assert cible.is_file()
        assert db.query(Blob).count() == 1

        second = _stager(db, "depot-partage-2", donnees, "second.pdf")

        # Forcer `written=True` : la ligne `blobs` existe (premier dépôt), mais
        # le fichier a disparu (état incohérent). La seconde promotion le
        # réécrit, donc `nouveaux_blobs` le contient — et la compensation doit
        # le CONSERVER grâce à la relecture de la base après rollback.
        cible.unlink()
        appels: list[dict] = []
        compensation = A._compenser_blobs

        def _compense(nouveaux, session):
            appels.append(dict(nouveaux))
            return compensation(nouveaux, session)

        monkeypatch.setattr(A, "_compenser_blobs", _compense)

        def _commit_refuse():
            raise RuntimeError("commit refusé")

        monkeypatch.setattr(db, "commit", _commit_refuse)
        with pytest.raises(RuntimeError, match="commit refusé"):
            _deposer_dans_espace(
                db, workspace_id, [second["stage_item_id"]], ["second.pdf"]
            )

        assert appels and sha in appels[0], "la compensation doit être exercée"
        assert db.query(Blob).count() == 1
        assert db.query(Artifact).count() == 1
        assert cible.is_file()
        assert _blobs_sur_disque() == [cible.name]
    finally:
        db.close()


def test_compensation_efface_un_blob_non_reference(depot_espace, tmp_path):
    """Le pendant du test précédent : sans ligne `blobs`, la compensation supprime."""
    sessions, _ = depot_espace
    db = sessions()
    try:
        chemin = tmp_path / "blob-compense.bin"
        chemin.write_bytes(b"octets non references")
        erreurs = A._compenser_blobs({"sha-compense": chemin}, db)
        assert erreurs == []
        assert not chemin.exists()
    finally:
        db.close()


def test_un_fichier_stage_tronque_est_refuse(depot_espace):
    """Un staging tronqué ne doit jamais devenir un artefact accepté."""
    sessions, _ = depot_espace
    db = sessions()
    try:
        workspace_id = _espace(db, "Staging tronqué")
        donnees = pdf_bytes("tronque")
        item = _stager(db, "depot-tronque", donnees, "tronque.pdf")
        staged = next(A._staged_dir().glob(f"{item['sha256']}*"))
        staged.write_bytes(donnees[: len(donnees) // 2])

        with pytest.raises(ValueError, match="tronqué|altéré"):
            _deposer_dans_espace(
                db, workspace_id, [item["stage_item_id"]], ["tronque.pdf"]
            )

        assert db.query(Blob).count() == 0
        assert db.query(Artifact).count() == 0
        assert _blobs_sur_disque() == []
        assert list(A._staged_dir().glob(f"{item['sha256']}*")) == []
    finally:
        db.close()


def test_les_tmp_de_staging_et_de_requete_sont_purges_par_ttl(depot_espace):
    """Un `.tmp` frais est conservé, un expiré est purgé — staging et requête."""
    sessions, _ = depot_espace
    db = sessions()
    try:
        _stager(db, "depot-tmp", pdf_bytes("tmp"), "tmp.pdf")
    finally:
        db.close()

    staged = A._staged_dir()
    requete = paths_bridge.temp_root() / "requete-ttl"
    requete.mkdir(parents=True, exist_ok=True)

    frais = [staged / ".frais.tmp", requete / ".frais.tmp"]
    expires = [staged / ".expire.tmp", requete / ".expire.tmp"]
    for chemin in frais + expires:
        chemin.write_bytes(b"temporaire")

    vieux = time.time() - A.STAGING_TTL_S - 60
    for chemin in expires:
        os.utime(chemin, (vieux, vieux))

    assert A.purger_staging_expires() == 1
    assert A.purger_temporaires_temp_expires() == 1
    assert all(chemin.is_file() for chemin in frais)
    assert all(not chemin.exists() for chemin in expires)
