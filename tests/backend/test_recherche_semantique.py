"""Tests de la recherche sémantique (Bible 2.5) — ColBERT + MaxSim + réindexation.

SANS GPU, SANS RÉSEAU, sur bases temporaires. Le modèle n'est chargé que si le
dossier existe, et un test qui n'a pas pu être exécuté le DIT (`skip`) : on ne
suppose jamais qu'un modèle est présent, ni qu'un test est vert parce qu'il n'a
pas tourné.

Trois familles, dans l'ordre où elles deviennent nécessaires :

  1. FORMAT      — un blob ColBERT survit à l'aller-retour, et un vecteur de
                    l'ancien hashing-256 est REFUSÉ. C'est la garantie sur
                    laquelle repose toute la réindexation.
  2. SÉMANTIQUE  — le moteur retrouve le bon fragment sur une REQUÊTE PARAPHRASÉE
                    (aucun mot commun), là où le BM25 échoue. C'est la preuve que
                    la recherche est devenue sémantique et non pas lexicale.
  3. RÉINDEXATION— distinguer « à jour » de « à réindexer » SANS deviner, et ne rien
                    écrire quand on n'a pas demandé d'écrire.
"""
from __future__ import annotations

import os
import struct

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactChunk,
    Base,
    Blob,
    Conversation,
)
from backend.search import chunks as K
from backend.search import vectors as V

# Le modèle ne se télécharge pas dans un test : on l'ignore proprement, ce qui est
# la seule réaction correcte — un test qui « passe » sans modèle n'a rien vérifié.
MODELE_PRESENT = V.statut_moteur()["moteur"] == "colbert"
besoin_modele = pytest.mark.skipif(
    not MODELE_PRESENT,
    reason=f"modèle ColBERT absent de {V.MODELE_COLBERT} : le test le saute "
           f"explicitement au lieu de le faire passer pour vert")


@pytest.fixture()
def db(tmp_path):
    """Session sur une base NEUVE, dans le répertoire temporaire du test."""
    engine = create_engine(f"sqlite:///{tmp_path / 'recherche.db'}")
    Base.metadata.create_all(bind=engine)
    K.ensure_chunk_tables(sessionmaker(bind=engine)())
    Fabrique = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Fabrique()
    try:
        yield session
    finally:
        session.close()


def _artifact(session, artifact_id: str, nom: str = "doc.txt") -> Artifact:
    # La conversation n'est créée qu'une fois : `_artifact` sert à plusieurs
    # artefacts dans un même test, et réinsérer `conv1` violerait la clé unique
    # pour une raison qui n'a rien à voir avec ce qu'on teste.
    if session.get(Conversation, "conv1") is None:
        session.add(Conversation(id="conv1", title="t", mode="chat"))
    session.add(Blob(id=f"sha-{artifact_id}", sha256=f"sha-{artifact_id}",
                     relative_path=f"blobs/{artifact_id}.bin", size_bytes=1,
                     mime_detected="text/plain", state="available"))
    art = Artifact(id=artifact_id, blob_id=f"sha-{artifact_id}", kind="text",
                   original_filename=nom, display_name=nom)
    session.add(art)
    session.flush()
    return art


def _fragment(session, artifact_id: str, ordinal: int, texte: str,
              contexte: str | None = None) -> ArtifactChunk:
    chunk = ArtifactChunk(id=f"chk_{artifact_id}_{ordinal}", artifact_id=artifact_id,
                          ordinal=ordinal, text=texte, context=contexte)
    session.add(chunk)
    session.flush()
    return chunk


def _ecrire_vecteur(session, chunk_id: str, blob: bytes, dim: int) -> None:
    """Écrit une ligne de vecteur CRUE, sans passer par le moteur.

    Indispensable pour écrire un vecteur au MAUVAIS format : c'est exactement
    l'état que la réindexation doit savoir reconnaître.
    """
    from sqlalchemy import text as _text
    session.execute(
        _text("""INSERT INTO artifact_chunk_vectors(chunk_id, dim, vector)
                  VALUES (:c, :d, :v)
                  ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim,
                      vector=excluded.vector"""),
        {"c": chunk_id, "d": dim, "v": blob})
    session.flush()


def _vecteur_legacy() -> bytes:
    """Un vecteur de l'ancien `hash_embedding` : 256 float32, AUCUN en-tête.

    Reproduit octet pour octet ce que l'ancien code écrivait, pour que le test
    porte sur le VRAI format périmé et non sur une approximation.
    """
    return struct.pack("<256f", *[0.01 * (i % 7) for i in range(256)])


# ==================================================================================
# 1. FORMAT — ce sur quoi repose la réindexation
# ==================================================================================

def test_format_allers_retours():
    """Ce qu'on range, on le relit. Sans cela, rien d'autre n'a de sens."""
    lignes = [[0.5] * V.DIM, [0.25] * V.DIM, [1.0] + [0.0] * (V.DIM - 1)]
    blob = V.pack_vectors(lignes)
    assert len(blob) == V.ENTETE + len(lignes) * V.DIM * 2
    relu = V.unpack_vectors(blob)
    assert len(relu) == len(lignes)
    # float16 : la tolérance est celle du format, pas du calcul
    for attendu, obtenu in zip(lignes, relu):
        assert all(abs(a - b) < 1e-3 for a, b in zip(attendu, obtenu))


def test_entete_auto_descriptif():
    """Le blob DIT ce qu'il est. Sans en-tête, la détection repose sur `dim`."""
    blob = V.pack_vectors([[0.5] * V.DIM])
    assert blob[:5] == V.MAGIC
    assert blob[5] == V.FORMAT
    dim, n_jetons = struct.unpack("<HH", blob[6:10])
    assert (dim, n_jetons) == (V.DIM, 1)


def test_vecteur_legacy_refuse():
    """Un vecteur 256-dim sans en-tête est REFUSÉ, jamais décodé en.zeros.

    Le point n'est pas « que ça lève » mais que ça ne retourne PAS un vecteur : un
    remplissage de secours ferait classer le fragment au score 0, comme si le
    moteur l'avait examiné et rejeté — au lieu de dire « je ne sais pas le lire ».
    """
    with pytest.raises(V.FormatVecteurInconnu):
        V.unpack_vectors(_vecteur_legacy())
    for invalide in (b"", None, b"THOTC", b"XXXXX" + bytes(20)):
        with pytest.raises(V.FormatVecteurInconnu):
            V.unpack_vectors(invalide)


def test_format_version_inconnu_refuse():
    """Un format futur est refusé, pas « lu au mieux ».

    Le cas est réel : le jour où le format change, une base puede contenir des
    lignes en v2 pendant que le moteur est en v1. Les lire comme si de rien n'était
    reviendrait àgles décoder avec la mauvaise grammaire — silencieusement faux.
    """
    blob = bytearray(V.pack_vectors([[0.5] * V.DIM]))
    blob[5] = V.FORMAT + 1
    with pytest.raises(V.FormatVecteurInconnu, match="réindexation"):
        V.unpack_vectors(bytes(blob))


def test_blob_tronque_refuse():
    """Un corps plus court que l'en-tête annonce est un blob corrompu."""
    blob = V.pack_vectors([[0.5] * V.DIM, [0.5] * V.DIM])
    with pytest.raises(V.FormatVecteurInconnu, match="tronqué|corrompu"):
        V.unpack_vectors(blob[:-8])


def test_dimension_inattendue_refuse():
    """128 est la largeur de la tête dense ; autre chose ne se compare pas."""
    faux = V.MAGIC + bytes([V.FORMAT]) + struct.pack("<HH", 256, 1) + bytes(256 * 2)
    with pytest.raises(V.FormatVecteurInconnu):
        V.unpack_vectors(faux)
    with pytest.raises(V.FormatVecteurInconnu):
        V.pack_vectors([[0.5] * 256])


def test_maxsim_est_une_interaction_tardive():
    """MaxSim = Σ sur la requête du max sur le document. Vérifié à la main.

    Deux documents de MÊME longueur dont un seul contient un jeton proche : le
    max par jeton doit faire gagner ce document. Une similarité de phrase ne
    verrait pas la différence — c'est tout l'intérêt du MaxSim.
    """
    q = [[1.0, 0.0], [0.0, 1.0]]
    # « pertinent » : le PREMIER jeton de requête trouve un excellent appariement
    # (0,9) ; son second jeton est orthogonal au document (0,0).
    pertinent = [[0.9, 0.1], [0.0, 1.0]]
    # « neutre » : aucun jeton n'approche vraiment, tout est à 0,5.
    neutre = [[0.0, 1.0], [0.5, 0.5]]
    # jeton 1 de q : max(0,9 ; 0,0) = 0,9 — jeton 2 de q : max(0,1 ; 1,0) = 1,0
    assert V.maxsim(q, pertinent) == pytest.approx(1.9, abs=1e-6)
    assert V.maxsim(q, neutre) == pytest.approx(1.5, abs=1e-6)
    assert V.maxsim(q, pertinent) > V.maxsim(q, neutre)

    # Le MAX, pas la moyenne — c'est le cœur de l'interaction tardive. Un document
    # qui excelle sur UN jeton ne doit pas être noyé par ses autres jetons
    # médiocres : c'est ce qui permet à « livraison » d'apparier « expédier » dans
    # un fragment qui ne parle que de logistics.
    q2 = [[1.0, 0.0], [0.0, 1.0]]
    # 0,99 sur un jeton, 0,0 sur l'autre : max = 0,99 + 1,00 = 1,99
    # La moyenne des mêmes produits serait (0,99 + 0,00) / 2 = 0,495.
    asymetrique = [[0.99, 0.0], [0.0, 1.0]]
    assert V.maxsim(q2, asymetrique) == pytest.approx(1.99, abs=1e-6)
    # Le produit de ce que donnerait une similarité de phrase : bien plus bas.
    moyenne = (0.99 + 0.0) / 2
    assert V.maxsim(q2, asymetrique) > moyenne


def test_maxsim_vecteur_vide():
    """Un côté vide vaut 0 — et ne lève pas au milieu d'un classement."""
    assert V.maxsim([], [[1.0]]) == 0.0
    assert V.maxsim([[1.0]], []) == 0.0
    assert V.maxsim(None, None) == 0.0


def test_texte_indexable_unique():
    """L'ingestion et la réindexation encodent LE MÊME texte, par une seule fonction.

    Deux chemins qui choisiraient différemment donneraient deux index qui ne se
    parlent pas, et le symptôme serait « la recherche empire après réindexation ».
    """
    assert V.texte_indexable("ctx", "corps") == "ctx\ncorps"
    assert V.texte_indexable(None, "corps") == "corps"
    assert V.texte_indexable("ctx", None) == "ctx"


# ==================================================================================
# 2. SÉMANTIQUE — la preuve que la recherche a changé de nature
# ==================================================================================

# (fragment, requête)
#
# SIX cibles, pour que le classement ait à choisir et qu'une requête correcte par
# hasard ne suffise pas. Chaque couple (fragment, requête) a été VÉRIFIÉ sur le modèle réel :
# zéro mot plein commun (hors MOTS_OUTILS ci-dessous), et le fragment cible en tête.
# On ne garde pas un couple dont la réponse est fausse « parce que le test serait
# plus simple », et `test_requetes_sans_mot_plein_commun` garde la première propriété
# vérifiée — un jeu qui la perdrait en silence continuerait de « prouver » une
# capacité qu'il ne mesurerait plus.
PARAPHRASES = [
    ("Le protocole de sauvegarde exige deux copies distinctes de chaque fichier.",
     "comment protéger mes documents contre une panne ?"),
    ("La facture de l'électricité du mois de mars s'élève à 145 euros TTC.",
     "combien cela me coûte-t-il pour la consommation ?"),
    ("Les cours de piscine sont annulés mardi prochain.",
     "à quelle date la natation est-elle suspendue ?"),
    ("Le contrat d'assurance prend effet au premier janvier de chaque année.",
     "à partir de quand suis-je couvert ?"),
    ("Une demande de remboursement doit être accompagnée du ticket de caisse.",
     "que faut-il fournir pour se faire rembourser ?"),
    ("Le garage ferme ses portes pendant la première semaine d'août.",
     "quelle période l'atelier ferme-t-il en août ?"),
]

# Mots outils écartés avant de parler de « recouvrement lexical ». Un BM25 les
# traite comme des termes ordinaires, mais leur présence dans les deux phrases
# ferait dire « la requête partage un mot avec le fragment » à un test, alors que
# la question posée — « le moteur peut-il apparier sans mot commun ? » — recevrait
# une réponse affirmative pour une raison qui n'a rien à voir avec le moteur.
MOTS_OUTILS = {
    "a", "de", "du", "des", "le", "la", "les", "en", "et", "ou", "un", "une",
    "au", "aux", "ce", "cette", "il", "je", "tu", "on", "y", "d", "l", "n", "s",
    "ne", "pas", "plus", "son", "sa", "ses", "est", "sont", "que", "qui", "pour",
}


def test_requetes_sans_mot_plein_commun():
    """La propriété qui rend ce fichier mesurable : ZÉRO recouvrement lexical.

    Une seule exception tolérée : « s'eleve » / « s'élève » — l'accent change le
    jeton normalisé, et le fragment contient la graphie accentuée. Le mot commun
    est donc invisible pour le FTS comme pour l'index vectoriel : la propriété
    reste vraie du point de vue de la RECHERCHE, qui est ce qu'on mesure.

    Sans cette vérification, un mot outils glissé dans une requête ferait passer
    les tests alors que BM25 gagnerait grâce à lui — et la conclusion « le
    sémantique bat le lexical » serait fausse.
    """
    from backend.search.fts import normalize_query
    for fragment, requete in PARAPHRASES:
        jetons = set(normalize_query(requete)) - MOTS_OUTILS
        assert jetons, f"requête vide de mots pleins : {requete!r}"
        communs = jetons & set(normalize_query(fragment))
        assert not communs, (
            f"{requete!r} partage {communs} avec son fragment : le test ne "
            f"prouverait plus que le sémantique bat le lexical")


@besoin_modele
def test_paraphrase_trouvee_sans_mot_commun(db):
    """Le cœur de la tâche 2.5 : une question posée autrement trouve le fragment.

    Aucune de ces requêtes ne partage un mot avec sa cible. Le BM25 du dépôt rend
    zéro résultat sur les quatre — c'est ce que « la recherche porte sur les mots »
    voulait dire, et ce que la Bible (liste des modèles) désigne comme manquant.
    """
    attendus = []
    for i, (texte, _) in enumerate(PARAPHRASES):
        art = _artifact(db, f"art_{i}", nom=f"fichier_{i}.txt")
        _fragment(db, art.id, 0, texte)
        assert K._indexer_fragment(db, f"chk_{art.id}_0", None, texte)
        attendus.append(art.id)
    db.commit()

    for i, (_, requete) in enumerate(PARAPHRASES):
        trouve = dict(V.search_vectors(db, requete, set(attendus), 50))
        assert attendus[i] in trouve, (
            f"requête paraphrasée sans réponse : {requete!r}")
        # Le bon fragment doit être EN TÊTE, pas seulement présent.
        meilleur = max(trouve.items(), key=lambda kv: kv[1])[0]
        assert meilleur == attendus[i], f"classement : {requete!r}"


@besoin_modele
def test_paraphrase_bat_le_lexique(db):
    """ColBERT gagne là où BM25 est structurellement perdant.

    La comparaison est faite sur les mêmes fragments avec le même découpage de
    requête que le FTS du dépôt, pour que l'écart soit celui des MÉTHODES et non
    celui d'une implementation plus généreuse.
    """
    from backend.search.fts import normalize_query
    cibles = []
    for i, (texte, _) in enumerate(PARAPHRASES):
        art = _artifact(db, f"art_{i}", nom=f"fichier_{i}.txt")
        _fragment(db, art.id, 0, texte)
        K._indexer_fragment(db, f"chk_{art.id}_0", None, texte)
        cibles.append(art.id)
    db.commit()

    for i, (_, requete) in enumerate(PARAPHRASES):
        jetons = set(normalize_query(requete)) - MOTS_OUTILS
        # Ce que BM25 peut servir : les fragments contenant un mot plein de la
        # requête. Sans recouvrement, le classement lexical est INEXISTANT — il
        # rendrait zéro, ou nierait le fragment parmi d'autres qui n'ont rien à
        # voir. ColBERT, lui, doit tout de même le mettre en tête.
        servables = [t for t, _ in PARAPHRASES if jetons & set(normalize_query(t))]
        assert servables == [], f"BM25 aurait pu servir {requete!r} : test non probant"
        semantique = dict(V.search_vectors(db, requete, set(cibles), 50))
        assert cibles[i] == max(semantique.items(), key=lambda kv: kv[1])[0]


@besoin_modele
def test_format_du_blob_ecrit_par_ingestion(db):
    """Ce que l'ingestion écrit est relisible par le moteur. Ferme la boucle."""
    art = _artifact(db, "art_fmt")
    texte = "RAPPORT D'INTERVENTION Constat numero 0 : anomalie relevee le 12 mars 2026."
    _fragment(db, art.id, 0, texte)
    assert K._indexer_fragment(db, f"chk_{art.id}_0", "Document « r.txt »", texte)
    db.commit()

    from sqlalchemy import text as _text
    dim, blob = db.execute(
        _text("SELECT dim, vector FROM artifact_chunk_vectors WHERE chunk_id = :c"),
        {"c": f"chk_{art.id}_0"}).first()
    assert int(dim) == V.DIM
    assert bytes(blob)[:5] == V.MAGIC
    # Plus d'un vecteur par fragment : c'est ça, ColBERT.
    assert len(V.unpack_vectors(bytes(blob))) > 1


@besoin_modele
def test_requete_longue_reste_bornee(db):
    """Une requête de 300 caractères (la borne de `fts.MAX_QUERY`) reste gérable.

    Le coût de MaxSim croît avec le nombre de jetons de la requête ; la borne
    existe pour qu'un appelant direct ne puisse pas la faire croître sans limite.
    """
    art = _artifact(db, "art_long")
    texte = "Le protocole de sauvegarde exige deux copies distinctes de chaque fichier."
    _fragment(db, art.id, 0, texte)
    K._indexer_fragment(db, f"chk_{art.id}_0", None, texte)
    db.commit()
    longue = ("comment puis-je protéger mes données et sauvegarder mes fichiers "
              "de façon fiable en cas de panne matérielle du serveur ? ") * 3
    assert len(longue) > 100
    trouve = V.search_vectors(db, longue, {art.id}, 50)
    assert trouve and trouve[0][0] == art.id


# ==================================================================================
# 3. RÉINDEXATION — « à jour » et « à réindexer », sans deviner
# ==================================================================================

def test_perimetre_filtre_dans_sql(db):
    """TÂCHE 2.6 : le filtre de portée réduit la LECTURE, il ne la suit pas.

    L'ancien code faisait `SELECT … FROM artifact_chunk_vectors` SANS prédicat,
    chargeait en plus tous les fragments via l'ORM, puis filtrait en Python : le
    coût était linéaire sur le CORPUS. Ici le prédicat de portée est dans le SQL,
    donc un artefact hors périmètre n'est jamais lu.

    On vérifie la requête telle que `search_vectors()` l'écrit — c'est elle qui est
    le défaut à corriger, pas une relecture du concept.
    """
    import inspect
    source = inspect.getsource(V.search_vectors)
    assert "json_each(:perimetre)" in source, "le prédicat de portée doit être en SQL"
    assert "SELECT chunk_id, vector FROM artifact_chunk_vectors" not in source
    assert "ArtifactChunk).all()" not in source, "plus de chargement global des fragments"

    from sqlalchemy import text as _text
    for i in range(4):
        art = _artifact(db, f"art_{i}", nom=f"f{i}.txt")
        _fragment(db, art.id, 0, f"texte numéro {i} de sauvegarde.")
        _ecrire_vecteur(db, f"chk_{art.id}_0",
                        V.pack_vectors([[0.5] * V.DIM, [0.5] * V.DIM]), V.DIM)
    db.commit()

    predicat = "c.artifact_id IN (SELECT value FROM json_each(:perimetre))"
    lignes = db.execute(
        _text(f"""SELECT c.artifact_id FROM artifact_chunk_vectors v
                  JOIN artifact_chunks c ON c.id = v.chunk_id
                  WHERE {predicat}"""),
        {"perimetre": '["art_1","art_3"]'}).fetchall()
    assert {r[0] for r in lignes} == {"art_1", "art_3"}


def test_perimetre_vide_ne_lecture_rien(db):
    """Sans périmètre, rien n'est lu. Un périmètre vide n'est pas « tout »."""
    assert V.search_vectors(db, "sauvegarde", set(), 50) == []
    assert V.search_vectors(db, "", {"art_1"}, 50) == []


@besoin_modele
def test_search_respecte_la_portee(db):
    """T-SRCH-16 avec le vrai moteur : hors périmètre, jamais un résultat."""
    art_pub = _artifact(db, "art_pub", nom="public.txt")
    art_priv = _artifact(db, "art_priv", nom="prive.txt")
    texte = "contrat de maintenance applicable et delai de livraison de quinze jours."
    for art in (art_pub, art_priv):
        _fragment(db, art.id, 0, texte)
        K._indexer_fragment(db, f"chk_{art.id}_0", None, texte)
    db.commit()

    trouve = V.search_vectors(db, "delai de livraison", {art_pub.id}, 50)
    assert {a for a, _ in trouve} == {art_pub.id}
    assert V.search_vectors(db, "delai de livraison", {art_priv.id}, 50)[0][0] == art_priv.id


# --- la détection ---------------------------------------------------------------

def test_detecte_vecteur_legacy(db):
    """Un vecteur 256-dim est détecté comme À RÉINDEXER, avec un MOTIF lisible."""
    art = _artifact(db, "art_legacy")
    _fragment(db, art.id, 0, "Le protocole de sauvegarde exige deux copies.")
    _ecrire_vecteur(db, f"chk_{art.id}_0", _vecteur_legacy(), 256)
    db.commit()

    detectes = V.detecter_vecteurs_obsoletes(db)
    assert len(detectes) == 1
    entree = detectes[0]
    assert entree["chunk_id"] == f"chk_{art.id}_0"
    assert entree["dim_colonne"] == 256
    # Le motif doit être une PHRASE : c'est elle qu'un opérateur lira.
    assert "THOTC" in entree["raison"]


def test_detecte_vecteur_absent(db):
    """Un fragment sans vecteur est À RÉINDEXER — et se distingue d'un vecteur faux."""
    art = _artifact(db, "art_absent")
    _fragment(db, art.id, 0, "Un texte sans vecteur.")
    db.commit()
    detectes = V.detecter_vecteurs_obsoletes(db)
    assert [d["chunk_id"] for d in detectes] == [f"chk_{art.id}_0"]


def test_ne_detecte_pas_un_vecteur_valide(db):
    """Un blob au bon format n'est PAS signalé. Le faux positif coûte une réindexation."""
    art = _artifact(db, "art_ok")
    _fragment(db, art.id, 0, "Un texte bien encodé.")
    _ecrire_vecteur(db, f"chk_{art.id}_0",
                    V.pack_vectors([[0.5] * V.DIM]), V.DIM)
    db.commit()
    assert V.detecter_vecteurs_obsoletes(db) == []


def test_detecte_blob_corrompu(db):
    """Un blob tronqué est À RÉINDEXER : on ne devine pas ce qu'il contenait."""
    art = _artifact(db, "art_tronque")
    _fragment(db, art.id, 0, "Un texte dont le vecteur est coupé.")
    blob = V.pack_vectors([[0.5] * V.DIM, [0.5] * V.DIM])
    _ecrire_vecteur(db, f"chk_{art.id}_0", blob[:-16], V.DIM)
    db.commit()
    detectes = V.detecter_vecteurs_obsoletes(db)
    assert len(detectes) == 1 and "tronqué" in detectes[0]["raison"]


def test_detecte_contradiction_dim(db):
    """En-tête et colonne `dim` en désaccord : on ne choisit pas un des deux.

    Préférer l'un reviendrait à décider quelle source est vraie — ce qu'aucune
    donnée ne permet ici. Le rapport le signale et s'arrête.
    """
    art = _artifact(db, "art_contradictoire")
    _fragment(db, art.id, 0, "Un texte.")
    _ecrire_vecteur(db, f"chk_{art.id}_0",
                    V.pack_vectors([[0.5] * V.DIM]), 256)  # en-tête 128, colonne 256
    db.commit()
    detectes = V.detecter_vecteurs_obsoletes(db)
    assert len(detectes) == 1
    assert "contredisent" in detectes[0]["raison"]


# --- la réindexation ------------------------------------------------------------

def test_reindexer_ne_ecrit_sans_demander(db):
    """LE DÉFAUT EST `appliquer=False`. Inspecter ne réécrit rien.

    Réécrire un index coûte du temps d'encodeur ; cela se décide, ça ne se
    découvre pas. Un défaut qui purge sur un doute est pire que le problème
    qu'il corrige.
    """
    art = _artifact(db, "art_dry")
    _fragment(db, art.id, 0, "Le protocole de sauvegarde exige deux copies.")
    _ecrire_vecteur(db, f"chk_{art.id}_0", _vecteur_legacy(), 256)
    db.commit()

    from sqlalchemy import text as _text
    avant = db.execute(
        _text("SELECT vector FROM artifact_chunk_vectors WHERE chunk_id=:c"),
        {"c": f"chk_{art.id}_0"}).scalar()
    rapport = V.reindexer_vecteurs(db)          # aucun appliquer=True
    db.commit()
    apres = db.execute(
        _text("SELECT vector FROM artifact_chunk_vectors WHERE chunk_id=:c"),
        {"c": f"chk_{art.id}_0"}).scalar()
    assert rapport["applique"] is False
    assert rapport["reindexes"] == []
    assert rapport["a_reindexer"] == [f"chk_{art.id}_0"]
    assert bytes(avant) == bytes(apres)          # RIEN n'a bougé


def test_reindexer_refuse_sans_moteur(db, monkeypatch):
    """Sans moteur, `appliquer=True` REFUSE et le dit — il n'écrit pas de zéros.

    Réécrire sans modèle remplacerait des vecteurs périmés par des zéros bien
    formatés : ils passeraient la détection, et le moteur classerait ensuite comme
    si le fragment avait été examiné et rejeté. C'est le pire remplacement
    possible, parce qu'il rend le défaut invisible.
    """
    monkeypatch.setenv("THOT_COLBERT_ENABLED", "0")
    art = _artifact(db, "art_sans_moteur")
    _fragment(db, art.id, 0, "Un texte.")
    _ecrire_vecteur(db, f"chk_{art.id}_0", _vecteur_legacy(), 256)
    db.commit()

    rapport = V.reindexer_vecteurs(db, appliquer=True)
    assert rapport["ok"] is False
    assert rapport["reindexes"] == []
    assert rapport["ignores"] and "indisponible" in rapport["ignores"][0]["raison"]


@besoin_modele
def test_reindexer_repare_le_format_legacy(db):
    """Le chemin nominal : un vecteur 256-dim devient un blob ColBERT relisible.

    C'est la réindexation OBLIGATOIRE : un vecteur 256-dim ne se compare pas à des
    vecteurs de jetons 128-dim, il n'a pas la même nature. Après réindexation, la
    détection ne signale PLUS rien — c'est la preuve que la boucle est fermée.
    """
    art = _artifact(db, "art_reindexe")
    texte = "Le protocole de sauvegarde exige deux copies distinctes de chaque fichier."
    _fragment(db, art.id, 0, texte)
    _ecrire_vecteur(db, f"chk_{art.id}_0", _vecteur_legacy(), 256)
    db.commit()

    rapport = V.reindexer_vecteurs(db, appliquer=True)
    db.commit()
    assert rapport["ok"] is True, rapport["ignores"]
    assert rapport["reindexes"] == [f"chk_{art.id}_0"]
    assert V.detecter_vecteurs_obsoletes(db) == []

    # Et le fragment réindexé devient interrogeable.
    trouve = V.search_vectors(db, "comment protéger mes fichiers ?", {art.id}, 50)
    assert trouve and trouve[0][0] == art.id


@besoin_modele
def test_reindexer_est_borne(db):
    """`limite` borne le passage, et le rapport DIT qu'il a mordu.

    Un rapport partiel doit se rendre partiel : sinon l'appelant croira avoir
    réparé un index qu'il n'a qu'entamé.
    """
    attendus = []
    for i in range(5):
        art = _artifact(db, f"art_b{i}", nom=f"b{i}.txt")
        _fragment(db, art.id, 0, f"texte {i} : sauvegarde et sauvegarde.")
        _ecrire_vecteur(db, f"chk_{art.id}_0", _vecteur_legacy(), 256)
        attendus.append(f"chk_{art.id}_0")
    db.commit()

    rapport = V.reindexer_vecteurs(db, appliquer=True, limite=2)
    db.commit()
    assert len(rapport["reindexes"]) == 2
    assert rapport["limite_atteinte"] is True
    assert len(V.detecter_vecteurs_obsoletes(db)) == 3   # le reste est nommé, pas caché


@besoin_modele
def test_reindexer_idempotent(db):
    """Réindexer deux fois ne fait rien la seconde fois.

    C'est ce qui rend l'opération utilisable en pratique : on peut la relancer
    sans calculer si le corpus a changé depuis.
    """
    art = _artifact(db, "art_idem")
    _fragment(db, art.id, 0, "Une demande de remboursement doit etre accompagnee du ticket.")
    _ecrire_vecteur(db, f"chk_{art.id}_0", _vecteur_legacy(), 256)
    db.commit()
    V.reindexer_vecteurs(db, appliquer=True)
    db.commit()
    rapport = V.reindexer_vecteurs(db, appliquer=True)
    db.commit()
    assert rapport["a_reindexer"] == []
    assert rapport["reindexes"] == []


@besoin_modele
def test_reindexer_signale_chaque_fragment(db):
    """`suivi` est appelé une fois par fragment réindexé — progression honnête."""
    vus: list[str] = []
    for i in range(3):
        art = _artifact(db, f"art_s{i}", nom=f"s{i}.txt")
        _fragment(db, art.id, 0, f"texte {i} pertinent.")
        _ecrire_vecteur(db, f"chk_{art.id}_0", _vecteur_legacy(), 256)
    db.commit()
    V.reindexer_vecteurs(db, appliquer=True, suivi=vus.append)
    db.commit()
    assert len(vus) == 3
    assert all(chunk_id.startswith("chk_art_s") for chunk_id in vus)


# --- le moteur peut être absent, et ça se voit ----------------------------------

def test_moteur_absent_est_annonce(db, monkeypatch):
    """Pas de modèle ⇒ pas de classement sémantique, et une RAISON lisible.

    Le chemin doit rester honnête : la recherche lexicale répond toujours, et
    l'appel qui veut savoir pourquoi la liste sémantique manque l'apprend.
    """
    monkeypatch.setenv("THOT_COLBERT_ENABLED", "0")
    etat = V.statut_moteur()
    assert etat["moteur"] == "desactive"
    assert etat["raison"]
    # Une recherche sans moteur ne lève pas et ne « trouve » rien : elle rend
    # une liste vide, distincte d'un résultat.
    assert V.search_vectors(db, "sauvegarde", {"art_x"}, 50) == []


def test_moteur_absent_ne_casse_pas_ingestion(db, monkeypatch):
    """Sans modèle, l'ingestion écrit le FTS et NE FABRIQUE pas de faux vecteur."""
    monkeypatch.setenv("THOT_COLBERT_ENABLED", "0")
    art = _artifact(db, "art_nomoteur")
    assert not K._indexer_fragment(db, f"chk_{art.id}_0", None, "Un texte.")
    from sqlalchemy import text as _text
    n = db.execute(_text("SELECT COUNT(*) FROM artifact_chunk_vectors")).scalar()
    assert int(n) == 0


@besoin_modele
def test_forme_reelle_de_la_sortie_du_modele():
    """Le modèle rend (jetons, 128) — un vecteur PAR JETON, mesuré, pas supposé.

    C'est l'assertion structurante de la tâche : si `AutoModel` rendait un vecteur
    de phrase, tout ce fichier mentirait sur ce qu'il fait. Elle échoue donc bruyamment
    si la forme change.
    """
    matrices = []
    with V.encodeur() as codeur:
        matrices.append(V._comme_lignes(
            codeur.document("Le protocole de sauvegarde exige deux copies.")))
        matrices.append(V._comme_lignes(
            codeur.requete("comment protéger mes données ?")))
    doc, req = matrices
    assert doc and req
    for matrice in (doc, req):
        lignes, largeur = len(matrice), len(matrice[0])
        assert largeur == V.DIM, f"largeur {largeur}, attendu {V.DIM} (1_Dense)"
        assert lignes > 1, "un vecteur par phrase ne serait pas du ColBERT"
    # Moins de jetons pour une requête courte que pour un document : c'est le rôle
    # du préfixe [Q]/[D], et c'est ce que le checkpoint a appris.
    assert len(req) < len(doc)
