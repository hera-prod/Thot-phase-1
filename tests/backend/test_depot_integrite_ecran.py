"""Le chemin d'écran de la réparation des dépôts : ce que l'appelant reçoit.

`depot_integrite.py` sait dire *ce qui ne va pas*. Il ne sait pas *où* le dire. Ces
tests verrouillent cette traduction — le regroupement par espace, les noms, et surtout
la frontière entre « rien à signaler » et « je n'ai pas su regarder », qui est la seule
chose que l'écran ne doit pas confondre.

Ils verrouillent aussi le canal d'écriture : une réparation qui s'exécute sans que
l'appelant ait confirmé modifierait la base sans que personne l'ait demandé. C'est un
refus, pas une defensive.

Montage repris de `test_depot_integrite.py` : base neuve, `SessionLocal` redirigée,
racines de données jetables, ingestion neutralisée. Aucun octet ni aucune ligne du
projet n'est touché.
"""

from __future__ import annotations

import pytest

from backend.db.models import Workspace
from backend.ipc import espaces as IPC
from backend.services import depot_integrite as integrite

from test_depot_integrite import (  # noqa: F401 — le montage, réutilisé tel quel
    _conversation,
    _depot,
    _espace,
    _verser_puis_depot,
    base_depot,
)


# ---------- 1. Ce que voit l'écran ----------

def test_un_depot_sain_donne_un_ecran_sain_et_aucune_ligne(base_depot):
    """Un dépôt sain se lit « sain », et la section n'affiche que ça.

    Le contrôle va jusqu'à `espaces == []` : un écran qui afficherait un espace sans
    anomalie ferait croire qu'il y a quelque chose à réparer là où il n'y a rien.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        espace = _espace(db, "Espace calme")
        _depot(db, espace, ["comptes.pdf"], ["comptes"])

        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))

        assert rapport["sain"] is True
        assert rapport["lecture_complete"] is True
        assert rapport["total"] == 0
        assert rapport["espaces"] == []
        assert rapport["diagnostics"] == []
    finally:
        db.close()


def test_les_anomalies_sont_regroupees_par_espace_avec_leur_nom(base_depot):
    """Deux espaces abîmés donnent DEUX lignes, nommées, et pas une liste d'identifiants.

    L'identifiant d'espace ne parle à personne : le nom est résolu ici, et son absence
    est signalée par `nom_connu` pour que l'écran n'invente pas un libellé.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        premier = _espace(db, "Refonte du site")
        second = _espace(db, "Stratégie marketing")
        # Un dépôt tardif par espace : un fichier ne traverse pas d'un espace à l'autre
        # (le partage l'interdit, `espaces.py:276`), donc chaque anomalie a sa source.
        _verser_puis_depot(db, premier, _conversation(db, "c1", premier), "tardif-a.pdf", "a")
        _verser_puis_depot(db, second, _conversation(db, "c2", second), "tardif-b.pdf", "b")

        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))

        # L'ordre est ALPHABÉTIQUE, par nom : l'ordre des identifiants générés est
        # arbitraire et ferait échouer ce test selon le jour.
        noms = [espace["nom"] for espace in rapport["espaces"]]
        assert noms == ["Refonte du site", "Stratégie marketing"]
        assert all(espace["nom_connu"] is True for espace in rapport["espaces"])
        assert rapport["total"] == 2
        assert rapport["reparable"] == 2
        assert rapport["refuse"] == 0
        # L'écran a besoin d'un exemple à nommer, pas seulement d'un compte.
        assert rapport["espaces"][0]["exemples"][0]["display_name"].startswith("tardif-a")
    finally:
        db.close()


def test_un_espace_illisible_ne_donne_pas_de_nom_fabrique(base_depot):
    """Un espace introuvable s'affiche par son identifiant, avec `nom_connu` faux.

    Le pire serait d'inventer un nom à partir d'un id : l'écran afficherait « Espace
    3 » comme si c'était un lieu, et l'utilisateur ne retrouverait pas son dépôt.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        espace = _espace(db, "Espace éphémère")
        discussion = _conversation(db, "c1", espace)
        _verser_puis_depot(db, espace, discussion, "tardif.pdf", "x")
        db.query(Workspace).filter(Workspace.id == espace).delete()
        db.commit()

        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))
        rapportee = rapport["espaces"][0]

        assert rapportee["nom_connu"] is False
        assert rapportee["nom"] == espace
    finally:
        db.close()


# ---------- 2. « Rien à signaler » n'est PAS « j'ai tout regardé » ----------

def test_une_table_illisible_donne_une_lecture_incomplete_pas_zero_anomalie(base_depot, monkeypatch):
    """Une base à moitié lisible produit `sain: false` SANS anomalie — et l'écran doit le voir.

    C'est le défaut que ce chemin pouvait commettre : un rapport sans anomalie ressemble
    à un bon rapport. `_lignes` attrape l'échec, `sain` devient faux, et si l'écran ne
    regardait que `total == 0` il afficherait « tout va bien » sur une base dont il n'a
    lu que la moitié. `lecture_complete` existe pour rendre ce cas distinct.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        espace = _espace(db, "Espace dégradé")
        _depot(db, espace, ["comptes.pdf"], ["comptes"])

        # On casse UNE table du rapport : assez pour rendre la lecture incomplète,
        # pas assez pour faire tomber la vérification — elle ne lève sur rien.
        vrai = integrite._lignes

        def _casse_blobs(db_, sql, params, etiquette, diagnostics):
            if "FROM blobs" in sql:
                diagnostics.append("blobs illisible (OperationalError) : cette partie n'est pas vérifiée")
                return None
            return vrai(db_, sql, params, etiquette, diagnostics)

        monkeypatch.setattr(integrite, "_lignes", _casse_blobs)

        brut = integrite.verifier_depot(db)
        rapport = integrite.resume_pour_ecran(db, brut)

        assert brut["total"] == 0, "le défaut simulé ne crée pas d'anomalie"
        assert brut["sain"] is False, "mais le rapport ne s'affirme pas sain"
        assert rapport["lecture_complete"] is False
        assert rapport["sain"] is False
        assert rapport["diagnostics"], "l'écran doit pouvoir dire ce qui n'a pas été lu"
    finally:
        db.close()


def test_une_verification_sur_base_vide_ne_pretend_pas_etre_saine(base_depot):
    """Le vide est une réponse, pas une absence : `sain` avec zéro anomalie et zéro espace."""
    _, sessions = base_depot
    db = sessions()
    try:
        rapport = integrite.resume_pour_ecran(db, integrite.verifier_depot(db))
        assert rapport["sain"] is True
        assert rapport["total"] == 0
        assert rapport["espaces"] == []
    finally:
        db.close()


# ---------- 3. Le canal d'écriture exige une confirmation exprimée ----------

def test_le_canal_de_reparation_refuse_sans_confirmation(base_depot):
    """Sans `confirme`, la réparation ne s'exécute pas — et ne modifie rien.

    Un appel égaré de l'interface ne doit pas suffire à réécrire l'index de tous les
    dépôts de l'atelier.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        espace = _espace(db, "Espace")
        discussion = _conversation(db, "c1", espace)
        _verser_puis_depot(db, espace, discussion, "tardif.pdf", "x")
        assert integrite.verifier_depot(db)["total"] == 1

        with pytest.raises(ValueError, match="CONFIRMATION_REQUIRED"):
            IPC.espace_depot_reparer(db, {})

        assert integrite.verifier_depot(db)["total"] == 1, "la base n'a pas bougé"
    finally:
        db.close()


@pytest.mark.parametrize("confirme", ["true", 1, "oui", {}, []])
def test_seul_un_vrai_booleen_confirme(base_depot, confirme):
    """`confirme: "true"` ou `confirme: 1` ne valent pas confirmation.

    C'est le seul moyen qu'ait l'appelant de dire qu'il n'a PAS confirmé ; le lire
    autrement reviendrait à écrire sur une intention qui n'a jamais été exprimée.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        with pytest.raises(ValueError, match="CONFIRMATION_REQUIRED"):
            IPC.espace_depot_reparer(db, {"confirme": confirme})
    finally:
        db.close()


def test_le_canon_ecrit_apres_confirmation_et_ne_supprime_rien(base_depot):
    """Confirmé, le canal répare — et le bilan distingue ce qui est réparé de ce qui ne l'est pas.

    C'est l'écran qui parle : il doit pouvoir dire « 1 réparé, 2 signalés ». Une liste
    unique des deux ne le permettrait pas, et l'utilisateur croirait le dépôt réparé en
    entier.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        espace = _espace(db, "Espace")
        discussion = _conversation(db, "c1", espace)
        _verser_puis_depot(db, espace, discussion, "tardif.pdf", "x")

        reponse = IPC.espace_depot_reparer(db, {"confirme": True})

        assert reponse["repares"]["index_manquant"] == 1
        assert reponse["apres"]["sain"] is True
        assert reponse["non_repares"] == []
        # L'« avant » doit rester consultable : c'est lui qui prouve qu'il y avait
        # quelque chose à faire, sans quoi l'écran ne peut pas annoncer le changement.
        assert reponse["avant"]["total"] == 1
    finally:
        db.close()


# ---------- 4. Le canal de lecture ne demande aucun espace ----------

def test_le_canon_de_verification_repond_sans_workspace_id(base_depot):
    """`espace_depot` exige un `workspaceId` ; celui-ci n'en demande aucun.

    S'il avait appelé `_workspace_id`, il lèverait `WORKSPACE_UNKNOWN` sur des `params`
    vides et la section Réglages afficherait « état inconnu » en permanence. Ce test
    verrouille la différence entre les deux canaux.
    """
    _, sessions = base_depot
    db = sessions()
    try:
        espace = _espace(db, "Espace")
        discussion = _conversation(db, "c1", espace)
        _verser_puis_depot(db, espace, discussion, "tardif.pdf", "x")

        rapport = IPC.espace_depot_verifier(db, {})

        assert rapport["total"] == 1
        assert rapport["espaces"][0]["nom"] == "Espace"
        with pytest.raises(ValueError, match="WORKSPACE_UNKNOWN"):
            IPC.espace_depot(db, {})
    finally:
        db.close()