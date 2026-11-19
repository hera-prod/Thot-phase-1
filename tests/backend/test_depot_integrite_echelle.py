"""Le dépôt reste-t-il réparable quand il y a beaucoup d'espaces ?

Cette suite répond à une question qu'aucun test à deux espaces ne peut trancher :
l'outil est-il conçu pour LE dépôt de l'utilisateur, ou pour les dépôts en général ?

La réponse se mesure, pas elle ne s'affirme pas. Trois échelles — 2, 5 et 30 espaces —
et, pour chacune, un compteur de requêtes SQL. La propriété attendue n'est pas
« ça marche », c'est « ça coûte la même chose » : la vérification doit rester à cinq
lectures QUEL QUE SOIT le nombre d'espaces. Le jour où quelqu'un ajoute une boucle
par espace, le coût devient proportionnel au nombre de dépôts, les gros ateliers
rament, et les petits ne le voient pas — un test qui passe sur 2 espaces ne le
relèverait jamais.

Les travaux non destructifs sont eux aussi vérifiés à l'échelle : un dépôt de 30
espaces ne doit pas perdre une ligne de plus qu'un dépôt de 2.

Montage repris de `test_depot_integrite.py`. Aucun octet ni aucune ligne du projet
n'est touché.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from backend.db.models import Artifact, ArtifactCard, ArtifactLink, Blob
from backend.services import depot_integrite as integrite

from test_depot_integrite import (  # noqa: F401 — le montage, réutilisé tel quel
    _conversation,
    _depot,
    _espace,
    _etat,
    _requetes,
    _session,
    _verser_puis_depot,
    base_depot,
)

# Les trois échelles demandées. 30 n'est pas un caprice : c'est le premier nombre
# où « par espace » commence à se voir — 30 × 5 lectures font 150 requêtes pour
# une information qu'une seule suffice.
ECHELLES = [2, 5, 30]

# Le coût de la vérification doit être PLAT. Une seule ligne de base suffit à le
# prouver : si la lecture devenait proportionnelle, 30 espaces la feraient échouer.
LECTURES_ATTENDUES = integrite.LECTURES_PAR_VERIFICATION


@pytest.mark.parametrize("nombre", ECHELLES)
def test_la_verification_coute_la_meme_quelle_que_soit_le_nombre_despaces(base_depot, nombre):
    """Cinq lectures, deux ou trente espaces.

    Le compteur est sur `executemany` exclu et statements comprises : c'est ce qui
    part réellement au moteur. Une jointure ou un `IN` supplémentaire par espace se
    verrait ici.
    """
    engine, sessions = base_depot
    with _session(sessions) as db:
        for index in range(nombre):
            espace = _espace(db, f"Espace {index}")
            _depot(db, espace, [f"fichier-{index}.pdf"], [f"f{index}"])

    with _session(sessions) as db, _requetes(engine) as vues:
        rapport = integrite.verifier_depot(db)

    assert rapport["sain"] is True
    assert rapport["total"] == 0
    assert len(vues) == LECTURES_ATTENDUES, (
        f"{len(vues)} lectures pour {nombre} espaces au lieu de {LECTURES_ATTENDUES} — "
        "le coût est-il devenu proportionnel ?"
    )


@pytest.mark.parametrize("nombre", ECHELLES)
def test_tous_les_espaces_abimes_sont_signales_nommes_et_repares(base_depot, nombre):
    """Chaque dépôt abîmé est trouvé, nommé, réparé — et le reste n'est pas touché.

    Le fichier est versé PUIS partagé au dépôt : c'est l'anomalie mesurée, celle que
    la réparation existe pour corriger. On en met un sain par espace à côté, pour
    vérifier qu'une réparation à l'échelle ne « répare » pas ce qui n'est pas cassé.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        attendus = {}
        for index in range(nombre):
            espace = _espace(db, f"Espace {index}")
            discussion = _conversation(db, f"c{index}", espace)
            _depot(db, espace, [f"sain-{index}.pdf"], [f"s{index}"])
            _verser_puis_depot(db, espace, discussion, f"tardif-{index}.pdf", f"t{index}")
            attendus[espace] = f"Espace {index}"

    with _session(sessions) as db:
        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))

        assert rapport["total"] == nombre, "chaque dépôt abîmé doit être trouvé"
        assert rapport["reparable"] == nombre
        assert rapport["refuse"] == 0
        assert len(rapport["espaces"]) == nombre
        assert [e["nom"] for e in rapport["espaces"]] == sorted(attendus.values()), (
            "les espaces doivent etre listes par nom, pas par identifiant genere")
        assert all(e["nom_connu"] is True for e in rapport["espaces"])
        # Chaque anomalie porte son espace : c'est ce qui permet à l'écran de dire
        # « 3 à réparer dans Refonte du site » plutôt qu'un total global.
        assert all(len(e["exemples"]) == 1 for e in rapport["espaces"])

        avant = _etat(sessions)
        bilan = integrite.reparer_depot(db)

        assert bilan["repares_effectuees"]["index_manquant"] == nombre
        assert bilan["apres"]["sain"] is True
        assert bilan["non_repares"] == []

        apres = _etat(sessions)

    # Aucune ligne métier perdue, quelle que soit l'échelle.
    for table in ("artifacts", "artifact_links", "blobs", "artifact_cards"):
        assert apres[table] == avant[table], f"{table} a changé pendant la réparation"
    assert apres["noms"] == avant["noms"], "un nom a été réécrit"
    # L'index, lui, doit AVOIR grandi : c'est le but de l'opération.
    assert apres["lignes_index"] == avant["lignes_index"] + nombre


@pytest.mark.parametrize("nombre", ECHELLES)
def test_la_reparation_est_idempotente_a_l_echelle(base_depot, nombre):
    """Une seconde passe ne refait rien, et ne coûte pas une écriture de plus.

    Idempotence n'est pas « ça ne casse pas » : c'est « le bouton peut rester
    pressé ». À 30 espaces, c'est aussi la preuve que le module ne maintient aucun
    état entre deux passages.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        for index in range(nombre):
            espace = _espace(db, f"Espace {index}")
            discussion = _conversation(db, f"c{index}", espace)
            _verser_puis_depot(db, espace, discussion, f"tardif-{index}.pdf", f"t{index}")
        integrite.reparer_depot(db)

        avec_index = {tuple(row) for row in db.execute(
            sql_text("SELECT artifact_id, scope_kind, scope_id FROM artifact_search"))}
        deuxieme = integrite.reparer_depot(db)
        apres = {tuple(row) for row in db.execute(
            sql_text("SELECT artifact_id, scope_kind, scope_id FROM artifact_search"))}

    assert deuxieme["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}
    assert apres == avec_index, "une seconde passe a modifié l'index"


def test_un_espace_avec_plusieurs_fichiers_reste_compte_correctement(base_depot):
    """Le regroupement ne dépend pas du nombre de fichiers PAR espace.

    Un espace avec 12 fichiers tardifs et un autre avec 1 : le compte doit dire 12 et 1,
    pas 13 regroupés ni 2 espaces de moyenne. C'est ici que « par espace » se
    confondrait le plus vite avec « au total ».
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        gros = _espace(db, "Gros dépôt")
        petit = _espace(db, "Petit dépôt")
        discussion = _conversation(db, "c1", gros)
        for index in range(12):
            _verser_puis_depot(db, gros, discussion, f"tardif-{index}.pdf", f"t{index}",
                               request_id=f"gros-{index}")
        _verser_puis_depot(db, petit, _conversation(db, "c2", petit), "seul.pdf", "s")

        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))

    comptes = {e["nom"]: e["total"] for e in rapport["espaces"]}
    assert comptes == {"Gros dépôt": 12, "Petit dépôt": 1}
    assert rapport["total"] == 13


def test_un_depot_sain_ne_demande_pas_a_etre_repare(base_depot):
    """Le cas inverse, à l'échelle : un atelier de 30 dépôts tous sains n'a rien à faire.

    Sans ce test, une régression qui proposerait toujours le bouton passerait les
    autres : on ne s'aperçoit d'un bouton inutile que quand il n'y a rien à réparer.
    """
    _, sessions = base_depot
    with _session(sessions) as db:
        for index in range(30):
            _depot(db, _espace(db, f"Espace {index}"), [f"fichier-{index}.pdf"], [f"f{index}"])

        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))

        assert rapport["sain"] is True
        assert rapport["espaces"] == [], "un dépôt sain ne donne aucune ligne à afficher"
        assert rapport["reparable"] == 0
        bilan = integrite.reparer_depot(db)
        assert bilan["repares_effectuees"] == {"index_manquant": 0, "nom_vide": 0}