"""La chaîne de citation web : de la page lue à la preuve relivable.

Une preuve web ne vaut que par sa **relecture** : `web_<12hex>:<n>` doit
désigner un fragment réellement stocké, dans CETTE conversation, à un rang
qui existe. Ces tests prouvent cette propriété par le chemin réel
(`research.resolve_evidence` — la SEULE lecture d'une preuve conservée, et
`brief.validate_submit_brief` pour la forme du handoff), pas par une
réimplémentation : le défaut que cette suite verrouillait était précisément un
test qui recopiait la vérification au lieu de l'appeler, et ne voyait donc pas
que toute preuve web était rejetée. Depuis la signature v1.2 (`{"brief"}`),
la validation du handoff ne porte plus de contrôle de preuve ; l'existence
d'une preuve vit dans la lecture resolvable, citée dans le texte du brief.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.migrations import apply_migrations
from backend.db.models import Base
from backend.inference import brief
from backend.services.web import cache as _cache
from backend.services.web import net as _net
from backend.services.web import research as _research
from backend.services.web.types import WebPage

TEXTE = "Le théorème de Pythagore relie les côtés d'un triangle rectangle. " * 80


def _page(*, titre: str = "Pythagore", texte: str = TEXTE) -> WebPage:
    import hashlib

    return WebPage(
        url="https://exemple.fr/pythagore",
        final_url="https://exemple.fr/pythagore",
        status=200,
        content_type="text/html",
        content_sha256=hashlib.sha256(texte.encode("utf-8")).hexdigest(),
        text=texte,
        extractor_version="web-extract-1",
        fetched_at=datetime.now(),
        title=titre,
        source_type="general",
        proposed_by="wikipedia",
    )


def _monter_base_temporaire(tmp_path, monkeypatch) -> None:
    """Une vraie base SQLite, dans l'ordre exact de `init_db()`.

    On remplace `SessionLocal` et non `THOT_DB_PATH` : `DB_PATH` est figé à
    l'import de `backend.db.database` (voir `database.py:21-47`), donc une
    variable posée ici serait lue trop tard et le test travaillerait sur la base
    du développeur. C'est le même outillage que `test_web_cache.py`.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", factory)
    monkeypatch.setenv("THOT_WEB_SETTINGS_PATH", str(tmp_path / "web.settings"))
    for nom in ("THOT_WEB_MODE", "THOT_WEB_SEARXNG_URL"):
        monkeypatch.delenv(nom, raising=False)


@pytest.fixture()
def conv_id(tmp_path, monkeypatch):
    """Identifiant de conversation adossé à une base temporaire isolée."""
    _monter_base_temporaire(tmp_path, monkeypatch)
    return "conv_test"


def _brief_citant(evidence_id: str) -> tuple[bool, str]:
    """Le handoff v1.2 : une preuve citée dans le TEXTE du brief."""
    return brief.validate_submit_brief(
        {"brief": f"D'après la page consultée ({evidence_id}) : a2+b2=c2."})[:2]


# --------------------------------------------------------------------------
# La forme
# --------------------------------------------------------------------------
def test_la_forme_web_se_decoupe_dans_le_resolveur() -> None:
    """`web_<12hex>:<n>` se lit comme `doc_N:k` : un label, un rang — la
    grammaire vit à l'endroit UNIQUE de la lecture (`research.resolve_evidence`)."""
    page = _page()
    public_id = _cache.remember("conv_forme", page, source_type="general", query_key="k")
    assert _research.resolve_evidence(f"{public_id}:0", conv_id="conv_forme") is not None
    assert _research.resolve_evidence(f"{public_id}:12", conv_id="conv_forme") is None


@pytest.mark.parametrize("candidat", [
    "web_1022ea9983f:0",      # 11 hex : trop court
    "web_1022ea9983f99:0",    # 14 hex : trop long
    "web_1022EA9983F9:0",     # majuscules : hors vocabulaire
    "web_1022ea9983f9",       # sans rang
    "web_1022ea9983f9:x",     # rang non numérique
    "doc_1:0",                # autre famille
    "obs_abc",
    "",
])
def test_les_formes_non_web_ne_sont_pas_des_preuves_web(candidat: str) -> None:
    """Une preuve qui n'est pas web ne passe pas par le contrôle web."""
    assert _research.resolve_evidence(candidat, conv_id="conv_forme") is None


# --------------------------------------------------------------------------
# L'existence, par le chemin réel
# --------------------------------------------------------------------------
def test_une_preuve_reellement_lue_est_relisible(conv_id: str) -> None:
    """Le chemin complet : page mémorisée → identifiant → lecture résolue →
    handoff v1.2 recevable qui la cite dans son texte.

    C'est le test qui manquait. Sans lui, la chaîne web pouvait être cassée
    sans qu'aucun test ne le voie : tous les tests antérieurs recopediaient la
    vérification d'admission au lieu de l'appeler.
    """
    page = _page()
    public_id = _cache.remember(conv_id, page, source_type="general", query_key="k")
    evidence_id = _research._evidence_ids(public_id, page)[0]

    assert _research.resolve_evidence(evidence_id, conv_id=conv_id) is not None
    assert _brief_citant(evidence_id) == (True, "ok")


def test_une_preuve_web_d_une_autre_conversation_est_refusee(conv_id: str) -> None:
    """La portée décide : le contenu connu d'une conversation n'héritera pas d'une autre."""
    page = _page()
    public_id = _cache.remember(conv_id, page, source_type="general", query_key="k")
    evidence_id = _research._evidence_ids(public_id, page)[0]

    assert _research.resolve_evidence(evidence_id, conv_id="autre_conversation") is None


def test_un_ordinal_hors_bornes_est_refuse(conv_id: str) -> None:
    """Une source lue ne rend pas relivable un fragment qui n'existe pas."""
    page = _page()
    public_id = _cache.remember(conv_id, page, source_type="general", query_key="k")
    nabime = f"{public_id}:9999"

    assert _research.resolve_evidence(nabime, conv_id=conv_id) is None


def test_une_source_inexistante_est_refusee(conv_id: str) -> None:
    """Une page peut être forgeée : rien ne la rattache à une lecture."""
    forge = "web_0123456789ab:0"
    assert _research.resolve_evidence(forge, conv_id=conv_id) is None


def test_une_preuve_web_ne_se_lit_pas_comme_une_preuve_de_document() -> None:
    """Une preuve web ne doit pas être validée par le contrôle des documents.

    Les deux familles ont la même grammaire, pas le même périmètre — depuis la
    signature v1.2, le handoff du brief ne porte plus ce contrôle : la règle
    survit dans la grammaire fermée du résolveur, qui refuse hors `web_`.
    """
    assert _research.resolve_evidence("doc_7:0", conv_id="conv_test") is None


def test_un_rendu_web_est_citable_dans_le_handoff_v1_2(conv_id: str) -> None:
    """La construction ne requiert pas d'inventaire : une preuve rendue ce tour
    se cite dans le texte du brief, le schéma v1.2 ne juge que la forme."""
    page = _page()
    public_id = _cache.remember(conv_id, page, source_type="general", query_key="k")
    ok, code, norm = brief.validate_submit_brief(
        {"brief": f"A2+B2=C2 (source consultée ce tour : {public_id}:0)."})
    assert (ok, code) == (True, "ok") and f"{public_id}:0" in norm["brief"]


# --------------------------------------------------------------------------
# Le compteur rendu : il compte ce qui est parti, rien d'autre
# --------------------------------------------------------------------------
def test_le_compteur_reseau_compte_les_requetes_reellement_parties(
    tmp_path, monkeypatch,
) -> None:
    """Le rendu ne doit compter que les requêtes sorties, jamais les tentatives.

    Trois sources d'erreur, et une seule bonne réponse : le décompte se fait à
    l'endroit unique où la requête part. Compter les appels à un moteur
    omettrait `robots.txt` et les pages, et compterait comme partant une
    adresse refusée par le garde SSRF.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    vues: list[str] = []

    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - nom imposé par http.server
            vues.append(self.path)
            if self.path == "/robots.txt":
                corps = b"User-agent: *\nAllow: /\n"
            else:
                corps = b"<article><p>Contenu de test.</p></article>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

        def log_message(self, format: str, *args: object) -> None:
            return  # le serveur de test se tait

    serveur = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(
            _research._providers, "applicable_providers",
            lambda _type: (_FauxMoteur(serveur.server_address[1]),),
        )
        monkeypatch.setenv("THOT_WEB_SETTINGS_PATH", str(tmp_path / "s"))
        # `run` persiste la page lue : il lui faut une base, comme la fixture
        # `conv_id` mais sans son identifiant de conversation.
        _monter_base_temporaire(tmp_path, monkeypatch)
        # Le serveur de test est en loopback, que le garde SSRF refuse — à
        # raison. On ne neutralise QUE la résolution, pas les autres gardes : la
        # redirection, les bornes et la provenance restent actifs et le prouvent.
        monkeypatch.setattr(
            _research.net, "resolve_global", lambda _h, _p: ["127.0.0.1"],
        )
        # Le serveur de test est en loopback : absent de la liste de domaines.
        # La liste n'est pas l'objet de ce test, on la désactive donc.
        monkeypatch.setenv("THOT_WEB_ALLOWED_DOMAINS", "*")

        issue = _research.run(
            queries=["contenu de test"],
            source_type="general",
            purpose="vérifier le compteur",
            conv_id="conv_compteur",
        )
    finally:
        serveur.shutdown()

    assert issue.ok is True
    # Le moteur simulé interroge l'API et le fetcher robots.txt puis la page :
    # trois requêtes parties, donc trois. Le compteur précédent en rendait 1.
    assert issue.network_queries == len(vues) == 3, (
        f"compteur={issue.network_queries}, requêtes vues par le serveur={len(vues)}"
    )
    assert issue.pages_read == 1


class _FauxMoteur:
    """Un moteur qui part sur le réseau, pour mesurer le vrai compteur."""

    name = "faux"
    covers = frozenset({"general"})
    coverage = "moteur de test"

    def __init__(self, port: int) -> None:
        self._base = f"http://127.0.0.1:{port}"

    def search(self, query, *, max_results: int, deadline: float):
        from backend.services.web import net
        from backend.services.web.types import ProviderAnswer, WebHit

        import urllib.parse

        budget = net.NetworkBudget(deadline=deadline)
        # La requête est échappée comme le ferait un vrai moteur : sans cela,
        # le garde d'URL refuse l'espace, et c'est bien fait.
        net.http_get(
            f"{self._base}/api?q={urllib.parse.quote(query.text)}", budget=budget, page=False,
        )
        return ProviderAnswer(
            provider=self.name,
            coverage=self.coverage,
            hits=(WebHit(url=f"{self._base}/article", title="Article",
                         snippet="résumé", provider=self.name, rank=1),),
        )


def test_une_url_refusee_par_le_garde_ssrf_n_est_pas_comptée_comme_requête(
    tmp_path, monkeypatch,
) -> None:
    """Un refus ne part pas sur le réseau, donc il ne se compte pas.

    C'est la différence entre « tentative » et « requête partie », et le contrat
    tranche pour la seconde. Un compteur qui gonfle à chaque refus annonce à
    l'utilisateur une politesse qu'il n'a pas eue. On garde ici le garde
    RÉEL — seule l'URL est anormale, jamais la résolution — sinon on
    prouverait un garde désactivé.
    """
    _monter_base_temporaire(tmp_path, monkeypatch)
    # Un moteur qui rend une URL d'métadonnées cloud : le garde la refuse.
    monkeypatch.setattr(
        _research._providers, "applicable_providers",
        lambda _type: (_MoteurURLInterdite(),),
    )
    avant = _net.requests_out_total()

    issue = _research.run(
        queries=["test"],
        source_type="general",
        purpose="vérifier le refus",
        conv_id="conv_refus",
        user_message="http://169.254.169.254/latest/meta-data",
    )

    assert _net.requests_out_total() == avant, "aucune requête ne aurait dû partir"
    assert issue.network_queries == 0
    assert issue.pages_read == 0
    assert issue.skipped, "le refus doit être motivé, jamais silencieux"


class _MoteurURLInterdite:
    """Un moteur qui propose une adresse que le garde doit refuser."""

    name = "interdit"
    covers = frozenset({"general"})
    coverage = "moteur de test"

    def search(self, query, *, max_results: int, deadline: float):
        from backend.services.web.types import ProviderAnswer, WebHit

        return ProviderAnswer(
            provider=self.name,
            coverage=self.coverage,
            hits=(WebHit(url="http://169.254.169.254/latest/meta-data",
                         title="interdite", provider=self.name, rank=1),),
        )
