"""Un résultat web conservé devient une ressource documentaire de sa discussion.

LE MANQUE VERROUILLÉ ICI : `web_research` persistait ses pages dans le cache
de conversation (`WebSnapshot`) et nulle part ailleurs. La recherche locale
(`search_info`, `find_files`) ne lit que le périmètre documentaire — un
résultat web cité au tour N était donc introuvable au tour N+1.

LA RÉFÉRENCE N'EST PAS ICI, ELLE EST DANS LES FICHES :

  - `docs/intentions/outils/web_research.md` §3.1 (v1.2), étapes 6-7 :
    « 6. Enregistrer les ressources documentaires produites dans la discussion.
    7. Réaliser ou planifier leur ingestion. » — la ressource naît à la
    conservation, à l'exécution de l'outil, pas à la citation ni à la diffusion.
  - `docs/intentions/mecanismes/ingestion/sources_web.md` §4.1 : « Le résultat
    utile devient une ressource documentaire de la discussion ayant lancé la
    recherche. » — la portée est la CONVERSATION (web_research.md §3.3 :
    « Une ressource web conservée appartient à sa discussion. »).
  - `sources_web.md` §4.3 : « Les passages utiles sont préparés selon le
    pipeline Texte [...] La fiche documentaire aide `find_files`. » — donc
    ingestion par le CHEMIN COMMUN (observation, fiche, fragments, index,
    `doc_N`), et ce fichier le vérifie : aucun pipeline parallèle.
  - `sources_web.md` §4.5 : « une nouvelle consultation peut être enregistrée
    sans dupliquer inutilement le corps conservé » — donc la déduplication par
    contenu est un CONTRAT, et ce fichier la verrouille dans les deux sens :
    même version = une seule ressource ; contenu changé = une nouvelle version,
    jamais une écrasée (§2.5 : « l'adresse seule ne suffit pas à représenter
    une version de contenu »).

Base neuve et jetable, réseau interdit (le fetcher est intercepté, le socket
est un échec de test), ingestion pilotée à la main : même outillage que
`test_web_tool_path.py` et `test_ingest_images.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.migrations import apply_migrations
from backend.db.models import (
    Artifact,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
    DocRef,
    Observation,
    Workspace,
)
from backend.ingestion import attachment_ingest
from backend.ingestion import sources_web
from backend.ipc import paths_bridge as paths_bridge_module
from backend.services.web import net
from backend.services.web import research as _research
from backend.services.web.fetcher import Fetcher
from backend.services.web.providers.searxng import SEARXNG_URL_ENV
from backend.services.web.settings import SETTINGS_ENV, SETTINGS_FILENAME
from backend.services.web.types import WebPage

URL_PAGE = "https://exemple.test/pythagore"
TITRE_PAGE = "Le théorème de Pythagore"
TEXTE_PAGE = (
    "Le théorème de Pythagore énonce que le carré de l'hypoténuse est égal à la "
    "somme des carrés des deux autres côtés d'un triangle rectangle. Cette "
    "relation de géométrie euclidienne sert de base à toute la trigonométrie "
    "moderne, et sa démonstration la plus connue remonte aux Éléments d'Euclide."
)


# --------------------------------------------------------------------------
# Montage
# --------------------------------------------------------------------------
def _base_neuve(tmp_path, monkeypatch, nom: str) -> Any:
    """Une vraie base jetable, dans l'ordre de `init_db()`, racines redirigées."""
    moteur = create_engine(f"sqlite:///{tmp_path / nom}")
    Base.metadata.create_all(moteur)
    apply_migrations(moteur)
    from backend.db.database import _ensure_fts

    _ensure_fts(moteur)
    factory = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", factory)
    racine = tmp_path / "app"
    (racine / "blobs").mkdir(parents=True, exist_ok=True)
    (racine / "temp").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(paths_bridge_module, "blobs_root", lambda: racine / "blobs", raising=False)
    monkeypatch.setattr(paths_bridge_module, "temp_root", lambda: racine / "temp", raising=False)
    monkeypatch.setattr(
        paths_bridge_module, "data_dirs", lambda: (racine, racine / "temp"), raising=False
    )
    return factory


@pytest.fixture()
def base(tmp_path, monkeypatch) -> Any:
    """La base des tests unitaires d'inscription ; ingestion pilotée à la main."""
    factory = _base_neuve(tmp_path, monkeypatch, "web-ressource.db")
    from backend.search.chunks import ensure_chunk_tables

    with factory() as db:
        ensure_chunk_tables(db)
        db.commit()
    return factory


@pytest.fixture()
def hors_du_reseau(tmp_path, monkeypatch) -> Iterator[None]:
    """Aucune socket ne part de ce fichier : même garde que `test_web_tool_path`."""
    net.reset_host_gate()
    monkeypatch.setattr(
        net, "http_get",
        lambda *a, **k: pytest.fail("ce test ne doit pas ouvrir de socket : le réseau est interdit"),
    )
    monkeypatch.delenv(SEARXNG_URL_ENV, raising=False)
    monkeypatch.delenv(_research.MODE_ENV, raising=False)
    monkeypatch.setenv(SETTINGS_ENV, str(tmp_path / SETTINGS_FILENAME))
    yield
    net.reset_host_gate()


def _page(*, url: str = URL_PAGE, titre: str = TITRE_PAGE, texte: str = TEXTE_PAGE,
          statut: int = 200, extracteur: str = "extract-1",
          fetched_at: datetime | None = None) -> WebPage:
    """Une page dont le `content_sha256` correspond VRAIMENT à son texte."""
    corps = texte.encode("utf-8")
    return WebPage(
        url=url,
        final_url=url,
        status=statut,
        content_type="text/html",
        content_sha256=hashlib.sha256(corps).hexdigest(),
        text=texte,
        extractor_version=extracteur,
        fetched_at=fetched_at or datetime.now(),
        title=titre,
        source_type="general",
        proposed_by="wikipedia",
    )


def _ingere_à_la_main(monkeypatch) -> list[str]:
    """Neutralise le fil d'arrière-plan : le chemin commun est CAPTÉ, pas exécuté.

    `lancer_ingestion` est le déclencheur RÉEL du chemin commun — le capter
    prouve que la ressource y passe ; le relancer à la main ensuite
    (`_ingérer_un`) rend l'ingestion déterministe sans recopier un
    pseudo-pipeline.
    """
    déclenchés: list[str] = []

    def _capturer(ids: list[str], *, modeles_autorises: bool = True) -> None:
        déclenchés.extend(str(i) for i in ids)

    monkeypatch.setattr(attachment_ingest, "lancer_ingestion", _capturer)
    return déclenchés


def _texte_travail_de(factory, artifact_id: str) -> str:
    """Le contenu du blob d'un artefact, lu par la résolution RÉELLE des chemins."""
    from backend.ipc import paths_bridge as _pb

    with factory() as db:
        artefact = db.get(Artifact, artifact_id)
        relatif = db.get(Blob, artefact.blob_id).relative_path
    return (_pb.data_dirs()[0] / relatif).read_text(encoding="utf-8")


# ==========================================================================
# A. La ressource : écriture, provenance, portée
# ==========================================================================
def test_une_page_conservee_devient_une_ressource_documentaire_de_sa_discussion(
    base, monkeypatch,
) -> None:
    """Chaîne complète : Blob, Artifact `text`, lien conversation, `doc_N`.

    Et l'ingestion est déclenchée par le déclencheur COMMUN
    (`lancer_ingestion`), pas par un pipeline web parallèle — c'est ce
    déclencheur partagé qui garantit qu'une ressource web se traite comme un
    `.txt` déposé.
    """
    déclenchés = _ingere_à_la_main(monkeypatch)

    artifact_id = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(), "web_abc123def456", source_type="general")

    assert artifact_id, "la ressource doit être créée"
    assert déclenchés == [artifact_id], "l'ingestion doit passer par le déclencheur commun"
    with base() as db:
        artefact = db.get(Artifact, artifact_id)
        assert artefact is not None and artefact.kind == "text"
        assert artefact.ingest_state == "depose"
        blob = db.get(Blob, artefact.blob_id)
        assert blob is not None and blob.mime_detected == "text/markdown"
        liens = db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == artifact_id).all()
        assert [(l.scope_kind, l.scope_id, l.role) for l in liens] == [
            ("conversation", "conv_c1", "web_research")]
        labels = db.query(DocRef).filter(DocRef.artifact_id == artifact_id).all()
        assert [l.doc_label for l in labels] == ["doc_1"], "le registre alloue le doc_N"


def test_la_ressource_porte_la_provenance_web_dans_son_contenu(base, monkeypatch) -> None:
    """URL, service, niveau de lecture : la provenance est LISIBLE et indexable.

    sources_web.md §2.5 : « La ressource conserve son titre ou nom lisible,
    l'URL ou l'identifiant documentaire disponible » ; §2.2 : un résumé ne
    doit pas se présenter comme une page lue. Le texte de travail porte donc
    la provenance AVANT le contenu — et elle est indexée avec lui.
    """
    _ingere_à_la_main(monkeypatch)
    resume = _page(statut=0, extracteur="engine-resume-1")

    artifact_id = sources_web.enregistrer_ressource_web(
        "conv_c1", resume, "web_abc123def456", source_type="academic")

    texte_travail = _texte_travail_de(base, artifact_id)
    assert f"Source : {URL_PAGE}" in texte_travail
    assert "Service ayant fourni le contenu : wikipedia" in texte_travail
    # §2.2 : un résumé de moteur ne s'annonce jamais comme une page lue.
    assert "Niveau de lecture : résumé fourni par le moteur — page non lue" in texte_travail
    assert "Niveau de lecture : contenu lu" not in texte_travail
    # Le contenu retenu est là, et rien d'inventé.
    assert "Le théorème de Pythagore" in texte_travail


def test_une_page_sans_contenu_ne_devient_pas_une_coquille_vide(base, monkeypatch) -> None:
    """Métadonnées sans aucun contenu retenu : rien à ingérer, rien de créé.

    sources_web.md §3.2 admet « une fiche de référence enregistrée sans texte
    intégral » ; mais sans AUCUN contenu retenu, un artefact texte ne porterait
    que son nom — un dépôt vide que la recherche chercherait en vain.
    """
    _ingere_à_la_main(monkeypatch)
    metadata_seule = _page(texte="")

    artifact_id = sources_web.enregistrer_ressource_web(
        "conv_c1", metadata_seule, "web_abc123def456", source_type="general")

    assert artifact_id == ""
    with base() as db:
        assert db.query(Artifact).count() == 0


def test_le_niveau_de_lecture_dune_page_lue_est_annonce(base, monkeypatch) -> None:
    """Une page RÉELLEMENT lue (statut 200, extracteur de page) le dit."""
    _ingere_à_la_main(monkeypatch)

    artifact_id = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(), "web_abc123def456", source_type="general")

    texte_travail = _texte_travail_de(base, artifact_id)
    assert "Niveau de lecture : contenu lu par extraction" in texte_travail
    assert "résumé fourni par le moteur" not in texte_travail


# ==========================================================================
# B. Déduplication — sources_web.md §4.5 et §2.5
# ==========================================================================
def test_une_revalidation_inchangee_ne_duplique_pas_la_ressource(base, monkeypatch) -> None:
    """Même contenu, re-datté : UNE ressource, UN doc_N, une ingestion.

    La date de consultation est exclue du texte de travail : elle change à
    chaque revalidation (§2.4 : « Une réutilisation sans réseau ne change pas
    la date de vérification extérieure »), et la faire entrer ferait de chaque
    relecture une nouvelle version — la duplication que §4.5 interdit.
    """
    déclenchés = _ingere_à_la_main(monkeypatch)
    maintenant = datetime.now()

    premier = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(fetched_at=maintenant), "web_abc123def456", source_type="general")
    rejoué = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(fetched_at=maintenant + timedelta(hours=1)),
        "web_abc123def456", source_type="general")

    assert rejoué == premier, "la même version doit retomber sur la même ressource"
    assert déclenchés == [premier], "une revalidation ne réingère rien"
    with base() as db:
        assert db.query(Artifact).count() == 1
        assert db.query(DocRef).count() == 1


def test_un_contenu_change_cree_une_ressource_nouvelle_sans_ecraser(base, monkeypatch) -> None:
    """Contenu changé : une VERSION nouvelle, l'ancienne reste intacte.

    §2.5 : « une même adresse pouvant renvoyer des contenus différents,
    l'adresse seule ne suffit pas à représenter une version de contenu » ;
    §4.5 : « Si le contenu change, conserver une distinction de version ».
    """
    _ingere_à_la_main(monkeypatch)

    ancien = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(), "web_abc123def456", source_type="general")
    nouveau = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(texte=TEXTE_PAGE + " Mise à jour : la démonstration d'Euclide."),
        "web_abc123def456", source_type="general")

    assert nouveau != ancien
    with base() as db:
        vivants = db.query(Artifact).filter(Artifact.deleted_at.is_(None)).all()
        assert {a.id for a in vivants} == {ancien, nouveau}


def test_un_contenu_identique_d_une_autre_discussion_est_une_ressource_distincte(
    base, monkeypatch,
) -> None:
    """§4.5 : « Une déduplication physique ne partage pas les ressources entre
    discussions. » Le blob peut être partagé ; la ressource, jamais."""
    _ingere_à_la_main(monkeypatch)

    de_c1 = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(), "web_abc123def456", source_type="general")
    de_c2 = sources_web.enregistrer_ressource_web(
        "conv_c2", _page(), "web_abc123def456", source_type="general")

    assert de_c2 != de_c1
    with base() as db:
        assert db.query(Blob).count() == 1, "le corps est dédupliqué physiquement"
        liens = db.query(ArtifactLink).filter(
            ArtifactLink.scope_kind == "conversation").all()
        assert {l.scope_id for l in liens} == {"conv_c1", "conv_c2"}


# ==========================================================================
# C. Le chemin commun, bout en bout : recherche et fiches
# ==========================================================================
def test_la_ressource_ingeree_est_retrouvable_par_la_recherche_locale(
    base, monkeypatch,
) -> None:
    """Le résultat web, une fois ingéré par le chemin commun, se retrouve par
    `find_files` (la fiche) ET par le contenu (les fragments indexés)."""
    _ingere_à_la_main(monkeypatch)
    artifact_id = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(), "web_abc123def456", source_type="general")
    attachment_ingest._ingérer_un(artifact_id)

    with base() as db:
        from backend.search import fts as _fts, scope as _scope

        assert artifact_id in _scope.resolve_allowed_artifacts("conv_c1")
        fragments = db.query(ArtifactChunk).filter(
            ArtifactChunk.artifact_id == artifact_id).all()
        assert fragments, "le chemin commun produit les fragments"
        observation = db.query(Observation).filter(
            Observation.artifact_id == artifact_id,
            Observation.tool_name == "attachment_ingest",
            Observation.status == "completed",
        ).all()
        assert observation, "l'ingestion commune a tourné"
        # find_files : la FICHE de la ressource est dans le registre.
        page = _fts.search_scopes(
            db, query="théorème Pythagore", conversation_id="conv_c1",
            allowed_ids={artifact_id})
        trouves = [r.get("artifact_id") for r in (page.get("results") or [])]
        assert artifact_id in trouves, page.get("results")
        resultat = next(r for r in page["results"] if r["artifact_id"] == artifact_id)
        assert resultat.get("kind") == "text"
        assert "pythagore" in (resultat.get("name") or "").lower()


def test_une_recherche_d_espace_retrouve_la_ressource(base, monkeypatch) -> None:
    """sources_web.md §4.1 : « Il alimente le corpus documentaire de l'espace
    de rattachement : les autres discussions de ce même espace peuvent le
    retrouver avec sa provenance. » — la ressource de portée conversation est
    vue par la recherche d'INFORMATION de l'espace (`search_space_information`)."""
    _ingere_à_la_main(monkeypatch)
    with base() as db:
        db.add(Workspace(id="ws_c1", name="Espace de test", state="active"))
        db.add(Conversation(id="conv_c1", title="Discussion de test",
                            workspace_id="ws_c1"))
        db.commit()
    artifact_id = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(), "web_abc123def456", source_type="general")
    attachment_ingest._ingérer_un(artifact_id)

    with base() as db:
        from backend.search import space_info as _space

        page = _space.search_space_information(db, "ws_c1", "théorème Pythagore")
        # La recherche d'information ne rend AUCUN identifiant technique
        # (`file_access: False`) : la ressource est reconnue à sa provenance —
        # le nom du document, qui est le titre de la page — depuis la portée
        # « conversation » de l'espace.
        assert page.get("ok") is True
        noms = [r["provenance"].lower() for r in (page.get("results") or [])]
        assert any("pythagore" in nom for nom in noms), page.get("results")
        resultat = next(r for r in page["results"] if "pythagore" in r["provenance"].lower())
        assert resultat["origin"] == "conversation", (
            "la provenance de la ressource reste la discussion qui l'a lancée"
        )


def test_supprimer_la_discussion_retire_la_ressource_du_corpus(base, monkeypatch) -> None:
    """sources_web.md §4.6 : « Supprimer une discussion retire ses ressources
    web conservées et leurs dérivés du périmètre local. » La corbeille retire
    les liens de portée conversation SANS exception de rôle."""
    _ingere_à_la_main(monkeypatch)
    artifact_id = sources_web.enregistrer_ressource_web(
        "conv_c1", _page(), "web_abc123def456", source_type="general")

    with base() as db:
        from datetime import datetime as _dt

        lien = db.query(ArtifactLink).filter(
            ArtifactLink.artifact_id == artifact_id).one()
        lien.removed_at = _dt.now()
        db.commit()

    with base() as db:
        from backend.search import scope as _scope

        assert artifact_id not in _scope.resolve_allowed_artifacts("conv_c1"), (
            "une ressource retirée ne reste pas dans le périmètre"
        )


# ==========================================================================
# D. La porte réelle : `research.run` enregistre, rend, et ne ment pas
# ==========================================================================
def _servir(conv_id: str, page: WebPage | None = None) -> str:
    """Mémorise la page dans le cache de CETTE conversation, clé de requête exacte.

    Servir la page par le cache est la seule façon de faire entrer une page
    dans `run` sans réseau — et c'est un chemin RÉEL de l'outil : la
    réutilisation est la fonctionnalité (web_research.md §2.1,
    `reuse_if_sufficient`).
    """
    from backend.services.web import cache as _cache
    from backend.services.web.types import WebQuery

    page = page or _page()
    query = WebQuery(text="theoreme de Pythagore", source_type="general")
    return _cache.remember(
        conv_id, page, source_type="general", query_key=query.cache_key())


def test_le_resultat_dun_appel_web_est_enregistre_et_rendu_comme_ressource(
    tmp_path, monkeypatch, hors_du_reseau,
) -> None:
    """La porte RÉELLE (`research.run`) : la page conservée devient une
    ressource, et le rendu l'annonce — fiche web_research §3.2 : « Les
    ressources conservées et leur état d'ingestion » doivent être
    identifiables."""
    _base_neuve(tmp_path, monkeypatch, "web-porte.db")
    from backend.search.chunks import ensure_chunk_tables

    with database_module.SessionLocal() as db:
        ensure_chunk_tables(db)
        db.commit()
    public_id = _servir("conv_c1")
    assert public_id, "le cache doit retenir la page pour ce test"

    issue = _research.run(
        queries=["theoreme de Pythagore"], source_type="general",
        purpose="verifier la ressource documentaire", conv_id="conv_c1",
        user_message="",
    )

    assert issue.ok is True
    assert issue.conserves, "chaque page conservée correspond à la ressource créée"
    assert "Conservation : ressource documentaire créée dans la discussion" in issue.text
    # L'ingestion n'est plus déléguée : la ressource a déjà changé d'état
    # au retour de l'appel (deposed n'est plus l'état terminal observé).
    with database_module.SessionLocal() as db:
        artefact = db.get(Artifact, next(iter(issue.conserves.values())))
        assert artefact is not None
        assert artefact.ingest_state != "depose"


def test_un_echec_de_conservation_est_rendu_sans_mentir(
    tmp_path, monkeypatch, hors_du_reseau,
) -> None:
    """web_research.md §4.1 : « Conservation échouée — ne pas promettre une
    réutilisation locale persistante. » Un échec d'inscription se rend comme
    tel, et ne détruit pas le résultat de la recherche déjà obtenu."""
    _base_neuve(tmp_path, monkeypatch, "web-echec.db")
    public_id = _servir("conv_c1")
    assert public_id
    monkeypatch.setattr(
        sources_web, "enregistrer_ressource_web",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("base de test fermée")),
    )

    issue = _research.run(
        queries=["theoreme de Pythagore"], source_type="general",
        purpose="verifier le rendu d'un echec", conv_id="conv_c1",
        user_message="",
    )

    assert issue.ok is True, "un échec de conservation ne détruit pas la recherche"
    assert issue.conserves == {URL_PAGE: ""}, "l'échec est nommé dans l'issue"
    assert "Conservation : ÉCHEC" in issue.text
    assert "ressource documentaire créée" not in issue.text
