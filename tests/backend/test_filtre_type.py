"""tests/backend/test_filtre_type.py — le filtre `types` ne doit JAMAIS échouer ouvert.

DÉFUT REPRODUIT ICI : `fts.search()` traduisait une liste de `types` en kinds, puis
n'ajoutait sa clause SQL que si l'ensemble obtenu était non vide. Un `type` demandé
qui ne se traduisait vers aucun kind laissait donc l'ensemble vide, donc AUCUNE
clause, donc TOUS les types en réponse — un filtre demandé et jamais appliqué, rendu
indiscernable d'une recherche sans filtre. Demander l'impossible répondait « voici
tout », jamais « je ne sais pas de quoi tu parles ».

Deux pièges de plus, tous deux couverts ici :

  - « tableur » ne se résout pas par `Artifact.kind` mais par
    `ArtifactCard.document_type`, le seul discriminant normalisé ET persisté. Voir
    `_TYPE_TO_DOCUMENT_TYPES` dans `backend/search/fts.py` pour le pourquoi ; ici on
    vérifie surtout qu'un filtre RÉSOLU trouve, parce qu'un filtre garanti vide
    serait un résultat vide silencieux — le défaut qu'on répare, déplacé d'un cran.
  - La recherche hybride (`_hybrid_rerank`) REMET dans la liste des artefacts venus
    des fragments, APRÈS la boucle de candidats. Un filtre posé dans cette boucle
    se laisserait passer par cette porte de derrière.

Ce que ces tests verrouillent :
  - `types` ABSENT → pas de filtre (un appelant peut légitimement ne rien filtrer) ;
  - `types=[]` VIDE → PAS de filtre non plus, et c'est un cas LÉGITIME distinct de
    « une valeur inconnue » : l'appelant a dit « aucun critère », pas « un critère
    que je ne comprends pas » ;
  - `types=["tableur_inconnu"]` → erreur NOMMÉE `[ENUM_INVALID]`, pas un résultat.

Sans modèle, sans GPU, sans réseau : DB SQLite temporaire + FTS5 réel, blobs écrits
dans un `tmp_path` — la base réelle n'est jamais touchée.
"""
from __future__ import annotations

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
    MessageAttachment,
)
from backend.search import cards as C
from backend.search import chunks as Chunks
from backend.search import fts as F

# Le mot que portent les artefacts trouvables PAR LE NOM. Sans lui, la preuve
# « pas de filtre » serait triviale : une requête sans correspondance ne prouve rien.
MOT = "rapport"
CLASSEUR = "art_classeur"
EXPORT_CSV = "art_export_csv"
IMAGE = "art_image"
# Un second classeur dont le NOM ne contient pas le mot : il n'est donc PAS une
# ligne FTS du `search`, mais OUI un fragment indexé — c'est ce qui le fait entrer
# par la voie hybride. Il sert au test de la porte de derrière.
CLASSEUR_FRAGMENT = "art_classeur_fragment"

TOUS = {CLASSEUR, EXPORT_CSV, IMAGE, CLASSEUR_FRAGMENT}


@pytest.fixture()
def depot(tmp_path, monkeypatch):
    """Quatre fichiers d'un même dossier : deux classeurs, un CSV, une image."""
    db_file = tmp_path / "filtre_type.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    from backend.db.database import _ensure_fts, _migrate_search_columns
    _migrate_search_columns(engine)
    _ensure_fts(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    _s = Session()
    try:
        # Les fragments servent à la recherche HYBRIDE. Ils sont donc créés ici,
        # sans quoi la porte de derrière que `test_…derriere…` doit surveiller
        # n'existerait pas et le test ne prouverait rien.
        Chunks.ensure_chunk_tables(_s)
        _s.commit()
    finally:
        _s.close()
    monkeypatch.setattr("backend.db.database.SessionLocal", Session)
    data_dir = tmp_path / "data"
    (data_dir / "blobs" / "ab").mkdir(parents=True)

    # `kind` = ce que rend `sniff_mime` : `image`, `pdf` ou `sheet` — et rien
    # d'autre. Un `.csv` y est donc un `sheet`, ce sur quoi repose `test_csv_…`.
    semences = (
        (CLASSEUR, "sheet", f"{MOT}_budget_2026.xlsx", "PK\x03\x04"),
        (EXPORT_CSV, "sheet", f"{MOT}_ventes.csv", "client;montant"),
        (IMAGE, "image", f"{MOT}_menu_restaurant.png", "\x89PNG\r\n\x1a\n"),
        (CLASSEUR_FRAGMENT, "sheet", "annexes_financieres.xlsx", "PK\x03\x04"),
    )
    db = Session()
    try:
        if db.get(Conversation, "conv1") is None:
            db.add(Conversation(id="conv1", title="t", mode="chat"))
        for art_id, kind, nom, octets in semences:
            sha = f"sha-{art_id}"
            (data_dir / "blobs" / "ab" / f"{art_id}.bin").write_bytes(octets.encode())
            db.add(Blob(id=sha, sha256=sha, relative_path=f"blobs/ab/{art_id}.bin",
                        size_bytes=len(octets), mime_detected="application/octet-stream",
                        state="available"))
            db.add(Artifact(id=art_id, blob_id=sha, kind=kind,
                            original_filename=nom, display_name=nom))
            db.add(ArtifactLink(id=f"lnk-{art_id}", artifact_id=art_id,
                                scope_kind="conversation", scope_id="conv1", role="attachment"))
            db.add(Message(id=f"msg-{art_id}", conv_id="conv1", role="user",
                           content="voici un fichier", run_id=f"run-{art_id}", status="done"))
            db.add(MessageAttachment(id=f"ma-{art_id}", message_id=f"msg-{art_id}",
                                     artifact_id=art_id, ordinal=1))
        db.commit()
        # Fiches + lignes d'index : le chemin réel (`ensure_card_on_import` écrit
        # `document_type = kind`), donc le filtre s'exerce sur une donnée PERSISTÉE.
        for art_id, _, _, _ in semences:
            C.ensure_card_on_import(db, db.get(Artifact, art_id))
        # Fragment indexé du seul classeur invisible par le nom : son texte porte le
        # mot, son nom non. Le rerank hybride le fusionnera donc dans les résultats
        # alors qu'aucune ligne FTS de `search` ne l'a jamais vu.
        n = Chunks.ensure_chunks_for_artifact(
            db, db.get(Artifact, CLASSEUR_FRAGMENT),
            f"Prévisions {MOT} du second semestre.", [("conversation", "conv1")])
        db.commit()
        assert n >= 1, "le fragment doit exister, sinon le test de la porte de derrière est vide"
    finally:
        db.close()
    return Session


def _rechercher(Session, types, *, via_scopes=False):
    db = Session()
    try:
        if via_scopes:
            return F.search_scopes(db, query=MOT, conversation_id="conv1",
                                   allowed_ids=set(TOUS), types=types)
        return F.search(db, query=MOT, scope_kind="conversation", scope_id="conv1",
                        allowed_ids=set(TOUS), types=types)
    finally:
        db.close()


def _trouve(page) -> set[str]:
    assert page["ok"], page
    return {r["artifact_id"] for r in page["results"]}


# ---------- LE DÉFAUT : une valeur inconnue ne vaut pas « pas de filtre » ----------

def test_type_inconnu_refuse_nomme(depot):
    """`types=["tableur_inconnu"]` doit LEVER `[ENUM_INVALID]`.

    Avant correction, aucune exception n'était levée et la page revenait `ok=True`
    avec TOUS les fichiers — la preuve que le filtre demandé n'avait jamais été
    appliqué et que le demandeur n'en était pas informé.
    """
    with pytest.raises(ValueError, match=r"ENUM_INVALID") as echec:
        _rechercher(depot, ["tableur_inconnu"])
    # Le message doit NOMMER la valeur refusée et l'enum : c'est cette phrase que
    # l'appelant reformule. Un « filtre ignoré » sans nom ne lui donnerait rien à
    # corriger, et il retryait à l'identique.
    assert "tableur_inconnu" in str(echec.value)


def test_type_inconnu_melange_a_un_type_valide_refuse(depot):
    """Une liste à moitié comprise est une liste incomprise.

    Appliquer la moitié connue en taisant l'autre serait le même défaut, en plus
    discret : le demandeur croirait avoir filtré par image ET par tableur, et n'aurait
    filtré que par image — sans un mot pour l'en informer.
    """
    with pytest.raises(ValueError, match=r"ENUM_INVALID"):
        _rechercher(depot, ["image", "tableur_inconnu"])


def test_enum_inconnue_ne_se_deguisse_pas_en_panne_de_portee(depot):
    """`search_scopes` doit rendre l'erreur d'argument, pas « recherche indisponible ».

    Une enum hors liste est indépendante de la portée : la laisser remonter par le
    `except Exception` de la boucle la maquillerait en panne multi-portée — une panne
    INVENTÉE pour un argument mal fourni, qui enverrait l'appelant relancer
    indéfiniment au lieu de reformuler son filtre.
    """
    with pytest.raises(ValueError, match=r"ENUM_INVALID"):
        _rechercher(depot, ["tableur_inconnu"], via_scopes=True)


def test_type_inconnu_ne_rend_pas_une_page_vide(depot):
    """Contre-épreuve : l'erreur ne doit JAMAIS se déguiser en page vide.

    Une page `ok=True` sans résultat pour un type inconnu serait un second défaut du
    même genre — le demandeur lirait « je n'ai rien trouvé » au lieu de « ce type
    n'existe pas chez moi ». Ce test échouerait si quelqu'un traduisait l'enum
    invalide par un filtre `document_type IN ()`, c'est-à-dire par un filtre VIDE :
    exactement le piège qu'on refuse.
    """
    with pytest.raises(ValueError, match=r"ENUM_INVALID"):
        _rechercher(depot, ["tableur_inconnu"])


# ---------- LES TROIS CAS DE `types` ----------

@pytest.mark.parametrize("types", [None, []], ids=["absent", "liste_vide"])
def test_absent_ou_vide_ne_filtre_pas(depot, types):
    """Pas de filtre demandé → pas de filtre appliqué, et ça se VOIT dans `filters`.

    Une liste vide est une demande légitime (« aucun critère »), pas une valeur
    inconnue : la confondre avec « tableur_inconnu » interdirait à un appelant de
    chercher large alors qu'il l'a demandé.
    """
    page = _rechercher(depot, types)
    assert _trouve(page) == TOUS
    # `filters.types` reste `None` pour les deux : rien n'a été demandé, donc rien ne
    # doit laisser croire qu'un filtre a été appliqué puis rendu vide.
    assert page["filters"]["types"] is None


# ---------- LE DISCRIMINANT : `document_type`, pas `kind` ----------

def test_filtre_resolvable_classeur_et_image(depot):
    """Un filtre qui se résout DOIT trouver : c'est la contre-épreuve de « vide ».

    Réclamer un tableur et ne rien obtenir serait un résultat vide silencieux —
    exactement le défaut déplacé d'un cran. Réclamer une image doit de même remonter
    l'image et rien d'autre.
    """
    assert _trouve(_rechercher(depot, ["sheet"])) == {CLASSEUR, EXPORT_CSV, CLASSEUR_FRAGMENT}
    assert _trouve(_rechercher(depot, ["image"])) == {IMAGE}


def test_csv_est_un_tableur_et_non_un_texte(depot):
    """`csv` doit désigner `document_type="sheet"`, jamais `"text"`.

    `sniff_mime` classe un `.csv` en `sheet` : c'est donc le tableur que le demandeur
    désigne. Traduire `csv` en `text` produirait un filtre GARANTI VIDE pour tout
    dépôt réel, puisque `text` n'est le `kind` d'aucun fichier déposé — le modèle se
    ferait répondre « aucun tableur » alors que le classeur est là, sous un autre nom.
    C'est le piège que ce test verrouille : il échouait avec un `set()` vide avant
    correction.
    """
    assert _trouve(_rechercher(depot, ["csv"])) == {CLASSEUR, EXPORT_CSV, CLASSEUR_FRAGMENT}


def test_alias_tableur_vient_du_vocabulaire_de_l_utilisateur(depot):
    """« tableur » est le mot de l'utilisateur ; `sheet` celui de la fiche.

    Le filtre doit comprendre le mot français sans que l'appelant ait à connaître le
    vocabulaire interne — sinon le modèle écrit « tableur », l'enum le refuse, et le
    refus devient une occasion de reformuler au lieu d'une réponse.
    """
    assert _trouve(_rechercher(depot, ["tableur"])) == _trouve(_rechercher(depot, ["sheet"]))


def test_filtre_resolu_exclure_bien_les_autres(depot):
    """Le filtre exclut VRAIMENT : rien ne doit ressortir d'un filtre d'un autre type.

    Une preuve d'exclusion, pas seulement d'inclusion : « trouver le bon » peut être
    accidentel quand le corpus ne contient qu'un candidat, « ne pas trouver l'autre »
    ne peut pas l'être.
    """
    image_seule = _trouve(_rechercher(depot, ["image"]))
    assert image_seule == {IMAGE}
    assert CLASSEUR not in image_seule and EXPORT_CSV not in image_seule


def test_le_filtre_ferme_la_porte_de_derriere_du_rerank(depot):
    """Un artefact réinjecté par l'hybride ne doit pas contourner le filtre.

    `_hybrid_rerank` Rajoute dans `ranked` des artefacts venus des fragments, après
    la boucle de candidats : un filtre posé dans cette boucle ne les verrait jamais.
    Le test vérifie donc les DEUX moitiés — que la porte est ouverte (sans filtre, le
    classeur-fantôme revient bien par les fragments) ET qu'elle est fermée (avec un
    filtre image, il ne revient plus). Sans la première assertion, la seconde ne
    prouverait rien.
    """
    assert CLASSEUR_FRAGMENT in _trouve(_rechercher(depot, None))  # la porte est ouverte
    assert CLASSEUR_FRAGMENT not in _trouve(_rechercher(depot, ["image"]))  # et fermée
    # Il reste trouvé par SON type : on l'a exclu de la mauvaise page, pas du moteur.
    assert CLASSEUR_FRAGMENT in _trouve(_rechercher(depot, ["sheet"]))


def test_discriminant_persiste_et_non_rederive(depot):
    """Le filtre porte sur la DONNÉE PERSISTÉE, pas sur l'extension du nom.

    `document_type` est écrit par la fiche (`ensure_card_on_import`) et vit dans
    `artifact_cards`. Plusieurs appelants redérivent le type depuis le nom au moment
    de l'afficher ; si le filtre faisait de même, un fichier renommé changerait de
    type sous l'index — et l'appelant n'en verrait jamais la raison.
    """
    db = depot()
    try:
        from backend.db.models import ArtifactCard as Card
        for art_id, kind in ((CLASSEUR, "sheet"), (EXPORT_CSV, "sheet"),
                             (IMAGE, "image"), (CLASSEUR_FRAGMENT, "sheet")):
            card = db.query(Card).filter(Card.artifact_id == art_id).first()
            assert card is not None, art_id
            assert card.document_type == kind, (art_id, card.document_type)
    finally:
        db.close()
