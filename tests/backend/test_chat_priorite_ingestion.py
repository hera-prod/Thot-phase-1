"""Le chat interactif ne doit plus échouer à cause d'une ingestion de fond.

Fakes uniquement : aucun modèle n'est chargé, aucune carte GPU n'est touchée.

Les propriétés verrouillées :

- le chat attend 120 s le bail `model`, l'ingestion 10 s ;
- la vision d'ingestion cède AVANT conversion d'image et AVANT `use_model`
  quand un tour interactif est actif ;
- un bail `model` tenu par une ingestion est rendu au chat qui attend ;
- l'extraction mémoire déclare `kind="ingestion"` sur ses deux chemins ;
- le message public d'un `ResourceBusy` ne fuit plus le marqueur interne.
"""

from __future__ import annotations

import io
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend.inference import resource_coordinator as rc
from backend.inference.manager import ModelManager
from backend.ingestion import paddle_client as PC
from backend.ingestion import vision_appel as V
from backend.services import activity
from backend.services import document_extraction_llm as E


@pytest.fixture(autouse=True)
def _etat_propre(monkeypatch):
    monkeypatch.setattr(V, "_couche", None)
    activity.marquer_tour_actif(False)
    yield
    activity.marquer_tour_actif(False)


# ---------------------------------------------------------------------------
# 1-2. Attente du bail selon la nature du travail
# ---------------------------------------------------------------------------


class _FauxCoordinator:
    def __init__(self) -> None:
        self.acquires: list[dict] = []
        self.releases: list[object] = []

    def acquire(self, resource, owner_id, timeout_s, *, kind="interactive"):
        self.acquires.append(
            {
                "resource": resource,
                "owner_id": owner_id,
                "timeout_s": timeout_s,
                "kind": kind,
            }
        )
        return object()

    def release(self, lease) -> None:
        self.releases.append(lease)


def _manager_sans_gpu() -> ModelManager:
    """Un manager dont le chargement réel est remplacé par un faux modèle."""

    @contextmanager
    def _modele_factice(*_args, **_kwargs):
        yield object()

    manager = ModelManager.__new__(ModelManager)
    manager._use_model_uncoordinated = _modele_factice
    return manager


def test_chat_attend_attente_chat(monkeypatch):
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)

    with _manager_sans_gpu().use_model("LFM2.5-8B", kind="interactive"):
        pass

    assert coordinator.acquires[0]["timeout_s"] == rc.ATTENTE_CHAT_S
    assert rc.ATTENTE_CHAT_S == 120.0
    assert coordinator.acquires[0]["kind"] == "interactive"


def test_ingestion_attend_attente_ingestion(monkeypatch):
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)

    with _manager_sans_gpu().use_model("LFM2.5-VL-3B", kind="ingestion"):
        pass

    assert coordinator.acquires[0]["timeout_s"] == rc.ATTENTE_INGESTION_S
    assert rc.ATTENTE_INGESTION_S == 10.0
    assert rc.ATTENTE_INGESTION_S < rc.ATTENTE_CHAT_S
    assert coordinator.acquires[0]["kind"] == "ingestion"


# ---------------------------------------------------------------------------
# 3-4. La vision d'ingestion cède au tour interactif
# ---------------------------------------------------------------------------


class _FauxModeleVision:
    def __init__(self) -> None:
        self.appels: list[dict] = []

    def create_chat_completion(self, **kwargs):
        self.appels.append(kwargs)
        return {"choices": [{"message": {"content": "figure décrite"}}]}


class _FauxManagerVision:
    def __init__(self) -> None:
        self.appels: list[dict] = []
        self.modele = _FauxModeleVision()

    @contextmanager
    def use_model(
        self, name, estimated_tokens=4096, has_image=False, *, kind="interactive"
    ):
        self.appels.append(
            {
                "name": name,
                "estimated_tokens": estimated_tokens,
                "has_image": has_image,
                "kind": kind,
            }
        )
        yield self.modele


def _png() -> bytes:
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(tampon, format="PNG")
    return tampon.getvalue()


def test_vision_ingestion_leve_busy_sans_toucher_au_modele(monkeypatch):
    monkeypatch.setattr(activity, "tour_actif", lambda: True)
    manager = _FauxManagerVision()
    V.enregistrer_couche_appel(V.couche_managere(manager))

    with pytest.raises(rc.ResourceBusy, match="tour interactif actif"):
        V.appeler_vision_generale("décris cette figure", _png())

    assert manager.appels == []
    assert manager.modele.appels == []


def test_vision_ingestion_passe_sans_tour_actif(monkeypatch):
    monkeypatch.setattr(activity, "tour_actif", lambda: False)
    manager = _FauxManagerVision()
    V.enregistrer_couche_appel(V.couche_managere(manager))

    texte = V.appeler_vision_generale("décris cette figure", _png())

    assert texte == "figure décrite"
    assert manager.appels == [
        {
            "name": "LFM2.5-VL-3B",
            "estimated_tokens": 4096,
            "has_image": True,
            "kind": "ingestion",
        }
    ]


# ---------------------------------------------------------------------------
# 5. Un bail d'ingestion est bien rendu au chat qui attend
# ---------------------------------------------------------------------------


def test_bail_ingestion_release_laisse_passer_le_chat():
    coordinator = rc.ResourceCoordinator()
    detenteur_pret = threading.Event()

    def detenir_puis_rendre() -> None:
        lease = coordinator.acquire(
            "model", "model:LFM2.5-VL-3B", timeout_s=1.0, kind="ingestion"
        )
        detenteur_pret.set()
        time.sleep(0.2)
        coordinator.release(lease)

    fil = threading.Thread(target=detenir_puis_rendre)
    fil.start()
    assert detenteur_pret.wait(1.0)

    debut = time.monotonic()
    try:
        lease_chat = coordinator.acquire(
            "model", "model:LFM2.5-8B", timeout_s=2.0, kind="interactive"
        )
    finally:
        attente = time.monotonic() - debut
        fil.join(timeout=2.0)

    try:
        assert not fil.is_alive()
        assert attente >= 0.1
        assert coordinator.snapshot()["owner"] == "model:LFM2.5-8B"
    finally:
        coordinator.release(lease_chat)


# ---------------------------------------------------------------------------
# 6. L'extraction mémoire est un travail d'INGESTION
# ---------------------------------------------------------------------------


class _FauxRuntimeExtraction:
    def __init__(self) -> None:
        self.requetes: list = []

    def complete_chat(self, request):
        self.requetes.append(request)
        return SimpleNamespace(raw={})


class _FauxModeleExtraction:
    def __init__(self) -> None:
        self.appels: list[dict] = []

    def create_chat_completion(self, **kwargs):
        self.appels.append(kwargs)
        return {}


class _FauxManagerExtraction:
    def __init__(self) -> None:
        self.modele = _FauxModeleExtraction()
        self.kinds: list[str] = []

    @contextmanager
    def use_model(self, name, estimated_tokens=0, *, kind="interactive"):
        self.kinds.append(kind)
        yield self.modele


def test_extraction_memoire_declare_ingestion_sur_les_deux_chemins():
    messages = ({"role": "user", "content": "Le serveur utilise SQLite depuis 2024."},)

    runtime = _FauxRuntimeExtraction()
    E._appeler_modele(messages=messages, runtime=runtime, manager=None, timeout_s=1.0)
    assert runtime.requetes[0].kind == "ingestion"

    manager = _FauxManagerExtraction()
    E._appeler_modele(messages=messages, runtime=None, manager=manager, timeout_s=1.0)
    assert manager.kinds == ["ingestion"]


# ---------------------------------------------------------------------------
# 7. Le message public ne fuit plus le marqueur interne
# ---------------------------------------------------------------------------


def test_message_public_resource_busy_est_lisible():
    from backend.main import _message_resource_busy

    message = _message_resource_busy(rc.ResourceBusy("RESOURCE_BUSY:model"))

    assert "RESOURCE_BUSY:" not in message
    assert "occupé" in message
    assert "Réessayez" in message


# ---------------------------------------------------------------------------
# 8-11. L'OCR d'ingestion cède AVANT le bail, l'OCR interactif jamais
# ---------------------------------------------------------------------------


def test_ocr_ingestion_leve_busy_sans_acquire(monkeypatch):
    monkeypatch.setattr(activity, "tour_actif", lambda: True)
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)
    monkeypatch.setattr(
        PC,
        "_parse_png_uncoordinated",
        lambda *_a, **_k: pytest.fail("le worker OCR ne doit pas être appelé"),
    )

    with pytest.raises(rc.ResourceBusy, match="OCR d'ingestion différé"):
        PC.parse_png(_png(), kind="ingestion")

    assert coordinator.acquires == []


def test_ocr_interactif_passe_pendant_son_propre_tour(monkeypatch):
    monkeypatch.setattr(activity, "tour_actif", lambda: True)
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)
    monkeypatch.setattr(
        PC, "_parse_png_uncoordinated", lambda *_a, **_k: "texte interactif"
    )

    assert PC.parse_png(_png(), kind="interactive") == "texte interactif"
    assert coordinator.acquires[0]["kind"] == "interactive"
    assert len(coordinator.releases) == 1


def test_ocr_ingestion_passe_sans_tour_actif(monkeypatch):
    monkeypatch.setattr(activity, "tour_actif", lambda: False)
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)
    monkeypatch.setattr(PC, "_parse_png_uncoordinated", lambda *_a, **_k: "texte")

    assert PC.parse_png(_png(), kind="ingestion") == "texte"
    assert coordinator.acquires[0]["kind"] == "ingestion"
    assert len(coordinator.releases) == 1


def test_detect_regions_ingestion_leve_busy_sans_acquire(monkeypatch):
    monkeypatch.setattr(activity, "tour_actif", lambda: True)
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)
    monkeypatch.setattr(
        PC,
        "_envoyer_locked",
        lambda *_a, **_k: pytest.fail("le worker layout ne doit pas être appelé"),
    )

    with pytest.raises(rc.ResourceBusy, match="OCR d'ingestion différé"):
        PC.detect_regions(_png(), kind="ingestion")

    assert coordinator.acquires == []


# ---------------------------------------------------------------------------
# 12. La génération d'image attend comme le chat, pas 30 s
# ---------------------------------------------------------------------------


class _FauxCoordinatorRefus(_FauxCoordinator):
    def acquire(self, resource, owner_id, timeout_s, *, kind="interactive"):
        self.acquires.append(
            {
                "resource": resource,
                "owner_id": owner_id,
                "timeout_s": timeout_s,
                "kind": kind,
            }
        )
        raise rc.ResourceBusy(f"RESOURCE_BUSY:{resource}")


def test_image_attend_attente_chat(monkeypatch):
    from backend.services import image_service as svc

    coordinator = _FauxCoordinatorRefus()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)
    monkeypatch.setattr(svc, "check_install", lambda: (True, "stub"))

    with pytest.raises(RuntimeError, match="SERVICE_UNAVAILABLE"):
        svc.generate(None, "run-image-attente", "un phare")

    assert coordinator.acquires[0]["timeout_s"] == rc.ATTENTE_CHAT_S
    assert rc.ATTENTE_CHAT_S == 120.0


# ---------------------------------------------------------------------------
# 13-14. Le PDF ne perd pas le refus, et le refus nomme le vrai détenteur
# ---------------------------------------------------------------------------


def test_resource_busy_pdf_remonte_au_lieu_d_etre_avale(tmp_path, monkeypatch):
    """La boucle page-par-page ne doit pas convertir un refus en page illisible.

    Sans cette remontée, un PDF ingéré pendant un tour interactif finirait avec
    des « pages non reconnues » au lieu d'être déposé et replanifié.
    """
    import pymupdf

    from backend.ingestion import attachment_ingest as A

    chemin = tmp_path / "une-page.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(str(chemin))
    document.close()

    def page_refusee(*_a, **_k):
        raise rc.ResourceBusy("RESOURCE_BUSY:ocr (tour interactif actif)")

    monkeypatch.setattr(A, "_extraire_page_pdf", page_refusee)

    with pytest.raises(rc.ResourceBusy, match="tour interactif actif"):
        A._extraire_pdf_page_par_page(str(chemin))


def test_message_refus_reservation_dit_le_vrai_detenteur():
    from backend import main as backend_main

    memoire = activity.try_reserver("memoire", "memoire:test-message")
    assert memoire is not None
    try:
        assert (
            backend_main._message_reservation_refusee()
            == backend_main.MESSAGE_ENTRETIEN_MEMOIRE
        )
        assert "entretien mémoire" in backend_main.MESSAGE_ENTRETIEN_MEMOIRE
    finally:
        activity.liberer(memoire)

    for kind in ("chat", "image"):
        reservation = activity.try_reserver(kind, f"{kind}:test-message")
        assert reservation is not None
        try:
            assert (
                backend_main._message_reservation_refusee()
                == backend_main.MESSAGE_AUTRE_TACHE
            )
            assert "autre tâche" in backend_main.MESSAGE_AUTRE_TACHE
        finally:
            activity.liberer(reservation)


# ---------------------------------------------------------------------------
# 15. La garde d'ingestion traverse gate_image (non-régression)
# ---------------------------------------------------------------------------


def test_gate_image_ingestion_leve_busy_pendant_tour_actif(monkeypatch):
    """gate_image(kind="ingestion") doit céder le GPU au chat actif, comme parse_png."""
    monkeypatch.setattr(activity, "tour_actif", lambda: True)
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)
    monkeypatch.setattr(
        PC,
        "_envoyer_locked",
        lambda *_a, **_k: pytest.fail("le worker layout ne doit pas être appelé"),
    )

    from backend.ingestion import gate as G

    with pytest.raises(rc.ResourceBusy, match="OCR d'ingestion différé"):
        G.gate_image(_png(), qualifier=lambda _: "document", kind="ingestion")

    assert coordinator.acquires == []


def test_gate_image_defaut_reste_interactif(monkeypatch):
    """gate_image() sans kind doit rester interactif : pas de garde, OCR autorisé."""
    monkeypatch.setattr(activity, "tour_actif", lambda: True)
    coordinator = _FauxCoordinator()
    monkeypatch.setattr(rc, "get_resource_coordinator", lambda: coordinator)

    # _envoyer_locked est appelé deux fois (detect_regions puis parse_png).
    # On simule un retour vide pour que l'OCR soit appelé.
    monkeypatch.setattr(
        PC, "_envoyer_locked", lambda *_a, **_k: {"regions": [], "text": ""}
    )

    from backend.ingestion import gate as G

    issue, text = G.gate_image(_png(), qualifier=lambda _: "document")

    assert issue == "document"
    assert text == ""
    # Deux acquisitions interactives : detect_regions puis parse_png.
    assert len(coordinator.acquires) == 2
    assert all(a["kind"] == "interactive" for a in coordinator.acquires)
