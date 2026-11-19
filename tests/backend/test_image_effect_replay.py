"""Rejeu d'effet image entre deux conversations + garde-fou du brief (sans GPU).

Trois défauts mesurés en E2E, verrouillés ici :

1. Redemander la même image depuis une conversation NOUVE retournait
   ``[EFFECT_PUBLISHED]`` avec ``ok=False`` et SANS ``artifact_id`` : aucune
   diffusion, aucun lien, aucun ``MessageAttachment``, et pourtant le run se
   terminait en ``completed``. Le rejeu doit rattacher l'artefact et réutiliser.
2. Le brief pouvait porter un fait « image générée » rédigé par l'orchestrateur
   pour un effet qui n'a jamais tourné.
3. Une tentative image non publiée doit toujours laisser une trace exploitable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Artifact, ArtifactLink, Base, Blob
from backend.inference import loop as loop_mod
from backend.services import image_service as svc


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'replay.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )()
    yield session
    session.close()


def _publish(db, artifact_id: str, conversation_id: str, kind: str = "image") -> None:
    """Simule une publication : Blob + Artifact + lien conversation/generated."""
    db.add(
        Blob(
            id=f"sha_{artifact_id}",
            sha256=f"sha_{artifact_id}",
            relative_path=f"blobs/sha256/aa/sha_{artifact_id}.bin",
            size_bytes=10,
            mime_detected="image/png",
            state="available",
        )
    )
    db.add(
        Artifact(
            id=artifact_id,
            blob_id=f"sha_{artifact_id}",
            kind=kind,
            original_filename="f.png",
            display_name="f.png",
        )
    )
    db.flush()
    db.add(
        ArtifactLink(
            id=f"lnk_{artifact_id}_{conversation_id}",
            artifact_id=artifact_id,
            scope_kind="conversation",
            scope_id=conversation_id,
            role="generated",
        )
    )
    db.commit()


class _Claim:
    def __init__(self, status: str, artifact_id: str | None) -> None:
        self.effect_id = "eff_1"
        self.status = status
        self.output_artifact_id = artifact_id


# --- Fix 1 : le rejeu rattache l'artefact, ne rediffuse pas -------------------


def test_replay_into_new_conversation_links_the_artifact(db, monkeypatch):
    """Conversation B, même demande : l'artefact de A est rattaché et rendu."""
    from backend.services.effect_ledger import is_artifact_replayable

    _publish(db, "art_reuse", "conv_a")
    diffusions: list[int] = []
    monkeypatch.setattr(
        "backend.services.image_service.generate", lambda *a, **k: diffusions.append(1)
    )

    # PRÉCONDITION MESURÉE en E2E : l'artefact existe et est valide, mais n'est
    # pas porté par conv_b — c'est exactement ce qui faisait échouer le rejeu.
    assert is_artifact_replayable(db, "art_reuse", "conv_a") is True
    assert is_artifact_replayable(db, "art_reuse", "conv_b") is False

    result = loop_mod._replay_published_image(
        db, "art_reuse", "run_1", "req_1", "conv_b", 0.0, relink_into_conversation=True
    )

    assert is_artifact_replayable(db, "art_reuse", "conv_b") is True, (
        "après rejeu, l'artefact doit être rejouable depuis conv_b"
    )

    assert result["ok"] is True
    assert result["image_published"] is True
    assert result["artifact_id"] == "art_reuse", (
        "l'ID d'artefact doit survivre au rejeu"
    )
    assert "sans nouvelle diffusion" in result["result"]
    assert diffusions == [], "un rejeu ne doit JAMAIS rediffuser"
    link = (
        db.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == "art_reuse",
            ArtifactLink.scope_kind == "conversation",
            ArtifactLink.scope_id == "conv_b",
        )
        .first()
    )
    assert link is not None, "l'artefact doit être visible depuis la conversation B"
    assert link.role == "generated", "le rôle doit rester 'generated' pour la galerie"


def test_replay_is_idempotent_on_repeated_calls(db):
    """Rappeler le rejeu ne duplique pas le lien (Law 3 : même entrée, même sortie)."""
    _publish(db, "art_reuse", "conv_a")
    loop_mod._replay_published_image(
        db, "art_reuse", "r1", "q1", "conv_b", 0.0, relink_into_conversation=True
    )
    assert (
        loop_mod._replay_published_image(
            db, "art_reuse", "r2", "q2", "conv_b", 0.0, relink_into_conversation=True
        )["ok"]
        is True
    )
    assert (
        db.query(ArtifactLink)
        .filter(
            ArtifactLink.artifact_id == "art_reuse", ArtifactLink.scope_id == "conv_b"
        )
        .count()
        == 1
    )


def test_replay_fails_loud_when_artifact_is_gone(db):
    """Loi 4 : un artefact introuvable s'arrête, il ne se fait jamais passer pour un succès."""
    with pytest.raises(RuntimeError, match="REPLAY_UNUSABLE"):
        loop_mod._replay_published_image(
            db, "art_absent", "r1", "q1", "conv_b", 0.0, relink_into_conversation=True
        )


def test_replay_fails_loud_when_artifact_is_not_an_image(db):
    _publish(db, "art_pdf", "conv_a", kind="pdf")
    with pytest.raises(RuntimeError, match="REPLAY_UNUSABLE"):
        loop_mod._replay_published_image(
            db, "art_pdf", "r1", "q1", "conv_b", 0.0, relink_into_conversation=True
        )


def test_replay_without_artifact_id_fails_loud(db):
    with pytest.raises(RuntimeError, match="REPLAY_UNUSABLE"):
        loop_mod._replay_published_image(
            db, "", "r1", "q1", "conv_b", 0.0, relink_into_conversation=True
        )


# --- Fix 2b : la prose de l'orchestrateur ne peut pas survivre --------------


def test_orchestrator_invented_image_claim_is_dropped_when_nothing_published():
    brief = {"brief": "Une image photo réaliste en macro d'un papillon a été générée."}
    status = loop_mod._record_image_status(
        None, {"ok": False, "artifact_id": "", "result": "[EFFECT_PUBLISHED]"}
    )
    merged = loop_mod._with_image_status(brief, status)
    assert merged is not None
    texte = merged["brief"]
    assert not loop_mod._IMAGE_SUCCESS_CLAIM_RE.search(texte), (
        f"une promesse image non publiée doit disparaître, reste : {texte}"
    )
    assert "partielle" in texte, "l'échec doit annoncer une couverture partielle"
    assert "Limite :" in texte, "l'échec doit produire une limite nommée"


def test_real_published_claim_survives():
    brief = {
        "brief": loop_mod._IMAGE_STATUS_FACT
        + " Description du contenu réellement préparée par le modèle."
    }
    status = loop_mod._record_image_status(
        None, {"ok": True, "image_published": True, "artifact_id": "art_ok"}
    )
    merged = loop_mod._with_image_status(brief, status)
    assert merged is not None
    texte = merged["brief"]
    assert loop_mod._IMAGE_STATUS_FACT in texte
    assert texte.count(loop_mod._IMAGE_STATUS_FACT) == 1
    # Une image PUBLIÉE n'annonce pas une couverture partielle.
    assert "partielle" not in texte


def test_claim_guard_does_not_eat_ordinary_image_mentions():
    """Motif étroit : une question ou une description d'image doit survivre."""
    for text in (
        "La question porte sur l'image jointe.",
        "L'image montre un homme en uniforme.",
        "Faut-il générer une image de plus ?",
        # Les quasi-échappements réels : une image DÉJÀ présente, dont on
        # parle, dont on mesure le fichier. « est dans » ≠ « est prête ».
        "L'image est dans la pièce jointe.",
        "Le fichier image fait 2 Mo.",
        "La photo du rapport est dans le dossier partagé.",
        "Il faut comparer les deux images jointes.",
        "Le générateur d'images est lent.",
        "La génération d'images prend du temps.",
        "Décris cette image en détail.",
        "L'image que j'ai envoyée est la bonne.",
    ):
        assert not loop_mod._IMAGE_SUCCESS_CLAIM_RE.search(text), text
    assert loop_mod._IMAGE_SUCCESS_CLAIM_RE.search("Une image a été générée.")


# --- M2 : les formulations que le 8B prononce RÉELLEMENT sur un échec -------
# Chacune a été lue en E2E alors qu'aucune image n'existait. Le premier motif
# n'en attrapait AUCUNE : il exigeait « image » (jamais « photo » ni «
# illustration ») et un passif « a/ont été <participe> » (jamais un actif).

ESCAPING_CLAIM_PHRASINGS = (
    "La photo d'un papillon a été générée.",
    "L'illustration de la forêt a été créée.",
    "J'ai généré une image de papillon.",
    "Voici l'image que j'ai créée.",
    "L'image est prête et disponible au téléchargement.",
    "Les photos ont été générées.",
    "J'ai fait une photo de la plage.",
    "Voici le portrait que j'ai réalisé.",
    "Le rendu que j'ai produit est disponible.",
    "La photo a été produite par le moteur.",
)


@pytest.mark.parametrize("text", ESCAPING_CLAIM_PHRASINGS)
def test_chaque_promesse_reelle_est_detectee(text):
    assert loop_mod._IMAGE_SUCCESS_CLAIM_RE.search(text), text


def test_le_vocabulaire_des_noms_est_partage_avec_le_service():
    # Le détecteur ne peut pas avoir son propre lexique : la demande de référence
    # est « papillon PHOTO réaliste en macro », pas « papillon image ». Le service
    # n'expose plus qu'UN lexique — celui-ci : l'admission par mot-clé a été supprimée.
    from backend.services.image_service import IMAGE_CLAIM_NOUNS

    for noun in (
        "image",
        "photo",
        "photographie",
        "illustration",
        "visuel",
        "paysage",
        "portrait",
        "affiche",
        "rendu",
    ):
        assert noun in IMAGE_CLAIM_NOUNS
    assert IMAGE_CLAIM_NOUNS in loop_mod._CLAIM_NOUN


# --- M1 : la RÉPONSE est filtrée elle aussi, pas seulement le brief ----------
# Le prompt interdit au générateur de nier une image ET d'en annoncer une, les
# deux inconditionnellement : sur un échec, le seul rempart est le backend.

_FAILED_ATTEMPT = {
    "ok": False,
    "image_published": False,
    "result": "[generate_image: SERVICE_UNAVAILABLE] moteur coupé",
    "latency_ms": 3,
}


def test_reponse_fabriquee_remplacee_par_la_limite_enregistree():
    status = loop_mod._record_image_status(None, _FAILED_ATTEMPT)
    # Toute la réponse est une promesse : il ne reste rien, donc le backend
    # écrit la limite LUI-MÊME (jamais une phrase de modèle).
    assert loop_mod._without_unsupported_image_claims(
        "Voici l'image que j'ai créée.", status
    ) == loop_mod._recorded_image_limit(status)
    assert "n’est pas disponible" in loop_mod._recorded_image_limit(status)


def test_reponse_fabriquee_ne_detruit_pas_le_reste():
    status = loop_mod._record_image_status(None, _FAILED_ATTEMPT)
    réponse = "Voici l'image que j'ai créée. Le papillon a des ailes bleues."
    corrigée = loop_mod._without_unsupported_image_claims(réponse, status)
    assert corrigée == "Le papillon a des ailes bleues."
    assert not loop_mod._IMAGE_SUCCESS_CLAIM_RE.search(corrigée)


def test_reponse_sans_promesse_passe_telle_quelle_octet_pour_octet():
    # Law 3 : une réponse qui ne ment pas ne doit subir AUCUNE normalisation.
    status = loop_mod._record_image_status(None, _FAILED_ATTEMPT)
    propre = "Je n'ai pas pu créer l'image : le moteur est indisponible.\n\nTu peux décrire la scène ?"
    assert loop_mod._without_unsupported_image_claims(propre, status) is propre
    # Et de même quand une image A bien été publiée : le garde ne s'active pas.
    publié = loop_mod._record_image_status(
        None, {"ok": True, "image_published": True, "artifact_id": "art_ok"}
    )
    assert (
        loop_mod._without_unsupported_image_claims(
            "Voici l'image que j'ai créée.", publié
        )
        == "Voici l'image que j'ai créée."
    )


# --- Tête nominale au passé : « Image générée selon… » sans image ----------
# D'OÙ CET INVARIANT : le générateur ouvrait sa réponse par un titre nominal au
# passé alors qu'aucune image n'avait été publiée (q-r.md). Le garde doit retirer
# cette phrase, sans confondre l'adjectif « L'image générée précédemment… ».


def test_tete_nominale_au_passe_retiree_quand_aucune_image():
    status = loop_mod._record_image_status(None, _FAILED_ATTEMPT)
    réponse = (
        "Image générée selon la synthèse d’espace : aucune orientation. "
        "Deux fichiers existent."
    )
    corrigée = loop_mod._without_unsupported_image_claims(réponse, status)
    assert "Image générée" not in corrigée
    assert corrigée == "Deux fichiers existent."


def test_brief_sans_tentative_image_repond_au_meme_garde():
    """Aucune tentative image : le brief ne peut pas affirmer une image publiée.

    Défaut q-r.md : `_with_image_status` retournait le brief inchangé quand
    `image_status` était absent, donc « Image générée selon la synthèse d'espace »
    survivait alors qu'aucun pixel n'avait été publié.
    """
    brief_data = {
        "brief": (
            "Image générée selon la synthèse d’espace : aucune orientation. "
            "Deux fichiers existent."
        )
    }
    merged = loop_mod._with_image_status(brief_data, None)
    assert merged is not None
    assert "Image générée" not in merged["brief"]
    assert merged["brief"] == "Deux fichiers existent."

    intact = {"brief": "Deux fichiers existent dans l’espace."}
    assert loop_mod._with_image_status(intact, None) == intact


def test_tete_nominale_adjectivale_survit_intacte():
    status = loop_mod._record_image_status(None, _FAILED_ATTEMPT)
    phrase = "L'image générée précédemment reste disponible dans la galerie."
    assert loop_mod._without_unsupported_image_claims(phrase, status) == phrase


def test_tete_nominale_au_passe_survit_si_image_publiee():
    publié = loop_mod._record_image_status(
        None, {"ok": True, "image_published": True, "artifact_id": "art_ok"}
    )
    phrase = "Image générée selon la demande : palette chaude et typographie serif."
    assert loop_mod._without_unsupported_image_claims(phrase, publié) == phrase


# --- M5 : une image supprimée ne fige plus le prompt pour toujours ----------

PAPILLON = "papillon photo réaliste en macro"


def _publish_new(sessions, artifact_id: str, conversation_id: str) -> None:
    """Publication dans SA propre session (comme le service), via le helper commun."""
    session = sessions()
    try:
        _publish(session, artifact_id, conversation_id)
    finally:
        session.close()


def _soft_delete(sessions, artifact_id: str) -> None:
    """Ce que fait ``image_delete`` (Rust) : soft-delete de l'artefact ET de ses liens."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    session = sessions()
    try:
        session.query(Artifact).filter(
            Artifact.id == artifact_id
        ).one().deleted_at = now
        for link in (
            session.query(ArtifactLink)
            .filter(ArtifactLink.artifact_id == artifact_id)
            .all()
        ):
            link.removed_at = now
        session.commit()
    finally:
        session.close()


@pytest.fixture()
def chat_db(tmp_path, monkeypatch):
    """Le chemin d'exécution RÉEL : SessionLocal de production, base temporaire.

    Un test qui monkeypatche `_execute_generate_image` lui-même ne prouve rien de
    son cœur de décision ; celui-ci le traverse avec la vraie DB et la vraie
    lecture/écriture d'effets.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'execute.db'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", sessions)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(loop_mod, "emit", lambda event, payload, **kwargs: True)
    return sessions


def _diffusion_double(sessions, artifact_ids):
    """image_service.generate simulé : publie pour de vrai, via before_commit."""

    def diffusion(*args, **kwargs):
        artifact_id = artifact_ids.pop(0)
        before_commit = kwargs.get("before_commit")
        session = sessions()
        try:
            _publish_new(sessions, artifact_id, str(kwargs.get("conv_id") or "conv"))
            if before_commit is not None:
                before_commit(session, artifact_id)
            session.commit()
        finally:
            session.close()
        return {
            "artifact_id": artifact_id,
            "stats": {
                "width": 1024,
                "height": 1024,
                "seed": 7,
                "duration_s": 12.0,
                "backend": "stub",
            },
        }

    return diffusion


def _compte(diffusion, journal: list[str]):
    """Enveloppe le service pour compter les VRAIES diffusions lancées."""

    def counted(*args, **kwargs):
        journal.append(str(kwargs.get("idempotency_key") or ""))
        return diffusion(*args, **kwargs)

    return counted


def _generate_chat(conv_id: str, request_id: str, prompt: str = PAPILLON) -> dict:
    return loop_mod._execute_generate_image(
        {
            "id": f"call_{request_id}",
            "args": {"prompt": prompt, "aspect_ratio": "1:1", "reference_ids": []},
        },
        manager=None,
        conv_id=conv_id,
        request_id=request_id,
        run_id=request_id,
    )


def test_promis_identique_apres_suppression_refait_une_diffusion(chat_db, monkeypatch):
    """image_delete supprime l'artefact ET ses liens ; l'effet reste « published ».

    Sans traitement, la demande retombait sur la branche de relink, levait
    REPLAY_UNUSABLE et rendait ok=False : l'utilisateur était bloqué pour
    toujours, dans TOUTES les conversations, avec la vraie raison visible
    uniquement dans la trace d'outil.
    """
    from backend.db.models import ImageEffect

    artifact_ids = ["art_1", "art_2"]
    diffusions: list[str] = []
    base = _diffusion_double(chat_db, artifact_ids)
    monkeypatch.setattr(
        "backend.services.image_service.generate", _compte(base, diffusions)
    )

    first = _generate_chat("conv_a", "req_1")
    assert first["ok"] is True and first["artifact_id"] == "art_1"
    _soft_delete(chat_db, "art_1")

    second = _generate_chat("conv_b", "req_2")

    assert len(diffusions) == 2, "une image supprimée doit être REDIFFUSÉE, pas refusée"
    assert second["ok"] is True
    assert second["image_published"] is True
    assert second["artifact_id"] == "art_2"
    assert "REPLAY_UNUSABLE" not in second["result"]
    # Le message dit CE QUI S'EST PASSÉ : ni le refus opaque d'avant, ni le
    # libellé générique « n'a pas pu être terminée ».
    assert "supprimée" in second["result"], second["result"]
    assert loop_mod._IMAGE_STATUS_DEFAULT_LIMIT not in second["result"]

    session = chat_db()
    try:
        rows = session.query(ImageEffect).order_by(ImageEffect.created_at.asc()).all()
        assert [(row.status, row.output_artifact_id) for row in rows] == [
            ("failed", "art_1"),
            ("published", "art_2"),
        ], "l'ancien effet devient un échec et un effet NEUF porte la nouvelle image"
        assert rows[0].id != rows[1].id
        assert (
            not session.query(ArtifactLink)
            .filter(
                ArtifactLink.artifact_id == "art_1", ArtifactLink.removed_at.is_(None)
            )
            .first()
        )
    finally:
        session.close()


def test_echec_de_regeneration_nomme_la_suppression(chat_db, monkeypatch):
    # Si la nouvelle diffusion échoue, la limite doit nommer la suppression :
    # le libellé générique « n'a pas pu être terminée » ferait passer une
    # décision de registre pour une panne de moteur.
    base = _diffusion_double(chat_db, ["art_1"])
    monkeypatch.setattr("backend.services.image_service.generate", base)
    first = _generate_chat("conv_a", "req_1")
    assert first["ok"] is True
    _soft_delete(chat_db, "art_1")

    def muet(*args, **kwargs):
        raise RuntimeError("[TIMEOUT] worker image muet")

    monkeypatch.setattr("backend.services.image_service.generate", muet)
    second = _generate_chat("conv_b", "req_2")
    assert second["ok"] is False
    assert second["user_limit"] == loop_mod._DELETED_ARTIFACT_USER_LIMIT
    assert second["user_limit"] != loop_mod._IMAGE_STATUS_DEFAULT_LIMIT


# --- Le cœur de décision, contre la VRAIE base (couverture réelle) ----------


def test_coeur_de_decision_chemins_frais_rejeu_et_refus(chat_db, monkeypatch):
    """Les trois branches de la décision, SANS monkeypatcher le cœur lui-même.

    Chaque test du dépôt qui touche generate_image doublait ``_execute_generate_image``
    : la condition même que la revue a fait réécrire n'avait AUCUNE couverture —
    inverser un « == » ne faisait échanger aucun test. Ici la vraie fonction décide,
    sur la vraie base, avec la vraie lecture/écriture d'effets.

    * demande neuve            → diffusion puis publication ;
    * même demande, autre conv → relink SANS nouvelle diffusion ;
    * effet déjà en cours      → refus nommé, jamais un succès fabriqué.
    """
    from backend.db.models import ImageEffect
    from backend.services.effect_ledger import (
        claim_image_effect,
        image_request_fingerprint,
        mark_running,
    )

    diffusions: list[str] = []
    base = _diffusion_double(chat_db, ["art_fraiche"])
    monkeypatch.setattr(
        "backend.services.image_service.generate", _compte(base, diffusions)
    )

    fresh = _generate_chat("conv_a", "req_fresh")
    assert fresh["ok"] is True and fresh["artifact_id"] == "art_fraiche"
    assert len(diffusions) == 1

    relink = _generate_chat("conv_b", "req_relink")
    assert relink["ok"] is True
    assert relink["artifact_id"] == "art_fraiche", (
        "l'artefact d'une autre conversation est rendu"
    )
    assert len(diffusions) == 1, "un rejeu ne rediffuse JAMAIS"
    assert "sans nouvelle diffusion" in relink["result"]
    session = chat_db()
    try:
        link = (
            session.query(ArtifactLink)
            .filter(
                ArtifactLink.artifact_id == "art_fraiche",
                ArtifactLink.scope_id == "conv_b",
            )
            .first()
        )
        assert link is not None and link.role == "generated", (
            "l'artefact doit être visible de conv_b"
        )
    finally:
        session.close()

    # Un effet « running » appartient à un job en cours : le refuser est un
    # verdict, pas une seconde diffusion sur le même effet.
    fingerprint = image_request_fingerprint(
        "pont en feu",
        "1:1",
        [],
        None,
    )
    session = chat_db()
    try:
        busy = claim_image_effect(
            session,
            run_id="req_busy",
            logical_action_id="image:busy",
            output_index=0,
            fingerprint=fingerprint,
        )
        mark_running(session, busy)
    finally:
        session.close()
    refused = loop_mod._execute_generate_image(
        {
            "id": "call_busy",
            "args": {
                "prompt": "pont en feu",
                "aspect_ratio": "1:1",
                "reference_ids": [],
            },
        },
        manager=None,
        conv_id="conv_c",
        request_id="req_refus",
        run_id="req_refus",
    )
    assert refused["ok"] is False
    assert "EFFECT_RUNNING" in refused["result"]
    assert "artifact_id" not in refused
    assert len(diffusions) == 1, "un refus ne lance aucune diffusion"
    session = chat_db()
    try:
        row = (
            session.query(ImageEffect)
            .filter(ImageEffect.fingerprint == fingerprint)
            .one()
        )
        assert row.status == "running", (
            "un refus ne réécrit jamais l'état d'un effet en cours"
        )
    finally:
        session.close()

    # Un effet dont la diffusion a échoué doit ATTEINDRE UN ÉTAT TERMINAL.
    # Mesuré en E2E : `[SERVICE_UNAVAILABLE]` laissait la ligne en `uncertain`, donc
    # l'empreinte restait dans l'index unique et la demande identique était
    # refusée `EFFECT_UNCERTAIN` POUR TOUJOURS, alors que rien n'avait été produit.
    def muet(*args, **kwargs):
        raise RuntimeError("[TIMEOUT] worker image muet")

    monkeypatch.setattr("backend.services.image_service.generate", muet)
    crashed = _generate_chat("conv_d", "req_crash", prompt="flamant rose dans un lac")
    assert crashed["ok"] is False
    session = chat_db()
    try:
        row = (
            session.query(ImageEffect)
            .filter(
                ImageEffect.fingerprint
                == (
                    image_request_fingerprint(
                        "flamant rose dans un lac",
                        "1:1",
                        [],
                        None,
                    )
                )
            )
            .one()
        )
        assert row.status == "failed", (
            "un effet abandonné doit être terminal, jamais incertain (empreinte bloquée)"
        )
    finally:
        session.close()
    # Le même run revoit l'effet échoué et le nomme, sans relancer de diffusion.
    relaunched = _generate_chat(
        "conv_d", "req_crash", prompt="flamant rose dans un lac"
    )
    assert relaunched["ok"] is False
    assert relaunched["result"] == "[EFFECT_FAILED]", relaunched["result"]
    # Un run NEUF, lui, peut réclamer l'empreinte libérée : l'utilisateur n'est
    # pas bloqué sur ce prompt.
    session = chat_db()
    try:
        assert (
            session.query(ImageEffect)
            .filter(
                ImageEffect.fingerprint
                == image_request_fingerprint(
                    "flamant rose dans un lac",
                    "1:1",
                    [],
                    None,
                )
            )
            .count()
            == 1
        )
        fresh = claim_image_effect(
            session,
            run_id="req_neuf",
            logical_action_id="image:flamant",
            output_index=0,
            fingerprint=image_request_fingerprint(
                "flamant rose dans un lac",
                "1:1",
                [],
                None,
            ),
        )
        assert fresh.created is True and fresh.status == "claimed", (
            "l'empreinte libérée doit laisser un run neuf créer un effet neuf"
        )
    finally:
        session.close()


def test_echec_moteur_laisse_un_etat_terminal_et_un_tag_unique(chat_db, monkeypatch):
    """Défaut 4 mesuré : `uncertain` figé + `[generate_image: [OUT_OF_MEMORY] …]`.

    Les deux sont visibles de l'utilisateur : l'un bloque la prochaine tentative,
    l'autre noie le code utile dans deux accolades.
    """
    from backend.db.models import ImageEffect

    def indisponible(*args, **kwargs):
        raise RuntimeError("[OUT_OF_MEMORY] VRAM libre insuffisante : 5.79 / 9.0 Go.")

    monkeypatch.setattr("backend.services.image_service.generate", indisponible)
    first = _generate_chat("conv_a", "req_1")
    assert first["ok"] is False
    assert (
        first["result"] == "[OUT_OF_MEMORY] VRAM libre insuffisante : 5.79 / 9.0 Go."
    ), f"un seul niveau de tag d'erreur, jamais imbriqué : {first['result']}"
    assert "generate_image:" not in first["result"]
    session = chat_db()
    try:
        row = session.query(ImageEffect).one()
        assert row.status == "failed", "l'effet ne doit pas rester en `uncertain`"
        assert "OUT_OF_MEMORY" in (row.error or ""), "la vraie cause reste tracée"
    finally:
        session.close()


# --- Fix 2a : toute tentative image laisse un statut -------------------------


def test_failed_attempt_still_yields_a_status():
    status = loop_mod._record_image_status(
        None, {"ok": False, "result": "[EFFECT_PUBLISHED]"}
    )
    attempts = loop_mod._image_status_attempts(status)
    assert len(attempts) == 1, "une tentative, même refusée, doit laisser une trace"
    assert status["published"] is False
    assert attempts[0]["user_limit"], "un échec doit dire pourquoi"


def test_link_helper_rejects_incomplete_input(db):
    assert svc.link_artifact_to_conversation(db, "", "conv_b") is False
    assert svc.link_artifact_to_conversation(db, "art_x", None) is False
