# tests/backend/test_resolution_chemins.py — la portabilité, preuves à l'appui
#
# §1.5 de la Bible : « on ne dépend pas de code tiers pour les inférences etc.
# Tout doit être dans le dossier du projet. C'est le but de portable. »
#
# Ce fichier ne teste pas une implémentation, il teste une CONTRAINTE. Chaque test
# pose une installation(portable) et vérifie une conséquence de la règle :
#   - le runtime se trouve à côté de l'application, sans variable d'environnement ;
#   - une variable fausse ne l'emporte pas sur un fichier réellement présent ;
#   - le `sha256` déclaré est VÉRIFIÉ, et un fichier altéré est refusé ;
#   - le modèle se trouve depuis le dossier de données, quel que soit le CWD ;
#   - une application déployée n'emprunte JAMAIS `python3` au PATH.
#
# AUCUN binaire réel n'est requis : les binaires sont des fichiers de trois
# octets, et rien n'est exécuté. C'est volontaire — la résolution doit être
# prouvable sur une machine sans GPU, sans pilote et sans Python de construction.
#
# AUCUNE écriture hors de `tmp_path` : chaque installation est un dossier temporaire
# avec son `portable.json` et son `distribution.json`, donc la base réelle et les
# modèles du dépôt ne sont jamais touchés.
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from backend.config import paths
from backend.config.paths import (
    DependanceSystemeAbsente,
    IntegriteManifesteRompue,
    ManifesteIncoherent,
    ResolutionImpossible,
    declaration_runtime_inference,
    est_mode_developpement,
    exiger_nvidia_smi,
    resoudre_executable,
    resoudre_runtime_inference,
    sha256_fichier,
)

VARIABLES_A_NEUTRALISER = (
    "THOT_LLAMA_SERVER",
    "THOT_LFM_MODEL",
    "THOT_COLBERT_PATH",
    "THOT_NVIDIA_SMI",
    "THOT_PORTABLE",
    "THOT_PACKAGED",
)


@pytest.fixture(autouse=True)
def isole_variables(monkeypatch):
    """Aucune variable de résolution ne doit venir de la machine de qui lance les
    tests. Un test qui passe « par hasard » parce que le poste a une variable
    posée ne prouve rien — et le CI le prouverait en rouge.
    """
    for nom in VARIABLES_A_NEUTRALISER:
        monkeypatch.delenv(nom, raising=False)
    monkeypatch.setenv("THOT_PORTABLE", "1")


def faux_binaire(chemin: Path, contenu: bytes = b"#!/bin/false\n") -> Path:
    """Un exécutable qui n'exécute rien. Ce qu'on prouve, c'est la RÉSOLUTION."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    chemin.chmod(chemin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return chemin


def installation(tmp_path: Path, manifeste: dict | None = None) -> Path:
    """Un dossier d'application portable : le marqueur + le manifeste.

    Le `portable.json` n'est pas décoratif : c'est lui qui fait de ce dossier la
    racine que cherche `racines_application()`. Sans lui, la racine portable
    serait le dépôt lui-même et les tests mesureraient le poste de développement.
    """
    racine = tmp_path / "app"
    racine.mkdir(parents=True, exist_ok=True)
    (racine / "portable.json").write_text(
        json.dumps({"format_version": 1, "mode": "portable_strict"}), encoding="utf-8"
    )
    if manifeste is not None:
        (racine / "distribution.json").write_text(json.dumps(manifeste), encoding="utf-8")
    return racine


def pointe_sur(racine: Path, monkeypatch) -> None:
    """Fait de `racine` le dossier de données ET la racine portable de l'app."""
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setenv("THOT_DATA_DIR", str(racine / "data"))


# ══════════════════════════════════════════════════════════════════════════════
# 1. Le runtime se trouve à côté de l'application, SANS variable d'environnement
# ══════════════════════════════════════════════════════════════════════════════


def test_runtime_trouve_sans_une_seule_variable(tmp_path, monkeypatch) -> None:
    """AUCUNE variable posée, un faux binaire à côté de l'app : c'est trouvé.

    C'est le test qui nie la violation. Avant, `THOT_LLAMA_SERVER` absent était une
    exception (`THOT_LLAMA_SERVER absent`) : sans elle, l'application ne démarrait
    pas, quelle que soit la contenu de son dossier.
    """
    racine = installation(tmp_path)
    faux_binaire(racine / "bin" / "llama-server")
    pointe_sur(racine, monkeypatch)
    assert "THOT_LLAMA_SERVER" not in os.environ

    resolution = resoudre_runtime_inference()

    assert resolution.chemin == racine / "bin" / "llama-server"
    assert resolution.origine.startswith("application :")


def test_manifeste_minimal_suffit_pour_trouver_le_runtime(tmp_path, monkeypatch) -> None:
    """Le manifeste peut ne rien déclarer : le code sait où un runtime se pose.

    `enabled: false, binary: null` est l'état réel du dépôt aujourd'hui. Il doit
    signifier « rien n'est déclaré », pas « rien ne peut fonctionner ».
    """
    racine = installation(
        tmp_path, {"tool_runtime": {"llama_server": {"enabled": False, "binary": None}}}
    )
    faux_binaire(racine / "bin" / "llama-server")
    pointe_sur(racine, monkeypatch)

    assert resoudre_runtime_inference().chemin == racine / "bin" / "llama-server"


def test_search_dirs_du_manifeste_sajoute_un_emplacement(tmp_path, monkeypatch) -> None:
    """`search_dirs` du manifeste complète les emplacements du code.

    Le champ est facultatif : `bin/llama-server` reste trouvé sans lui (test
    précédent). Ici on prouve qu'un emplacement supplémentaire, décidé par la
    release, est pris en compte — c'est ce qui permet à un conditionnement de
    choisir son propre `bin/` sans toucher au code.
    """
    racine = installation(
        tmp_path,
        {
            "tool_runtime": {
                "llama_server": {
                    "enabled": False,
                    "binary": None,
                    "search_dirs": ["runtime/llama-server"],
                }
            }
        },
    )
    faux_binaire(racine / "runtime" / "llama-server")
    pointe_sur(racine, monkeypatch)

    resolution = resoudre_runtime_inference()

    assert resolution.chemin == racine / "runtime" / "llama-server"
    assert "runtime/llama-server" in declaration_runtime_inference().emplacements


# ══════════════════════════════════════════════════════════════════════════════
# 2. Le sort exact d'une variable d'environnement
# ══════════════════════════════════════════════════════════════════════════════


def test_une_surcharge_valide_gagne_sur_le_dossier(tmp_path, monkeypatch) -> None:
    """POSÉE ET VALIDE, la variable gagne : l'opérateur l'a demandée."""
    racine = installation(tmp_path)
    faux_binaire(racine / "bin" / "llama-server")
    ailleurs = faux_binaire(tmp_path / "ailleurs" / "llama-server")
    pointe_sur(racine, monkeypatch)
    monkeypatch.setenv("THOT_LLAMA_SERVER", str(ailleurs))

    assert resoudre_runtime_inference().chemin == ailleurs


def test_une_surcharge_fausse_ne_l_emporte_pas_sur_le_disque(tmp_path, monkeypatch) -> None:
    """POSÉE ET FAUSSE, la variable NE L'EMPORTE PAS.

    Le choix est explicite, donc il est testé. Respecter un pointeur mort
    rendrait l'application inutilisable sur un poste qui a oublié la variable
    depuis six mois — exactement le défaut qu'on supprime, déplacé d'un cran.
    Le refus est VISIBLE : il est dans l'origine retenue, donc il se lit.
    """
    racine = installation(tmp_path)
    faux_binaire(racine / "bin" / "llama-server")
    pointe_sur(racine, monkeypatch)
    monkeypatch.setenv("THOT_LLAMA_SERVER", str(tmp_path / "absent" / "llama-server"))

    resolution = resoudre_runtime_inference()

    assert resolution.chemin == racine / "bin" / "llama-server"
    assert resolution.origine.startswith("application :")
    assert any("THOT_LLAMA_SERVER" in ecart for ecart in resolution.ecarts)


def test_surcharge_fausse_sans_rien_d_autre_nomme_le_chemin_manquant(tmp_path, monkeypatch) -> None:
    """Sans rien d'autre, le refus DIT ce qui a été cherché.

    « introuvable » seul laisserait deviner entre une clé USB mal branchée, une
    release jamais construite et une variable mal orthographiée. Les trois se
    réparent différemment, donc les trois doivent être lisibles dans le refus.
    """
    racine = installation(tmp_path)
    pointe_sur(racine, monkeypatch)
    monkeypatch.setenv("THOT_LLAMA_SERVER", "/nulle/part/llama-server")

    with pytest.raises(ResolutionImpossible) as echec:
        resoudre_runtime_inference()

    message = str(echec.value)
    assert "/nulle/part/llama-server" in message
    assert str(racine / "bin" / "llama-server") in message


def test_un_chemin_explicite_absent_est_un_bug_d_appelant(tmp_path, monkeypatch) -> None:
    """Niveau 1 : l'appelant SAIT. S'il se trompe, on ne va pas chercher ailleurs."""
    racine = installation(tmp_path)
    faux_binaire(racine / "bin" / "llama-server")
    pointe_sur(racine, monkeypatch)

    with pytest.raises(ResolutionImpossible) as echec:
        resoudre_runtime_inference(tmp_path / "absent" / "llama-server")

    assert "chemin explicite absent" in str(echec.value)


# ══════════════════════════════════════════════════════════════════════════════
# 3. L'INTÉGRITÉ : le sha256 déclaré est vérifié, un binaire altéré est refusé
# ══════════════════════════════════════════════════════════════════════════════


def _manifeste_integrite(racine: Path, binaire: Path, empreinte: str) -> dict:
    return {
        "tool_runtime": {
            "llama_server": {
                "enabled": True,
                "binary": "bin/llama-server",
                "sha256": empreinte,
            }
        },
        "_racine_inutilisee": str(racine),
    }


def test_sha256_declare_est_verifie(tmp_path, monkeypatch) -> None:
    """Un runtime conforme passe, et le résultat DIT qu'il a été vérifié.

    Sans ce drapeau, « trouvé » et « trouvé et conforme » seraient le même mot.
    Ils ne le sont pas : le premier peut être le binaire d'un autre.
    """
    racine = installation(tmp_path)
    binaire = faux_binaire(racine / "bin" / "llama-server", b"#version reelle\n")
    # Le manifeste se rédige APRÈS le fichier : il annonce ce qui est construit.
    (racine / "distribution.json").write_text(
        json.dumps(_manifeste_integrite(racine, binaire, sha256_fichier(binaire))),
        encoding="utf-8",
    )
    pointe_sur(racine, monkeypatch)

    resolution = resoudre_runtime_inference()

    assert resolution.chemin == binaire
    assert resolution.sha256_verifie is True
    assert resolution.sha256_attendu == sha256_fichier(binaire)


def test_binaire_altere_est_refuse(tmp_path, monkeypatch) -> None:
    """Un octet de plus, et l'application REFUSE de démarrer.

    Une application portable qui n'empêche pas de lancer autre chose que le
    runtime qu'elle a construit ne transporte pas son runtime : elle transporte
    une confiance. Le refus nomme les deux empreintes, parce que « ça ne
    correspond pas » sans les deux valeurs oblige à refaire le calcul.
    """
    racine = installation(tmp_path)
    binaire = faux_binaire(racine / "bin" / "llama-server", b"#version reelle\n")
    empreinte = sha256_fichier(binaire)
    (racine / "distribution.json").write_text(
        json.dumps(_manifeste_integrite(racine, binaire, empreinte)), encoding="utf-8"
    )
    pointe_sur(racine, monkeypatch)

    # Altération d'un seul octet : ce que fait une copie interrompue, un
    # téléchargement tronqué ou une modification.
    binaire.write_bytes(b"#version alteree\n")

    with pytest.raises(IntegriteManifesteRompue) as echec:
        resoudre_runtime_inference()

    assert echec.value.attendu == empreinte
    assert echec.value.obtenu == sha256_fichier(binaire)
    assert echec.value.obtenu != echec.value.attendu
    assert "intégrité rompue" in str(echec.value)


def test_la_resolution_par_llama_server_traduit_le_refus_en_LlamaServerError(
    tmp_path, monkeypatch
) -> None:
    """Le module qui lance le runtime parle avec SES exceptions à l'appelant.

    `default_llama_server_config()` est le point d'entrée de `build_runtime()`. Il
    doit échouer en `LlamaServerError` — mais SANS perdre le message, qui est la
    seule chose qui dise où chercher.
    """
    from backend.inference.llama_server import LlamaServerError, default_llama_server_config

    racine = installation(tmp_path)
    binaire = faux_binaire(racine / "bin" / "llama-server", b"#version reelle\n")
    (racine / "distribution.json").write_text(
        json.dumps(_manifeste_integrite(racine, binaire, sha256_fichier(binaire))),
        encoding="utf-8",
    )
    pointe_sur(racine, monkeypatch)
    binaire.write_bytes(b"#version alteree\n")

    with pytest.raises(LlamaServerError) as echec:
        default_llama_server_config()

    assert "intégrité rompue" in str(echec.value)
    assert str(racine / "bin" / "llama-server") in str(echec.value)


def test_manifeste_incoherent_refuse_de_promettre(tmp_path, monkeypatch) -> None:
    """`enabled: true` SANS binaire est un manifeste qui promet l'impossible.

    Le laisser passer produirait exactement le symptôme interdit : une
    application qui ne démarre pas et qui ne dit pas pourquoi.
    """
    racine = installation(
        tmp_path, {"tool_runtime": {"llama_server": {"enabled": True, "binary": None}}}
    )
    pointe_sur(racine, monkeypatch)

    with pytest.raises(ManifesteIncoherent, match="sans binaire"):
        declaration_runtime_inference()


def test_un_manifeste_illisible_ne_tient_pas_le_demarrage(tmp_path, monkeypatch) -> None:
    """Un manifeste CORROMPU n'est pas une erreur de démarrage, mais il se dit.

    Le refuser serait honnête et ruineux : il ferait dépendre le développement
    d'un fichier de release, et une release ne se répare pas. Le tolérer en
    silence serait pire — on ne peut pas vérifier d'intégrité ce qu'on n'a pas
    lu, donc la résolution doit savoir qu'elle n'a rien vérifié. Ici le
    résultat tient les deux : la résolution aboutit, et le manifeste illisible
    rend `{}` au lieu d'inventer une déclaration.
    """
    racine = installation(tmp_path)
    (racine / "distribution.json").write_text("{ ceci n'est pas du json", encoding="utf-8")
    faux_binaire(racine / "bin" / "llama-server")
    pointe_sur(racine, monkeypatch)

    assert paths.lire_manifeste() == {}
    assert declaration_runtime_inference().livre is False
    assert resoudre_runtime_inference().chemin == racine / "bin" / "llama-server"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Le modèle vient du dossier de données, ET DU CWD QUOI QU'IL ARRIVE
# ══════════════════════════════════════════════════════════════════════════════


def test_modele_trouve_depuis_le_dossier_de_donnees_hors_cwd(tmp_path, monkeypatch) -> None:
    """Le test CHANGE de répertoire courant, et pose un leurre dedans.

    `Path.cwd()` était la résolution d'avant : `(Path.cwd() / model_path)`. Un
    leurre posé dans le répertoire courant et un modèle absent du dossier de
    données donnaient « trouvé » ; l'inverse donnait « absent » alors que le
    modèle était là. Ici les deux sont posés, et le modèle de l'application
    gagne — ce qui prouve les deux moitiés d'un coup.
    """
    from backend.inference.llama_server import chemin_modele

    racine = installation(tmp_path)
    modele_app = racine / "models" / "LLM" / "LFM2.5-8B-A1B-UD-Q4_K_M.gguf"
    modele_app.parent.mkdir(parents=True)
    modele_app.write_bytes(b"GGUF-de-l-application")

    leurre = tmp_path / "cwd-quelconque"
    (leurre / "models" / "LLM").mkdir(parents=True)
    (leurre / "models" / "LLM" / "LFM2.5-8B-A1B-UD-Q4_K_M.gguf").write_bytes(b"GGUF-leurre")

    pointe_sur(racine, monkeypatch)
    monkeypatch.chdir(leurre)
    assert Path.cwd() == leurre

    assert chemin_modele() == modele_app


def test_le_dossier_de_donnees_prime_sur_le_bundle_de_developpement(
    tmp_path, monkeypatch
) -> None:
    """Le modèle du poste ne doit pas gagner sur celui de la clé USB.

    Le dépôt de développement contient un vrai `models/LLM/x.gguf` de plusieurs
    gigaoctets. En mode portable, c'est celui de la clé qui doit servir : sinon
    l'application transportée écrit ailleurs et lit le modèle de celui qui l'a
    construite.
    """
    from backend.inference.llama_server import chemin_modele

    racine = installation(tmp_path)
    modele_app = racine / "models" / "LLM" / "LFM2.5-8B-A1B-UD-Q4_K_M.gguf"
    modele_app.parent.mkdir(parents=True)
    modele_app.write_bytes(b"GGUF-de-la-cle")

    pointe_sur(racine, monkeypatch)

    assert chemin_modele() == modele_app
    assert not chemin_modele().is_relative_to(paths.get_bundle_dir()) or paths.get_bundle_dir() == racine


def test_surcharge_de_modele_posee_et_valide_gagne(tmp_path, monkeypatch) -> None:
    """`THOT_LFM_MODEL` reste une surcharge : posée, elle gagne."""
    from backend.inference.llama_server import chemin_modele

    racine = installation(tmp_path)
    (racine / "models" / "LLM").mkdir(parents=True)
    (racine / "models" / "LLM" / "LFM2.5-8B-A1B-UD-Q4_K_M.gguf").write_bytes(b"defaut")
    ailleurs = tmp_path / "ailleurs" / "autre.gguf"
    ailleurs.parent.mkdir(parents=True)
    ailleurs.write_bytes(b" surcharge")
    pointe_sur(racine, monkeypatch)
    monkeypatch.setenv("THOT_LFM_MODEL", str(ailleurs))

    assert chemin_modele() == ailleurs


# ══════════════════════════════════════════════════════════════════════════════
# 5. COLBERT : même règle, et le test du chemin du modèle vaut aussi pour lui
# ══════════════════════════════════════════════════════════════════════════════


def test_colbert_resolu_depuis_le_dossier_de_donnees(tmp_path, monkeypatch) -> None:
    """ColBERT est un modèle comme un autre : il vit dans le dossier de l'app.

    Avant : `parents[2]/models`, donc le SOURCE. Dans un sidecar gelé, `__file__`
    est extrait dans `_MEIPASS` et le dossier n'y est pas — le moteur se
    déclarait absent alors qu'il était posé à côté de l'application.
    """
    from backend.search import vectors as V

    racine = installation(tmp_path)
    modele = racine / "models" / "colbert" / "LFM2.5-ColBERT-350M"
    (modele / "1_Dense").mkdir(parents=True)
    (modele / "config.json").write_text("{}", encoding="utf-8")
    (modele / "model.safetensors").write_bytes(b"poids")
    (modele / "1_Dense" / "model.safetensors").write_bytes(b"projection")

    leurre = tmp_path / "cwd-colbert"
    leurre.mkdir()
    pointe_sur(racine, monkeypatch)
    monkeypatch.chdir(leurre)

    assert V.chemin_modele() == modele
    # Et le statut ne ment pas sur la pièce manquante : `CHEMIN_DENSE` est relatif,
    # donc il est testé contre le dossier TROUVÉ, pas contre celui du dépôt.
    statut = V.statut_moteur()
    assert statut["moteur"] == "colbert", statut
    assert statut["chemin"] == str(modele)


def test_colbert_surcharge_fausse_ne_remplace_pas_le_dossier_trouve(
    tmp_path, monkeypatch
) -> None:
    """Une surcharge morte est consignée puis écartée : le modèle est là, on l'utilise."""
    from backend.search import vectors as V

    racine = installation(tmp_path)
    modele = racine / "models" / "colbert" / "LFM2.5-ColBERT-350M"
    modele.mkdir(parents=True)
    pointe_sur(racine, monkeypatch)
    monkeypatch.setenv("THOT_COLBERT_PATH", "/nulle/part/colbert")

    assert V.chemin_modele() == modele


# ══════════════════════════════════════════════════════════════════════════════
# 6. EN DÉPLOYÉ, LE PATH EST STRUCTURALEMENT FERMÉ
# ══════════════════════════════════════════════════════════════════════════════


def test_en_deploye_le_path_python3_nest_pas_emprunte(tmp_path, monkeypatch) -> None:
    """La preuve centrale du mode packagé, en deux temps.

    Temps 1 : un `python3` FAUX est sur le PATH, et il EST trouvé. Sans ce temps,
    le test d'après ne prouverait rien — il pourrait passer parce que la
    résolution est cassée.
    Temps 2 : `THOT_PACKAGED=1`, même PATH, et la résolution refuse. Le PATH n'est
    pas « moins utilisé », il n'est pas consulté : l'écart le dit dans le
    message d'erreur.
    """
    faux = faux_binaire(tmp_path / "bin-positif" / "python3")
    monkeypatch.setenv("PATH", str(tmp_path / "bin-positif"))
    monkeypatch.setattr(paths, "NOM_LLAMA_SERVER", "python3", raising=False)

    # Temps 1 — en développement, le PATH est un niveau légitime.
    assert est_mode_developpement() is True
    assert resoudre_executable("python3").chemin == faux

    # Temps 2 — déployé, ce même PATH est fermé.
    monkeypatch.setenv("THOT_PACKAGED", "1")
    assert est_mode_developpement() is False
    with pytest.raises(ResolutionImpossible) as echec:
        resoudre_executable("python3")

    message = str(echec.value)
    assert "PATH non consulté" in message
    assert "application déployée" in message


def test_un_depot_python3_ne_tient_pas_le_cote_python(tmp_path, monkeypatch) -> None:
    """Le refus doit être LISIBLE, pas seulement vrai.

    Sans le motif dans le message, le diagnostic retombe sur « ça marche chez
    moi » — c'est-à-dire exactement sur le poste du développeur.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "bin-vide"))
    monkeypatch.setenv("THOT_PACKAGED", "1")

    with pytest.raises(ResolutionImpossible) as echec:
        resoudre_executable("python3")

    assert "PATH non consulté : application déployée, pas un checkout source" in str(echec.value)


# ══════════════════════════════════════════════════════════════════════════════
# 7. nvidia-smi : un outil de pilote, dont l'absence doit être FRANCHE
# ══════════════════════════════════════════════════════════════════════════════


def test_nvidia_smi_absent_leve_avec_son_consequence_et_son_remede(
    tmp_path, monkeypatch
) -> None:
    """Aucun `nvidia-smi` sur cette machine : ce qui en dépend LEVE.

    Le message ne dit pas seulement « introuvable ». Il dit ce qui cesse de
    fonctionner (« aucune VRAM ne peut être MESURÉE, les portes mémoire passeraient
    au vert ») et ce que l'utilisateur peut faire. Un refus sans issue se vit
    comme un défaut, et se signale comme un bug.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "bin-vide"))
    monkeypatch.setattr(paths, "CHEMINS_NVIDIA_SMI", (str(tmp_path / "absent" / "nvidia-smi"),))
    monkeypatch.setattr(paths, "EMPLACEMENTS_NVIDIA_SMI", ())

    with pytest.raises(DependanceSystemeAbsente) as echec:
        exiger_nvidia_smi()

    message = str(echec.value)
    assert "MESURÉE" in message
    assert paths.VARIABLE_NVIDIA_SMI in message


def test_nvidia_smi_manquant_est_nomme_dans_la_detection(tmp_path, monkeypatch) -> None:
    """« Pas de carte » et « pas d'outil » ne doivent pas être le même `None`.

    La cascade retombe légitimement sur le CPU, mais elle doit le dire. Avant,
    `_run` renvoyait `None` dans les deux cas et le produit ne pouvait pas les
    distinguer — donc il ne pouvait pas empêcher une porte mémoire de passer
    sur des zéros.
    """
    from backend.inference import hardware

    monkeypatch.setattr(hardware, "outil_present", lambda nom: False)

    cfg = hardware.HardwareDetector().detect()

    assert cfg.backend == "cpu"
    assert "nvidia-smi" in cfg.dependances_absentes


def test_nvidia_smi_present_ne_produit_pas_dabsence_declaree(monkeypatch) -> None:
    """Le cas nominal ne doit PAS annoncer une absence : sinon le champ est du bruit."""
    from backend.inference import hardware

    monkeypatch.setattr(hardware, "outil_present", lambda nom: True)
    monkeypatch.setattr(
        hardware,
        "_run",
        lambda cmd: "NVIDIA GeForce RTX 4070 SUPER, 12282, 11500\n",
    )

    cfg = hardware.HardwareDetector().detect()

    assert cfg.backend == "cuda"
    assert cfg.dependances_absentes == ()