"""Recherche web — le chemin OUTIL et le chemin BOUCLE de `web_research`.

Ce fichier ne teste pas la mécanique d'un appel : il teste les deux portes par
lesquelles `web_research` entre vraiment dans le produit, et il met chacune
devant une propriété qui se vérifie sans jamais toucher le monde.

* **La porte OUTIL** (`WebResearchTool.call`) est la seule qui produit les
  codes du registre : c'est par elle que le modèle apprend *pourquoi* son
  argument est refusé. Un code exact vaut mieux qu'un `BAD_ARGS` générique,
  parce qu'un refus vague se retente à l'identique.
* **La porte BOUCLE** (`_execute_web_research`) est le chemin vivant : elle
  valide avec le même schéma et rend un résultat que la boucle sait inventorier
  comme preuve.

**Aucun réseau, et c'est une propriété testée.** `net.http_get` est le SEUL
point de sortie du projet pour le web (moteurs, `robots.txt`, téléchargement) ;
le remplacer ici par un `pytest.fail` ne simule donc pas le réseau, il **prouve
son absence** : un appel qui l'attraperait échouerait au lieu de sortir. Les
deux seuls endroits où l'on simule sont nommés et justifiés à chaque fois — la
décision de provenance (`Fetcher.fetch`) et l'horloge de politesse.

Les fixtures reprennent celles de `test_web_cache.py` : un vrai fichier SQLite
temporaire, `create_all` puis `apply_migrations` dans l'ordre de `init_db()`.
Jamais la base du projet. Les variables d'environnement se posent par
`monkeypatch` et se restaurent seules : aucun test ne fuit d'état.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.defaults import TOOL_LIMITS
from backend.db.migrations import apply_migrations
from backend.db.models import Base
from backend.inference.loop import DISCOVERY_TOOL_NAMES, _execute_web_research
from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR
from backend.services.web import cache, net, research
from backend.services.web.fetcher import Fetcher
from backend.services.web.providers.searxng import SEARXNG_URL_ENV
from backend.services.web.settings import SETTINGS_ENV, SETTINGS_FILENAME
from backend.services.web.types import (
    CONSENT_REQUIRED,
    NETWORK_BLOCKED,
    NO_PROVIDER,
    ROBOTS_DENIED,
    URL_NOT_ADMITTED,
    WEB_DISABLED,
    WebError,
    WebPage,
    WebQuery,
)
from backend.tools.base import ToolContext, ToolResult
from backend.tools.registry import get_default_registry
from backend.tools.web_research import WebResearchArgs, WebResearchTool

# --------------------------------------------------------------------------
# Vocabulaire du test
# --------------------------------------------------------------------------
# L'URL que « l'utilisateur » a écrite dans son message : c'est la moitié de la
# règle de provenance qui passe par le texte, l'autre étant un résultat antérieur.
URL_ECRITE = "https://exemple.test/la-page-que-l-utilisateur-a-ecrite"
# La forme d'une preuve rendue : `web_` + 12 hexadécimaux + deux-points + rang.
FORME_PREUVE = re.compile(r"web_[0-9a-f]{12}:(\d+)\Z")
FORME_PUBLIC_ID = re.compile(r"web_[0-9a-f]{12}\Z")
# Les deux portes d'admission de preuve de `loop.py`, recopiées mot pour mot :
# la clé `evidence_ids` du résultat, puis la regex appliquée au rendu. Les
# appliquer ici, c'est vérifier ce que la boucle vérifie — et non ce qu'on
# voudrait qu'elle vérifie.
_OUTIL_A_PREUVES = frozenset({"search_info", "ask_files", "web_research"})
_REGEX_PREUVE = re.compile(r'"evidence_id"\s*:\s*"([^"]+)"')

# La requête du tour : volontairement thématique, elle ne contient aucun terme
# privé et le cache de la conversation la sert donc sans jamais l'envoyer.
_REQUETE = "theoreme de Pythagore"
_APPEL_VALIDE: dict[str, Any] = {
    "queries": [_REQUETE],
    "freshness": "reuse_if_sufficient",
    "purpose": "etablir l'enonce public du theoreme",
}

# Le texte de la page servie par le cache. Assez long pour être découpé, assez
# simple pour que le découpage ne normalise aucun blanc (donc la localisation
# en caractères est vraie et se compte de tête).
TEXTE_PAGE = (
    "Le theoreme de Pythagore enonce que le carre de l'hypotenuse est egal a la "
    "somme des carres des deux autres cotes d'un triangle rectangle. Cette relation "
    "de geometrie euclidienne sert de base a toute la trigonometrie."
)


# --------------------------------------------------------------------------
# Outillage
# --------------------------------------------------------------------------
@pytest.fixture()
def session_factory(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Une vraie base, jetable : fichier SQLite temporaire, ordre de `init_db()`."""
    engine = create_engine(f"sqlite:///{tmp_path / 'web_outil.db'}")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", factory)
    return factory


@pytest.fixture(autouse=True)
def hors_du_reseau(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Aucune socket ne part de ce fichier, et le refus est un échec de test.

    `net.http_get` est l'unique sortie réseau du projet pour le web : la poser
    sur un `pytest.fail` ne rend pas le réseau simulé, il rend son absence
    vérifiable. `pytest.fail` lève `OutcomeException`, qui dérive de
    `BaseException` : ni le `except Exception` de `_lire`, ni celui de l'outil ne
    peuvent l'avaler — un appel qui chercherait à sortir échoue franchement.
    """
    net.reset_host_gate()
    monkeypatch.setattr(
        net, "http_get",
        lambda *a, **k: pytest.fail("ce test ne doit pas ouvrir de socket : le réseau est interdit"),
    )
    # Un moteur déclaré par le développeur rendrait `news` applicable et partirait
    # vers une instance inconnue : on repart de l'installation nue. Le fichier de
    # réglages fait partie de cette installation — un `web_search.settings` laissé
    # sur la machine déciderait du mode et du moteur de chaque test de ce fichier.
    monkeypatch.delenv(SEARXNG_URL_ENV, raising=False)
    monkeypatch.delenv(research.MODE_ENV, raising=False)
    monkeypatch.setenv(SETTINGS_ENV, str(tmp_path / SETTINGS_FILENAME))
    yield
    net.reset_host_gate()


def _page(url: str = URL_ECRITE) -> WebPage:
    """Une page lue, dont le `content_sha256` correspond VRAIMENT à son texte."""
    corps = TEXTE_PAGE.encode("utf-8")
    return WebPage(
        url=url,
        final_url=url,  # zéro redirection suivie (C18)
        status=200,
        content_type="text/html",
        content_sha256=hashlib.sha256(corps).hexdigest(),
        text=TEXTE_PAGE,
        extractor_version="extract-1",
        fetched_at=datetime.now(),
        title="La page de l'utilisateur",
        source_type="general",
        proposed_by="wikipedia",
    )


def _servir(conv_id: str, *, url: str = URL_ECRITE) -> str:
    """Mémorise une page pour CETTE conversation et rend son `public_id`.

    La base est celle de la fixture `session_factory` : cette fonction n'a pas
    besoin de la session, seulement du fait qu'elle soit branchée.
    """
    query = WebQuery(text=_REQUETE, source_type="general")
    return cache.remember(conv_id, _page(url), source_type="general", query_key=query.cache_key())


def _appeler(args: dict[str, Any]) -> ToolResult:
    """La porte OUTIL, telle que la boucle et l'UI l'appellent."""
    return asyncio.run(WebResearchTool().call(args, ToolContext(run_id="r1", request_id="r1", conv_id="c1")))


def _preuves_admises_par_la_boucle(outil: str, resultat: dict[str, Any]) -> set[str]:
    """Les preuves que la boucle met dans l'inventaire du tour, pour CET outil.

    C'est le code de `loop.py`, dans l'ordre où il l'applique : d'abord la clé
    `evidence_ids` du résultat, puis la regex `"evidence_id": "…"` appliquée au
    rendu. Les deux portes sont reprises ici parce qu'elles existent toutes les
    deux ; l'assertion dit laquelle porte effectivement la preuve du web.
    """
    if outil not in _OUTIL_A_PREUVES:
        return set()
    admis = {str(valeur) for valeur in (resultat.get("evidence_ids") or [])}
    return admis | set(_REGEX_PREUVE.findall(str(resultat.get("result") or "")))


# ==========================================================================
# A. Le schéma publié et la validation — `BaseTool.call`, la seule porte à codes
# ==========================================================================
def test_un_appel_valide_passe_et_rend_la_forme_de_l_outil(session_factory: Any) -> None:
    """Un appel conforme traverse la validation ET rend `[outil] … [fin]`.

    La question est servie par le cache de la conversation, donc le rendu porte
    une vraie source datée sans qu'aucune requête ne parte : c'est la seule
    façon de prouver la forme du rendu sans réseau.
    """
    _servir("c1")

    resultat = _appeler(dict(_APPEL_VALIDE))

    assert resultat.error is None, resultat.error
    assert resultat.ok is True
    assert resultat.display_summary.startswith("[web_research] ")
    assert "[fin]" in resultat.display_summary
    # Le contenu rendu au modèle est du JSON, pas un texte à relire.
    charge = json.loads(resultat.content)
    assert charge["ok"] is True
    assert charge["result"] == resultat.display_summary
    assert charge["web_counters"]["network_queries"] == 0


# Chaque contrat hors contrat, et le code EXACT que le validateur écrit.
# `purpose: None` — la clé présente et vide — est la forme que le modèle produit
# quand il oublie l'argument ; la clé purement absente est un autre cas, mesuré
# par `test_un_purpose_omis_passe_malgre_son_qualification_d_obligatoire`.
_CONTRATS_HORS_CONTRAT: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("queries vide", {"queries": [], "purpose": "x"}, "EMPTY_QUERY"),
    ("seize requetes", {"queries": [f"q{i}" for i in range(16)], "purpose": "x"}, "TOO_MANY"),
    ("requete de 151 caracteres", {"queries": ["x" * 151], "purpose": "x"}, "TOO_LONG"),
    ("type retiré du contrat", {"queries": ["a"], "type": "blogs", "purpose": "x"}, "UNKNOWN_ARG"),
    ("freshness hors enumeration", {"queries": ["a"], "freshness": "hier", "purpose": "x"}, "ENUM_INVALID"),
    ("purpose nul", {"queries": ["a"], "purpose": None}, "MISSING_REQUIRED"),
    ("purpose de 201 caracteres", {"queries": ["a"], "purpose": "p" * 201}, "TOO_LONG"),
    ("ni requete ni rien", {"purpose": "x"}, "MISSING_REQUIRED"),
)


@pytest.mark.parametrize("cas,arguments,code_attendu", _CONTRATS_HORS_CONTRAT, ids=[c[0] for c in _CONTRATS_HORS_CONTRAT])
def test_chaque_ecart_au_pacte_rend_son_code_nomme(
    cas: str, arguments: dict[str, Any], code_attendu: str,
) -> None:
    resultat = _appeler(arguments)

    assert resultat.ok is False
    erreur = resultat.error or ""
    assert erreur.startswith(f"[{code_attendu}]"), f"{cas} : {erreur}"


def test_un_purpose_omis_est_refuse_comme_un_purpose_vide() -> None:
    """`purpose` absent est un `purpose` manquant, pas un but vide.

    Un `field_validator` ne s'exécute pas sur une valeur ABSENTE : sans le
    contrôle du modèle, l'appel passait avec une intention vide — et une
    recherche part avec une trace de son intention que rien n'a porté. Le
    défaut était mesuré par un test qui l'assertait ; il est corrigé, et le
    test affirme désormais le refus.
    """
    vide = _appeler({"queries": ["théorème de Pythagore"], "purpose": "   "})
    assert vide.ok is False
    assert "[MISSING_REQUIRED]" in str(vide.error)

    # Même refus par la porte de la boucle, qui n'a pas de pré-contrôle
    # `EMPTY_CALL` : c'est bien le validateur qui écrit le code.
    rendu = _execute_web_research({"args": {"queries": ["x"]}}, conv_id="conv_a")
    assert "[MISSING_REQUIRED]" in str(rendu.get("result"))



def test_une_url_tombee_de_la_signature_est_une_cle_inconnue() -> None:
    """Décision D4 : `url` a quitté la signature, donc la clé inconnue a son nom.

    L'ancien test vérifiait qu'un schéma interdit (`file://`) restait bloqué.
    La signature n'admet plus le champ : un `url` quelconque — interdit ou pas —
    est désormais un argument inconnu, nommé comme tel. La lettre de la règle de
    provenance (`URL_NOT_ADMITTED`, redirections refusées) reste mesurée au
    niveau service, inchangée : c'est le `run(url=…)` interne qui la porte, et
    les tests de la couche service la vérifient, pas la signature.
    """
    resultat = _appeler({**_APPEL_VALIDE, "url": "file:///etc/passwd"})

    assert resultat.ok is False
    erreur = resultat.error or ""
    assert erreur.startswith("[UNKNOWN_ARG]"), erreur
    assert "BAD_ARGS" not in erreur
    assert "Retire la clé" in erreur


@pytest.mark.parametrize("inconnue", ["provider", "limit", "timeout", "api_key", "mode", "url"])
def test_une_cle_inconnue_est_nommee_et_jamais_un_bad_args_generique(inconnue: str) -> None:
    """Un faux argument s'appelle par son nom — le modèle peut le retirer.

    Le 8B écrit régulièrement `provider`, `limit`, `timeout` : c'est exactement
    la situation pour laquelle `UNKNOWN_ARG` existe. Un `BAD_ARGS` générique
    ferait retenter le même appel. `url` est entré dans cette liste avec la
    décision D4 : il n'est plus un argument, il est un reste à retirer.
    """
    resultat = _appeler({**_APPEL_VALIDE, inconnue: "valeur"})

    assert resultat.ok is False
    erreur = resultat.error or ""
    assert erreur.startswith("[UNKNOWN_ARG]"), erreur
    assert "BAD_ARGS" not in erreur
    assert "Retire la clé" in erreur


# La borne publiée et la borne appliquée doivent être le MÊME nombre : la
# première est une phrase que le modèle lit, la seconde est ce que le
# validateur applique. Un décalage entre les deux est un mensonge de contrat.
_BORNES_ET_CODES: tuple[tuple[str, dict[str, Any], str, str, str], ...] = (
    ("nombre de requetes", {"queries": [f"q{i}" for i in range(16)], "purpose": "x"},
     "TOO_MANY", "web_queries_max", "Un appel est limité à 15 requêtes maximum."),
    ("longueur d'une requete", {"queries": ["x" * 151], "purpose": "x"},
     "TOO_LONG", "query_max_chars", "Une requête est limitée à 150 caractères maximum"),
    ("longueur du but", {"queries": ["a"], "purpose": "p" * 201},
     "TOO_LONG", "web_purpose_max_chars", "purpose est limité à 200 caractères."),
    # La borne « longueur d'une URL » a quitté cette table avec le champ `url` :
    # la décision D4 l'a sorti de la signature, donc sa borne n'est plus publiée
    # ni appliquée sur le chemin exposé (le chemin interne, `run(url=…)`, lui
    # reste inchangé pour la provenance).
)

@pytest.mark.parametrize("borne,arguments,code,cle_limite,phrase_attendue", _BORNES_ET_CODES,
                         ids=[b[0] for b in _BORNES_ET_CODES])
def test_la_borne_citee_par_le_refus_est_celle_de_tool_limits(
    borne: str, arguments: dict[str, Any], code: str, cle_limite: str, phrase_attendue: str,
) -> None:
    limite = int(TOOL_LIMITS[cle_limite])

    erreur = _appeler(arguments).error or ""

    assert erreur.startswith(f"[{code}]"), f"{borne} : {erreur}"
    # Le chiffre du refus est celui du point unique des seuils, et non un littéral.
    assert str(limite) in phrase_attendue, f"phrase de test désynchronisée de {cle_limite}"
    assert phrase_attendue in erreur, f"{borne} : {erreur}"


# ==========================================================================
# B. Le refus avant tout réseau — le cœur de la sûreté
# ==========================================================================
def test_le_reseau_ferme_rend_le_cache_et_dit_pourquoi(session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`local_only` rend ce que la conversation sait et nomme la fermeture.

    L'appel n'est pas un échec technique : c'est un mode. Le rendu doit donc
    dire ce qui est disponible (le cache), ce qui ne l'est pas (le réseau) et
    pourquoi — sinon le modèle conclut que l'information n'existe pas.
    """
    _servir("c1")
    monkeypatch.setenv(research.MODE_ENV, research.MODE_LOCAL_ONLY)

    resultat = _appeler(dict(_APPEL_VALIDE))
    charge = json.loads(resultat.content)
    rendu = resultat.display_summary

    assert resultat.ok is False
    assert charge["ok"] is False
    assert charge["web_counters"]["network_queries"] == 0
    assert rendu.startswith("[web_research] réseau fermé (mode local_only)")
    # Ce que la conversation sait DÉJÀ, chiffré, et la raison de la fermeture.
    assert "Ce que cette conversation sait déjà : 0 requête(s) mémorisée(s), 1 source(s) conservée(s)" in rendu
    assert "Lacune : rien n'a été demandé au réseau" in rendu
    assert rendu.rstrip().splitlines()[-1].startswith("[fin]")


def test_le_code_du_reseau_ferme_est_nomme_pour_qui_lit_le_resultat(session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`WEB_DISABLED` est porté par l'issue, pour qui sait le lire.

    ÉCART CONSTATÉ : contrairement à `[NO_PROVIDER]` ou `[URL_NOT_ADMITTED]`,
    ce code n'apparaît ni dans le rendu ni dans la charge utile : le modèle
    n'apprend « réseau fermé » que par la phrase du rendu. Ce test verrouille
    que le code existe et se distingue de « rien trouvé », pas qu'il est
    transmis — la boucle ne le relaie pas aujourd'hui.
    """
    monkeypatch.setenv(research.MODE_ENV, research.MODE_LOCAL_ONLY)

    issue = research.run(
        queries=[_REQUETE], source_type="general", purpose="etablir l'enonce",
        conv_id="c1", user_message="",
    )

    assert issue.code == WEB_DISABLED
    assert issue.ok is False
    assert issue.network_queries == 0


@pytest.mark.parametrize("requete", [
    "le salaire de mon client",
    "mon mot de passe",
])
def test_une_requete_privee_ne_quitte_pas_la_machine(session_factory: Any, requete: str) -> None:
    """Un terme privé déclenche un refus d'envoi, pas une réduction silencieuse.

    Un outil sans interface ne peut pas demander un accord au milieu d'un
    appel : il refuse d'envoyer et nomme la raison. C'est plus austère que
    demander, et c'est la seule réponse qui ne mente pas.

    Ce qui est prouvé ici est l'absence de fuite : le code est porté par le
    résultat, `ok` est faux, aucune requête n'est partie et la requête n'a
    même pas été journalisée — le refus laisse la conversation où elle était.
    ÉCART CONSTATÉ : le rendu final écrase la phrase de refus, donc le modèle
    ne lit `[CONSENT_REQUIRED]` qu'à travers le code du résultat, pas à
    travers le texte. Ce test verrouille la sûreté, pas cette rédaction.
    """
    issue = research.run(
        queries=[requete], source_type="general", purpose="chercher une information publique",
        conv_id="c1", user_message="",
    )

    assert issue.code == CONSENT_REQUIRED
    assert issue.ok is False
    assert issue.network_queries == 0
    assert issue.pages_read == 0
    assert issue.hosts == []
    # La requête n'a pas été journalisée non plus : le refus laisse la
    # conversation exactement où elle était.
    assert cache.count_queries("c1") == 0


def test_une_url_que_l_utilisateur_n_a_pas_ecrite_n_est_pas_lue(session_factory: Any) -> None:
    """Une adresse inventée par le modèle n'est pas une source : elle n'est pas lue.

    Ni le message utilisateur ni un résultat antérieur ne la mentionnent, donc
    elle est refusée AVANT la première requête. C'est la fermeture du canal
    d'exfiltration : sans elle, `web_research` devient un proxy.
    """
    issue = research.run(
        queries=[_REQUETE], source_type="general", purpose="lire une page inventee",
        url="https://ailleurs.test/une-page-que-personne-na-ecrite", conv_id="c1", user_message="",
    )

    assert issue.code == URL_NOT_ADMITTED
    assert issue.ok is False
    assert issue.text.startswith(f"[{URL_NOT_ADMITTED}]")
    assert "ni dans ton message ni dans un résultat obtenu précédemment" in issue.text
    assert issue.pages_read == 0
    assert issue.network_queries == 0


_ORIGINES_ADMISES: tuple[tuple[str, str], ...] = (
    ("message de l'utilisateur", f"lis-moi cette page {URL_ECRITE}"),
    ("resultat anterieur", ""),
)


@pytest.mark.parametrize("origine,message", _ORIGINES_ADMISES, ids=[o[0] for o in _ORIGINES_ADMISES])
def test_une_url_admise_arrive_jusqu_a_la_lecture(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, origine: str, message: str,
) -> None:
    """Une URL de provenance admise échoue plus à l'admission — elle est lue.

    Les deux moitiés de la règle sont vérifiées ici : le message de
    l'utilisateur, et un résultat antérieur du même tour. On intercepte
    `Fetcher.fetch` et non le réseau : ce qui est mesuré est la DÉCISION
    (« cette adresse a le droit d'être lue »), pas la réponse du serveur. Le
    fetcher simulé refuse par `robots.txt`, ce qui prouve que l'appel est
    arrivé jusqu'à lui — et qu'un refus de lecture ne devient jamais un
    verdict sur l'adresse.

    ÉCART CONSTATÉ : quand le chemin `url` ne produit aucune page, `run`
    rend un texte VIDE — le motif de l'écart est bien porté par `skipped`,
    mais le modèle ne le lit pas. Ce test verrouille la décision, pas ce rendu.
    """
    _appeles: list[tuple[str, frozenset[str] | None]] = []

    def _robots(_lui: Any, url: str, *, admitted: frozenset[str] | None = None, **kwargs: Any) -> WebPage:
        _appeles.append((url, admitted))
        raise WebError(ROBOTS_DENIED, "robots.txt de test : page interdite")

    monkeypatch.setattr(Fetcher, "fetch", _robots)
    # La liste de domaines n'est pas l'objet de ce test : on la désactive pour
    # que la décision observée reste celle de l'admission.
    monkeypatch.setenv("THOT_WEB_ALLOWED_DOMAINS", "*")
    # `exemple.test` ne résout pas — c'est voulu par la RFC 2606. On neutralise
    # la SEULE résolution, pas les autres gardes : la destination est validée
    # avant `robots`, et ce test prouve qu'elle ne l'arrête pas quand elle est
    # légitime. L'admission reste le seul objet mesuré ici.
    monkeypatch.setattr(research.net, "resolve_global", lambda _h, _p: ["93.184.216.34"])
    admis = frozenset({URL_ECRITE}) if origine == "resultat anterieur" else None

    issue = research.run(
        queries=[], source_type="official", purpose="lire la page designee par l'utilisateur",
        url=URL_ECRITE, conv_id="c1", user_message=message, admitted_before=admis,
    )

    assert [url for url, _ in _appeles] == [URL_ECRITE], f"{origine} : le fetcher n'a pas ete sollicite"
    # Le fetcher a reçu l'adresse ET l'ensemble des adresses admises : c'est la
    # porte fermée qui s'ouvre ici, pas un contournement.
    assert _appeles[0][1] is not None and URL_ECRITE in _appeles[0][1]
    # L'échec vient d'autre part : il est retenu comme un écart motivé, jamais
    # comme un refus d'admission — qui serait, lui, un verdict sur l'adresse.
    assert issue.code is None
    assert [(item.url, item.reason) for item in issue.skipped] == [(URL_ECRITE, "robots")]
    assert issue.pages_read == 0


def test_une_actualite_sans_moteur_dit_que_rien_n_a_ete_cherche(session_factory: Any) -> None:
    """`NO_PROVIDER` distingue l'échec de l'absence — le mensonge le plus grave.

    « Aucun moteur applicable » et « aucune correspondance » ne se ressemblent
    pas pour qui écrit le brief : le premier se corrige en changeant de moteur,
    le second ferait conclure que l'information n'existe pas.
    """
    issue = research.run(
        queries=["derniere actualite economique"], source_type="news",
        purpose="rechercher une actualite", conv_id="c1", user_message="",
    )

    assert issue.code == NO_PROVIDER
    assert issue.ok is False
    assert issue.text.startswith(f"[{NO_PROVIDER}]")
    assert "Aucun moteur applicable pour « news »" in issue.text
    assert "Ce n'est pas une absence de résultat : rien n'a été cherché." in issue.text
    # La phrase du « rien trouvé » du rendu normal ne doit pas pouvoir se lire ici.
    assert "Aucune correspondance parmi les sources consultées" not in issue.text
    assert issue.network_queries == 0


# ==========================================================================
# C. Le chemin de la boucle
# ==========================================================================
def test_web_research_est_propose_a_l_orchestrateur_sans_forme_copiable() -> None:
    """L'outil est dans le lot de découverte, et le prompt ne montre pas sa forme.

    Le 8B copie la forme qu'il voit : le prompt peut nommer l'outil, jamais
    écrire `web_research(...)`. Le garde de `test_prompt_orchestrateur.py` ne cite
    pas ce nom dans sa liste, donc c'est cette assertion qui le couvre.
    """
    assert "web_research" in DISCOVERY_TOOL_NAMES
    outil = get_default_registry().try_get("web_research")
    assert isinstance(outil, WebResearchTool)
    proposees = [
        definition["function"]["name"]
        for definition in get_default_registry().definitions()
        if definition["function"]["name"] in DISCOVERY_TOOL_NAMES
    ]
    assert "web_research" in proposees

    assert "web_research" in SYSTEM_ORCHESTRATOR
    copiees = re.findall(r"\bweb_research\s*\(", SYSTEM_ORCHESTRATOR, re.IGNORECASE)
    assert copiees == [], f"formes copiables dans le prompt : {copiees}"


def test_le_boucle_rend_le_contrat_de_son_outil(session_factory: Any) -> None:
    """La clé de la boucle reçoit ce qu'elle sait lire : ok, result, latence, preuves.

    Ce sont les quatre champs dont `execute_discovery` et l'inventaire de preuves
    se servent ; un résultat sans `evidence_ids` serait rendu mais jamais cité.
    """
    public_id = _servir("conv_a")

    rendu = _execute_web_research({"args": dict(_APPEL_VALIDE)}, conv_id="conv_a")

    assert {"ok", "result", "latency_ms", "evidence_ids"} <= set(rendu)
    assert rendu["ok"] is True
    assert rendu["latency_ms"] >= 0
    assert rendu["evidence_ids"] == [f"{public_id}:0"]


_ECARTS_VALIDES: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("aucun argument", {}, "MISSING_REQUIRED"),
    ("seize requetes", {"queries": [f"q{i}" for i in range(16)], "purpose": "x"}, "TOO_MANY"),
    ("requete trop longue", {"queries": ["x" * 151], "purpose": "x"}, "TOO_LONG"),
    ("type retiré du contrat", {"queries": ["a"], "type": "blogs", "purpose": "x"}, "UNKNOWN_ARG"),
    ("but manquant", {"queries": ["a"], "purpose": None}, "MISSING_REQUIRED"),
)


@pytest.mark.parametrize("cas,arguments,code_attendu", _ECARTS_VALIDES, ids=[c[0] for c in _ECARTS_VALIDES])
def test_la_boucle_conserve_le_code_du_validateur(
    session_factory: Any, cas: str, arguments: dict[str, Any], code_attendu: str,
) -> None:
    """Un argument hors contrat rend la phrase du validateur, pas un `BAD_ARGS` mou.

    `_premier_code_spec` relit le `[CODE]` que le validateur a écrit dans
    l'erreur pydantic. Le préserver est ce qui permet au modèle de corriger au
    bon endroit au lieu de retenter à l'identique.
    """
    rendu = _execute_web_research({"args": arguments}, conv_id="conv_a")

    assert rendu["ok"] is False
    resultat = str(rendu["result"])
    assert resultat.startswith(f"[{code_attendu}]"), f"{cas} : {resultat}"
    assert "Arguments hors contrat" not in resultat, cas


def test_la_preuve_rendue_est_admise_dans_l_inventaire_du_tour(session_factory: Any) -> None:
    """La citation annoncée par le rendu est exactement celle que la boucle admet.

    Le brief ne cite que les preuves de l'inventaire du tour : si le rendu
    annonce un identifiant différent de celui admis, la citation est refusée
    par le validateur. Les deux portes d'admission de `loop.py` sont appliquées
    ici telles quelles, sur le résultat rendu et son aller-retour JSON.

    Les DEUX portes de `loop.py` doivent y répondre : la clé `evidence_ids` du
    résultat, et la regex `"evidence_id": "…"` appliquée au rendu. Le rendu écrit
    la forme JSON entre guillemets précisément pour que la seconde porte ne
    laisse rien passer — un rendu en prose traverserait la garde et la citation
    serait refusée au brief sans que personne ne sache pourquoi.
    """
    public_id = _servir("conv_a")

    rendu = _execute_web_research({"args": dict(_APPEL_VALIDE)}, conv_id="conv_a")
    # Le résultat traverse bien du JSON : c'est ce que la boucle stocke et relit.
    payload = json.loads(json.dumps(rendu, ensure_ascii=False))

    admis = _preuves_admises_par_la_boucle("web_research", payload)
    texte = str(payload["result"])
    annonces = set(_REGEX_PREUVE.findall(texte))

    assert annonces, f"le rendu n'annonce aucune preuve : {texte}"
    assert annonces <= admis, f"le brief refusera ces citations : {annonces - admis}"
    # La seconde porte doit trouver exactement le même ensemble : sans cela, la
    # citation dépendrait d'un seul chemin et disparaîtrait à la première
    # refonte de l'autre.
    # La clé du rendu est celle que la boucle sait relire, guillemets compris.
    assert '"evidence_id":' in texte
    assert annonces <= admis, f"le brief refusera ces citations : {annonces - admis}"
    assert admis == {f"{public_id}:0"}
    assert FORME_PREUVE.fullmatch(f"{public_id}:0")
    assert FORME_PUBLIC_ID.fullmatch(public_id)


def test_une_preuve_se_resout_avec_son_extrait_et_sa_localisation(session_factory: Any) -> None:
    """Une preuve mémorisée se relit : extrait, source, et place dans la page.

    C'est ce que le validateur de brief demande pour trancher entre « la preuve
    existe » et « la preuve soutient l'affirmation » : sans extrait ni
    localisation, il ne peut que refuser.
    """
    public_id = _servir("conv_a")

    preuve = research.resolve_evidence(f"{public_id}:0", conv_id="conv_a")

    assert preuve is not None
    assert preuve["public_id"] == public_id
    assert preuve["excerpt"].strip() in TEXTE_PAGE
    assert preuve["fragment"] == 0
    assert re.fullmatch(r"chars:\d+-\d+", preuve["chars"])
    assert preuve["source"] == URL_ECRITE
    assert preuve["host"] == "exemple.test"
    assert preuve["content_sha256"] == hashlib.sha256(TEXTE_PAGE.encode("utf-8")).hexdigest()


def test_une_preuve_d_autre_conversation_ne_se_resout_pas(session_factory: Any) -> None:
    """Le hash identifie un contenu, la PORTE décide de qui l'a.

    Une preuve connue d'une autre conversation n'est pas une preuve de celle-ci
    (C10) : la résoudre reviendrait à faire citer à un utilisateur une source
    qu'il n'a jamais vue.
    """
    public_id = _servir("conv_a")

    assert research.resolve_evidence(f"{public_id}:0", conv_id="conv_a") is not None
    assert research.resolve_evidence(f"{public_id}:0", conv_id="conv_b") is None


@pytest.mark.parametrize("evidence_id", [
    "web_pas_un_hexadecimal:0",   # forme fausse
    "web_5b4d84283fa5",           # pas de rang
    "web_5b4d84283fa5:x",         # rang non numérique
    "doc_1:0",                    # identifiant d'une autre famille
    "",
])
def test_un_identifiant_mal_forme_ne_se_resout_jamais(session_factory: Any, evidence_id: str) -> None:
    """Une preuve inventée rend `None`, jamais une exception.

    La fonction est appelée sur des identifiants venus d'un modèle : une levée
    ici interromprait le tour au moment précis où il faut décider.
    """
    _servir("conv_a")

    assert research.resolve_evidence(evidence_id, conv_id="conv_a") is None


def test_un_ordinal_hors_bornes_ne_se_resout_pas(session_factory: Any) -> None:
    """`web_…:99` sur une page d'un seul fragment est une absence, pas le fragment 0.

    Un rang hors bornes accepterait de citer n'importe quel extrait de la page
    sous une étiquette fausse : c'est exactement ce que le validateur de brief
    doit pouvoir repérer.
    """
    public_id = _servir("conv_a")

    assert research.resolve_evidence(f"{public_id}:99", conv_id="conv_a") is None
    assert research.resolve_evidence(f"{public_id}:0", conv_id="conv_a") is not None


class _HorlogeSansDormir:
    """L'horloge de `net`, avec des attentes notées au lieu d'être subies.

    Un test qui attendrait une seconde entière pour mesurer la politesse
    mesurerait surtout sa propre lenteur. L'intervalle minimal est une
    PROPRIÉTÉ de l'attente demandée, pas une durée subie par le test — c'est
    donc l'horloge du module qu'on remplace, jamais `time.sleep` en global.

    Chaque `sleep` fait avancer l'horloge comme une vraie sieste l'aurait
    fait : sans cela, la file d'un hôte ne cesserait de reculer et chaque
    appel exigerait un intervalle de plus en plus long.
    """

    def __init__(self) -> None:
        self.attentes: list[float] = []
        self._avance = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self._avance

    def sleep(self, secondes: float) -> None:
        self.attentes.append(secondes)
        self._avance += secondes


def test_la_politesse_bloque_la_troisieme_page_mais_pas_une_requete_de_controle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le plafond porte sur les PAGES, jamais sur les requêtes de contrôle.

    Un moteur hébergé sur le même domaine que ses résultats — Wikipédia en est
    le cas exact — épuiserait le plafond avant sa première page, et la
    politesse deviendrait une panne. `robots.txt` et les appels d'API restent
    donc sérialisés et espacés, sans consommer de page.
    """
    hote = "politesse.test"  # nom réservé à ce test : la file est globale au processus
    seuil = net.HOST_MIN_INTERVAL_S
    horloge = _HorlogeSansDormir()
    budget = net.NetworkBudget(deadline=horloge.monotonic() + 30.0)
    monkeypatch.setattr(net, "time", horloge)

    net.wait_for_host(hote, budget)   # page 1 : premier appel, rien à attendre
    net.wait_for_host(hote, budget)   # page 2 — et l'intervalle minimal s'applique
    assert net.host_slots_left(hote) == 0

    with pytest.raises(WebError) as refus:
        net.wait_for_host(hote, budget)  # la 3e PAGE de ce tour est refusée
    assert refus.value.code == NETWORK_BLOCKED
    assert f"plafond de politesse atteint pour {hote}" in refus.value.message

    # robots.txt et appel d'API : deux requêtes de plus, dont aucune page.
    net.wait_for_host(hote, budget, page=False)
    net.wait_for_host(hote, budget, page=False)
    assert net.host_slots_left(hote) == 0, "une requete de controle a consomme une page"

    # L'intervalle minimal est tenu entre deux appels au même hôte…
    assert horloge.attentes == pytest.approx([seuil, seuil, seuil], abs=0.01)
    # …et l'attente est bridée par le délai du tour : la politesse ne doit
    # jamais faire déborder le budget imparti à l'appel.
    budget_court = net.NetworkBudget(deadline=horloge.monotonic() + 0.05)
    net.wait_for_host(hote, budget_court, page=False)
    assert 0.0 < horloge.attentes[-1] <= 0.05, horloge.attentes
