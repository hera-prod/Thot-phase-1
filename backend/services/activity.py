# services/activity.py — le drapeau d'occupation d'un tour d'inférence.
#
# Une seule inférence lourde à la fois. La maintenance mémoire ne doit jamais
# charger un modèle (ni lire/écrire durablement) pendant qu'un tour est en cours :
# ce drapeau est la porte que le planificateur consulte avant de travailler.
from __future__ import annotations

import threading
from dataclasses import dataclass

TURN_ACTIVE = threading.Event()


def marquer_tour_actif(actif: bool) -> None:
    """Pose ou retire le drapeau d'un tour en cours."""
    if actif:
        TURN_ACTIVE.set()
    else:
        TURN_ACTIVE.clear()


def tour_actif() -> bool:
    """Un tour d'inférence est-il en cours ?"""
    return TURN_ACTIVE.is_set()


# Le verrou binaire couvre chat, image ET mémoire : un seul travail lourd à la
# fois dans le processus. `TURN_ACTIVE` reste le drapeau historique du chat et
# de l'image ; la mémoire ne le pose jamais, elle n'est pas un tour d'inférence.
_RESERVATION_LOCK = threading.Lock()
_OWNER_LOCK = threading.Lock()
_OWNER: tuple[str, str] | None = None
_GENERATION = 0


@dataclass(frozen=True)
class Reservation:
    kind: str  # "chat" | "image" | "memoire"
    owner_id: str
    generation: int


def try_reserver(kind: str, owner_id: str, timeout: float = 0.0) -> Reservation | None:
    """Réserve atomiquement le travail lourd. timeout=0 = jamais d'attente."""
    global _OWNER, _GENERATION
    if kind not in ("chat", "image", "memoire"):
        raise ValueError(f"[RESERVATION_KIND] {kind}")
    if not _RESERVATION_LOCK.acquire(timeout=max(0.0, float(timeout))):
        return None
    with _OWNER_LOCK:
        _GENERATION += 1
        _OWNER = (kind, owner_id)
        generation = _GENERATION
    if kind in ("chat", "image"):
        TURN_ACTIVE.set()
    return Reservation(kind, owner_id, generation)


def liberer(res: Reservation) -> None:
    """Libère seulement SA réservation. Idempotent."""
    global _OWNER
    with _OWNER_LOCK:
        if _OWNER != (res.kind, res.owner_id) or _GENERATION != res.generation:
            return
        if res.kind in ("chat", "image"):
            TURN_ACTIVE.clear()
        _OWNER = None
    try:
        _RESERVATION_LOCK.release()
    except RuntimeError:
        pass


def etat_activite() -> dict:
    with _OWNER_LOCK:
        owner = _OWNER
    return {
        "kind": owner[0] if owner else None,
        "owner_id": owner[1] if owner else None,
        "tour_actif": TURN_ACTIVE.is_set(),
    }
