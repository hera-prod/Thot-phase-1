"""Recherche web — couche 5 : le cache indexé sur la conversation, prouvé contre une vraie base.

La fonctionnalité distinctive du projet est ici : *dans une conversation de vingt
échanges, on ne paie pas vingt fois la même question*. Ce fichier la mesure par
ses conséquences, jamais par son code :

* une page servie depuis le cache **n'appelle pas le réseau** — prouvé en
  instrumentant `net.http_get`, le seul point de sortie du projet ;
* une revalidation du même contenu produit **une seule ligne**, redatée et
  comptée, jamais une copie (C16 : « si réponse inchangée, conserver le contenu
  et dater la vérification ») ;
* un contenu changé crée **une ligne nouvelle sans réécrire l'ancienne** (C16 :
  « une URL identique peut correspondre à plusieurs états temporels ») ;
* la fraîcheur suit RFC 9111 §4.2.2 dans ses trois branches, avec des valeurs
  construites dont l'écart se compte à la main ;
* une purge ne touche pas les autres conversations et refuse de supprimer une
  page encore citée par une réponse déjà rendue (T-W12) ;
* une base en panne ne fait échouer aucune de ces opérations : le cache est une
  économie, jamais une condition d'accès.

Les fixtures utilisent de vrais fichiers SQLite temporaires, jamais la base du
projet ni des simulateurs : `create_all` puis `apply_migrations`, dans l'ordre
exact de `init_db()`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.config.defaults import TOOL_LIMITS
from backend.db.migrations import apply_migrations
from backend.db.models import Base, Conversation, Message, ToolCall, WebSnapshot
from backend.services.web import cache, net
from backend.services.web.types import WebPage, WebQuery

FORMAT_PUBLIC_ID = re.compile(r"web_[0-9a-f]{12}\Z")
URL_PAGE = "https://exemple.test/une-page"
# Deux en-têtes dont l'écart se compte de tête : 07:28 → 11:28, soit 14 400 s.
DERNIERE_MODIFICATION = "Wed, 21 Oct 2015 07:28:00 GMT"
DATE_REPONSE = "Wed, 21 Oct 2015 11:28:00 GMT"
QUATRE_HEURES_S = 14_400.0


# --------------------------------------------------------------------------
# Outillage : vraie base temporaire, dans l'ordre de init_db()
# --------------------------------------------------------------------------
@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'web_cache.db'}")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", factory)
    return factory


def _page(
    url: str = URL_PAGE,
    *,
    texte: str = "Le contenu de la page.",
    age_s: float = 0.0,
    last_modified: str | None = DERNIERE_MODIFICATION,
) -> WebPage:
    """Une page lue, avec un `content_sha256` qui correspond VRAIMENT à son texte."""
    corps = texte.encode("utf-8")
    return WebPage(
        url=url,
        final_url=url,  # zéro redirection suivie (C18)
        status=200,
        content_type="text/html",
        content_sha256=hashlib.sha256(corps).hexdigest(),
        text=texte,
        extractor_version="extract-1",
        fetched_at=datetime.now() - timedelta(seconds=age_s),
        etag='W/"v1"',
        last_modified=last_modified,
        title="Le titre de la page",
        source_type="general",
        proposed_by="moteur-exemple",
    )


def _query(texte: str = "quelle est cette page", source_type: str = "general") -> WebQuery:
    return WebQuery(text=texte, source_type=source_type)


def _rendre_reponse(session_factory, conv_id: str, resultat: str, brief: str | None = None) -> None:
    """Écrit un message et son appel d'outil : c'est là qu'une citation devient visible."""
    db = session_factory()
    try:
        db.add(Conversation(id=conv_id, title=conv_id, mode="chat"))
        db.add(Message(id=f"msg_{conv_id}", conv_id=conv_id, role="assistant",
                       content="réponse", status="completed", brief_json=brief))
        db.add(ToolCall(id=f"tc_{conv_id}", msg_id=f"msg_{conv_id}",
                        tool="web_research", args={"purpose": "test"}, result=resultat, ok=True))
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Identité publique
# --------------------------------------------------------------------------
def test_public_id_for_est_deterministe_et_suit_le_contenu():
    # Même URL, même contenu → le même identifiant, mot pour mot.
    assert cache.public_id_for(URL_PAGE, "abc123") == cache.public_id_for(URL_PAGE, "abc123")
    # Contenu différent → un identifiant différent, alors que l'URL n'a pas bougé.
    assert cache.public_id_for(URL_PAGE, "abc123") != cache.public_id_for(URL_PAGE, "abc124")
    # Et la forme est celle que le rendu doit pouvoir afficher sans risque.
    assert FORMAT_PUBLIC_ID.fullmatch(cache.public_id_for(URL_PAGE, "abc123"))


# --------------------------------------------------------------------------
# La fonctionnalité : servir sans requêter
# --------------------------------------------------------------------------
def test_find_reusable_sert_la_page_sans_requete_reseau(session_factory, monkeypatch):
    appels: list[str] = []

    def _reseau_bouche(*args, **kwargs):
        appels.append(str(args))
        raise AssertionError("le cache suffisait : aucune requête réseau ne doit partir")

    monkeypatch.setattr(net, "http_get", _reseau_bouche)
    page = _page()
    query = _query()
    public_id = cache.remember("conv_a", page, source_type="general", query_key=query.cache_key())

    servie = cache.find_reusable("conv_a", query, now=datetime.now())

    assert appels == [], "aucun appel à net.http_get ne doit avoir eu lieu"
    assert servie is not None
    assert servie.text == page.text
    assert servie.url == URL_PAGE
    assert servie.content_sha256 == page.content_sha256
    # La preuve rendue porte l'identifiant PUBLIC, jamais l'identifiant technique.
    assert FORMAT_PUBLIC_ID.fullmatch(public_id)
    assert not public_id.startswith("web_snap_")


def test_un_contenu_change_cree_une_seconde_ligne_sans_ecraser_l_ancienne(session_factory):
    premiere = _page(texte="Version de lundi.")
    seconde = _page(texte="Version de mardi, qui contredit la première.")
    assert premiere.content_sha256 != seconde.content_sha256

    cache.remember("conv_a", premiere, source_type="general", query_key="general|cached|q")
    cache.remember("conv_a", seconde, source_type="general", query_key="general|cached|q")

    db = session_factory()
    try:
        lignes = db.query(WebSnapshot).all()
        textes = sorted(ligne.text for ligne in lignes)
    finally:
        db.close()
    # Deux états temporels de la MÊME adresse : l'ancien reste lisible tel quel.
    assert textes == ["Version de lundi.", "Version de mardi, qui contredit la première."]


def test_remember_deux_fois_revalide_sans_dupliquer(session_factory):
    query = _query()
    avant = _page()
    public_id = cache.remember("conv_a", avant, source_type="general", query_key=query.cache_key())

    relue = _page()  # même contenu, horodatage plus tard
    relue = dataclasses.replace(relue, fetched_at=datetime.now() + timedelta(seconds=5))
    assert relue.content_sha256 == avant.content_sha256
    assert cache.remember("conv_a", relue, source_type="general", query_key=query.cache_key()) == public_id

    db = session_factory()
    try:
        lignes = db.query(WebSnapshot).all()
    finally:
        db.close()
    assert len(lignes) == 1, "une revalidation ne crée pas une seconde ligne"
    assert lignes[0].reused_count == 1, "la ré-exploitation est comptée"
    assert lignes[0].fetched_at == relue.fetched_at, "la vérification est datée, pas le contenu réécrit"
    assert lignes[0].text == avant.text


def test_un_snapshot_perime_n_est_pas_reutilise(session_factory):
    query = _query()
    # Trois heures, sans Cache-Control ni Last-Modified : la fraîcheur par défaut (1 h) est dépassée.
    page = _page(age_s=3 * 3600, last_modified=None)
    cache.remember("conv_a", page, source_type="general", query_key=query.cache_key())

    assert cache.find_reusable("conv_a", query, now=datetime.now()) is None

    # La RÉTENTION, elle, n'a pas expiré : la page est toujours là, datée, pour une revalidation.
    assert cache.find_by_url("conv_a", URL_PAGE, now=datetime.now()) is not None


# --------------------------------------------------------------------------
# Fraîcheur — RFC 9111 §4.2.2
# --------------------------------------------------------------------------
def test_freshness_s_suit_les_trois_branches_de_rfc_9111():
    # 1. Cache-Control: max-age l'emporte, même face à un Last-Modified présent.
    explicite = cache.freshness_s({
        "cache_control": "public, max-age=120",
        "last_modified": DERNIERE_MODIFICATION,
        "date": DATE_REPONSE,
    })
    assert explicite == 120.0

    # 2. Sans max-age : 10 % de l'écart Date−Last-Modified, soit 1 440 s ici.
    heuristique = cache.freshness_s({
        "cache_control": None,
        "last_modified": DERNIERE_MODIFICATION,
        "date": DATE_REPONSE,
    })
    assert heuristique == pytest.approx(QUATRE_HEURES_S * cache.HEURISTIC_RATIO)

    # 3. Sans rien : la valeur par défaut du registre des seuils (1 h).
    assert cache.freshness_s({}) == float(TOOL_LIMITS["web_page_ttl_s"]) == 3600.0


def test_un_max_age_illisible_ne_vaut_pas_zero_fraicheur():
    # `max-age=abc` n'est pas une indication de péremption : c'est une absence
    # d'indication, donc la branche heuristique prend le relais.
    assert cache.freshness_s({
        "cache_control": "max-age=abc",
        "last_modified": DERNIERE_MODIFICATION,
        "date": DATE_REPONSE,
    }) == pytest.approx(QUATRE_HEURES_S * cache.HEURISTIC_RATIO)


def test_un_cache_control_max_age_long_rend_la_page_reutilisable(session_factory):
    query = _query()
    page = _page(age_s=3 * 3600)
    cache.remember("conv_a", page, source_type="general", query_key=query.cache_key(),
                   cache_control="public, max-age=86400")

    assert cache.find_reusable("conv_a", query, now=datetime.now()) is not None


# --------------------------------------------------------------------------
# Réparation d'une réponse
# --------------------------------------------------------------------------
def test_find_by_url_retrouve_la_page_meme_si_la_requete_differente(session_factory):
    page = _page()
    query = _query()
    cache.remember("conv_a", page, source_type="general", query_key=query.cache_key())

    # Une AUTRE question, portant sur la même page : c'est une réparation, pas
    # une réutilisation de requête — donc `find_by_url` répond, `find_reusable` non.
    autre = _query("la source citée dit le contraire")
    assert cache.find_reusable("conv_a", autre, now=datetime.now()) is None

    reparée = cache.find_by_url("conv_a", URL_PAGE, now=datetime.now())
    assert reparée is not None
    assert reparée.text == page.text
    assert reparée.fetched_at == page.fetched_at, "l'état rendu est celui qui a été lu, pas un autre"


def test_le_cache_ne_traverse_pas_les_conversations(session_factory):
    query = _query()
    page = _page()
    cache.remember("conv_a", page, source_type="general", query_key=query.cache_key())

    assert cache.find_reusable("conv_b", query, now=datetime.now()) is None
    assert cache.find_by_url("conv_b", URL_PAGE, now=datetime.now()) is None


def test_le_type_de_source_part_de_l_identite_de_la_requete(session_factory):
    # Même texte, classe de source différente : ce n'est PAS la même question,
    # donc le cache ne se déclenche pas — un moteur académique n'est pas un moteur général.
    _query()  # general
    cache.remember("conv_a", _page(), source_type="general", query_key=_query().cache_key())

    academique = _query(source_type="academic")
    assert academique.cache_key() != _query().cache_key()
    assert cache.find_reusable("conv_a", academique, now=datetime.now()) is None


# --------------------------------------------------------------------------
# Journal des requêtes
# --------------------------------------------------------------------------
def test_note_query_ne_double_pas_le_compte_pour_une_meme_cle(session_factory):
    cle = _query().cache_key()
    assert cache.count_queries("conv_a") == 0

    cache.note_query("conv_a", cle)
    cache.note_query("conv_a", cle)  # doublon strict : la même question, deux fois
    assert cache.count_queries("conv_a") == 1

    cache.note_query("conv_a", _query("autre question").cache_key())
    assert cache.count_queries("conv_a") == 2
    assert cache.count_queries("conv_b") == 0, "le journal est propre à sa conversation"


# --------------------------------------------------------------------------
# Compteurs
# --------------------------------------------------------------------------
def test_stat_compte_requetes_snapshots_et_reexploitations(session_factory):
    query = _query()
    page = _page()
    cache.note_query("conv_a", query.cache_key())
    cache.remember("conv_a", page, source_type="general", query_key=query.cache_key())
    cache.remember("conv_a", page, source_type="general", query_key=query.cache_key())

    assert cache.stat("conv_a") == {"queries": 1, "snapshots": 1, "reused": 1}
    assert cache.stat("conv_inconnue") == {"queries": 0, "snapshots": 0, "reused": 0}


# --------------------------------------------------------------------------
# Purge (T-W12)
# --------------------------------------------------------------------------
def test_purge_convocation_eface_sa_conversation_et_pas_les_autres(session_factory):
    query = _query()
    cache.remember("conv_a", _page(), source_type="general", query_key=query.cache_key())
    cache.remember("conv_b", _page(url="https://exemple.test/autre"),
                   source_type="general", query_key=query.cache_key())
    cache.note_query("conv_a", query.cache_key())
    cache.note_query("conv_b", query.cache_key())

    assert cache.purge_convocation("conv_a") == 1
    assert cache.count_queries("conv_a") == 0, "le journal part avec les snapshots"
    assert cache.stat("conv_a") == {"queries": 0, "snapshots": 0, "reused": 0}
    assert cache.stat("conv_b")["snapshots"] == 1, "une autre conversation n'est jamais touchée"


def test_purge_refuse_de_supprimer_une_page_encore_citee(session_factory):
    citee = _page(url="https://exemple.test/source-citee")
    libre = _page(url="https://exemple.test/source-libre")
    public_id = cache.remember("conv_a", citee, source_type="general", query_key="general|cached|q")
    cache.remember("conv_a", libre, source_type="general", query_key="general|cached|q")

    # Une réponse déjà rendue cite `web_…` : c'est la preuve de la dépendance.
    _rendre_reponse(session_factory, "conv_a", resultat=f'{{"sources": [{{"public_id": "{public_id}"}}]}}')

    assert cache.purge_convocation("conv_a") == 1, "seule la page non citée part"
    assert cache.find_by_url("conv_a", citee.url, now=datetime.now()) is not None, "la page citée est conservée"
    assert cache.find_by_url("conv_a", libre.url, now=datetime.now()) is None, "la page libre est partie"


def test_purge_refuse_aussi_une_citation_portee_par_le_brief(session_factory):
    page = _page(url="https://exemple.test/source-brief")
    public_id = cache.remember("conv_a", page, source_type="general", query_key="general|cached|q")
    _rendre_reponse(session_factory, "conv_a", resultat='{"sources": []}',
                    brief=f'{{"evidence_ids": ["{public_id}"]}}')

    assert cache.purge_convocation("conv_a") == 0
    assert cache.find_by_url("conv_a", page.url, now=datetime.now()) is not None


# --------------------------------------------------------------------------
# Panne : le cache ne fait jamais échouer une recherche
# --------------------------------------------------------------------------
def test_une_base_en_panne_ne_leve_jamais(session_factory, monkeypatch):
    def _base_fermee(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("base fermée"))

    monkeypatch.setattr("backend.db.database.SessionLocal", _base_fermee)
    query = _query()
    page = _page()

    cache.note_query("conv_a", query.cache_key())  # ne doit rien faire, surtout pas lever
    assert cache.find_reusable("conv_a", query, now=datetime.now()) is None
    assert cache.find_by_url("conv_a", URL_PAGE, now=datetime.now()) is None
    assert cache.count_queries("conv_a") == 0
    assert cache.stat("conv_a") == {}
    assert cache.purge_convocation("conv_a") == 0
    # Une preuve non écrite ne s'annonce pas : aucun identifiant n'est rendu.
    assert cache.remember("conv_a", page, source_type="general", query_key=query.cache_key()) == ""
