"""Tests de la couche IPC des espaces — le dernier kilomètre vers l'écran.

`backend/ipc/espaces.py` existed without a single test, alors qu'il est
exactement l'endroit où une fuite se glisserait : c'est lui qui décide ce que le
renderer voit. Un service correct peut avoir une IPC qui rend le mauvais champ,
renseigne mal une clé, ou transforme un refus en liste vide.

Ces tests vérifient donc trois choses que les tests du service ne vérifient pas :
  1. la traduction IPC — arguments camelCase, retours snake_case, dates ISO ;
  2. l'IDENTITÉ DES CLÉS, parce que le pont Electron unwrape `RESULT_KEY` et
     qu'une clé renommée casse l'écran sans casser le service ;
  3. le refus — un espace inconnu ne doit JAMAIS rendre une liste vide, sinon
     « aucun espace » et « espace introuvable » deviennent la même réponse.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    Message,
    Workspace,
)
from backend.ipc import espaces as IPC
from backend.services import activity
from backend.services import espaces as service
from backend.services import memoire as memoire_service


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ipc.db'}")
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


def _espace(db, nom="Refonte du site web") -> Workspace:
    return service.create_workspace(db, nom, "Design et structure.")


def _conv_avec_fichier(db, workspace, conv_id="conv_1", art_id="art_1", nom="note.txt"):
    db.add(
        Conversation(
            id=conv_id,
            title="Structure de la page",
            mode="chat",
            workspace_id=workspace.id,
        )
    )
    db.add(
        Blob(
            id=f"sha_{art_id}",
            sha256=f"sha_{art_id}",
            relative_path=f"b/{art_id}",
            size_bytes=120,
            mime_detected="text/plain",
            state="available",
        )
    )
    db.add(
        Artifact(
            id=art_id,
            blob_id=f"sha_{art_id}",
            kind="text",
            original_filename=nom,
            display_name=nom,
        )
    )
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}",
            artifact_id=art_id,
            scope_kind="conversation",
            scope_id=conv_id,
            role="attachment",
        )
    )
    # Le même document est VERSÉ dans l'espace. Sans ce second lien il n'entrerait
    # pas dans `stats["fichiers"]` : le dépôt ne compte que ce que l'utilisateur y
    # a déposé, pas les pièces jointes des discussions (voir `depot()`).
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}_ws",
            artifact_id=art_id,
            scope_kind="workspace",
            scope_id=workspace.id,
            role="shared",
        )
    )
    db.commit()


# --- Traduction et identité des clés -----------------------------------------


def test_espace_list_rend_un_tableau_de_cles_attendues(db):
    """Le pont Electron unwrape `RESULT_KEY['espace_list'] = 'workspaces'`.

    Si la clé change, l'écran reçoit `{count: n}` la où il attend une liste — et
    rien ne lève côté service. Ce test verrouille donc la clé, pas la valeur.
    """
    _espace(db, "Alpha")
    _espace(db, "Beta")
    payload = IPC.espace_list(db, {})
    assert set(payload) == {"workspaces", "count"}
    assert payload["count"] == 2
    assert {e["name"] for e in payload["workspaces"]} == {"Alpha", "Beta"}
    for entree in payload["workspaces"]:
        assert set(entree) == {
            "id",
            "name",
            "description",
            "state",
            "created_at",
            "updated_at",
            "stats",
        }


def test_espace_conversations_rend_la_cle_conversations(db):
    """`RESULT_KEY['espace_conversations'] = 'conversations'` : même raison."""
    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    payload = IPC.espace_conversations(db, {"workspaceId": ws.id})
    assert set(payload) == {"conversations"}
    assert payload["conversations"][0]["title"] == "Structure de la page"


def test_dates_serialisables_et_stats_natives(db):
    """Tout doit survivre au `json.dumps` du protocole.

    Un `datetime` brut ferait échouer l'émission entière, pas un seul champ : la
    page entière disparaîtrait sans message.
    """
    import json

    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    memoire_service.accept(
        db,
        memoire_service.propose(
            db,
            ws.id,
            "Le site utilise SQLite.",
            origin_context="c",
            promotion_reasons=[{"kind": "outil"}],
        )["entry_id"],
    )

    for payload in (
        IPC.espace_list(db, {}),
        IPC.espace_get(db, {"workspaceId": ws.id}),
        IPC.espace_stats(db, {"workspaceId": ws.id}),
        IPC.espace_conversations(db, {"workspaceId": ws.id}),
    ):
        json.dumps(payload)  # ne doit pas lever


def test_stats_exposent_les_quatre_statuts_de_c17(db):
    """Les maquettes affichent « Active » : l'écart est visible dès la clé.

    On verrouille `memoire_par_statut` ET son contenu, parce que l'écran 05-1
    s'appuie dessus pour.detailer les statuts.
    """
    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    out = IPC.espace_stats(db, {"workspaceId": ws.id})
    assert set(out["memoire_par_statut"]) == {
        "candidate",
        "accepted",
        "stale",
        "rejected",
    }
    assert "active" not in out["memoire_par_statut"]
    assert out["discussions"] == 1 and out["fichiers"] == 1
    assert out["memoire"] == 0  # une fiche simply不接受 ne devient pas mémoire


def test_memoire_acceptee_comptee_separement_des_candidates(db):
    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    source = "art_1"
    eid = memoire_service.propose(
        db,
        ws.id,
        "Fait validé.",
        source_artifact_ids=[source],
        origin_context="c",
        promotion_reasons=[{"kind": "o"}],
    )["entry_id"]
    candidat = memoire_service.propose(
        db,
        ws.id,
        "Fait en attente.",
        source_artifact_ids=[source],
        origin_context="c",
        promotion_reasons=[{"kind": "o"}],
    )["entry_id"]
    memoire_service.accept(db, eid)
    stats = IPC.espace_stats(db, {"workspaceId": ws.id})
    assert stats["memoire"] == 1
    assert stats["memoire_par_statut"]["accepted"] == 1
    assert stats["memoire_par_statut"]["candidate"] == 1
    assert candidat


# --- Refus : jamais de liste vide maquillée en panne --------------------------


def test_espace_inconnu_refuse_sur_toutes_les_commandes(db):
    """Un id deviné ne doit rendre ni liste vide ni `None` silencieux.

    C'est le point que l'écran ne peut pas rattraper : sans refus, « aucun
    espace » et « espace introuvable » sont la même image.
    """
    for appel in (
        lambda: IPC.espace_get(db, {"workspaceId": "ws_fantome"}),
        lambda: IPC.espace_stats(db, {"workspaceId": "ws_fantome"}),
        lambda: IPC.espace_conversations(db, {"workspaceId": "ws_fantome"}),
        lambda: IPC.espace_depot(db, {"workspaceId": "ws_fantome"}),
        lambda: IPC.espace_depot_retirer(
            db, {"workspaceId": "ws_fantome", "artifactId": "art_1"}
        ),
    ):
        with pytest.raises(ValueError) as err:
            appel()
        assert "WORKSPACE_UNKNOWN" in str(err.value)


def test_espace_en_corbeille_reste_lisible_pour_permettre_la_restauration(db):
    """La corbeille doit être RÉCUPÉRABLE, donc lisible — avec son état visible.

    C'est le contrepoint de `test_espace_inconnu_refuse_sur_toutes_les_commandes` :
    un espace en corbeille n'est pas un secret, c'est celui de l'utilisateur, et
    il doit pouvoir être restauré. Ce qui compte est que `state` soit rendu, sans
    quoi l'écran ne peut ni proposer « Restaurer » ni expliquer l'absence de la
    carte dans la liste.
    """
    ws = _espace(db)
    service.trash_workspace(db, ws.id)
    out = IPC.espace_get(db, {"workspaceId": ws.id})
    assert out["workspace"]["state"] == "trashed"
    assert out["workspace"]["id"] == ws.id
    # Il reste absent de la liste : la corbeille n'est pas la page d'accueil.
    assert ws.id not in {e["id"] for e in IPC.espace_list(db, {})["workspaces"]}
    # Et il est restaurable.
    service.restore_workspace(db, ws.id)
    assert IPC.espace_get(db, {"workspaceId": ws.id})["workspace"]["state"] == "active"


def test_id_absant_refuse_au_lieu_de_lister(db):
    """Sans id, il n'y a rien à désigner : refuser, pas lister « tout »."""
    for params in ({}, {"workspaceId": ""}, {"workspaceId": "   "}):
        with pytest.raises(ValueError):
            IPC.espace_get(db, params)


def test_espace_list_exclut_la_corbeille(db):
    """Un espace en corbeille n'est pas restaurable par accident depuis la liste."""
    actif = _espace(db, "Actif")
    poubelle = _espace(db, "En corbeille")
    service.trash_workspace(db, poubelle.id)
    noms = {e["name"] for e in IPC.espace_list(db, {})["workspaces"]}
    assert noms == {"Actif"}
    assert actif.id in {e["id"] for e in IPC.espace_list(db, {})["workspaces"]}


def test_espace_vide_rend_bien_une_liste_vide(db):
    """Le contrepoint : un espace RÉELLEMENT vide doit rendre une liste vide.

    C'est la paire avec le refus ci-dessus. Sans elle, on « corrigerait » un id
    inconnu en renvoyant `[]`, et l'écran ne pourrait plus distinguer les deux.
    """
    ws = _espace(db)
    assert IPC.espace_conversations(db, {"workspaceId": ws.id}) == {"conversations": []}
    assert IPC.espace_list(db, {})["count"] == 1


# --- Commandes et arguments ----------------------------------------------------


def test_nom_vide_refuse_par_le_service(db):
    """L'IPC n'invente pas d'erreur : elle relaie le refus du service."""
    with pytest.raises(ValueError) as err:
        IPC.espace_create(db, {"name": "   "})
    assert "NAME_REQUIRED" in str(err.value)


def test_creation_rend_un_espace_deja_decompte(db):
    """La carte affichée après création ne doit pas afficher zéro partout.

    L'écran navigue direct vers l'espace créé : s'il ne reçoit pas les compteurs
    dans la même réponse, il affiche « 0 discussions » sur un espace neuf.
    """
    out = IPC.espace_create(
        db, {"name": "Mon projet", "description": "Une description."}
    )
    assert out["name"] == "Mon projet"
    assert out["description"] == "Une description."
    assert set(out["stats"]) >= {"discussions", "fichiers", "octets", "memoire"}
    assert out["stats"]["discussions"] == 0


def test_arguments_acceptes_en_camel_et_en_snake(db):
    """L'ancien pont envoyait du camelCase ; le service raisonne en snake_case."""
    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    for params in ({"workspaceId": ws.id}, {"workspace_id": ws.id}):
        assert IPC.espace_conversations(db, params)["conversations"]


def test_les_conversations_les_plus_recentes_d_abord(db):
    """Le tri se fait en SQL : `updated_at` décroissant, `id` en départage."""
    ws = _espace(db)
    db.add(
        Conversation(id="c_ancienne", title="Ancienne", mode="chat", workspace_id=ws.id)
    )
    db.add(
        Conversation(id="c_recente", title="Récente", mode="chat", workspace_id=ws.id)
    )
    db.commit()
    db.execute(
        __import__("sqlalchemy").text(
            "UPDATE conversations SET updated_at='2024-05-01' WHERE id='c_ancienne'"
        )
    )
    db.execute(
        __import__("sqlalchemy").text(
            "UPDATE conversations SET updated_at='2024-06-01' WHERE id='c_recente'"
        )
    )
    db.commit()
    titres = [
        c["title"]
        for c in IPC.espace_conversations(db, {"workspaceId": ws.id})["conversations"]
    ]
    assert titres == ["Récente", "Ancienne"]


def test_aucun_espace_ne_fuit_vers_un_autre_perimetre(db):
    """Deux espaces, deux dépôts : la lecture d'un ne rend pas les fichiers de l'autre."""
    a, b = _espace(db, "Alpha"), _espace(db, "Beta")
    _conv_avec_fichier(db, a, conv_id="cA", art_id="art_a", nom="de-alpha.txt")
    _conv_avec_fichier(db, b, conv_id="cB", art_id="art_b", nom="de-beta.txt")

    entrees = {e["id"]: e for e in IPC.espace_list(db, {})["workspaces"]}
    assert entrees[a.id]["stats"]["fichiers"] == 1
    assert entrees[b.id]["stats"]["fichiers"] == 1
    # Le dépôt d'Alpha ne doit nommer que le fichier d'Alpha.
    contenu = service.depot(db, a.id)
    assert {f["display_name"] for f in contenu["files"]} == {"de-alpha.txt"}


# --- Le dépôt de l'espace : les deux commandes de la fiche -----------------------


def test_espace_depot_rend_exactement_les_cinq_cles_attendues(db):
    """`listWorkspaceDepot` lit `{workspace_id, files, hidden, conversation_count, is_physical_folder}`.

    Le pont Electron renvoie le payload tel quel, sans unwrapping : une clé
    renommée ne casse rien côté service et fait lire `undefined` côté écran. Ce
    test verrouille donc les cinq clés, et rien d'autre — une clé de plus
    rendrait la forme du payload ambigüe pour le prochain lecteur.

    `hidden` est la cinquième pour une raison de fond, pas de comptage : c'est
    elle qui dit à l'écran ce que le dépôt n'affiche pas. Un dépôt sain la rend
    vide, et c'est cette clé qui empêche un dépôt amputé de se lire complet.
    """
    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    payload = IPC.espace_depot(db, {"workspaceId": ws.id})
    assert set(payload) == {
        "workspace_id",
        "files",
        "hidden",
        "conversation_count",
        "is_physical_folder",
    }
    assert payload["workspace_id"] == ws.id
    assert payload["conversation_count"] == 1
    assert payload["is_physical_folder"] is False
    assert payload["hidden"] == []
    assert [f["display_name"] for f in payload["files"]] == ["note.txt"]


def test_espace_depot_d_un_espace_vide_rend_un_depot_vide(db):
    """Le contrepoint du refus : un espace RÉELLEMENT vide rend une liste vide.

    Sans ce test, on « corrigerait » un id inconnu en renvoyant `[]`, et l'écran
    ne distinguerait plus une panne d'un dépôt réellement vide.
    """
    ws = _espace(db)
    payload = IPC.espace_depot(db, {"workspaceId": ws.id})
    assert payload["files"] == [] and payload["conversation_count"] == 0


def test_espace_depot_retirer_rend_les_quatre_cles_attendues(db):
    """Le retrait est un `removed_at` : `{ok, link_id, already_removed, blob_preserved}`.

    `already_removed` se lit à l'écran pour distinguer « retiré » de « déjà
    retiré », et `blob_preserved` pour rappeler que les octets, eux, restent. Deux
    clés absentes feraient mentir l'un des deux messages.
    """
    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    payload = IPC.espace_depot_retirer(
        db, {"workspaceId": ws.id, "artifactId": "art_1"}
    )
    assert set(payload) == {"ok", "link_id", "already_removed", "blob_preserved"}
    assert payload["ok"] is True
    assert payload["link_id"] == "lnk_art_1_ws"
    assert payload["already_removed"] is False
    assert payload["blob_preserved"] is True
    assert service.depot(db, ws.id)["files"] == []


def test_espace_depot_retirer_sans_artifact_id_refuse(db):
    """Sans id de document, il n'y a rien à désigner : refus, pas « tout retirer »."""
    ws = _espace(db)
    _conv_avec_fichier(db, ws)
    for params in (
        {"workspaceId": ws.id},
        {"workspaceId": ws.id, "artifactId": ""},
        {"workspaceId": ws.id, "artifactId": "   "},
    ):
        with pytest.raises(ValueError) as err:
            IPC.espace_depot_retirer(db, params)
        assert "LINK_NOT_FOUND" in str(err.value)
    assert [f["display_name"] for f in service.depot(db, ws.id)["files"]] == [
        "note.txt"
    ]


def test_un_document_d_un_autre_espace_ne_se_devine_pas(db):
    """Le refus ne distingue pas « lien inconnu » de « lien d'un autre espace ».

    Y verser l'id d'un document d'un espace voisin ne doit pas répondre « absent »
    là où il répondrait « présent ailleurs » : deux messages le confirmeraient, et
    il suffirait d'itérer des identifiants pour inventorier les dépôts.
    """
    a, b = _espace(db, "Alpha"), _espace(db, "Beta")
    _conv_avec_fichier(db, a, conv_id="cA", art_id="art_a", nom="de-alpha.txt")
    _conv_avec_fichier(db, b, conv_id="cB", art_id="art_b", nom="de-beta.txt")

    with pytest.raises(LookupError) as ailleurs:
        IPC.espace_depot_retirer(db, {"workspaceId": a.id, "artifactId": "art_b"})
    with pytest.raises(LookupError) as nulle_part:
        IPC.espace_depot_retirer(db, {"workspaceId": a.id, "artifactId": "art_fantome"})
    assert "LINK_NOT_FOUND" in str(ailleurs.value)
    assert str(ailleurs.value) == str(nulle_part.value)
    assert [f["display_name"] for f in service.depot(db, b.id)["files"]] == [
        "de-beta.txt"
    ]


# --- Déplacement d'une conversation (§7.3) -----------------------------------


def test_espace_conversation_deplacer_evalue_puis_sans_rapport_fabrique(db):
    """Sans `confirme`, le retour est le rapport : rien n'est modifié, rien n'inventé."""
    a, b = _espace(db, "Alpha"), _espace(db, "Beta")
    _conv_avec_fichier(db, a, conv_id="cA", art_id="art_a", nom="de-alpha.txt")

    rapport = IPC.espace_conversation_deplacer(
        db,
        {
            "conversationId": "cA",
            "targetWorkspaceId": b.id,
        },
    )
    assert rapport["allowed"] is True and "applied" not in rapport
    assert rapport["contributions"] == ["art_a"]
    assert (
        db.query(Conversation).filter(Conversation.id == "cA").first().workspace_id
        == a.id
    )

    # Le refus est NÉ aux arguments : pas d'évaluation fabriquée sur rien.
    for params in (
        {},
        {"targetWorkspaceId": b.id},
        {"conversationId": "cA"},
        {"conversationId": "cA", "targetWorkspaceId": ""},
    ):
        with pytest.raises(ValueError):
            IPC.espace_conversation_deplacer(db, params)
    assert (
        db.query(Conversation).filter(Conversation.id == "cA").first().workspace_id
        == a.id
    )


def test_espace_conversation_deplacer_confirme_applique_le_deplacement(db):
    """`confirme` s'écrit dans l'appel : le rattachement change, une seule fois."""
    a, b = _espace(db, "Alpha"), _espace(db, "Beta")
    _conv_avec_fichier(db, a, conv_id="cA", art_id="art_a", nom="de-alpha.txt")

    argv = {"conversationId": "cA", "targetWorkspaceId": b.id, "confirme": True}
    out = IPC.espace_conversation_deplacer(db, argv)
    assert out.get("applied") is True
    assert service.conversation_workspace(db, "cA") == b.id

    # Relire en évaluation au SUIVANT de l'acte : déjà en destination.
    rapport = IPC.espace_conversation_deplacer(
        db,
        {
            "conversationId": "cA",
            "targetWorkspaceId": b.id,
        },
    )
    assert rapport["changed"] is False
    # `confirme: "true"` n'est pas un accord : la lecture est booléenne stricte.
    assert (
        db.query(Conversation).filter(Conversation.id == "cA").first().workspace_id
        == b.id
    )


# --- Renommage d'un document du dépôt (depot.md §5.2) ------------------------


def _fts_aligne(db):
    """Le même chemin que le fichier test_espaces.py : FTS5 + colonnes migrées."""
    from backend.db.database import _ensure_fts, _migrate_search_columns

    engine = db.get_bind()
    _migrate_search_columns(engine)
    _ensure_fts(engine)


def test_espace_depot_renommer_renvoie_le_nom_actualise(db):
    """Le retour dit QUI et QUOI ; le changement efface l'étiquette, rien d'autre."""
    ws = _espace(db, "Alpha")
    _conv_avec_fichier(db, ws, conv_id="cA", art_id="art_1", nom="notes.txt")

    payload = IPC.espace_depot_renommer(
        db,
        {
            "workspaceId": ws.id,
            "artifactId": "art_1",
            "nouveauNom": "Notes de suivi.txt",
        },
    )
    assert payload == {
        "ok": True,
        "artifact_id": "art_1",
        "changed": True,
        "display_name": "Notes de suivi.txt",
    }
    art = db.query(Artifact).filter(Artifact.id == "art_1").first()
    assert art.display_name == "Notes de suivi.txt"
    # §5.2 : le contenu n'est pas altéré — l'original porte l'identité.
    assert art.original_filename == "notes.txt"


def test_espace_depot_renommer_actualise_le_titre_indexe(db):
    """Le « moteur de recherche » sert la fiche révisée : titre neuf, nom originel."""
    from sqlalchemy import text as _text

    _fts_aligne(db)
    ws = _espace(db, "Alpha")
    _conv_avec_fichier(db, ws, conv_id="cA", art_id="art_1", nom="notes.txt")

    IPC.espace_depot_renommer(
        db,
        {
            "workspaceId": ws.id,
            "artifactId": "art_1",
            "nouveauNom": "Suivi projet.pdf",
        },
    )
    db.commit()
    rows = (
        db.execute(_text("SELECT title, filename FROM artifact_search"))
        .mappings()
        .all()
    )
    assert len(rows) == 2  # une ligne d'index par lien actif (conversation + dépôt)
    assert all(r["title"] == "Suivi projet.pdf" for r in rows)
    assert all(r["filename"] == "notes.txt" for r in rows)


def test_espace_depot_renommer_refuse_ce_qu_il_ne_peut_pas_nommer(db):
    """Les deux arguments sont nommés à l'entrée : aucun résultat fabriqué."""
    a, b = _espace(db, "Alpha"), _espace(db, "Beta")
    _conv_avec_fichier(db, a, conv_id="cA", art_id="art_1", nom="de-alpha.txt")

    # Rien à désigner du tout : le refus de la porte d'espace.
    with pytest.raises(ValueError, match=r"\[WORKSPACE_UNKNOWN\]"):
        IPC.espace_depot_renommer(db, {})
    # Le document est nommé mais le nom non : renommage refusé, jamais inventé.
    with pytest.raises(ValueError, match=r"\[NAME_REQUIRED\]"):
        IPC.espace_depot_renommer(
            db, {"workspaceId": a.id, "artifactId": "art_1", "nouveauNom": "   "}
        )
    # L'identifiant manquant produit le refus d'un lien, pas celui d'un fichier.
    with pytest.raises(ValueError, match=r"LINK_NOT_FOUND"):
        IPC.espace_depot_renommer(db, {"workspaceId": a.id, "nouveauNom": "Nouveau"})
    # L'espace est en corbeille : la porte d'espace refuse avant toute écriture.
    service.trash_workspace(db, a.id)
    avec_corbeille = {
        "workspaceId": a.id,
        "artifactId": "art_1",
        "nouveauNom": "Nouveau",
    }
    with pytest.raises(LookupError, match=r"\[WORKSPACE_NOT_FOUND\]"):
        IPC.espace_depot_renommer(db, avec_corbeille)
    # Le document d'un autre espace ne se renomme pas : un seul refus, comme pour
    # le retrait — un id deviné n'apprend rien.
    with pytest.raises(LookupError) as ailleurs:
        IPC.espace_depot_renommer(
            db,
            {
                "workspaceId": b.id,
                "artifactId": "art_1",
                "nouveauNom": "d'un autre.txt",
            },
        )
    assert "LINK_NOT_FOUND" in str(ailleurs.value)


def test_espace_depot_renommer_lit_le_nom_sous_ses_deux_formes(db):
    """camelCase (renderer) comme snake_case : le même verbe, une seule lecture."""
    ws = _espace(db, "Alpha")
    _conv_avec_fichier(db, ws, conv_id="cA", art_id="art_1", nom="notes.txt")

    payload = IPC.espace_depot_renommer(
        db,
        {
            "workspace_id": ws.id,
            "artifact_id": "art_1",
            "nouveau_nom": "Notes en snake.txt",
        },
    )
    assert payload["display_name"] == "Notes en snake.txt"


# --- Promotion d'un fichier de discussion vers le dépôt (espaces.md §6) ------


def _pièce_de_chat(db, conv_id="cA", art_id="img_1", workspace=None):
    """Une discussion (rattachée ou non), une image, et le lien de discussion SEUL."""
    db.add(
        Conversation(
            id=conv_id,
            title="Le phare",
            mode="chat",
            workspace_id=(workspace.id if workspace else None),
        )
    )
    db.add(
        Blob(
            id=f"sha_{art_id}",
            sha256=f"sha_{art_id}",
            relative_path=f"b/{art_id}",
            size_bytes=2048,
            mime_detected="image/png",
            state="available",
        )
    )
    db.add(
        Artifact(
            id=art_id,
            blob_id=f"sha_{art_id}",
            kind="image",
            original_filename="phare.png",
            display_name="phare.png",
        )
    )
    db.add(
        ArtifactLink(
            id=f"lnk_{art_id}_conv",
            artifact_id=art_id,
            scope_kind="conversation",
            scope_id=conv_id,
            role="generated",
        )
    )
    db.commit()


def test_espace_conversation_partager_cree_le_lien_du_depot(db):
    """La cible est l'espace DE LA discussion : l'id du client ne donne rien."""
    ws = _espace(db, "Alpha")
    _pièce_de_chat(db, conv_id="cA", art_id="img_1", workspace=ws)

    payload = IPC.espace_conversation_partager(
        db, {"conversationId": "cA", "artifactId": "img_1"}
    )
    assert payload["ok"] is True and payload["created"] is True
    assert payload["artifact_id"] == "img_1"
    # Le dépôt de l'espace liste le fichier ; l'image RÉPOSE à sa place en galerie
    # (aucun octet copié — le blob reste seul).
    fichiers = service.depot(db, ws.id)["files"]
    assert [f["artifact_id"] for f in fichiers] == ["img_1"]

    # Idempotent : repartager le même fichier ne duplique rien.
    encore = IPC.espace_conversation_partager(
        db, {"conversationId": "cA", "artifactId": "img_1"}
    )
    assert encore["created"] is False and encore["link_id"] == payload["link_id"]


def test_espace_conversation_partager_refuse_ce_qui_ne_peut_pas_entrer(db):
    """Refus nommés à l'entrée : jamais un résultat fabriqué sur rien."""
    ws = _espace(db, "Alpha")
    _pièce_de_chat(db, conv_id="cA", art_id="img_1", workspace=ws)
    _pièce_de_chat(db, conv_id="maree", art_id="img_no_ws")  # hors de tout espace

    # Ni discussion, ni fichier : rien à verser.
    with pytest.raises(ValueError, match=r"\[CONVERSATION_REQUIRED\]"):
        IPC.espace_conversation_partager(db, {"artifactId": "img_1"})
    with pytest.raises(ValueError, match=r"\[ARTIFACT_REQUIRED\]"):
        IPC.espace_conversation_partager(db, {"conversationId": "cA"})
    # Une discussion hors espace n'a PAS de dépôt à alimenter : le refus est
    # clair, jamais une invention de cible.
    with pytest.raises(LookupError, match=r"\[CONVERSATION_SANS_ESPACE\]"):
        IPC.espace_conversation_partager(
            db, {"conversationId": "maree", "artifactId": "img_no_ws"}
        )
    # Le fichier qui ne relève pas de la discussion ne se verse pas.
    _pièce_de_chat(db, conv_id="cb", art_id="img_2", workspace=ws)
    with pytest.raises(LookupError, match=r"ARTIFACT_NOT_FOUND"):
        IPC.espace_conversation_partager(
            db, {"conversationId": "cA", "artifactId": "img_2"}
        )
    # Un workspaceId explicite qui contredit celui de la discussion : refus du
    # service, pas une promotion silencieuse vers l'autre espace.
    faux = _espace(db, "Beta")
    with pytest.raises(LookupError, match=r"ARTIFACT_NOT_FOUND"):
        IPC.espace_conversation_partager(
            db, {"conversationId": "cA", "artifactId": "img_1", "workspaceId": faux.id}
        )


def test_espace_conversation_partager_lit_ses_arguments_snake_case(db):
    """camelCase (renderer) et snake_case : le même verbe, une seule lecture."""
    ws = _espace(db, "Alpha")
    _pièce_de_chat(db, conv_id="cA", art_id="img_1", workspace=ws)

    payload = IPC.espace_conversation_partager(
        db, {"conversation_id": "cA", "artifact_id": "img_1"}
    )
    assert payload["ok"] is True and payload["artifact_id"] == "img_1"


# --- L'état d'ingestion visible dans le dépôt (depot.md §6) ------------------


def test_espace_depot_expose_l_etat_d_ingestion_par_fichier(db):
    """§6 : « L'état d'ingestion est visible et distingue disponibilité,
    traitement partiel et échec. » Une ligne par fichier, par le SEUL chemin
    de lecture (`etat.lire`) — jamais une déduction sur la présence d'une
    observation : le défaut mesuré était exactement là.
    """
    from backend.ingestion import etat as sorte

    ws = _espace(db, "Alpha")
    _conv_avec_fichier(db, ws, conv_id="cA", art_id="art_1", nom="illisible.pdf")
    _conv_avec_fichier(db, ws, conv_id="cB", art_id="art_2", nom="holo.txt")
    # L'écriture passe par LE SEUL point d'écriture du relevé d'ingestion.
    sorte.marquer(db, "art_1", sorte.ETAT_ECHEC, "Aucun texte extrait de ce PDF.")

    fichier = {
        f["artifact_id"]: f
        for f in IPC.espace_depot(db, {"workspaceId": ws.id})["files"]
    }
    assert fichier["art_1"]["ingest_state"] == "echec"
    assert fichier["art_1"]["ingest_reason"] == "Aucun texte extrait de ce PDF."
    # Jamais traité : l'état reste celui du relevé, sans invention de « prêt ».
    assert fichier["art_2"]["ingest_state"] == "inconnu"
    assert fichier["art_2"]["ingest_reason"] == ""


# --- Mémoire d'espace : lecture, forçage, correction, suppression ----------


def _message_utilisateur(db, workspace, conv_id="cM", msg_id="mM"):
    """Une discussion et un message que l'extraction déterministe sait classer."""
    db.add(
        Conversation(id=conv_id, title="Projet", mode="chat", workspace_id=workspace.id)
    )
    db.add(
        Message(
            id=msg_id,
            conv_id=conv_id,
            role="user",
            content="On utilise SQLite comme base de données.",
            created_at=datetime(2026, 10, 25, 12, 0, 0),
        )
    )
    db.commit()


def test_espace_memoire_rend_elements_et_synthese(db):
    """L'onglet lit les deux dimensions : les fiches ET la synthèse courante.

    Un espace vide rend une synthèse `None`, jamais un objet partiel : l'écran
    distingue « pas encore de synthèse » de « synthèse vide ».
    """
    ws = _espace(db)
    payload = IPC.espace_memoire(db, {"workspaceId": ws.id})
    assert set(payload) == {"elements", "synthese", "entretien"}
    assert payload["elements"] == []
    assert payload["synthese"] is None


def test_espace_memoire_forcer_extrait_les_faits_et_la_synthese(db):
    """Le forçage suit le chemin déterministe : messages → fiches → synthèse."""
    ws = _espace(db)
    _message_utilisateur(db, ws)

    payload = IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    assert set(payload) == {"elements", "synthese", "rafraichissement", "entretien"}
    assert payload["rafraichissement"]["statut"] == "effectue"
    assert [e["content"] for e in payload["elements"]] == [
        "On utilise SQLite comme base de données."
    ]
    assert payload["synthese"] is not None
    assert "SQLite" in payload["synthese"]["content"]


def test_espace_memoire_corriger_cree_une_version_et_une_synthese(db):
    """Corriger ne réécrit pas : la nouvelle version remplace l'ancienne."""
    ws = _espace(db)
    _message_utilisateur(db, ws)
    IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    fiche = IPC.espace_memoire(db, {"workspaceId": ws.id})["elements"][0]

    payload = IPC.espace_memoire_corriger(
        db,
        {
            "workspaceId": ws.id,
            "entryId": fiche["entry_id"],
            "content": "On utilise PostgreSQL comme base de données.",
            "expectedVersion": fiche["version"],
        },
    )
    assert payload["correction"]["ok"] is True
    assert payload["correction"]["version"] == fiche["version"] + 1
    assert [e["content"] for e in payload["elements"]] == [
        "On utilise PostgreSQL comme base de données."
    ]
    assert payload["synthese"] is not None
    assert "PostgreSQL" in payload["synthese"]["content"]


def test_espace_memoire_corriger_avec_version_perimee_leve_et_ne_touche_pas_la_synthese(
    db,
):
    """Un conflit de révision n'est pas un succès : erreur typée, état inchangé."""
    ws = _espace(db)
    _message_utilisateur(db, ws)
    IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    fiche = IPC.espace_memoire(db, {"workspaceId": ws.id})["elements"][0]
    synthese_avant = IPC.espace_memoire(db, {"workspaceId": ws.id})["synthese"]

    with pytest.raises(ValueError, match=r"\[REVISION_CONFLICT\]"):
        IPC.espace_memoire_corriger(
            db,
            {
                "workspaceId": ws.id,
                "entryId": fiche["entry_id"],
                "content": "On utilise PostgreSQL comme base de données.",
                "expectedVersion": fiche["version"] + 1,
            },
        )

    elements = IPC.espace_memoire(db, {"workspaceId": ws.id})["elements"]
    assert [e["content"] for e in elements] == [
        "On utilise SQLite comme base de données."
    ]
    synthese_apres = IPC.espace_memoire(db, {"workspaceId": ws.id})["synthese"]
    assert synthese_apres == synthese_avant


def test_espace_memoire_corriger_refuse_une_version_non_entier(db):
    """`expectedVersion` non entier est un refus typé, pas une exception brute."""
    ws = _espace(db)
    _message_utilisateur(db, ws)
    IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    fiche = IPC.espace_memoire(db, {"workspaceId": ws.id})["elements"][0]

    with pytest.raises(ValueError, match=r"\[INVALID_VERSION\]"):
        IPC.espace_memoire_corriger(
            db,
            {
                "workspaceId": ws.id,
                "entryId": fiche["entry_id"],
                "content": "On utilise PostgreSQL comme base de données.",
                "expectedVersion": "pas-un-entier",
            },
        )


def test_espace_memoire_corriger_refuse_une_fiche_d_un_autre_espace(db):
    """`workspaceId` borne l'écriture : une fiche voisine n'est pas corrigée."""
    a, b = _espace(db, "Alpha"), _espace(db, "Beta")
    _message_utilisateur(db, a)
    IPC.espace_memoire_forcer(db, {"workspaceId": a.id})
    fiche = IPC.espace_memoire(db, {"workspaceId": a.id})["elements"][0]

    with pytest.raises(LookupError, match=r"\[MEMORY_NOT_IN_WORKSPACE\]"):
        IPC.espace_memoire_corriger(
            db,
            {
                "workspaceId": b.id,
                "entryId": fiche["entry_id"],
                "content": "Correction illégitime.",
            },
        )
    assert [
        e["content"] for e in IPC.espace_memoire(db, {"workspaceId": a.id})["elements"]
    ] == ["On utilise SQLite comme base de données."]


def test_espace_memoire_supprimer_exclut_durablement_le_contenu(db):
    """Supprimer dépose une exclusion : un second forçage ne recrée pas la fiche."""
    ws = _espace(db)
    _message_utilisateur(db, ws)
    IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    fiche = IPC.espace_memoire(db, {"workspaceId": ws.id})["elements"][0]

    payload = IPC.espace_memoire_supprimer(
        db,
        {
            "workspaceId": ws.id,
            "entryId": fiche["entry_id"],
        },
    )
    assert set(payload) == {"elements", "synthese", "suppression"}
    assert payload["suppression"]["excluded"] is True
    assert payload["elements"] == []
    assert memoire_service.est_exclu(db, ws.id, fiche["content"]) is True

    # Le second forçage relit les MÊMES messages : l'exclusion tient.
    ws = db.query(Workspace).filter(Workspace.id == ws.id).first()
    ws.memory_last_processed_at = None
    ws.auto_memory_turn_count = 0
    db.commit()
    encore = IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    assert encore["elements"] == []


def test_espace_memoire_rafraichir_accepte_seuil_messages_sans_type_error(db):
    """Le seuil camelCase est lu, et aucun `tour_count` ne fuit vers le service."""
    ws = _espace(db)
    payload = IPC.espace_memoire_rafraichir(
        db,
        {
            "workspaceId": ws.id,
            "contenu": "On utilise SQLite comme base de données.",
            "seuilMessages": 5,
        },
    )
    assert set(payload) == {"elements", "synthese", "rafraichissement", "entretien"}
    assert payload["rafraichissement"]["statut"] == "attente"
    assert payload["rafraichissement"]["seuil_messages"] == 5


def test_espace_memoire_expose_en_attente_pour_cinq_tours_et_activite_recente(db):
    """L'onglet Mémoire a un statut durable `en_attente` quand le seuil est
    atteint mais que l'activité est trop récente pour lancer l'entretien."""
    ws = _espace(db)
    maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
    ws.auto_memory_turn_count = 5
    ws.last_activity_at = maintenant - timedelta(seconds=30)
    db.commit()

    entretien = IPC.espace_memoire(db, {"workspaceId": ws.id})["entretien"]
    assert entretien["tours"] == 5
    assert entretien["etat"] == "en_attente"
    assert entretien["raison"] == "activite_recente"


def test_espace_memoire_corriger_differe_pendant_un_entretien(db):
    """Le verrou mémoire tenu : corriger ne doit RIEN écrire."""
    ws = _espace(db)
    _message_utilisateur(db, ws)
    IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    fiche = IPC.espace_memoire(db, {"workspaceId": ws.id})["elements"][0]

    verrou = activity.try_reserver("memoire", f"memoire:{ws.id}")
    assert verrou is not None
    try:
        payload = IPC.espace_memoire_corriger(
            db,
            {
                "workspaceId": ws.id,
                "entryId": fiche["entry_id"],
                "content": "On utilise PostgreSQL comme base de données.",
                "expectedVersion": fiche["version"],
            },
        )
        assert set(payload) == {
            "ok",
            "statut",
            "raison",
            "elements",
            "synthese",
            "entretien",
        }
        assert payload["ok"] is False
        assert payload["statut"] == "differe"
        assert payload["raison"] == "verrou_occupe"
        assert [e["content"] for e in payload["elements"]] == [fiche["content"]]
    finally:
        activity.liberer(verrou)

    # Verrou libéré : la même correction passe.
    payload = IPC.espace_memoire_corriger(
        db,
        {
            "workspaceId": ws.id,
            "entryId": fiche["entry_id"],
            "content": "On utilise PostgreSQL comme base de données.",
            "expectedVersion": fiche["version"],
        },
    )
    assert payload["correction"]["ok"] is True
    assert [e["content"] for e in payload["elements"]] == [
        "On utilise PostgreSQL comme base de données."
    ]


def test_espace_memoire_supprimer_differe_pendant_un_entretien(db):
    """Le verrou mémoire tenu : la fiche et son exclusion restent intactes."""
    ws = _espace(db)
    _message_utilisateur(db, ws)
    IPC.espace_memoire_forcer(db, {"workspaceId": ws.id})
    fiche = IPC.espace_memoire(db, {"workspaceId": ws.id})["elements"][0]

    verrou = activity.try_reserver("memoire", f"memoire:{ws.id}")
    assert verrou is not None
    try:
        payload = IPC.espace_memoire_supprimer(
            db, {"workspaceId": ws.id, "entryId": fiche["entry_id"]}
        )
        assert payload["ok"] is False
        assert payload["statut"] == "differe"
        assert payload["raison"] == "verrou_occupe"
        assert [e["entry_id"] for e in payload["elements"]] == [fiche["entry_id"]]
        assert memoire_service.est_exclu(db, ws.id, fiche["content"]) is False
    finally:
        activity.liberer(verrou)

    # Verrou libéré : suppression et exclusion passent.
    payload = IPC.espace_memoire_supprimer(
        db, {"workspaceId": ws.id, "entryId": fiche["entry_id"]}
    )
    assert payload["suppression"]["excluded"] is True
    assert payload["elements"] == []


def test_espace_memoire_rafraichir_explicite_differe_pendant_un_entretien(db):
    """Un contenu explicite hors entretien doit respecter le verrou global."""
    ws = _espace(db)
    _message_utilisateur(db, ws)

    verrou = activity.try_reserver("memoire", f"memoire:{ws.id}")
    assert verrou is not None
    try:
        payload = IPC.espace_memoire_rafraichir(
            db,
            {
                "workspaceId": ws.id,
                "contenu": "On utilise PostgreSQL comme base de données.",
                "force": True,
            },
        )
        assert payload["ok"] is False
        assert payload["statut"] == "differe"
        assert payload["raison"] == "verrou_occupe"
        assert payload["elements"] == []
    finally:
        activity.liberer(verrou)

    # Verrou libéré : l'écriture explicite passe.
    payload = IPC.espace_memoire_rafraichir(
        db,
        {
            "workspaceId": ws.id,
            "contenu": "On utilise PostgreSQL comme base de données.",
            "force": True,
        },
    )
    assert payload["rafraichissement"]["statut"] == "rafraichi"
    assert [e["content"] for e in payload["elements"]] == [
        "On utilise PostgreSQL comme base de données."
    ]


def test_espace_en_corbeille_refuse_les_ecritures_de_memoire(db):
    """Un espace en corbeille n'accepte ni forçage, ni correction, ni suppression."""
    ws = _espace(db)
    service.trash_workspace(db, ws.id)

    for appel in (
        lambda: IPC.espace_memoire_forcer(db, {"workspaceId": ws.id}),
        lambda: IPC.espace_memoire_corriger(
            db, {"workspaceId": ws.id, "entryId": "e", "content": "x"}
        ),
        lambda: IPC.espace_memoire_supprimer(
            db, {"workspaceId": ws.id, "entryId": "e"}
        ),
    ):
        with pytest.raises(LookupError, match=r"\[WORKSPACE_NOT_FOUND\]"):
            appel()
