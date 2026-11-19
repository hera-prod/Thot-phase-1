"""Tests d'acceptation T-M01 → T-M11 — espaces et mémoire (C17, phase 5).

Sans modèle, sans GPU : base temporaire, périmètre réel, zéro appel d'outil.
Ces tests sont la partie qui PROUVE le cloisonnement : les deux premiers points
de `04_classe_perimetre.md:108-116` (filtre serveur, pré-filtre avant score) ne
sont vérifiables que par le troisième — tenter les accès hors périmètre et
constater que rien ne fuit, ni titre, ni contenu, ni existence.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactCard,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    MemoryDocumentExtraction,
    MemoryEntry,
    Message,
    Observation,
    Workspace,
)
from backend.search import scope as S
from backend.services import espaces as E
from backend.services import memoire as M


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Session sur base temporaire, injectée comme SessionLocal du projet."""
    engine = create_engine(f"sqlite:///{tmp_path / 'espaces.db'}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        yield s
    finally:
        s.close()


def _artifact(s, art_id: str, name: str = "doc.txt", body: bytes = b"contenu") -> str:
    """Crée l'artefact s'il n'existe pas — idempotent, pour qu'un test puisse
    appeller plusieurs fois `_source` sans violer l'unicité du blob."""
    if s.query(Artifact).filter(Artifact.id == art_id).first() is None:
        s.add(
            Blob(
                id=f"sha_{art_id}",
                sha256=f"sha_{art_id}",
                relative_path=f"blobs/sha_{art_id}.bin",
                size_bytes=len(body),
                mime_detected="text/plain",
                state="available",
            )
        )
        s.add(
            Artifact(
                id=art_id,
                blob_id=f"sha_{art_id}",
                kind="text",
                original_filename=name,
                display_name=name,
            )
        )
        s.commit()
    return art_id


def _observation(
    s, art_id: str, texte: str, observation_id: str = "obs_partage"
) -> str:
    """Observation d'extraction réussie : ce qui rend un artefact lisible."""
    s.add(
        Observation(
            id=observation_id,
            artifact_id=art_id,
            tool_name="attachment_ingest",
            tool_version="test/1",
            status="completed",
            payload_ref=texte,
            result_json="{}",
        )
    )
    s.commit()
    return observation_id


def _source(s, ws_id: str) -> str:
    """Identifiant d'une source partageable dans l'espace.

    C17 exige des « sources accessibles » pour promouvoir, donc la valeur par
    défaut d'un test est une vraie source dans le bon périmètre. La ligne de
    dépôt est créée à la demande par `_partage_source`.
    """
    art_id = f"src_{ws_id}"
    _artifact(s, art_id, name=f"{art_id}.txt")
    return art_id


def _partage_source(s, ws, conv_id: str) -> str:
    """Source réellement partagée dans l'espace : la provenance par défaut."""
    art_id = _source(s, ws.id)
    if (
        not s.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == art_id,
            ArtifactLink.scope_kind == "workspace",
            ArtifactLink.scope_id == ws.id,
        )
        .first()
    ):
        _importe_et_partage(s, ws, art_id, conv_id)
    return art_id


def _importe_et_partage(s, ws, art_id: str, conv_id: str) -> dict:
    """Le parcours réel : import dans un chat, PUIS partage vers l'espace.

    C'est aussi ce que la règle C1 impose désormais — on ne partage que ce qu'on
    voit depuis une portée déclarée.
    """
    from backend.services import espaces as _E

    _link(s, art_id, "conversation", conv_id)
    return _E.share_artifact(s, ws.id, art_id, from_conv_id=conv_id)


def _conv(s, conv_id: str, ws: str | None = None, answer: str | None = None) -> str:
    s.add(Conversation(id=conv_id, title="t", mode="chat", workspace_id=ws))
    if answer is not None:
        s.add(
            Message(
                id=f"msg_{conv_id}",
                conv_id=conv_id,
                role="assistant",
                content=answer,
                status="done",
            )
        )
    s.commit()
    return conv_id


def _link(s, art_id: str, kind: str, scope_id: str) -> None:
    s.add(
        ArtifactLink(
            id=f"lnk_{art_id}_{kind}_{scope_id}",
            artifact_id=art_id,
            scope_kind=kind,
            scope_id=scope_id,
            role="shared",
        )
    )
    s.commit()


# ---------------------------------------------------------------------------
# Migration facultative — P5-J1
# ---------------------------------------------------------------------------


def test_espace_facultatif_aucune_migration_forcee(db):
    """C17:248 — reprendre 10 conversations hors espace, sans créer d'espace."""
    for i in range(10):
        _conv(db, f"conv{i}", ws=None)
    assert E.list_workspaces(db) == []
    assert len(E.space_conversation_ids(db, "ws_inexistant")) == 0
    for i in range(10):
        assert E.conversation_workspace(db, f"conv{i}") is None


def test_creation_ne_stocke_aucun_chemin_ni_secret(db):
    """C18:270 — ni chemin absolu, ni paramètre secret dans l'espace."""
    ws = E.create_workspace(db, "Recherche", description="notes de travail")
    assert ws.id.startswith("ws_")
    assert ws.state == "active"
    assert not hasattr(ws, "path")
    columns = {c.name for c in Workspace.__table__.columns}
    assert "path" not in columns and "root" not in columns
    assert "secret" not in columns and "api_key" not in columns


def test_identifiants_stables_et_non_reattribues(db):
    """C10:148 — un id d'espace n'est jamais réattribué après suppression."""
    ws = E.create_workspace(db, "A")
    kept = ws.id
    E.trash_workspace(db, kept)
    E.purge_workspace(db, kept)
    assert E.get_workspace(db, kept) is None
    # Un uuid n'est jamais réattribué : on le vérifie sur un lot, pas sur un
    # échantillon — un id séquentiel passerait aussi un test à une seule ligne.
    nouveaux = {E.create_workspace(db, f"Espace {i}").id for i in range(25)}
    assert len(nouveaux) == 25 and kept not in nouveaux


# ---------------------------------------------------------------------------
# T-M01 — cloisonnement entre deux espaces + conversation hors espace
# ---------------------------------------------------------------------------


def test_T_M01_deux_espaces_isoles_et_hors_espace(db):
    """Droits séparés ; aucune fuite par ID, hash, recherche ou suggestion."""
    a, b = E.create_workspace(db, "Alpha"), E.create_workspace(db, "Beta")
    # Même NOM de fichier et même contenu des deux côtés : ce n'est pas le
    # hash qui décide, c'est le lien.
    _artifact(db, "art_a", name="projet.txt", body=b"meme contenu")
    _artifact(db, "art_b", name="projet.txt", body=b"meme contenu")
    _link(db, "art_a", "workspace", a.id)
    _link(db, "art_b", "workspace", b.id)
    _conv(db, "conv_a", ws=a.id)
    _conv(db, "conv_b", ws=b.id)
    _conv(db, "conv_hors", ws=None)

    assert S.resolve_allowed_artifacts("conv_a") == {"art_a"}
    assert S.resolve_allowed_artifacts("conv_b") == {"art_b"}
    assert S.resolve_allowed_artifacts("conv_hors") == set()
    # Connaître l'id de l'autre espace ne donne rien.
    assert "art_b" not in S.resolve_allowed_artifacts("conv_a")
    denied = S.check_artifact_scope(
        "art_b", S.resolve_allowed_artifacts("conv_a"), exists=True
    )
    assert denied is not None and denied["code"] == "ID_OUT_OF_SCOPE"
    # L'information d'espace ne déborde pas non plus.
    assert S.resolve_space_information_scope(a.id) == {"art_a"}
    assert S.resolve_space_information_scope(b.id) == {"art_b"}


def test_T_M01_espace_en_corbeille_n_accorde_plus(db):
    """La corbeille est un état, pas une portée."""
    ws = E.create_workspace(db, "Alpha")
    _artifact(db, "art_a")
    _link(db, "art_a", "workspace", ws.id)
    _conv(db, "conv_a", ws=ws.id)
    assert S.resolve_allowed_artifacts("conv_a") == {"art_a"}
    E.trash_workspace(db, ws.id)
    assert S.resolve_allowed_artifacts("conv_a") == set()


# ---------------------------------------------------------------------------
# T-M02 — importer une fois, ouvrir deux conversations
# ---------------------------------------------------------------------------


def test_T_M02_ressource_partagee_sans_nouvel_import(db):
    """Une seule ligne ArtifactLink, deux conversations qui la voient."""
    ws = E.create_workspace(db, "Alpha")
    art = _artifact(db, "art_partage")
    _conv(db, "conv_1", ws=ws.id)
    _conv(db, "conv_2", ws=ws.id)
    _link(db, art, "conversation", "conv_1")  # import dans le chat 1

    out = E.share_artifact(db, ws.id, art, from_conv_id="conv_1")
    assert out["bytes_copied"] == 0  # C10:172 — un lien, jamais une copie
    assert S.resolve_allowed_artifacts("conv_1") == {"art_partage"}
    assert S.resolve_allowed_artifacts("conv_2") == {"art_partage"}
    # Idempotent : re-partager ne crée pas de seconde ligne.
    assert E.share_artifact(db, ws.id, art, from_conv_id="conv_1")["created"] is False
    links = (
        db.query(ArtifactLink)
        .filter(ArtifactLink.artifact_id == art, ArtifactLink.scope_kind == "workspace")
        .all()
    )
    assert len(links) == 1


def test_partage_persiste_le_ledger_dans_une_nouvelle_session(db):
    """🟠 1 — la ligne `pending` doit survivre à la fermeture de la session.

    `share_artifact` committe le lien, puis l'enqueue documentaire écrit dans
    la MÊME session. Sans commit explicite, `_dispatch_ipc` fermerait la
    session et annulerait la ligne. On rouvre une session comme le fait le
    dispatcher pour vérifier la persistance réelle.
    """
    from backend.db.database import SessionLocal
    from backend.services import memoire_scheduler as S

    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    art = _artifact(db, "art_ledger")
    db.query(Artifact).filter(Artifact.id == art).update({"ingest_state": "extrait"})
    db.commit()
    _observation(
        db, art, "Le serveur utilise SQLite comme base de données depuis 2024."
    )
    _link(db, art, "conversation", "conv_1")
    S._reveil_doc.clear()

    out = E.share_artifact(db, ws.id, art, from_conv_id="conv_1")

    assert out["ok"] is True and out["created"] is True
    assert S._reveil_doc.is_set()
    ws_id, art_id = ws.id, art
    db.close()

    nouvelle = SessionLocal()
    try:
        ligne = nouvelle.query(MemoryDocumentExtraction).one()
        assert ligne.workspace_id == ws_id
        assert ligne.artifact_id == art_id
        assert ligne.status == "pending"
    finally:
        nouvelle.close()
        S._reveil_doc.clear()


def test_partage_sans_texte_ne_reveille_pas(db):
    """Aucune ligne créée (artefact sans texte) -> aucun réveil inutile."""
    from backend.services import memoire_scheduler as S

    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    art = _artifact(db, "art_illisible")
    db.query(Artifact).filter(Artifact.id == art).update(
        {"ingest_state": "binaire_refuse"}
    )
    db.commit()
    _link(db, art, "conversation", "conv_1")
    S._reveil_doc.clear()

    out = E.share_artifact(db, ws.id, art, from_conv_id="conv_1")

    assert out["ok"] is True and out["created"] is True
    assert db.query(MemoryDocumentExtraction).count() == 0
    assert not S._reveil_doc.is_set()


def test_echec_enqueue_apres_partage_incremente_le_compteur(db, monkeypatch):
    """🟡 3 — un échec technique d'enqueue n'est plus avalé sans trace."""
    from backend.services import memoire_documentaire as D

    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    art = _artifact(db, "art_panne")
    db.query(Artifact).filter(Artifact.id == art).update({"ingest_state": "extrait"})
    db.commit()
    _observation(
        db, art, "Le serveur utilise SQLite comme base de données depuis 2024."
    )
    _link(db, art, "conversation", "conv_1")

    def boom(*_args, **_kwargs):
        raise RuntimeError("panne simulée d'enqueue")

    monkeypatch.setattr(D, "enfiler_apres_relink", boom)
    avant = E.compteurs_enqueue_documentaire()["echecs"]

    out = E.share_artifact(db, ws.id, art, from_conv_id="conv_1")

    assert out["ok"] is True  # le partage déjà committé reste acquis
    assert E.compteurs_enqueue_documentaire()["echecs"] == avant + 1


def test_une_piece_jointe_reste_privee(db):
    """Une pièce jointe n'est PAS partagée automatiquement avec les autres chats."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    _conv(db, "conv_2", ws=ws.id)
    _artifact(db, "art_prive")
    _link(db, "art_prive", "conversation", "conv_1")
    assert S.resolve_allowed_artifacts("conv_1") == {"art_prive"}
    assert S.resolve_allowed_artifacts("conv_2") == set()


def test_les_liens_du_chat_ne_sont_pas_detruits_par_la_corbeille(db):
    """C11:182 — supprimer un lien d'espace conserve le blob et les liens de chat."""
    ws = E.create_workspace(db, "Alpha")
    _artifact(db, "art_x")
    _conv(db, "conv_1", ws=ws.id)
    _link(db, "art_x", "conversation", "conv_1")
    E.share_artifact(db, ws.id, "art_x", from_conv_id="conv_1")
    E.unshare_artifact(db, ws.id, "art_x")
    assert S.resolve_allowed_artifacts("conv_1") == {"art_x"}
    assert db.query(Artifact).filter(Artifact.id == "art_x").first() is not None


# ---------------------------------------------------------------------------
# Dépôt en vue de portée — l'union de deux ensembles
# ---------------------------------------------------------------------------


def test_le_depot_ne_contient_que_les_fichiers_de_l_espace(db):
    """Le dépôt est la zone de l'utilisateur — pas l'union avec les discussions.

    Un fichier déposé dans une discussion y reste : il n'apparaît pas au dépôt.
    `02_classe_depot.md:26-49` décrivait l'union des deux ensembles, et ce test
    verrouillait cette lecture. La décision du porteur la retire : présenter comme
    « ma référence » ce qu'une conversation a produit au passage serait un mensonge.
    """
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    _conv(db, "conv_2", ws=ws.id)
    _artifact(db, "art_chat", name="du-chat.txt")
    _artifact(db, "art_user", name="du-poste.txt")
    _artifact(db, "art_les_deux", name="partage.txt")
    _link(db, "art_chat", "conversation", "conv_1")
    _link(db, "art_les_deux", "conversation", "conv_2")
    _link(db, "art_user", "conversation", "conv_1")
    E.share_artifact(db, ws.id, "art_user", from_conv_id="conv_1")
    E.share_artifact(db, ws.id, "art_les_deux", from_conv_id="conv_2")

    content = E.depot(db, ws.id)
    assert content["is_physical_folder"] is False  # vue de portée, pas un dossier
    names = {f["display_name"] for f in content["files"]}
    # `du-chat.txt` est absent : il n'appartient qu'à une conversation. Les deux
    # autres ont été partagés DANS l'espace, donc ils y sont.
    assert names == {"du-poste.txt", "partage.txt"}
    # Un fichier partagé reste visible par sa discussion d'origine ET par l'espace.
    partage = next(f for f in content["files"] if f["display_name"] == "partage.txt")
    assert partage["size_bytes"] == len(b"contenu")
    assert partage["kind"]
    assert partage["added_at"]


def test_le_corpus_suit_le_rattachement_de_la_conversation(db):
    """Un chat supprimé continue d'alimenter l'information de l'espace.

    Le contrat réalise cela par la corbeille sept jours (annexe10:146) : la ligne
    de conversation survit en `trashed` et garde son `workspace_id`. Tant que
    cette ligne existe, ses fichiers alimentent le corpus d'information — et
    aucun chat ne les manipule. Voir la réserve de `depot()` : la corbeille de
    conversation n'est pas encore implémentée, donc c'est sa durée de vie qu'on
    vérifie ici, pas un état `trashed` qui n'existe pas encore en base.
    """
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_supprime", ws=ws.id)
    _artifact(db, "art_ancien")
    _link(db, "art_ancien", "conversation", "conv_supprime")

    # Tant que la conversation est dans l'espace, son corpus est là…
    assert S.resolve_allowed_artifacts("conv_supprime") == {"art_ancien"}
    assert "art_ancien" in S.resolve_space_information_scope(ws.id)

    # …et comme cette conversation n'a ni historique ni ressource d'espace à
    # divulguer, son déplacement est démontrable. `attach_conversation` sur un
    # AUTRE espace ne déplace rien : il rend le rapport, et c'est
    # `request_move(confirm=True)` qui vaut accord explicite de l'utilisateur.
    autre = E.create_workspace(db, "Autre")
    blob_avant = (
        db.query(Blob).filter(Blob.id == "sha_art_ancien").first().relative_path
    )
    rapport = E.attach_conversation(db, "conv_supprime", autre.id)
    assert rapport["allowed"] is True and rapport["artifacts_moved"] == 0
    assert E.conversation_workspace(db, "conv_supprime") == ws.id  # rien encore

    applique = E.request_move(db, "conv_supprime", autre.id, confirm=True)
    assert applique["applied"] is True
    assert S.resolve_allowed_artifacts("conv_supprime") == {"art_ancien"}
    assert "art_ancien" not in S.resolve_space_information_scope(ws.id)
    assert "art_ancien" in S.resolve_space_information_scope(autre.id)
    assert (
        db.query(Blob).filter(Blob.id == "sha_art_ancien").first().relative_path
        == blob_avant
    )


def test_compteurs_recalcules_et_non_stockes(db):
    """Un compteur stocké se désynchronise et devient un mensonge."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    _artifact(db, "art_1")
    _importe_et_partage(db, ws, "art_1", "conv_1")
    s1 = E.stats(db, ws.id)
    assert (
        s1["discussions"] == 1
        and s1["fichiers"] == 1
        and s1["octets"] == len(b"contenu")
    )

    _conv(db, "conv_2", ws=ws.id)
    s2 = E.stats(db, ws.id)
    assert s2["discussions"] == 2  # recalculé, pas incrémenté
    assert not any(
        c.name in {"nb_discussions", "nb_fichiers"} for c in Workspace.__table__.columns
    )
    # « Mémoire : Active » des maquettes n'a pas de statut : on expose les 4 réels.
    assert set(s2["memoire_par_statut"]) == {
        "candidate",
        "accepted",
        "stale",
        "rejected",
    }
    assert "active" not in s2["memoire_par_statut"]


# ---------------------------------------------------------------------------
# T-M03 / T-M04 — l'IA ne modifie pas la zone utilisateur, la promotion est humaine
# ---------------------------------------------------------------------------


def test_T_M03_proposition_de_modification_est_un_nouvel_artefact(db):
    """E07 — l'IA est en lecture seule sur la zone utilisateur.

    Le contrat ne dit pas « le service s'abstient » : il dit qu'une révision
    proposée est un NOUVEL artefact, et que seule une commande utilisateur
    distincte accepte le remplacement. On vérifie donc les deux moitiés : le
    parcours « je propose une modification » ne touche aucun artefact, et le
    module n'expose aucun remplacement d'un document de la zone.
    """
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    _artifact(db, "art_user", name="consigne.txt", body=b"texte d'origine")
    _importe_et_partage(db, ws, "art_user", "conv_1")

    def _etat():
        return [
            (a.id, a.display_name, a.blob_id)
            for a in db.query(Artifact).order_by(Artifact.id).all()
        ]

    avant = _etat()
    eid = _propose(
        db, ws.id, "Une révision proposée du contenu.", sources=["art_user"]
    )["entry_id"]
    M.accept(db, eid)
    M.correct(db, eid, "Une révision proposée du contenu, corrigée.")
    assert _etat() == avant, "aucun artefact créé, modifié ni supprimé"

    interdits = [
        n
        for n in dir(E)
        if any(
            n.startswith(p)
            for p in (
                "write_",
                "update_artifact",
                "overwrite_",
                "edit_",
                "replace_",
                "set_artifact",
                "remove_artifact",
            )
        )
    ]
    assert interdits == [], (
        f"la zone utilisateur ne doit exposer aucune écriture : {interdits}"
    )


def test_T_M04_proposition_reste_candidate_jusqu_a_validation(db):
    """C17:252, D07 — le candidat n'est pas une connaissance acceptée."""
    ws = E.create_workspace(db, "Alpha")
    _source_nommee(db, ws, "src")
    out = _propose(
        db, ws.id, "Le projet utilise SQLite pour le registre local.", sources=["src"]
    )
    assert out["status"] == "candidate" and out["created"] is True
    row = db.query(MemoryEntry).filter(MemoryEntry.id == out["entry_id"]).first()
    assert row.status == "candidate"
    assert E.stats(db, ws.id)["memoire"] == 0  # une candidate n'est pas de la mémoire


def test_T_M04_refus_et_correction_conservent_les_versions(db):
    """phase_5:76 — une correction crée une version traçable, rien n'est effacé."""
    ws = E.create_workspace(db, "Alpha")
    _source_nommee(db, ws, "src")
    eid = _propose(db, ws.id, "Le projet utilise SQLite.", sources=["src"])["entry_id"]
    acc = M.accept(db, eid)
    assert acc["status"] == "accepted" and acc["version"] == 2
    cor = M.correct(db, eid, "Le projet utilise SQLite 3.45.")
    assert cor["version"] == 3 and cor["superseded_from"] is not None

    versions = M.list_versions(db, cor["entry_key"])
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert (
        versions[-1]["content"] == "Le projet utilise SQLite."
    )  # l'historique n'est pas réécrit
    assert E.stats(db, ws.id)["memoire"] == 1  # une seule fiche courante


def test_T_M04_conflit_de_revision_refuse(db):
    """backend_espaces.md:122 — refuser le conflit, pas écraser."""
    ws = E.create_workspace(db, "Alpha")
    _source_nommee(db, ws, "src")
    eid = _propose(db, ws.id, "Fait A.", sources=["src"])["entry_id"]
    M.accept(db, eid)
    stale = M.correct(db, eid, "Fait B.", expected_version=99)
    assert stale["ok"] is False and stale["code"] == "REVISION_CONFLICT"
    assert stale["current_version"] == 2


def test_T_M04_refus_est_un_bouton_pas_un_outil(db):
    """phase_5:80 — refuser ne demande rien au modèle."""
    ws = E.create_workspace(db, "Alpha")
    _source_nommee(db, ws, "src")
    eid = _propose(db, ws.id, "Fait contestable.", sources=["src"])["entry_id"]
    out = M.reject(db, eid, motif="faux")
    assert out["status"] == "rejected"
    assert E.stats(db, ws.id)["memoire"] == 0
    found = M.search(db, ws.id, "contestable")
    assert found["results"] == []  # `rejected` écarté de l'usage ordinaire


def test_proposition_idempotente_et_source_inaccessible_refusee(db):
    """C09 pour la clé ; une provenance qui ne tient pas n'écrit rien."""
    ws = E.create_workspace(db, "Alpha")
    _source_nommee(db, ws, "src_ok")
    a = _propose(db, ws.id, "Fait.", sources=["src_ok"], key="k1")
    b = _propose(db, ws.id, "Fait.", sources=["src_ok"], key="k1")
    assert b["created"] is False and b["entry_id"] == a["entry_id"]

    ko = M.propose(
        db,
        ws.id,
        "Fait.",
        source_artifact_ids=["src_inexistant"],
        origin_context="c",
        promotion_reasons=[{"kind": "x"}],
    )
    assert ko["ok"] is False and ko["code"] == "SOURCE_NOT_ACCESSIBLE"
    assert db.query(MemoryEntry).filter(MemoryEntry.workspace_id == ws.id).count() == 1


def test_acceptance_refuse_si_une_source_a_disparu(db):
    """Les droits sont revalidés AU MOMENT de la confirmation."""
    ws = E.create_workspace(db, "Alpha")
    _source_nommee(db, ws, "src")
    eid = _propose(db, ws.id, "Dépend d'une source.", sources=["src"])["entry_id"]
    db.query(Artifact).filter(Artifact.id == "src").first().deleted_at = __import__(
        "datetime"
    ).datetime.now()
    db.commit()
    out = M.accept(db, eid)
    assert (
        out["ok"] is False and out["code"] == "SOURCE_LOST" and out["status"] == "stale"
    )


# ---------------------------------------------------------------------------
# T-M05 / T-M06 / T-M07 — pertinence avant utilité, pagination, usage réel
# ---------------------------------------------------------------------------


def _source_nommee(db, ws, art_id: str, conv_id: str = "conv_src") -> str:
    """Source nommée ET effectivement partagée dans l'espace.

    C17 exige des « sources accessibles » : un artefact qui existe sans lien
    n'est pas une provenance. Les tests qui citent une source par son nom
    passent par là, pour ne pas écrire deux fois la règle dans chaque corps.
    """
    _artifact(db, art_id)
    if db.query(Conversation).filter(Conversation.id == conv_id).first() is None:
        _conv(db, conv_id, ws=ws.id)
    _importe_et_partage(db, ws, art_id, conv_id)
    return art_id


def _propose(
    db,
    ws_id,
    contenu,
    *,
    sources=(),
    key=None,
    origin="conversation de travail",
    conv_id=None,
):
    """Proposition COMPLÈTE, c'est-à-dire conforme à ce que C17 exige d'une
    promotion : sources accessibles, contexte d'origine, raisons structurées.

    `sources` vide signifie « la source partagée de cet espace » : une fiche
    sans provenance ne peut pas être promue, donc un test qui veut atteindre
    `accepted` doit en fournir une.
    """
    if not sources:
        from backend.db.models import Workspace as _Ws

        ws = db.query(_Ws).filter(_Ws.id == ws_id).first()
        sources = [
            _partage_source(db, ws, conv_id or _premiere_conversation(db, ws_id))
        ]
    return M.propose(
        db,
        ws_id,
        contenu,
        source_artifact_ids=list(sources),
        origin_context=origin,
        promotion_reasons=[{"kind": "outil", "detail": "usage répété"}],
        idempotency_key=key,
    )


def _premiere_conversation(db, ws_id: str) -> str:
    """Une conversation de l'espace — sinon la source ne pourrait pas y entrer."""
    row = db.query(Conversation).filter(Conversation.workspace_id == ws_id).first()
    if row is None:
        _conv(db, f"conv_src_{ws_id[:6]}", ws=ws_id)
        return f"conv_src_{ws_id[:6]}"
    return str(row.id)


def _fiche(db, ws_id, key, contenu, *, statut="accepted", usage=0):
    """Fiche via le vrai service, statut forcé, usage posé directement.

    On relit la version COURANTE par `entry_key` plutôt que par une recherche :
    une recherche prend le premier résultat, donc la fiche d'une autre, et le
    test testerait alors autre chose que ce qu'il prétend.

    `usage` est posé en base et non via `record_usage` parce qu'on veut simuler
    un historique accumulé (des centaines d'usages), pas un usage unique — c'est
    T-M07 qui vérifie le chemin `record_usage`.
    """
    out = _propose(db, ws_id, contenu, key=key)
    entry_key = out["entry_key"]
    if statut == "candidate":
        current = out["entry_id"]
    else:
        current = M.accept(db, out["entry_id"])["entry_id"]
        if statut == "stale":
            current = M.mark_stale(db, current)["entry_id"]
        elif statut == "rejected":
            current = M.reject(db, current)["entry_id"]
    if usage:
        row = (
            db.query(MemoryEntry)
            .filter(MemoryEntry.entry_key == entry_key, MemoryEntry.is_current == 1)
            .first()
        )
        row.usage_count = usage
        db.commit()
    return current


def test_T_M05_la_pertinence_prime_sur_la_popularite(db):
    """Une fiche rare mais pertinente reste devant une fiche populaire hors sujet."""
    ws = E.create_workspace(db, "Alpha")
    _fiche(db, ws.id, "k1", "Le protocole de sauvegarde utilise SQLite.", usage=999)
    _fiche(db, ws.id, "k2", "Le café de la salle de réunion.", usage=0)
    page = M.search(db, ws.id, "protocole sauvegarde", limit=5)
    assert page["results"][0]["content"].startswith("Le protocole")
    assert page["results"][0]["relevance"] > page["results"][1]["relevance"]
    # La fiche populaire est bien là, juste pas en tête.
    assert page["total"] == 2


def test_T_M06_la_page_de_cinq_nest_pas_une_limite(db):
    """C17:262 — k=5 est une taille de page, jamais une limite de découverte."""
    ws = E.create_workspace(db, "Alpha")
    for i in range(12):
        _fiche(
            db,
            ws.id,
            f"k{i}",
            fFiche := f"Fiche numero {i} sur le transport de la Palace.",
        )
    page1 = M.search(db, ws.id, "transport Palace", limit=5)
    assert page1["returned_count"] == 5 and page1["has_more"] is True
    vus = {r["entry_id"] for r in page1["results"]}
    cursor = page1["next_cursor"]
    while cursor is not None:
        p = M.search(db, ws.id, "transport Palace", limit=5, cursor=cursor)
        vus |= {r["entry_id"] for r in p["results"]}
        cursor = p["next_cursor"]
    assert len(vus) == 12  # la douzième est atteignable


def test_T_M07_usage_reel_est_idempotent(db):
    """Dix consultations ne comptent rien ; une relance ne compte qu'une fois."""
    ws = E.create_workspace(db, "Alpha")
    eid = _fiche(db, ws.id, "k1", "Fait utile.")
    avant = M.search(db, ws.id, "utile")["results"][0]["usage_count"]
    for _ in range(10):
        M.search(db, ws.id, "utile")  # consultation pure : aucun incrément
    assert M.search(db, ws.id, "utile")["results"][0]["usage_count"] == avant

    assert M.record_usage(db, eid, "msg_reponse_1")["counted"] is True
    assert (
        M.record_usage(db, eid, "msg_reponse_1")["counted"] is False
    )  # même réponse rejouée
    assert M.search(db, ws.id, "utile")["results"][0]["usage_count"] == avant + 1


def test_T_M08_contradiction_et_peremption_visibles(db):
    """Une contradiction et une ancienneté sont visibles, corrigibles."""
    ws = E.create_workspace(db, "Alpha")
    _source_nommee(db, ws, "src")
    a = _propose(db, ws.id, "La base est PostgreSQL.", sources=["src"])["entry_id"]
    b = _propose(db, ws.id, "La base est MySQL.", sources=["src"])["entry_id"]
    a_actuel = M.accept(db, a)["entry_id"]  # l'acceptation crée une version
    b_actuel = M.accept(db, b)["entry_id"]
    page = M.search(db, ws.id, "base", limit=5)
    assert {r["content"] for r in page["results"]} == {
        "La base est PostgreSQL.",
        "La base est MySQL.",
    }  # aucune ne gagne en silence

    a_stale = M.mark_stale(db, a_actuel, reason="contradiction")["entry_id"]
    stale = next(
        r for r in M.search(db, ws.id, "base")["results"] if r["entry_id"] == a_stale
    )
    assert stale["is_stale"] is True
    assert (
        "cite la source" in stale["citation_hint"]
    )  # la synthèse ne remplace pas la preuve
    # La fiche périmée n'est plus présentée comme valide ; l'autre reste vigente.
    assert b_actuel in {r["entry_id"] for r in M.search(db, ws.id, "base")["results"]}


def test_fusion_conserve_provenance_et_desaccords(db):
    """backend_espaces.md:148 — la fusion ne fait pas gagner la plus populaire."""
    ws = E.create_workspace(db, "Alpha")
    a = _fiche(db, ws.id, "k1", "Version A du protocole.")
    b = _fiche(db, ws.id, "k2", "Version B du protocole.")
    out = M.merge(db, [a, b], "Version A et B du protocole, non tranchées.")
    assert out["merged_count"] == 2

    # La fiche issue de la fusion est accepted…
    fusion = M.list_versions(db, out["entry_key"])
    assert fusion[0]["status"] == "accepted"
    assert "non tranchées" in fusion[0]["content"]
    # …et l'absorbée devient stale dans SAUTE fiche, pas effacée : le désaccord
    # reste traçable, y compris hors de la fiche fusionnée.
    absorbee = M.list_versions(db, b)
    assert absorbee[0]["status"] == "stale"
    assert any(v["status"] == "accepted" for v in absorbee)  # son passé reste lisible


def test_recherche_memoire_exclut_les_rejets_et_signale_les_candidates(db):
    ws = E.create_workspace(db, "Alpha")
    _fiche(db, ws.id, "k1", "Fait candidat non validé.", statut="candidate")
    page = M.search(db, ws.id, "candidat")
    assert page["results"][0]["is_candidate"] is True
    assert page["results"][0]["status"] == "candidate"  # jamais présenté comme accepté


# ---------------------------------------------------------------------------
# T-M09 / T-M11 — suppression, purge, déplacement conservateur
# ---------------------------------------------------------------------------


def test_T_M09_purge_retrogade_et_preserve_la_zone_utilisateur(db):
    """Décision E-SUPPR : corbeille 7 jours puis rétrogradation, pas cascade."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    _artifact(db, "art_user")
    _importe_et_partage(db, ws, "art_user", "conv_1")
    eid = _propose(db, ws.id, "Fait mémoire.", sources=["art_user"])["entry_id"]
    M.accept(db, eid)

    out = E.trash_workspace(db, ws.id)
    assert out["retention_days"] == 7
    trashed = E.get_workspace(db, ws.id)
    assert trashed is not None and trashed.state == "trashed"
    assert E.expired_trash(db) == []  # pas encore échue

    purge = E.purge_workspace(db, ws.id)
    assert purge["conversations_demoted"] == 1
    # La conversation SURVIT et redevient un chat simple.
    assert (
        db.query(Conversation).filter(Conversation.id == "conv_1").first().workspace_id
        is None
    )
    # La zone utilisateur et ses octets sont intacts.
    assert db.query(Artifact).filter(Artifact.id == "art_user").first() is not None
    assert db.query(Blob).filter(Blob.id == "sha_art_user").first() is not None
    # La mémoire part avec l'espace.
    assert db.query(MemoryEntry).filter(MemoryEntry.workspace_id == ws.id).count() == 0


def test_restauration_avant_echeance(db):
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    E.trash_workspace(db, ws.id)
    E.restore_workspace(db, ws.id)
    restored = E.get_workspace(db, ws.id)
    assert restored is not None and restored.state == "active"
    assert E.conversation_workspace(db, "conv_1") == ws.id


def test_purge_memoire_n_altere_pas_la_zone_utilisateur(db):
    """phase_5:128 — purger la mémoire ne touche pas aux documents."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    _artifact(db, "art_user")
    _importe_et_partage(db, ws, "art_user", "conv_1")
    eid = _propose(db, ws.id, "Fait.", sources=["art_user"])["entry_id"]
    M.accept(db, eid)
    out = M.purge_memory(db, eid, ws.id)
    assert out["user_zone_untouched"] is True
    assert db.query(Artifact).filter(Artifact.id == "art_user").first() is not None
    assert S.resolve_allowed_artifacts("conv_x", workspace_id=ws.id) == {"art_user"}


def test_T_M11_deplacement_assoupli_liste_les_consequences_sans_refus(db):
    """§7.3 — le déplacement est une opération normale : ses conséquences se listent.

    « Déplacer une discussion de l'espace X vers l'espace Y déplace la
    discussion et les fichiers qui lui sont liés. » Le profil initial refusait
    tant que l'indépendance n'était pas démontrée ; l'évaluation nomme
    désormais ce qui transite et ce qui reste à X, et ne bloque plus.
    """
    src = E.create_workspace(db, "Source")
    dst = E.create_workspace(db, "Destination")
    art_cite = _artifact(s=db, art_id="art_cite", name="cite.txt")
    _link(db, art_cite, "conversation", "conv_1")
    _conv(
        db,
        "conv_1",
        ws=src.id,
        answer="D'après le document de l'espace source, la base est PostgreSQL.",
    )
    M.accept(
        db,
        _propose(db, src.id, "Fait cité depuis la discussion.", sources=[art_cite])[
            "entry_id"
        ],
    )
    art_depot = _artifact(db, "art_depot_zone")
    _importe_et_partage(db, src, art_depot, "conv_hors_zone")
    _conv(db, "conv_hors_zone", ws=src.id)

    report = E.assess_move(db, "conv_1", dst.id)
    assert report["allowed"] is True
    assert report["blocking"] == []
    assert report["contributions"] == ["art_cite"]
    # Ce qui reste à X : la zone utilisateur n'est pas déplacée en bloc (§7.3).
    assert {f["artifact_id"] for f in report["lost_links"]} == {art_depot}
    # La fiche citant la CONTRIBUTION est nommée pour invalidation à l'accord.
    assert report["syntheses_a_invalider"]
    # Aucune écriture pendant l'évaluation.
    assert (
        db.query(Conversation).filter(Conversation.id == "conv_1").first().workspace_id
        == src.id
    )


def test_deplacement_application_transfere_et_invalide_la_synthese_citante(db):
    """§7.3 — à l'accord, le corpus suit et les fiches citantes deviennent révisables.

    « Les synthèses des deux espaces sont actualisées selon leurs contributions
    présentes. » X perd les contributions (la fiche qui les cite ne sera plus
    servie comme un fait actuel) ; Y les reçoit ; le dépôt de X reste le sien.
    """
    src = E.create_workspace(db, "Source")
    dst = E.create_workspace(db, "Destination")
    art_cite = _artifact(db, "art_cite", name="cite.txt")
    _link(db, art_cite, "conversation", "conv_1")
    _conv(db, "conv_1", ws=src.id)
    eid = _propose(db, src.id, "Fait cité depuis la discussion.", sources=[art_cite])[
        "entry_id"
    ]
    M.accept(db, eid)

    out = E.request_move(db, "conv_1", dst.id, confirm=True)
    assert out.get("applied") is True
    assert (
        db.query(Conversation).filter(Conversation.id == "conv_1").first().workspace_id
        == dst.id
    )
    # Corpus : X perd, Y reçoit, SANS déplacer un octet.
    assert "art_cite" not in S.resolve_space_information_scope(src.id)
    assert "art_cite" in S.resolve_space_information_scope(dst.id)
    blob = db.query(Blob).filter(Blob.id == "sha_art_cite").first()
    assert blob.relative_path == "blobs/sha_art_cite.bin"
    # La fiche citante est passée révisable : VERSION N+1, pas écrasement.
    assert out["syntheses_invalidees"], "la fiche citante devait être invalidée"
    courante = (
        db.query(MemoryEntry)
        .filter(MemoryEntry.id == out["syntheses_invalidees"][0])
        .first()
    )
    assert courante is not None
    assert courante.is_current == 1
    assert courante.status == "stale"
    assert "déplacée" in (courante.decision or {}).get("reason", "")


def test_deplacement_conserve_les_images_sans_dupliquer(db):
    """§7.3 — « les images restent préservées… le déplacement ne les supprime pas
    et ne les duplique pas inutilement. »

    Une image liée à la discussion change de rattachement AVEC elle : c'est son
    lien de conversation qui suit. Un déplacement qui créerait un second lien,
    un second blob ou supprimerait le lien d'origine violerait la phrase.
    """
    src = E.create_workspace(db, "Source")
    dst = E.create_workspace(db, "Destination")
    blob_path = f"blobs/sha_art_img.bin"
    db.add(
        Blob(
            id="sha_art_img",
            sha256="sha_art_img",
            relative_path=blob_path,
            size_bytes=5,
            mime_detected="image/png",
            state="available",
        )
    )
    db.add(
        Artifact(
            id="art_img",
            blob_id="sha_art_img",
            kind="image",
            original_filename="capture.png",
            display_name="capture.png",
        )
    )
    db.add(
        ArtifactLink(
            id="lnk_img",
            artifact_id="art_img",
            scope_kind="conversation",
            scope_id="conv_img",
            role="attachment",
        )
    )
    _conv(db, "conv_img", ws=src.id)
    db.commit()

    out = E.request_move(db, "conv_img", dst.id, confirm=True)
    assert out.get("applied") is True and out["artifacts_moved"] == 0
    # Une image, un lien, un blob : rien ne bouge, rien ne duplique.
    assert db.query(Artifact).filter(Artifact.id == "art_img").count() == 1
    assert (
        db.query(ArtifactLink).filter(ArtifactLink.artifact_id == "art_img").count()
        == 1
    )
    assert (
        db.query(Blob).filter(Blob.id == "sha_art_img").first().relative_path
        == blob_path
    )
    # Le rattachement a suivi : la portée conversation est TOUJOURS résoluble.
    assert S.resolve_allowed_artifacts("conv_img") == {"art_img"}


def test_deplacement_refus_nommes_sur_objets_inconnus(db):
    """La fiche ne rend pas magique : conversation ou cible inconnue, refus nommé."""
    dst = E.create_workspace(db, "Destination")
    with pytest.raises(LookupError) as exc:
        E.assess_move(db, "conv_rien", dst.id)
    assert "CONVERSATION_NOT_FOUND" in str(exc.value)

    _conv(db, "conv_9")
    with pytest.raises(LookupError) as manque:
        E.request_move(db, "conv_9", "ws_rien", confirm=True)
    assert "WORKSPACE_NOT_FOUND" in str(manque.value)

    # L'espace en corbeille n'accorde pas de rattachement entrant.
    trashed = E.create_workspace(db, "Corbeille")
    E.trash_workspace(db, str(trashed.id))
    with pytest.raises(LookupError) as corbeille:
        E.request_move(db, "conv_9", str(trashed.id), confirm=True)
    assert "WORKSPACE_NOT_FOUND" in str(corbeille.value)
    assert (
        db.query(Conversation).filter(Conversation.id == "conv_9").first().workspace_id
        is None
    )


def test_deplacement_sans_confirmation_ne_modifie_rien(db):
    """L'évaluation peut être rejouée : sans `confirm`, aucune ligne n'est écrite."""
    src = E.create_workspace(db, "Source")
    dst = E.create_workspace(db, "Destination")
    art = _artifact(db, "art_cite", name="cite.txt")
    _link(db, art, "conversation", "conv_1")
    _conv(db, "conv_1", ws=src.id)
    M.accept(db, _propose(db, src.id, "Fait cité.", sources=[art])["entry_id"])

    rapport_1 = E.request_move(db, "conv_1", dst.id)
    rapport_2 = E.request_move(db, "conv_1", dst.id)
    assert rapport_1 == rapport_2
    assert "applied" not in rapport_1
    assert (
        db.query(Conversation).filter(Conversation.id == "conv_1").first().workspace_id
        == src.id
    )
    # Aucune fiche invalidée à ce stade : l'invalidation vit à l'accord.
    assert db.query(MemoryEntry).filter(MemoryEntry.status == "stale").count() == 0


def test_deplacement_discussion_sans_espace_liste_ses_contributions(db):
    """Sans espace d'origine, le rapport DIT ce qui suit — pas « rien à faire ».

    C'est le cas le plus fréquent depuis l'historique : une discussion lancée
    hors espace. `request_move(confirm=True)` la rattache réellement et ses
    fichiers liés suivent ; l'évaluation ne doit donc ni annoncer `changed=False`
    ni une liste de contributions vide.
    """
    dst = E.create_workspace(db, "Destination")
    art = _artifact(db, "art_libre", name="libre.txt")
    _link(db, art, "conversation", "conv_libre")
    _conv(db, "conv_libre")  # aucune source
    rapport = E.assess_move(db, "conv_libre", dst.id)
    assert rapport["changed"] is True
    assert rapport["source_workspace_id"] is None
    assert rapport["contributions"] == ["art_libre"]
    assert rapport["lost_links"] == []
    assert rapport["syntheses_a_invalider"] == []
    out = E.request_move(db, "conv_libre", dst.id, confirm=True)
    assert out["applied"] is True
    assert "syntheses_erreurs" not in out
    assert E.conversation_workspace(db, "conv_libre") == dst.id


def test_rattachement_ne_deplace_aucun_octet(db):
    """La règle la plus importante du modèle (01_architecture...:80-87)."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=None)
    _artifact(db, "art_x")
    _link(db, "art_x", "conversation", "conv_1")
    avant = db.query(Artifact).filter(Artifact.id == "art_x").first()
    blob_avant = db.query(Blob).filter(Blob.id == "sha_art_x").first().relative_path

    out = E.attach_conversation(db, "conv_1", ws.id)
    assert out["artifacts_moved"] == 0
    apres = db.query(Artifact).filter(Artifact.id == "art_x").first()
    assert apres.id == avant.id and apres.blob_id == avant.blob_id
    assert (
        db.query(Blob).filter(Blob.id == "sha_art_x").first().relative_path
        == blob_avant
    )
    assert S.resolve_allowed_artifacts("conv_1") == {"art_x"}


def test_detachement_ne_perd_aucun_fichier(db):
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "conv_1", ws=ws.id)
    _artifact(db, "art_x")
    _link(db, "art_x", "conversation", "conv_1")
    out = E.detach_conversation(db, "conv_1")
    assert out["artifacts_removed"] == 0
    assert S.resolve_allowed_artifacts("conv_1") == {"art_x"}  # le sien reste le sien


def test_les_quatre_interdits_du_modele_sont_structuraux(db):
    """01_classe_espace.md:74-84 — le modèle ne peut rien de tout ça.

    Vérifié sur le SCHÉMA des outils : un nom de fonction ou une description en
    français ne prouverait rien."""
    from backend.tools.registry import get_default_registry

    registre = get_default_registry()
    interdits = {
        "attach_conversation",
        "detach_conversation",
        "share_artifact",
        "unshare_artifact",
        "request_move",
        "purge_workspace",
        "trash_workspace",
        "accept",
        "reject",
        "merge",
        "propose",
    }
    assert not (interdits & set(registre.tool_names()))

    # Aucun outil ne prend un périmètre ou un espace en ARGUMENT : la frontière
    # d'autorisation est côté serveur, et un argument la contournerait. On vérifie
    # le SCHÉMA, pas la prose — les descriptions sont en français, et demander
    # « aucun mot workspace » ne prouverait rien.
    champs: set[str] = set()
    for nom in registre.tool_names():
        champs |= set(getattr(registre.get(nom).input_model, "model_fields", {}) or {})
    assert not {
        c for c in champs if "workspace" in c or "scope_id" in c or "tenant" in c
    }


# ---------------------------------------------------------------------------
# Recherche d'information au niveau espace — FTS réel, sans modèle
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_fts(tmp_path, monkeypatch):
    """Base avec FTS5 et fiches actives : la recherche d'espace se teste pour de vrai."""
    from backend.db.database import _ensure_fts, _migrate_search_columns
    from backend.search import cards as _cards, chunks as _chunks

    engine = create_engine(f"sqlite:///{tmp_path / 'fts.db'}")
    Base.metadata.create_all(bind=engine)
    _migrate_search_columns(engine)
    _ensure_fts(engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    s = TestSession()
    try:
        _chunks.ensure_chunk_tables(s)
        s.commit()
        yield s, _cards
    finally:
        s.close()


def _indexe(s, cards, art_id, *, kind, name):
    """Blob + artefact seulement. Le lien vient après, puis la fiche — voir
    `_indexe_sous` : `ensure_card_on_import` indexe sous la portée de chaque lien
    déjà présent, donc l'ordre compte et c'est l'ordre de production."""
    s.add(
        Blob(
            id=f"sha_{art_id}",
            sha256=f"sha_{art_id}",
            relative_path=f"blobs/sha_{art_id}.bin",
            size_bytes=10,
            mime_detected="text/plain",
            state="available",
        )
    )
    s.add(
        Artifact(
            id=art_id,
            blob_id=f"sha_{art_id}",
            kind=kind,
            original_filename=name,
            display_name=name,
        )
    )
    s.flush()
    return art_id


def _indexe_sous(s, cards, art_id, scope_kind, scope_id):
    """Crée le lien, puis la fiche active, puis réindexe le contenu réel."""
    from backend.search import fts as _fts

    _link(s, art_id, scope_kind, scope_id)
    s.commit()
    art = s.query(Artifact).filter(Artifact.id == art_id).first()
    cards.ensure_card_on_import(s, art)
    s.commit()
    return (
        s.query(ArtifactCard)
        .filter(ArtifactCard.artifact_id == art_id, ArtifactCard.is_current == 1)
        .first()
    )


def test_recherche_espace_voit_le_corpus_complet_sans_rendre_de_fichier(db_fts):
    """L'information couvre tous les chats de l'espace ; le fichier, jamais."""
    from backend.search import space_info as SI

    s, cards = db_fts
    ws = E.create_workspace(s, "Alpha")
    _conv(s, "conv_1", ws=ws.id)
    _conv(s, "conv_2", ws=ws.id)

    # Un fichier dans le chat 1, un autre dans le chat 2, un troisième à l'espace.
    _indexe(s, cards, "art_c1", kind="text", name="c1.txt")
    _indexe(s, cards, "art_c2", kind="text", name="c2.txt")
    _indexe(s, cards, "art_usr", kind="text", name="consignes.txt")

    contenu = {
        "art_c1": (
            "Protocole de sauvegarde",
            "Procédure de sauvegarde et restauration du registre.",
            "sauvegarde",
        ),
        "art_c2": (
            "Note de reunion",
            "Compte rendu de la reunion de la semaine.",
            "reunion",
        ),
        "art_usr": (
            "Consignes projet",
            "Règles de nommage et protocole de sauvegarde.",
            "sauvegarde",
        ),
    }
    for art_id, (scope_kind, scope_id) in (
        ("art_c1", ("conversation", "conv_1")),
        ("art_c2", ("conversation", "conv_2")),
        ("art_usr", ("workspace", ws.id)),
    ):
        row = _indexe_sous(s, cards, art_id, scope_kind, scope_id)
        titre, resume, mots = contenu[art_id]
        row.title, row.summary, row.keywords_json, row.analysis_status = (
            titre,
            resume,
            mots,
            "indexed",
        )
        s.commit()
        from backend.search import fts as _fts

        _fts.index_card_row(
            s,
            s.query(Artifact).filter(Artifact.id == art_id).first(),
            row,
            scope_kind,
            scope_id,
        )
    s.commit()

    page = SI.search_space_information(s, ws.id, "sauvegarde", limit=10)
    assert page["ok"] is True and page["file_access"] is False
    assert page["total"] == 2  # c1 et le fichier utilisateur, pas la note de réunion
    assert {r["provenance"] for r in page["results"]} == {"c1.txt", "consignes.txt"}
    # Aucun artifact_id nulle part dans la réponse : un extrait n'est pas une
    # poignée sur le fichier.
    assert "artifact_id" not in str(page)
    # Sur les VALEURS : une clé ne peut pas commencer par "art_", seule une valeur
    # betrayerait un identifiant technique rendu au modèle.
    assert not any(
        v.startswith("art_") or v.startswith("src_")
        for r in page["results"]
        for v in r.values()
        if isinstance(v, str)
    )

    # Le garde-fou n°2, indépendant du premier. Matrice, ligne 2 : un chat
    # d'espace voit SES pièces jointes + les fichiers UTILISATEUR de l'espace —
    # jamais ceux d'un autre conversation du même espace.
    assert S.resolve_allowed_artifacts("conv_2") == {"art_c2", "art_usr"}
    refuse = S.check_artifact_scope(
        "art_c1", S.resolve_allowed_artifacts("conv_2"), exists=True
    )
    assert refuse is not None and refuse["code"] == "ID_OUT_OF_SCOPE"
    # L'information, elle, le voyait : c'est bien deux droits distincts.
    assert "c1.txt" in {r["provenance"] for r in page["results"]}


def test_recherche_espace_hors_perimetre_ne_fuit_rien(db_fts):
    """Un espace ne voit ni le contenu ni l'existence d'un autre espace."""
    from backend.search import fts as _fts
    from backend.search import space_info as SI

    s, cards = db_fts
    a, b = E.create_workspace(s, "Alpha"), E.create_workspace(s, "Beta")
    _indexe(s, cards, "art_a", kind="text", name="secret.txt")
    row = _indexe_sous(s, cards, "art_a", "workspace", a.id)
    row.title, row.summary, row.keywords_json, row.analysis_status = (
        "Secret Alpha",
        "Mot de passe du coffre de Alpha.",
        "secret",
        "indexed",
    )
    s.commit()
    _fts.index_card_row(
        s,
        s.query(Artifact).filter(Artifact.id == "art_a").first(),
        row,
        "workspace",
        a.id,
    )
    s.commit()

    page = SI.search_space_information(s, b.id, "secret", limit=10)
    assert page["total"] == 0 and page["results"] == []
    assert "coffre" not in str(page)  # ni le contenu, ni un indice d'existence
    # Le même document EST trouvé depuis son propre espace : la recherche ne
    # fuit rien par accident, elle refuse vraiment.
    assert SI.search_space_information(s, a.id, "secret", limit=10)["total"] == 1


# ---------------------------------------------------------------------------
# Non-régression : les trous trouvés à la revue, un test chacun
# ---------------------------------------------------------------------------


def test_partager_une_ressource_etrangere_est_refuse(db):
    """C1 — un partage ne peut pas faire sortir un fichier privé d'un espace.

    Le défaut : `share_artifact` ne vérifiait que l'EXISTENCE de l'artefact.
    anyone able to name an id could thus hand a private file of space B to
    space A, et toutes les conversations de A le voyaient ensuite.
    """
    a, b = E.create_workspace(db, "Alpha"), E.create_workspace(db, "Beta")
    _conv(db, "cA", ws=a.id)
    _conv(db, "cB", ws=b.id)
    _artifact(db, "art_priv_b")
    _link(db, "art_priv_b", "conversation", "cB")  # pièce jointe PRIVÉE de Beta

    assert S.resolve_allowed_artifacts("cA") == set()
    with pytest.raises(LookupError) as err:
        E.share_artifact(db, a.id, "art_priv_b", from_workspace_id=b.id)
    assert "ARTIFACT_NOT_FOUND" in str(err.value)
    assert S.resolve_allowed_artifacts("cA") == set(), "rien ne doit fuiter"

    # L'erreur ne confirme pas l'existence du fichier.
    with pytest.raises(LookupError) as inexistant:
        E.share_artifact(db, a.id, "art_qui_nexiste_pas", from_conv_id="cA")
    assert str(inexistant.value) == str(err.value)

    # Ni origine déclarée, ni partage : les deux sont refusés.
    with pytest.raises(ValueError):
        E.share_artifact(db, a.id, "art_priv_b")
    # Une conversation qui ne voit pas le fichier ne peut pas le partager.
    with pytest.raises(LookupError):
        E.share_artifact(db, a.id, "art_priv_b", from_conv_id="cA")


def test_partager_depuis_son_propre_perimetre_est_autorise(db):
    """Le contrepoint de C1 : le parcours normal fonctionne toujours."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    _artifact(db, "art_mien")
    _link(db, "art_mien", "conversation", "cA")
    out = E.share_artifact(db, ws.id, "art_mien", from_conv_id="cA")
    assert out["created"] is True and out["bytes_copied"] == 0
    assert S.resolve_allowed_artifacts("cA") == {"art_mien"}


def test_fusion_inter_espaces_refusee(db):
    """C2 — une fusion ne traverse pas les périmètres, et ne divulgue aucun id."""
    a, b = E.create_workspace(db, "Alpha"), E.create_workspace(db, "Beta")
    _conv(db, "cA", ws=a.id)
    _conv(db, "cB", ws=b.id)
    src_b = _source_nommee(db, b, "art_b", conv_id="cB")

    ea = _propose(db, a.id, "Fait de Alpha.", conv_id="cA")["entry_id"]
    M.accept(db, ea)
    eb = _propose(db, b.id, "Fait de Beta.", sources=[src_b], conv_id="cB")["entry_id"]
    M.accept(db, eb)

    with pytest.raises(ValueError) as err:
        M.merge(db, [ea, eb], "Fusion interdite.")
    assert "MERGE_CROSSES_WORKSPACES" in str(err.value)

    # Beta n'a pas bougé : sa fiche n'a pas été pushed stale en silence.
    versions_b = M.list_versions(db, eb)
    assert versions_b[0]["status"] == "accepted"
    # Aucun artifact_id de Beta dans l'espace d'Alpha.
    fiche_a = M.list_versions(db, M.search(db, a.id, "Alpha")["results"][0]["entry_id"])
    assert all(src_b not in v["sources"] for v in fiche_a)


def test_reproposer_avec_la_meme_cle_cree_une_version(db):
    """C3 — un `entry_key` stable veut dire « même fiche », pas « fiche sœur ».

    Le défaut : l'insertion violait l'index partiel et levait une
    `IntegrityError` qui sortait du contrat d'erreur typée du module.
    """
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    premiere = _propose(db, ws.id, "Fait A.", conv_id="cA")
    seconde = M.propose(
        db,
        ws.id,
        "Fait A révisé.",
        entry_key=premiere["entry_key"],
        origin_context="c",
        promotion_reasons=[{"kind": "outil"}],
    )
    assert premiere["created"] is True
    assert seconde["ok"] is True and seconde["version"] == 2
    versions = M.list_versions(db, premiere["entry_key"])
    assert [v["version"] for v in versions] == [2, 1]


def test_purge_refusee_sur_un_espace_actif(db):
    """M3 — la rétention de sept jours est un calendrier, pas une dispense."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    with pytest.raises(LookupError) as err:
        E.purge_workspace(db, ws.id)
    assert "WORKSPACE_NOT_TRASHED" in str(err.value)
    assert db.query(Conversation).filter(Conversation.id == "cA").first() is not None

    E.trash_workspace(db, ws.id)
    assert E.purge_workspace(db, ws.id)["conversations_demoted"] == 1


def test_espace_en_corbeille_n_accorde_plus_d_information(db):
    """M4 — les DEUX chemins d'accès disent non, pas l'un oui et l'autre non."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    _conv(db, "cB", ws=ws.id)  # n'a JAMAIS eu le fichier en pièce jointe
    art = _source_nommee(db, ws, "art_x", conv_id="cA")
    assert S.resolve_allowed_artifacts("cA") == {art}  # le sien + le partagé
    assert S.resolve_allowed_artifacts("cB") == {art}  # uniquement via l'espace
    assert S.resolve_space_information_scope(ws.id) == {art}

    E.trash_workspace(db, ws.id)
    # cA garde SON fichier — un corbeille d'espace ne déshérite personne.
    # cB, qui ne le tenait que par l'espace, ne le voit plus : c'est exactement
    # ce que la corbeille doit faire.
    assert S.resolve_allowed_artifacts("cA") == {art}
    assert S.resolve_allowed_artifacts("cB") == set()
    # `resolve_space_information_scope(workspace_id)` est l'API EXPLICITE, qui
    # assume que l'appelant a résolu le périmètre : comme
    # `resolve_allowed_artifacts(workspace_id=…)`, elle ne filtre pas l'état.
    # Ce qui compte est la porte que traversent les outils.
    assert E.conversation_workspace(db, "cB") is None
    assert E.conversation_workspace(db, "cB", include_trashed=True) == ws.id


def test_une_source_hors_perimetre_refuse_la_promotion(db):
    """M5 — « sources ACCESSIBLES », pas « sources existantes »."""
    a, b = E.create_workspace(db, "Alpha"), E.create_workspace(db, "Beta")
    _conv(db, "cA", ws=a.id)
    _conv(db, "cB", ws=b.id)
    prive_b = _source_nommee(db, b, "art_priv_b", conv_id="cB")

    ko = M.propose(
        db,
        a.id,
        "Un fait fondé sur un secret de Beta.",
        source_artifact_ids=[prive_b],
        origin_context="c",
        promotion_reasons=[{"kind": "outil"}],
    )
    assert ko["ok"] is False and ko["code"] == "SOURCE_NOT_ACCESSIBLE"
    assert ko["inaccessible_count"] == 1
    assert db.query(MemoryEntry).filter(MemoryEntry.workspace_id == a.id).count() == 0


def test_promotion_refusee_si_il_manque_un_champ_requis(db):
    """M8 — C17 « nécessite » contexte d'origine et raisons structurées."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    src = _partage_source(db, ws, "cA")

    # Sans contexte d'origine ni raison : la candidate existe…
    eid = M.propose(db, ws.id, "Fait sans provenance.", source_artifact_ids=[src])[
        "entry_id"
    ]
    assert eid  # une candidate peut être incomplète, et doit pouvoir l'être
    # …mais elle ne se promeut pas.
    ko = M.accept(db, eid)
    assert ko["ok"] is False and ko["code"] == "INCOMPLETE_FOR_PROMOTION"
    assert set(ko["missing"]) == {"origin_context", "promotion_reasons"}
    assert E.stats(db, ws.id)["memoire"] == 0


def test_les_sources_manquantes_empêchent_la_promotion(db):
    """M8 — une source absente est un motif de refus, pas un détail."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    src = _partage_source(db, ws, "cA")
    eid = _propose(db, ws.id, "Fait fondé sur une source.", sources=[src])["entry_id"]
    M.accept(db, eid)

    # La source disparaît APRÈS la promotion.
    db.query(Artifact).filter(Artifact.id == src).first().deleted_at = _now_utc()
    db.commit()
    # La fiche devient `stale` par une VERSION tracée, pas par une écriture en place.
    M.mark_stale(db, eid)
    courante = M.list_versions(db, eid)[0]
    assert courante["status"] == "stale"
    assert courante["decision"]["action"] == "mark_stale"
    assert courante["decision"]["actor"] == "system"


def test_correction_ne_reecrit_pas_une_version_perimee(db):
    """M2 — un snapshot n'est pas un champ modifiable, même pour ses sources."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    s1 = _partage_source(db, ws, "cA")
    eid = _propose(db, ws.id, "Fait fondé sur s1.", sources=[s1])["entry_id"]
    M.accept(db, eid)

    s2 = (
        _partage_source(db, ws, "cA2")
        if False
        else _source_nommee(db, ws, "art_s2", conv_id="cA")
    )
    avant = M.list_versions(db, eid)
    M.correct(db, eid, "Fait re-fonde sur s2.", source_artifact_ids=[s2])
    apres = M.list_versions(db, eid)

    # La version 2, déjà superseded, garde EXACTEMENT ses sources d'origine.
    v2_avant = next(v for v in avant if v["version"] == 2)
    v2_apres = next(v for v in apres if v["version"] == 2)
    assert v2_apres["sources"] == v2_avant["sources"] == [s1]
    # La version 3 porte les nouvelles.
    assert apres[0]["sources"] == [s2]
    assert apres[0]["version"] == 3


def test_curseur_invalide_si_le_classement_bouge(db):
    """M6 — pas de décalage silencieux entre deux pages."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    for i in range(12):
        _fiche(db, ws.id, f"k{i}", f"Fiche numero {i} sur le transport de la Palace.")

    page1 = M.search(db, ws.id, "transport Palace", limit=5)
    assert page1["has_more"] is True
    # Un usage réel change le classement entre les deux pages.
    M.record_usage(
        db,
        M.search(db, ws.id, "transport Palace")["results"][-1]["entry_id"],
        "msg_reponse_1",
    )

    page2 = M.search(
        db, ws.id, "transport Palace", limit=5, cursor=page1["next_cursor"]
    )
    assert page2["ok"] is False
    assert page2["code"] == "MEMORY_CURSOR_MISMATCH"
    # Relance explicite : les douze sont de nouveau atteignables, sans doublon.
    vus, cursor = set(), None
    while cursor is not None or not vus:
        p = M.search(db, ws.id, "transport Palace", limit=5, cursor=cursor)
        vus |= {r["entry_id"] for r in p["results"]}
        cursor = p["next_cursor"]
    assert len(vus) == 12


def test_curseur_de_page_sur_la_recherche_d_une_seule_page(db):
    """Un curseur d'une AUTRE recherche est refusé, pas suivi en silence."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    for i in range(7):
        _fiche(db, ws.id, f"k{i}", f"Fiche {i} sur le transport de la Palace.")
    autre = _fiche(db, ws.id, "kx", "Fiche sur le protocole de sauvegarde.")
    page = M.search(db, ws.id, "transport Palace", limit=5)
    melange = M.search(db, ws.id, "protocole", limit=5, cursor=page["next_cursor"])
    assert melange["ok"] is False and melange["code"] == "MEMORY_CURSOR_MISMATCH"
    assert autre


def test_erreur_de_recherche_d_espace_nest_pas_une_absence(db_fts, monkeypatch):
    """M7 — une panne ne doit pas se lire « rien à trouver »."""
    from backend.search import space_info as SI

    s, cards = db_fts
    ws = E.create_workspace(s, "Alpha")
    _conv(s, "cA", ws=ws.id)
    _indexe(s, cards, "art_x", kind="text", name="x.txt")
    row = _indexe_sous(s, cards, "art_x", "workspace", ws.id)
    row.title, row.summary, row.analysis_status = "Titre", "Un contenu.", "indexed"
    s.commit()
    from backend.search import fts as _fts

    _fts.index_card_row(
        s,
        s.query(Artifact).filter(Artifact.id == "art_x").first(),
        row,
        "workspace",
        ws.id,
    )
    s.commit()
    assert SI.search_space_information(s, ws.id, "contenu")["ok"] is True

    def _panne(*a, **k):
        raise RuntimeError("index indisponible")

    monkeypatch.setattr(_fts, "search", _panne)
    casse = SI.search_space_information(s, ws.id, "contenu")
    assert casse["ok"] is False
    assert casse["code"] == "SPACE_SEARCH_FAILED"
    # On signale la classe de panne, pas le message : un message d'exception
    # peut contenir un chemin ou une requête, et il traverse l'IPC.
    assert casse["scopes_failed"] == [
        "workspace:RuntimeError",
        "conversation:RuntimeError",
    ]


def test_extraits_de_recherche_d_espace_non_vides(db_fts):
    """M1 — l'information d'espace rend des EXTRAITS, pas seulement un nom."""
    from backend.search import space_info as SI

    s, cards = db_fts
    ws = E.create_workspace(s, "Alpha")
    _conv(s, "cA", ws=ws.id)
    _indexe(s, cards, "art_x", kind="text", name="x.txt")
    row = _indexe_sous(s, cards, "art_x", "workspace", ws.id)
    row.title, row.summary, row.analysis_status = (
        "Protocole de sauvegarde",
        "Procédure complète de sauvegarde et restauration.",
        "indexed",
    )
    s.commit()
    from backend.search import fts as _fts

    _fts.index_card_row(
        s,
        s.query(Artifact).filter(Artifact.id == "art_x").first(),
        row,
        "workspace",
        ws.id,
    )
    s.commit()

    page = SI.search_space_information(s, ws.id, "sauvegarde", limit=5)
    assert page["total"] == 1
    extrait = page["results"][0]["excerpt"]
    assert "sauvegarde" in extrait.lower()
    assert len(extrait) <= SI.MAX_EXCERPT_CHARS


def _now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_aucun_appelant_de_production_ne_transmet_un_perimetre():
    """L'invariant le plus fragile du lot.

    C18:264 : « Le backend constitue la frontière d'autorisation ». Toute la
    composition tient à ce que l'espace soit DÉDUIT de la conversation, jamais
    reçu. Un seul appelant qui transmettrait `workspace_id=` rouvrirait la
    frontière — et il serait invisible dans une relecture de fonction, puisque
    le paramètre existe et est légitime pour l'API explicite.
    """
    import inspect
    from pathlib import Path

    racine = Path(__file__).resolve().parents[2] / "backend"
    coupables: list[str] = []
    for chemin in racine.rglob("*.py"):
        if "tests" in chemin.parts or "search/scope.py" in str(chemin):
            continue
        for n, ligne in enumerate(chemin.read_text(encoding="utf-8").splitlines(), 1):
            if "resolve_allowed_artifacts(" not in ligne or ligne.lstrip().startswith(
                "#"
            ):
                continue
            if "workspace_id=" in ligne or ", workspace_id" in ligne:
                coupables.append(f"{chemin.relative_to(racine)}:{n}")
    assert coupables == [], (
        "ces appelants transmettent un périmètre au lieu de le laisser dériver : "
        f"{coupables}"
    )
    # Et la signature existe bien, pour l'API explicite (recherche d'espace, tests).
    assert "workspace_id" in inspect.signature(S.resolve_allowed_artifacts).parameters


# ---------------------------------------------------------------------------
# Deuxième tour de revue : les formes adjacentes que les tests n'exerçaient pas
# ---------------------------------------------------------------------------


def test_partager_vers_un_autre_espace_est_refuse(db):
    """#7 — nommer une portée qu'on contrôle ne franchit pas les espaces.

    La première correction demandait « le fichier est-il lié à la portée
    d'origine ? » sans demander si CETTE origine a un rapport avec la cible. Il
    suffisait donc d'écrire `from_workspace_id=<l'autre espace>`.
    """
    a, b = E.create_workspace(db, "Alpha"), E.create_workspace(db, "Beta")
    _conv(db, "cA", ws=a.id)
    _conv(db, "cB", ws=b.id)
    art = _artifact(db, "art_a")
    _link(db, art, "conversation", "cA")
    E.share_artifact(db, a.id, art, from_conv_id="cA")

    # Alpha -> Beta, en nommant Alpha comme origine : refusé.
    with pytest.raises(LookupError):
        E.share_artifact(db, b.id, art, from_workspace_id=a.id)
    # Alpha -> Beta, en nommant une conversation d'Alpha comme origine : refusé.
    with pytest.raises(LookupError):
        E.share_artifact(db, b.id, art, from_conv_id="cA")
    assert S.resolve_allowed_artifacts("cB") == set()
    # Le refus ne distingue toujours pas « ailleurs » de « inexistant ».
    with pytest.raises(LookupError) as inexistant:
        E.share_artifact(db, b.id, "art_inexistant", from_workspace_id=a.id)
    assert "ARTIFACT_NOT_FOUND" in str(inexistant.value)


def test_importer_depuis_un_chat_simple_vers_un_espace_reste_possible(db):
    """Le contrepoint de #7 : le « inviter des fichiers » doit fonctionner."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "hors_espace", ws=None)  # chat simple
    _conv(db, "cA", ws=ws.id)
    art = _artifact(db, "art_du_chat_simple")
    _link(db, art, "conversation", "hors_espace")
    out = E.share_artifact(db, ws.id, art, from_conv_id="hors_espace")
    assert out["created"] is True
    assert S.resolve_allowed_artifacts("cA") == {art}


def test_fusion_avec_un_identifiant_repete_ne_crashe_pas(db):
    """#1 — une même fiche deux fois faisait违反 uq_memory_current_entry."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    src = _partage_source(db, ws, "cA")
    a = _propose(db, ws.id, "Version A du protocole.", sources=[src])["entry_id"]
    b = _propose(db, ws.id, "Version B du protocole.", sources=[src])["entry_id"]
    M.accept(db, a)
    M.accept(db, b)

    with pytest.raises(ValueError) as err:
        M.merge(db, [a, a], "fusion")
    assert "MERGE_NEEDS_TWO" in str(err.value)
    # Rien n'a été écrit par l'appel refusé.
    assert M.list_versions(db, a, ws.id)[0]["status"] == "accepted"

    # Deux versions de la MÊME fiche : une seule, donc refus aussi.
    v1 = M.list_versions(db, a, ws.id)[-1]["entry_id"]
    with pytest.raises(ValueError):
        M.merge(db, [v1, a], "fusion")


def test_fusion_signale_les_identifiants_inconnus(db):
    """Une fusion qui ne prélève pas tout ce qu'on lui donne doit le dire."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    src = _partage_source(db, ws, "cA")
    a = _propose(db, ws.id, "A.", sources=[src])["entry_id"]
    b = _propose(db, ws.id, "B.", sources=[src])["entry_id"]
    M.accept(db, a)
    M.accept(db, b)
    with pytest.raises(ValueError) as err:
        M.merge(db, [a, "mem_inexistant", b], "fusion")
    assert "MERGE_UNKNOWN_ENTRY" in str(err.value)
    assert M.list_versions(db, a, ws.id)[0]["status"] == "accepted"


def test_fusion_passe_par_la_porte_de_promotion(db):
    """#8 — la fusion EST une promotion, donc elle exige la même provenance."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    # Deux candidates SANS provenance : accept() les refuse, la fusion aussi.
    x = M.propose(db, ws.id, "Sans provenance A.")["entry_id"]
    y = M.propose(db, ws.id, "Sans provenance B.")["entry_id"]
    ko = M.merge(db, [x, y], "Fusion sans provenance.")
    assert ko["ok"] is False and ko["code"] == "INCOMPLETE_FOR_PROMOTION"
    assert E.stats(db, ws.id)["memoire"] == 0


def test_purge_memoire_exige_le_bon_espace(db):
    """#3 — lire et supprimer hors périmètre, c'est deux fuites."""
    a, b = E.create_workspace(db, "Alpha"), E.create_workspace(db, "Beta")
    _conv(db, "cA", ws=a.id)
    _conv(db, "cB", ws=b.id)
    src_a = _partage_source(db, a, "cA")
    src_b = _partage_source(db, b, "cB")
    ea = _propose(db, a.id, "Privé de Alpha.", sources=[src_a])["entry_id"]
    eb = _propose(db, b.id, "Privé de Beta.", sources=[src_b])["entry_id"]
    M.accept(db, ea)
    M.accept(db, eb)

    with pytest.raises(LookupError):
        M.purge_memory(db, eb, a.id)  # Alpha ne purge pas Beta
    with pytest.raises(LookupError):
        M.list_versions(db, eb, a.id)  # ni n'en affiche la provenance
    with pytest.raises(ValueError):
        M.purge_memory(db, eb, None)  # et sans espace, pas de purge du tout
    assert M.list_versions(db, eb, b.id)[0]["content"] == "Privé de Beta."
    assert M.purge_memory(db, eb, b.id)["ok"] is True


def test_purge_memoire_accepte_la_cle_de_fiche(db):
    """#2 — la docstring annonçait les deux formes, le corps n'en acceptait qu'une."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    src = _partage_source(db, ws, "cA")
    out = _propose(db, ws.id, "Fait.", sources=[src])
    assert M.purge_memory(db, out["entry_key"], ws.id)["ok"] is True
    assert M.list_versions(db, out["entry_key"], ws.id) == []


def test_reproposition_conserve_la_cle_d_idempotence(db):
    """#5 — sans elle, un rejeu créait une version à chaque coup."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    src = _partage_source(db, ws, "cA")
    premiere = _propose(db, ws.id, "Fait.", sources=[src], key="CLE-X")
    assert premiere["ok"] is True

    # Rejeu de la MÊME clé : la garde d'idempotence rend la fiche existante, et
    # aucune version supplémentaire n'est créée.
    rejeu = M.propose(
        db,
        ws.id,
        "Fait révisé.",
        entry_key=premiere["entry_key"],
        source_artifact_ids=[src],
        origin_context="c",
        promotion_reasons=[{"kind": "outil"}],
        idempotency_key="CLE-X",
    )
    assert rejeu["created"] is False and rejeu["version"] == 1
    assert (
        db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == premiere["entry_key"])
        .count()
        == 1
    )

    # Proposition LÉGITIMEMENT nouvelle (autre clé) : elle passe par
    # `_new_version`, qui doit RECOMPOSER la clé au lieu de la perdre.
    M.propose(
        db,
        ws.id,
        "Fait révisé.",
        entry_key=premiere["entry_key"],
        source_artifact_ids=[src],
        origin_context="c",
        promotion_reasons=[{"kind": "outil"}],
        idempotency_key="CLE-Y",
    )
    cles = [
        r.idempotency_key
        for r in db.query(MemoryEntry)
        .filter(MemoryEntry.entry_key == premiere["entry_key"])
        .order_by(MemoryEntry.version)
        .all()
    ]
    assert cles == ["CLE-X", "CLE-Y"]


def test_une_fiche_refusee_ne_se_repropose_pas(db):
    """Le refus doit fermer la porte, pas seulement la fenêtre."""
    ws = E.create_workspace(db, "Alpha")
    _conv(db, "cA", ws=ws.id)
    src = _partage_source(db, ws, "cA")
    out = _propose(db, ws.id, "Fait contestable.", sources=[src])
    M.reject(db, out["entry_id"], motif="faux")
    ko = M.propose(
        db,
        ws.id,
        "Fait contestable,Bis.",
        entry_key=out["entry_key"],
        source_artifact_ids=[src],
        origin_context="c",
        promotion_reasons=[{"kind": "outil"}],
    )
    assert ko["ok"] is False and ko["code"] == "ALREADY_REJECTED"
    assert M.list_versions(db, out["entry_key"], ws.id)[0]["status"] == "rejected"


def test_provenance_jamais_un_identifiant_technique(db_fts):
    """#9 — un artefact sans nom de fichier rendait son `artifact_id` brut."""
    from backend.search import space_info as SI

    s, cards = db_fts
    ws = E.create_workspace(s, "Alpha")
    _conv(s, "cA", ws=ws.id)
    s.add(
        Blob(
            id="sha_x",
            sha256="sha_x",
            relative_path="b/x",
            size_bytes=4,
            mime_detected="text/plain",
            state="available",
        )
    )
    s.add(
        Artifact(
            id="art_sans_nom",
            blob_id="sha_x",
            kind="text",
            original_filename="",
            display_name="",
        )
    )
    s.commit()
    row = _indexe_sous(s, cards, "art_sans_nom", "workspace", ws.id)  # crée le lien
    row.title, row.summary, row.analysis_status = "Titre", "Contenu_eu.", "indexed"
    s.commit()
    from backend.search import fts as _fts

    _fts.index_card_row(
        s,
        s.query(Artifact).filter(Artifact.id == "art_sans_nom").first(),
        row,
        "workspace",
        ws.id,
    )
    s.commit()

    page = SI.search_space_information(s, ws.id, "contenu", limit=5)
    assert page["total"] == 1
    assert page["results"][0]["provenance"] == "document"
    assert "art_sans_nom" not in str(page)


# ---------------------------------------------------------------------------
# Renommage d'un fichier du dépôt — `depot.md` §5.2
# ---------------------------------------------------------------------------


def test_renommage_change_le_nom_affiche_et_actualise_la_fiche(db_fts):
    """§5.2 — le nom affiché bouge, le contenu et les extraits restent intacts.

    « Le moteur de recherche doit actualiser les métadonnées utiles au
    repérage par nom ou titre » : la fiche redevient servie à `find_files`
    avec son TITRE à jour, sans aucune réextraction — l'aperçu déjà indexé
    est préservé, `original_filename` garde l'identité du fichier.
    """
    import json
    from sqlalchemy import text as _text

    s, cards = db_fts
    ws = E.create_workspace(s, "Alpha")
    _indexe(s, cards, "art_1", kind="text", name="factures.txt")
    card1 = _indexe_sous(s, cards, "art_1", "workspace", ws.id)
    art = s.query(Artifact).filter(Artifact.id == "art_1").first()
    # Un extrait DÉJÀ indexé, porté par la ligne depôt — ce que le renommage
    # doit préserver au lieu de reconstruire (une image reconstruite par
    # `_text_preview` serait condamnée à tort).
    cards.reindexer_carte(
        s,
        art,
        card1,
        "workspace",
        ws.id,
        extracted_preview="Somme trimestrielle budget",
    )
    s.commit()
    avant = s.execute(_text("SELECT extracted_preview FROM artifact_search")).scalar()

    r = E.renommer_depot(s, ws.id, "art_1", "Factures du T3 2026.pdf")
    s.commit()

    assert r["changed"] is True and r["display_name"] == "Factures du T3 2026.pdf"
    art = s.query(Artifact).filter(Artifact.id == "art_1").first()
    assert art.display_name == "Factures du T3 2026.pdf"
    # §5.2 contraire à toute réextraction : l'original n'est pas réécrit.
    assert art.original_filename == "factures.txt"

    versions = (
        s.query(ArtifactCard)
        .filter(ArtifactCard.artifact_id == "art_1")
        .order_by(ArtifactCard.revision.asc())
        .all()
    )
    assert [c.revision for c in versions] == [1, 2]  # jamais d'écrasement
    courante = versions[1]
    assert courante.is_current == 1 and courante.title == "Factures du T3 2026.pdf"
    assert versions[0].is_current == 0
    # Les mots-clés GARDENT l'ancien nom et gagnent les stems du nouveau :
    # la recherche continue de raccrocher par les deux.
    mots = json.loads(courante.keywords_json or "[]")
    assert "factures" in mots and "t3" in mots and "2026" in mots
    # Tout le reste est COPIÉ, jamais redécidé.
    for champ in ("summary", "entities_json", "detected_language", "quality"):
        assert getattr(courante, champ) == getattr(versions[0], champ)
    (row,) = (
        s.execute(
            _text(
                "SELECT title, filename, card_revision, extracted_preview "
                "FROM artifact_search"
            )
        )
        .mappings()
        .all()
    )
    assert row["title"] == "Factures du T3 2026.pdf"
    assert row["filename"] == "factures.txt"  # nom d'origine : toujours mappé
    assert row["card_revision"] == 2
    assert row["extracted_preview"] == avant == "Somme trimestrielle budget"
    # La fiche n'est PAS condamnée : un renommage ne parle pas du contenu.
    assert (courante.status or "pending") != "failed"
    # L'inventaire du dépôt reflète déjà le nouveau nom.
    fichier = E.depot(s, ws.id)["files"][0]
    assert fichier["display_name"] == "Factures du T3 2026.pdf"


def test_renommage_meme_nom_ne_cree_pas_de_revision_inutile(db_fts):
    """Idempotent : renommer vers le nom courant est un non-évènement."""
    import json
    from sqlalchemy import text as _text

    s, cards = db_fts
    ws = E.create_workspace(s, "Alpha")
    _indexe(s, cards, "art_1", kind="text", name="notes.txt")
    _indexe_sous(s, cards, "art_1", "workspace", ws.id)
    revision_avant = s.execute(
        _text("SELECT card_revision FROM artifact_search")
    ).scalar()
    n_before = s.query(ArtifactCard).filter(ArtifactCard.artifact_id == "art_1").count()

    r = E.renommer_depot(s, ws.id, "art_1", "notes.txt")

    assert r["changed"] is False
    assert (
        s.query(ArtifactCard).filter(ArtifactCard.artifact_id == "art_1").count()
        == n_before
    )
    assert (
        s.execute(_text("SELECT card_revision FROM artifact_search")).scalar()
        == revision_avant
    )


def test_renommage_refus_nommes(db_fts):
    """Un renommage qui ne dit pas QUI ni QUOI est refusé, jamais fabriqué."""
    s, cards = db_fts
    ws, autre = E.create_workspace(s, "Alpha"), E.create_workspace(s, "Beta")
    _indexe(s, cards, "art_1", kind="text", name="doc.txt")
    _indexe_sous(s, cards, "art_1", "workspace", ws.id)

    with pytest.raises(ValueError, match=r"\[NAME_REQUIRED\]"):
        E.renommer_depot(s, ws.id, "art_1", "   ")
    with pytest.raises(LookupError, match=r"\[WORKSPACE_NOT_FOUND\]"):
        E.renommer_depot(s, "ws_absent", "art_1", "Nouveau nom")
    with pytest.raises(LookupError, match=r"\[LINK_NOT_FOUND\]"):
        E.renommer_depot(s, autre.id, "art_1", "Nouveau nom")
    # Espace en corbeille : plus d'écriture.
    E.trash_workspace(s, ws.id)
    with pytest.raises(LookupError, match=r"\[WORKSPACE_NOT_FOUND\]"):
        E.renommer_depot(s, ws.id, "art_1", "Nouveau nom")
    # Lien vivant mais artefact détruit : le refus est le MÊME que pour un
    # nom inverse — dire « ce document a existé » le confirmerait.
    gamma = E.create_workspace(s, "Gamma")
    _indexe(s, cards, "art_2", kind="text", name="alogne.txt")
    _indexe_sous(s, cards, "art_2", "workspace", gamma.id)
    s.query(Artifact).filter(Artifact.id == "art_2").update({"deleted_at": E._now()})
    s.commit()
    with pytest.raises(LookupError, match=r"\[LINK_NOT_FOUND\]"):
        E.renommer_depot(s, gamma.id, "art_2", "Nouveau nom")
