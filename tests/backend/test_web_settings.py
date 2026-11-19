"""Réglages de la recherche web — le fichier, l'environnement, et leur effet.

Ce fichier ne teste pas une fonction : il teste la **promesse** que fait le
socle à l'utilisateur quand il écrit dans une case à cocher. La promesse est
en quatre morceaux, et chacun a sa section ici :

* **Un réglage illisible ne tombe jamais.** Fichier absent, tronqué, illisible,
  écrit par une version future : on rend le défaut, ou ce qui est lisible, et
  aucune exception. Un réglage cassé ne peut pas faire tomber un tour de chat.
* **L'environnement l'emporte, dans les deux sens.** Un déploiement et un E2E
  imposent un mode sans toucher au disque de l'utilisateur ; le fichier ne
  s'applique donc que là où aucune variable ne parle. C'est vérifié dans les DEUX
  sens, parce qu'un ordre qui ne tient que dans un sens n'est pas un ordre.
* **Un réglage s'applique à chaud.** Écrire puis relire, sans rien relancer : un
  utilisateur qui ferme la recherche ne doit pas avoir à redémarrer Thot.
* **Une écriture qui échoue se voit.** `write()` rend l'état RELU, jamais
  l'état espéré ; une écriture impossible rend `written=False` sans mentir sur
  le reste de la charge utile.

L'effet est mesuré là où il doit agir, jamais dans le réglage lui-même : le mode
par `research.current_mode()` et par le rendu `WEB_DISABLED`, le moteur par
`registry.applicable_providers()` et par le `NO_PROVIDER` qui en découle, et
l'offre faite au modèle par la composition de `tool_defs`.

**Aucune machine, aucun réseau.** La fixture `installation_nue` pointe le
fichier de réglages sur un chemin temporaire et efface les deux variables
d'environnement qui le concurrencent : aucun test de ce fichier ne peut mesurer
la configuration de la personne qui l'exécute. `net.http_get` est posé sur un
`pytest.fail` — c'est l'unique sortie réseau du projet pour le web, donc le
poser ne simule pas le réseau, il rend son absence vérifiable. La base est un
vrai fichier SQLite temporaire, comme partout ailleurs dans le dépôt.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.inference import loop
from backend.inference import web_settings as _ws
from backend.services.web import net, research, settings
from backend.services.web.providers import registry
from backend.services.web.providers.searxng import SEARXNG_URL_ENV, SearxngProvider
from backend.services.web.settings import SETTINGS_ENV, SETTINGS_FILENAME
from backend.services.web.types import NO_PROVIDER, WEB_DISABLED

# --------------------------------------------------------------------------
# Vocabulaire du test
# --------------------------------------------------------------------------
# Le fichier de réglages, tel que l'interface l'écrit : `clé=valeur`, une
# ligne par clé. Ce n'est pas une invention de test — c'est le format que le
# contrat `web.settings` impose à l'écrivain, Rust compris.
LIGNE_DESACTIVEE = "enabled=0"
LIGNE_SEARXNG = "searxng_url=http://127.0.0.1:8080"
# Le mode que la boucle applique quand le réseau est fermé, et le code qu'elle
# rend alors : le premier est un mot, le second ce que le brief sait lire.
MODE_FERME = research.MODE_LOCAL_ONLY
MODE_OUVERT = research.MODE_WEB_ALLOWED


# --------------------------------------------------------------------------
# Outillage
# --------------------------------------------------------------------------
@pytest.fixture()
def session_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Une vraie base, jetable : fichier SQLite temporaire, ordre de `init_db()`."""
    from backend.db.migrations import apply_migrations
    from backend.db.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'web_reglages.db'}")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", factory)
    return factory


@pytest.fixture(autouse=True)
def installation_nue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ni le disque ni l'environnement du développeur n'atteignent ce test.

    Le fichier de réglages est pointé sur un chemin temporaire — donc absent
    au départ, comme sur une installation neuve — et les deux variables qui le
    concurrencent (`THOT_WEB_MODE`, `THOT_WEB_SEARXNG_URL`) sont effacées. Sans
    cela, un `web_search.settings` laissé sur la machine ferait échouer la
    moitié de ce fichier, et le serait dans le sens inverse selon le développeur.
    """
    monkeypatch.setenv(SETTINGS_ENV, str(tmp_path / SETTINGS_FILENAME))
    monkeypatch.delenv(research.MODE_ENV, raising=False)
    monkeypatch.delenv(SEARXNG_URL_ENV, raising=False)
    net.reset_host_gate()
    monkeypatch.setattr(
        net, "http_get",
        lambda *a, **k: pytest.fail("ce test ne doit pas ouvrir de socket : le réseau est interdit"),
    )
    yield
    net.reset_host_gate()


def _ecrire_fichier(contenu: str) -> Path:
    """Écrit le fichier de réglages pointé par la fixture, et rend son chemin.

    Le contenu est écrit tel quel : c'est le texte qu'un éditeur — ou une
    version future du produit — déposerait, coupures et clés inconnues
    comprises.
    """
    chemin = settings.settings_path()
    chemin.write_text(contenu, encoding="utf-8")
    return chemin


# ==========================================================================
# A. Un fichier illisible ne fait jamais tomber une lecture
# ==========================================================================
def test_un_fichier_absent_rend_les_defauts_et_le_dit() -> None:
    """Pas de fichier = installation neuve : tout est actif, et c'est annoncé.

    `source == "defaut"` est ce qui permet à l'interface de distinguer « rien
    n'a été réglé » de « quelqu'un a réglé » : sans cette nuance, une case
    cochée ne dit pas si elle vient de l'utilisateur ou du code.
    """
    assert not settings.settings_path().exists()

    etat = settings.read()

    assert etat.enabled is True
    assert etat.provider == settings.PROVIDER_AUTO
    assert etat.searxng_url == ""
    assert etat.source == "defaut"
    assert settings.enabled_or_disabled() == (True, "defaut")
    assert settings.provider_choice() == settings.PROVIDER_AUTO
    assert settings.mode_texte(etat) == MODE_OUVERT


def test_un_fichier_illisible_ne_leve_pas_et_rend_les_defauts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un chemin illisible est un réglage absent, jamais une exception.

    Le chemin est ici un RÉPERTOIRE : la lecture échoue au niveau du système de
    fichiers, pas du format. C'est la panne la plus fréquente sur une machine
    réelle (un dossier à la place du fichier, un disque non monté) et elle ne
    doit pas se distinguer d'un fichier vide.
    """
    chemin = tmp_path / "reglages_sont_un_dossier"
    chemin.mkdir()
    monkeypatch.setenv(SETTINGS_ENV, str(chemin))

    etat = settings.read()

    assert etat.enabled is True
    assert etat.source == "defaut"
    assert settings.provider_choice() == settings.PROVIDER_AUTO


# Trois genres de fichiers illisibles, sept cas : ce qui n'est pas exploitable
# ne doit jamais devenir un réglage. Une ligne qui ne porte AUCUNE clé de ce
# fichier — un commentaire, une clé de version future, une ligne tronquée —
# laisse `source` à « defaut » : « l'utilisateur a réglé quelque chose » ne
# doit pas devenir vrai parce qu'un fichier existe.
_TRONQUES: tuple[tuple[str, bool, str, str], ...] = (
    ("", True, "defaut", "vide"),
    ("\n\n   \n", True, "defaut", "blancs"),
    ("# commentaire\n", True, "defaut", "commentaire"),
    ("enabled", True, "defaut", "cle_sans_signe_egal"),
    ("=0", True, "defaut", "cle_absente"),
    ("\x00\xff", True, "defaut", "octets_illisibles"),
    # `enabled=` (valeur tronquée) : une clé présente mais VIDE n'est pas un
    # réglage, c'est une écriture interrompue. Elle vaut le défaut — sinon une
    # ligne tronquée suffisait à fermer la recherche sans que personne l'ait
    # demandé. Le défaut était mesuré par ce test, qui l'assertait ; il est
    # corrigé, et le test affirme désormais l'ouverture.
    ("enabled=", True, "defaut", "valeur_absente"),
)


@pytest.mark.parametrize("contenu,actif_attendu,source_attendue,cas", _TRONQUES,
                         ids=[cas for _, _, _, cas in _TRONQUES])
def test_un_fichier_illisible_ne_leve_pas_et_rend_ce_qui_est_lisible(
    contenu: str, actif_attendu: bool, source_attendue: str, cas: str,
) -> None:
    """Ce qui n'est pas lisible ne devient pas un réglage — et ne lève pas.

    Le rendu est celui d'une installation neuve pour tout ce qui n'est pas
    déclaré : un fichier illisible ne doit pas pouvoir éteindre la recherche,
    choisir un moteur ou inventer une adresse. L'`source` rend compte du
    fichier tel qu'il a été LU, pas de ce qu'il a permis de lire.
    """
    _ecrire_fichier(contenu)

    etat = settings.read()

    assert etat.enabled is actif_attendu, cas
    assert etat.provider == settings.PROVIDER_AUTO
    assert etat.searxng_url == ""
    assert etat.source == source_attendue, cas
    assert settings.mode_texte(etat) == (MODE_OUVERT if actif_attendu else WEB_DISABLED)


def test_une_cle_inconnue_est_ignoree_et_le_reste_est_lu() -> None:
    """Une clé d'une version future n'empêche pas de lire les clés d'aujourd'hui.

    Le fichier est lu clé par clé : ce qui est connu s'applique, ce qui ne
    l'est pas est ignoré. Le refuser en bloc ferait perdre un mode valide pour
    une raison sans rapport avec lui.
    """
    _ecrire_fichier("qualite_du_web=haute\n" + LIGNE_DESACTIVEE + "\n" + LIGNE_SEARXNG + "\n")

    etat = settings.read()

    assert etat.enabled is False
    assert etat.searxng_url == "http://127.0.0.1:8080"
    assert etat.source == "fichier"


def test_un_fichier_sans_la_cle_enabled_laisse_le_recherche_active() -> None:
    """L'absence de `enabled` vaut « actif », pas « fermé ».

    C'est le seul défaut qui ne soit pas neutre : un fichier qui ne parle que
    de l'adresse SearXNG ne doit pas éteindre la recherche web par surprise.
    """
    _ecrire_fichier(LIGNE_SEARXNG + "\n")

    assert settings.read().enabled is True


# ==========================================================================
# B. L'environnement l'emporte, dans les deux sens
# ==========================================================================
# Les deux sens de la priorité, et une seule écriture de variable : `0`/`1`,
# que `settings.read()` sait lire comme un booléen. Le mode se lit alors par
# les deux couches, qui doivent dire la même chose.
_ENV_PARLE: tuple[tuple[str, str, str], ...] = (
    ("enabled=0\n", "1", MODE_OUVERT),   # la variable ouvre un fichier qui ferme
    ("enabled=1\n", "0", MODE_FERME),   # la variable ferme un fichier qui ouvre
)


@pytest.mark.parametrize("contenu_fichier,valeur_env,mode_attendu", _ENV_PARLE,
                         ids=["env_ouvre_un_fichier_ferme", "env_ferme_un_fichier_ouvert"])
def test_l_environnement_l_emporte_sur_le_fichier_dans_les_deux_sens(
    contenu_fichier: str, valeur_env: str, mode_attendu: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ce que la variable décide, le fichier ne peut pas le reprendre.

    Le mode se lit par la variable ET par la charge utile : les deux doivent
    dire la même chose, sinon l'interface affiche « désactivé » pendant que la
    boucle ouvre le réseau — ou l'inverse, un réglage que rien n'applique.
    `writable=False` complète le tableau : une case que l'environnement
    refuse ne doit pas rester cochable.
    """
    _ecrire_fichier(contenu_fichier)
    monkeypatch.setenv(research.MODE_ENV, valeur_env)

    charge = _ws.apply_and_describe({})

    assert research.current_mode() == mode_attendu
    assert charge[_ws.OUT_SOURCE] == "env"
    assert charge[_ws.OUT_ENABLED] is (mode_attendu == MODE_OUVERT)
    assert charge[_ws.OUT_WRITABLE] is False, "l'interface doit savoir qu'elle ne peut pas écrire"
    assert charge[_ws.OUT_MODE] == (WEB_DISABLED if mode_attendu == MODE_FERME else MODE_OUVERT)


# Ces deux cas gardent leur valeur historique : le FICHIER dit l'inverse de la
# variable, et c'est la variable qui doit gagner. Ils vérifient que la
# précédence n'a pas été rétablie par le correctif des deux vocabulaires.
_MODES_NOMMES: tuple[tuple[str, str, str], ...] = (
    ("enabled=0\n", MODE_OUVERT, MODE_OUVERT),
    ("enabled=1\n", MODE_FERME, MODE_FERME),
)


@pytest.mark.parametrize("valeur_env,actif_attendu,mode_attendu", [
    ("local_only", False, "local_only"),
    ("web_allowed", True, "web_allowed"),
    ("0", False, "local_only"),          # le drapeau booléen reste accepté
    ("1", True, "web_allowed"),
], ids=["nomme_local_only", "nomme_web_allowed", "drapeau_0", "drapeau_1"])
def test_le_mode_nomme_est_le_meme_registre_que_les_deux_couches_lisent(
    valeur_env: str, actif_attendu: bool, mode_attendu: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une seule variable, deux lecteurs, une seule réponse.

    Le défaut mesuré : `current_mode()` lisait `local_only`/`web_allowed` et la
    page lisait `1`/`0`. Avec `THOT_WEB_MODE=web_allowed` — ce que pose
    l'armateur d'E2E — la boucle suivait la variable et la page affichait le
    FICHIER, `writable=true` : l'interface promettait un réglage qui n'était pas
    celui qui s'appliquait. Le test Pinnait la divergence ; il affirme
    aujourd'hui l'accord.
    """
    _ecrire_fichier("enabled=0\nprovider=auto\n")
    monkeypatch.setenv(research.MODE_ENV, valeur_env)

    assert research.current_mode() == mode_attendu
    payload = _ws.apply_and_describe({})
    assert payload[_ws.OUT_EFFECTIVE_ENABLED] is actif_attendu, "la page et la boucle divergent"
    assert payload[_ws.OUT_SOURCE] == "env"
    assert payload[_ws.OUT_WRITABLE] is False, "elle laisse croire qu'elle peut écrire"



def test_une_variable_inconnue_laisse_la_parole_au_fichier() -> None:
    """Une variable qui ne dit rien de connu n'impose rien.

    Ni un mode nommé, ni un booléen : la variable est du bruit, et le bruit ne
    peut pas décider du mode de l'utilisateur. Le fichier garde la parole, et il
    la garde même quand elle contient quelque chose — c'est le principe même du
    repli, mesuré ici sur sa valeur la plus simple.
    """
    _ecrire_fichier(LIGNE_DESACTIVEE + "\n")

    with pytest.MonkeyPatch.context() as contexte:
        contexte.setenv(research.MODE_ENV, "ce-que-ce-mode-ne-signifie-rien")
        assert research.current_mode() == MODE_FERME
        assert settings.read().source == "fichier"


# ==========================================================================
# C. Le réglage agit, à chaud
# ==========================================================================
def test_le_mode_ferme_par_le_fichier_ferme_le_reseau_et_le_nomme(
    session_factory: Any,
) -> None:
    """`enabled=0` rend le mode fermé, et l'appel rend `WEB_DISABLED`.

    Les deux mots sont distincts et tous deux nécessaires : `local_only` est ce
    que la boucle applique, `WEB_DISABLED` est ce que le brief sait lire. Un
    appel qui n'affiche pas le code laisse croire que la recherche a été faite
    sans rien trouver.
    """
    _ecrire_fichier(LIGNE_DESACTIVEE + "\n")

    assert research.current_mode() == MODE_FERME
    assert settings.mode_texte() == WEB_DISABLED

    issue = research.run(
        queries=["qui a decouvert les lois de l'electricite"], source_type="general",
        purpose="etablir un fait public", conv_id="c1", user_message="",
    )

    assert issue.code == WEB_DISABLED
    assert issue.ok is False
    assert issue.network_queries == 0, "un mode fermé ne part jamais vers le réseau"
    assert issue.text.startswith("[web_research] réseau fermé (mode local_only)")


def test_un_reglement_s_aplique_a_chaud_sans_rien_relancer() -> None:
    """Écrire puis relire : la boucle de l'utilisateur n'a rien à relancer.

    Un réglage qui ne s'appliquerait qu'au prochain démarrage serait un réglage
    que l'utilisateur ne peut pas vérifier. Les deux sens sont mesurés, parce
    qu'un cache qui n'invalide qu'à l'ouverture casserait le retour en arrière
    sans que rien ne le dise.
    """
    assert research.current_mode() == MODE_OUVERT

    settings.write(enabled=False)
    assert research.current_mode() == MODE_FERME
    assert _ws.apply_and_describe({})[_ws.OUT_MODE] == WEB_DISABLED

    settings.write(enabled=True)
    assert research.current_mode() == MODE_OUVERT
    assert _ws.apply_and_describe({})[_ws.OUT_MODE] == MODE_OUVERT


def test_le_moteur_choisi_est_le_seul_retenu_pour_le_type_quil_couvre(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un choix de moteur remplace l'ordre de préférence, il ne s'y ajoute pas.

    L'utilisateur qui choisit OpenAlex veut des articles évalués par les pairs,
    pas « OpenAlex en premier, arXiv ensuite » : arXiv reste donc hors de la
    recherche, et le brief ne peut pas citer une pré-publication qu'on ne lui a
    pas demandé.
    """

    # Ce test porte sur le choix du moteur, pas sur la liste de domaines :
    # on la désactive explicitement pour ne pas tester la nouvelle politique
    # par accident.
    monkeypatch.setenv("THOT_WEB_ALLOWED_DOMAINS", "*")
    _ecrire_fichier("provider=openalex\n")

    retenus = registry.applicable_providers("academic")

    assert [moteur.name for moteur in retenus] == ["openalex"]
    assert settings.provider_choice() == "openalex"


def test_un_moteur_choisi_qui_ne_couvre_pas_le_type_renvoie_aucun_moteur(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: Any,
) -> None:
    """Wikipédia pour `academic` : aucune source, et le rendu le DIT.

    Le repli silencieux sur arXiv servirait l'utilisateur à ses yeux tout en
    livrant au brief une autre nature de sources que celles qu'il a choisies.
    L'appel rend donc `NO_PROVIDER`, qui distingue « rien n'a été cherché » de
    « rien n'a été trouvé » — le seul écart que le brief ne doit jamais confondre.
    """

    # Ce test porte sur le choix du moteur, pas sur la liste de domaines :
    # on la désactive explicitement pour ne pas tester la nouvelle politique
    # par accident.
    monkeypatch.setenv("THOT_WEB_ALLOWED_DOMAINS", "*")
    _ecrire_fichier("provider=wikipedia\n")

    assert registry.applicable_providers("academic") == ()

    issue = research.run(
        queries=["effet placebo controle"], source_type="academic",
        purpose="chercher un article evalue par les pairs", conv_id="c1", user_message="",
    )

    assert issue.code == NO_PROVIDER
    assert issue.ok is False
    assert issue.network_queries == 0
    assert issue.text.startswith(f"[{NO_PROVIDER}]")


# ==========================================================================
# D. Écrire, et écrire honnêtement
# ==========================================================================
def test_write_rend_l_etat_relu_et_pas_l_etat_espere() -> None:
    """Ce que `write()` renvoie est relu depuis le disque.

    Le même appel, sans lire le disque, donnerait la même réponse même quand
    l'écriture échoue — donc l'interface annoncerait un réglage appliqué qui
    ne l'est pas. La relecture est la seule preuve qu'on peut se donner à soi-même.
    """
    ecrit, rendu = settings.write(
        enabled=False, provider="searxng", searxng_url="http://127.0.0.1:8080",
    )

    assert ecrit is True
    assert rendu == settings.read()
    assert (rendu.enabled, rendu.provider, rendu.searxng_url) == (
        False, "searxng", "http://127.0.0.1:8080",
    )
    assert settings.settings_path().read_text(encoding="utf-8") == (
        "enabled=0\nprovider=searxng\nsearxng_url=http://127.0.0.1:8080\n"
    )


def test_une_ecriture_impossible_rend_written_false_sans_mentir_du_reste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'échec d'écriture se voit, et le reste de la charge utile reste vrai.

    Le fichier demandé est un chemin dont le parent est un FICHIER : l'écriture
    échoue donc à coup sûr. La charge utile doit alors dire `written=False` **et**
    décrire l'état réellement en vigueur — celui d'une installation sans
    réglage. Annoncer `enabled=false` aurait été promettre un réglage que rien
    n'a retenu.
    """
    obstacle = tmp_path / "parent"
    obstacle.write_text("je suis un fichier, pas un dossier", encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV, str(obstacle / "web.settings"))

    charge = _ws.apply_and_describe({"enabled": False, "provider": "openalex"})

    assert charge[_ws.OUT_WRITTEN] is False
    assert charge[_ws.OUT_ERROR] == "", "une écriture qui échoue n'est pas une entrée invalide"
    assert charge[_ws.OUT_ENABLED] is True, "l'état rendu est l'état relu, pas l'état demandé"
    assert charge[_ws.OUT_PROVIDER] == settings.PROVIDER_AUTO
    assert charge[_ws.OUT_SOURCE] == "defaut"
    assert charge[_ws.OUT_MODE] == MODE_OUVERT
    assert settings.read().enabled is True


# ==========================================================================
# E. Le contrat de charge utile
# ==========================================================================
def test_une_lecture_seule_dit_qu_ecrit_rien_et_ne_se_trompe_pas() -> None:
    """Des paramètres vides = une lecture. Elle ne touche pas au fichier.

    `written=True` sur une lecture se lit mal, sauf que c'est ce qui permet à
    l'interface de ne pas distinguer deux intentions qu'elle ne sait pas
    séparer : ici, « rien à écrire » est un succès. Le fichier, lui, reste
    byte pour byte ce qu'il était.
    """
    _ecrire_fichier(LIGNE_SEARXNG + "\n")
    avant = settings.settings_path().read_text(encoding="utf-8")

    charge = _ws.apply_and_describe({})

    assert charge[_ws.OUT_WRITTEN] is True
    assert charge[_ws.OUT_ERROR] == ""
    assert charge[_ws.OUT_WRITABLE] is True
    assert charge[_ws.OUT_SEARXNG_URL] == "http://127.0.0.1:8080"
    assert settings.settings_path().read_text(encoding="utf-8") == avant


@pytest.mark.parametrize("params", [
    {"enabled": "oui"},          # une chaîne n'est pas un booléen
    {"enabled": 1},              # un entier n'est pas un booléen : `True == 1` en Python
    {"enabled": None},
    {"provider": ""},            # un moteur vide n'est pas un choix
    {"provider": 3},
    {"searxng_url": 42},
], ids=["chaine", "entier", "nul", "moteur_vide", "moteur_non_texte", "adresse_non_texte"])
def test_une_entree_invalide_renvoie_une_erreur_et_n_ecrit_rien(params: dict[str, Any]) -> None:
    """Une entrée malformée laisse le fichier INTACT.

    Le refus vient de `sanitize_params`, donc aucune clé n'est écrite : un
    réglage à moitié changé n'a aucune représentation dans une case à cocher, et
    l'utilisateur ne pourrait pas savoir ce qui a été pris. Le refus nomme la
    clé fautive, pour que l'interface puisse la nommer à son tour.
    """
    _ecrire_fichier(LIGNE_SEARXNG + "\n")
    avant = settings.settings_path().read_text(encoding="utf-8")

    charge = _ws.apply_and_describe(params)

    assert charge[_ws.OUT_ERROR], f"entrée refusée sans message : {params}"
    assert next(iter(params)) in charge[_ws.OUT_ERROR], "le refus nomme la clé fautive"
    assert charge[_ws.OUT_WRITTEN] is False
    assert settings.settings_path().read_text(encoding="utf-8") == avant
    assert charge[_ws.OUT_SEARXNG_URL] == "http://127.0.0.1:8080", "l'état rendu reste l'état relu"


def test_les_options_de_moteur_commencent_par_auto_et_sont_exhaustives() -> None:
    """La liste des choix est utilisable telle quelle par l'interface.

    `auto` vient en premier parce que c'est le seul choix qui ne restreint
    rien : le proposer après les autres donnerait l'impression que le mode par
    défaut est le dernier. Chaque option porte TOUS les champs du contrat —
    une clé manquante ferait planter une interface qui l'attend — et
    exactement une est sélectionnée : deux cases cochées signifieraient que
    l'interface ne sait pas quoi décocher.
    """
    charge = _ws.apply_and_describe({})
    options: list[dict[str, Any]] = charge[_ws.OUT_PROVIDER_OPTIONS]

    assert options[0]["name"] == settings.PROVIDER_AUTO
    assert [option["name"] for option in options[1:]] == ["arxiv", "openalex", "wikipedia", "searxng"]
    for option in options:
        assert set(_ws.PROVIDER_FIELDS) <= set(option), f"champs manquants : {option.get('name')}"
        assert option["label"].strip(), f"option sans libellé : {option['name']}"
        assert isinstance(option["configured"], bool), option["name"]
        assert isinstance(option["selected"], bool), option["name"]
        # `requires` ne vaut quelque chose que pour ce qui n'est PAS configuré :
        # un moteur déjà là n'a rien à requérir, et l'interface affiche la case
        # en conséquence.
        if not option["configured"]:
            assert option["requires"].strip(), f"option non configurée sans exigence : {option}"
    assert [option["name"] for option in options if option["selected"]] == [settings.PROVIDER_AUTO]


def test_le_moteur_selectionne_est_annonce_comme_selectionne() -> None:
    """Le choix de l'utilisateur revient dans la charge utile, marqué.

    Sans ce marquage, l'interface rouvrirait la page sur `auto` alors que le
    fichier dit OpenAlex : l'utilisateur verrait son réglage s'effacer à l'écran
    alors qu'il s'applique.
    """
    _ecrire_fichier("provider=openalex\n")

    charge = _ws.apply_and_describe({})
    options: list[dict[str, Any]] = charge[_ws.OUT_PROVIDER_OPTIONS]

    assert charge[_ws.OUT_PROVIDER] == "openalex"
    assert [option["name"] for option in options if option["selected"]] == ["openalex"]
    assert options[0]["selected"] is False


# ==========================================================================
# F. Les deux promesses de la couche basse
# ==========================================================================
def test_le_mode_par_defaut_garde_web_research_dans_l_offre_du_modele() -> None:
    """Sans réglage, l'offre est celle d'aujourd'hui : les cinq outils.

    Le filtre se fait au moment de composer `tool_defs`, jamais sur la
    constante : celle-ci reste le CONTRAT, épinglé des deux côtés, et l'offre
    d'un déploiement nu doit être exactement celle que les tests d'orchestration
    attendent.
    """
    assert len(loop.DISCOVERY_TOOL_NAMES) == 5
    assert loop.WEB_RESEARCH_TOOL_NAME in loop.DISCOVERY_TOOL_NAMES

    assert loop._discovery_tool_names() == loop.DISCOVERY_TOOL_NAMES


def test_une_recherche_fermee_retire_web_research_de_l_offre_du_modele() -> None:
    """Recherche fermée : le modèle ne voit plus l'outil qu'il ne peut pas servir.

    Le contrat reste entier — la constante garde ses cinq noms, et c'est bien
    elle qui est rendue quand la recherche se rouvre.
    """
    settings.write(enabled=False)

    assert loop._discovery_tool_names() == loop.DISCOVERY_TOOL_NAMES - {loop.WEB_RESEARCH_TOOL_NAME}
    assert len(loop.DISCOVERY_TOOL_NAMES) == 5, "le contrat ne se rétrécit pas"


def test_la_fiche_des_moteurs_ne_declenche_aucun_appel_reseau() -> None:
    """`provider_status()` ouvre la fenêtre des réglages : elle ne sort pas.

    La fixture `installation_nue` a posé `net.http_get` sur un `pytest.fail` :
    l'appel ici ne peut donc pas être un « réseau simulé », il peut seulement
    être un appel absent. L'ordre est celui d'annonce du registre, épinglé ici
    parce que l'interface affiche les cases dans cet ordre.
    """
    status = registry.provider_status()

    assert [entree["name"] for entree in status] == ["arxiv", "openalex", "wikipedia", "searxng"]
    assert [entree["configured"] for entree in status] == [True, True, True, False], \
        "sans déclaration, seul SearXNG est absent — et il est annoncé comme tel"


def test_une_adresse_declaree_dans_le_fichier_construit_le_moteur(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'adresse écrite dans les réglages suffit, sans variable d'environnement.

    C'est le chemin ordinaire d'un utilisateur qui héberge son instance : il ne
    lance pas Thot depuis un terminal, il écrit l'adresse dans la page des
    réglages. L'environnement reste prioritaire, donc une variable déclarée ici
    l'emporte — c'est le même ordre, mesuré dans l'autre sens.
    """
    _ecrire_fichier(LIGNE_SEARXNG + "\n")

    moteur = SearxngProvider.from_environment()

    assert moteur is not None
    assert moteur.base_url == "http://127.0.0.1:8080"
    assert "searxng" in [retenu.name for retenu in registry.applicable_providers("news")]

    monkeypatch.setenv(SEARXNG_URL_ENV, "http://127.0.0.1:9999")

    impose = SearxngProvider.from_environment()
    assert impose is not None and impose.base_url == "http://127.0.0.1:9999"
