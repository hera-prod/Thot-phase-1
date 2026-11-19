"""Coordinateur d'accès aux ressources lourdes (GPU, OCR, modèle).

PHASE 3, TÂCHE 3.4 — file de waiters équitable.

LE DÉFAUT QUE CE MODULE CORRIGE : un mutex global sans file ni priorité. Un travail
d'ingestion pouvait monopoliser le GPU et affamer indéfiniment le chat interactif,
ou inversement un chat pouvait passer devant une ingestion déjà en attente depuis
longtemps. Le coordinateur doit :

- servir d'abord les travaux INTERACTIFS (chat, image, OCR interactif) ;
- faire vieillir les travaux d'INGESTION : après 60 s d'attente, un travail
  d'ingestion devient prioritaire (aging) ;
- borner l'attente du chat à 120 s, puis lever `ResourceBusy` RETRYABLE ;
- borner l'attente d'une INGESTION à 10 s : un travail de fond cède vite à
  l'interactif au lieu de tenir la file ; l'appelant le replanifie ;
- ne jamais admettre deux détenteurs : le bail n'a pas de TTL — un TTL sur un
  `threading.Lock` admettrait deux détenteurs. `finally` est garanti, l'état est
  exposé, le coordinateur est réinitialisé au redémarrage.

LE CONTRAT D'ORDRE (tâche 3.5) : le claim d'ingestion est acquis et committé
AVANT l'appel à `acquire`. Aucune transaction SQLite ne doit être ouverte pendant
l'attente (jusqu'à 120 s) : le coordinateur ne touche pas à la base.

RÉENTRANCE : un même `owner_id` qui tente de réacquérir alors qu'il détient déjà
le bail lève `GPUReentrantAcquire` (héritage de `ResourceBusy`). C'est un défaut
de l'appelant, pas une attente.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


class ResourceBusy(RuntimeError):
    """La ressource n'a pas pu être acquise dans le délai imparti.

    RETRYABLE côté chat : ce n'est pas un échec du travail, c'est une contention.
    Le coordinateur n'a pas de TTL de bail ; le timeout est porté par l'appelant.
    """


class GPUReentrantAcquire(ResourceBusy):
    """Le même propriétaire tente de réacquérir une ressource qu'il détient."""


class ResourceLeaseLost(RuntimeError):
    """La libération a été tentée avec un jeton qui n'est plus le détenteur."""


@dataclass(frozen=True)
class ResourceLease:
    token: str
    resource: str
    owner_id: str
    generation: int


# Délai d'aging : un travail d'ingestion qui attend ce temps devient prioritaire.
AGING_SECONDES = 60.0
# Attente maximale du chat interactif avant `ResourceBusy` retryable.
ATTENTE_CHAT_S = 120.0
# Attente maximale d'un travail d'INGESTION avant `ResourceBusy` retryable.
# Un travail de fond cède vite à l'interactif au lieu de tenir la file : au-delà
# de 10 s, l'appelant diffère et replanifie (figures_worker → `_differe`,
# attachment_ingest → `depose`), ce qui rend la ressource au chat.
ATTENTE_INGESTION_S = 10.0

# Priorités : plus grand = servi plus tôt. L'aging promeut une ingestion en attente.
_PRIORITE_INGESTION = 1
_PRIORITE_INTERACTIVE = 2


@dataclass
class _Waiter:
    """Un travail en attente de la ressource. La file est triée par priorité et âge."""

    owner_id: str
    resource: str
    priority: int
    enqueued_at: float
    kind: str


class ResourceCoordinator:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._owner: str | None = None
        self._resource: str | None = None
        self._token: str | None = None
        self._generation = 0
        self._stoppers: dict[str, Callable[[], None]] = {}
        self._waiters: list[_Waiter] = []

    def register_stopper(self, resource: str, stopper: Callable[[], None]) -> None:
        with self._condition:
            self._stoppers[resource] = stopper

    def _priorite_effective(self, waiter: _Waiter) -> int:
        """Priorité avec aging : une ingestion qui attend 60 s devient interactive."""
        if (
            waiter.priority == _PRIORITE_INGESTION
            and time.monotonic() - waiter.enqueued_at >= AGING_SECONDES
        ):
            return _PRIORITE_INTERACTIVE
        return waiter.priority

    def _prochain_waiter(self) -> _Waiter | None:
        """Le waiter à servir : priorité la plus haute, puis le plus ancien.

        L'aging est recalculé à chaque appel, pas figé à l'enfilage : un travail
        qui vieillit pendant l'attente doit passer devant un arrivant frais.
        """
        meilleur: _Waiter | None = None
        meilleure_priorite = -1
        meilleur_age = float("inf")
        for waiter in self._waiters:
            priorite = self._priorite_effective(waiter)
            if priorite > meilleure_priorite or (
                priorite == meilleure_priorite and waiter.enqueued_at < meilleur_age
            ):
                meilleur = waiter
                meilleure_priorite = priorite
                meilleur_age = waiter.enqueued_at
        return meilleur

    def acquire(
        self,
        resource: str,
        owner_id: str,
        timeout_s: float = ATTENTE_CHAT_S,
        *,
        kind: str = "interactive",
    ) -> ResourceLease:
        """Acquiert la ressource en respectant la file et l'aging.

        `kind` vaut "interactive" (chat, image, OCR interactif) ou "ingestion".
        L'attente est bornée à `timeout_s` (120 s par défaut). Au-delà, `ResourceBusy`
        est levée : côté chat, c'est une contention RETRYABLE, pas un échec.
        """
        if not resource or not owner_id:
            raise ValueError("resource et owner_id sont requis")
        kind = str(kind or "interactive").strip().lower()
        priorite = _PRIORITE_INGESTION if kind == "ingestion" else _PRIORITE_INTERACTIVE
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            if self._owner == owner_id:
                raise GPUReentrantAcquire(f"GPU_REENTRANT_ACQUIRE:{owner_id}")
            waiter = _Waiter(
                owner_id=owner_id,
                resource=resource,
                priority=priorite,
                enqueued_at=time.monotonic(),
                kind=kind,
            )
            self._waiters.append(waiter)
            try:
                while True:
                    if self._owner is None and self._prochain_waiter() is waiter:
                        self._waiters.remove(waiter)
                        self._generation += 1
                        self._owner = owner_id
                        self._resource = resource
                        self._token = uuid.uuid4().hex
                        return ResourceLease(
                            self._token, resource, owner_id, self._generation
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._waiters.remove(waiter)
                        self._condition.notify_all()
                        raise ResourceBusy(f"RESOURCE_BUSY:{resource}")
                    self._condition.wait(remaining)
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()

    def release(self, lease: ResourceLease) -> None:
        with self._condition:
            if self._token != lease.token or self._owner != lease.owner_id:
                raise ResourceLeaseLost("RESOURCE_LEASE_LOST")
            self._owner = None
            self._resource = None
            self._token = None
            self._condition.notify_all()

    @contextmanager
    def lease(
        self,
        resource: str,
        owner_id: str,
        timeout_s: float = ATTENTE_CHAT_S,
        *,
        kind: str = "interactive",
    ) -> Iterator[ResourceLease]:
        acquired = self.acquire(resource, owner_id, timeout_s, kind=kind)
        try:
            yield acquired
        finally:
            self.release(acquired)

    def stop_registered(self, resource: str) -> None:
        with self._condition:
            stopper = self._stoppers.get(resource)
        if stopper is not None:
            stopper()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "owner": self._owner,
                "resource": self._resource,
                "generation": self._generation,
                "waiters": len(self._waiters),
                "waiter_details": [
                    {
                        "owner": w.owner_id,
                        "resource": w.resource,
                        "priority": self._priorite_effective(w),
                        "kind": w.kind,
                        "age_s": round(time.monotonic() - w.enqueued_at, 1),
                    }
                    for w in self._waiters
                ],
            }

    def reset(self) -> None:
        """Réinitialise l'état du coordinateur (redémarrage). Aucun détenteur.

        Un bail n'a pas de TTL : si le processus redémarre, l'ancien détenteur
        est mort avec lui. Cette méthode remet l'état à zéro sans attendre.
        """
        with self._condition:
            self._owner = None
            self._resource = None
            self._token = None
            self._generation += 1
            self._waiters.clear()
            self._condition.notify_all()


_GLOBAL_COORDINATOR = ResourceCoordinator()


def get_resource_coordinator() -> ResourceCoordinator:
    return _GLOBAL_COORDINATOR
