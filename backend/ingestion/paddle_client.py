# ingestion/paddle_client.py — Client singleton du worker PaddleOCR-VL
#
# OÙ VA QUOI : spawn du worker, handshake "ready", requêtes JSONL verrouillées
# avec timeout, redémarrage transparent sur crash.
#
# POURQUOI LE PYTHON COURANT ET NON UN VENV : un venv était prévu parce qu'une
# version de transformers particulière semblait nécessaire. Elle ne l'était pas.
# Les deux modèles ont une implémentation native dans la version installée ; le
# code distant embarqué dans les dépôts, lui, est écrit pour une fenêtre étroite
# de versions et produit une sortie illisible. `trust_remote_code=False` dans le
# worker suffit. Un venv ici ajouterait un point de rupture de plus sans
# bénéfice, et il était de surcroît placé dans /tmp, donc effacé au reboot.
#
# POURQUOI FAIL LOUD : modèle absent → RuntimeError explicite (jamais de magie
# silencieuse) ; l'appelant (gate.py) décide du repli (caption VL-3B).
#
# POURQUOI UN `liberer_pour_diffusion()` QUI ARRÊTE LE WORKER : le worker est un
# process CUDA RÉSIDENT. Il tient ~1,9 Go de VRAM entre deux requêtes, sans rien
# faire. Le pipeline image en réclame 3,06 Go — pic MESURÉ le 05/10/2026 à 1024²
# sur ce code (`flux_spike.PIC_VRAM_MESURE_GB`), contre les « ~7,9 Go » qu'on lui
# imputait depuis `models/README.md` et qu'aucune mesure de ce dépôt n'attestait.
# Les deux ensemble font 4,96 Go : sous les 8 Go de la Bible §1.4, donc la
# violation cumulée qu'on citait ici n'est plus établie par les chiffres — et c'est
# justement pour ça que la fonction reste. Un modèle lourd à la fois est une RÈGLE
# de séquence, pas un calcul : faire cohabiter tient parce que la somme passe
# aujourd'hui, sur cette carte, avec cette version. Le déchargement du LLM ne
# change rien à cela : cet argent n'est pas à lui. D'où cette fonction, appelée par
# le service image AVANT de sonder la marge — sonder pendant que l'occupant qu'on
# va évincer est encore là, c'est faire dépendre l'admission d'un état que le run
# suivant ne verra pas.
#
# POURQUOI `SIGTERM` D'ABORD, ET JAMAIS `kill` EN PREMIER : le worker tient un
# contexte CUDA ; un `kill` laisse au pilote le soin de le reprendre, et il arrive
# que la reprise diffère, ce qui retarde la VRAM pour le process suivant.
# `terminate()` laisse le worker relâcher son contexte. `kill` ne subsiste qu'en
# escalade, et l'escalade est REPORTÉE au journal — parce qu'une libération qui
# doit tuer est une libération qui a échoué, et qu'un échec muet est pire qu'un
# échec bruyant.
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")

WORKER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "paddle_worker.py"
)
FIRST_LOAD_TIMEOUT_S = 300
# 120 s suffisaient pour une page simple. Une page A4 à 600 dpi demande à elle
# seule plusieurs minutes ; un timeout trop court tuait des OCR parfaitement
# valides en les faisant passer pour des pannes.
INFER_TIMEOUT_S = 600
# Délai accordé au worker pour rendre sa VRAM après SIGTERM, puis après SIGKILL.
ARRET_GRACIEUX_S = 10.0
ARRET_FORCE_S = 5.0


_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_req_id = 0


class WorkerOccupe(RuntimeError):
    """Le worker OCR est en train de travailler : on ne le tue pas.

    Distingué de `LiberationImpossible` : ici rien n'est cassé, la machine est
    simplement occupée par une ingestion. Le remède est d'attendre, pas
    d'intervenir.
    """


class LiberationImpossible(RuntimeError):
    """Le worker n'a pas pu être arrêté, donc sa VRAM n'a pas été rendue."""


class ModeleIndisponible(RuntimeError):
    """Le modèle OCR n'a pas pu être lancé, chargé, ou n'a pas répondu à temps.

    Ce n'est PAS une erreur de contenu : le fichier n'est peut-être pas fautif.
    L'appelant doit laisser la dette rejouable (`depose`) au lieu d'écrire un
    `echec` terminal. Quatre familles sont couvertes ici — spawn, chargement du
    worker, timeout d'inférence, et panne du worker en cours d'échange (écriture
    impossible, crash, stdout fermé) — parce que ce sont celles dont le remède
    est « réessayer plus tard », pas « corriger le fichier ».
    """


@dataclass(frozen=True)
class RapportLiberation:
    """Ce que la libération a réellement fait. Journalisable, donc vérifiable.

    `arret_propre` distingue le chemin normal (SIGTERM) de l'escalade : on peut
    ainsi distinguer « la VRAM a été rendue proprement » de « elle a été rendue,
    mais de force », ce qui n'a pas les mêmes suites.
    """

    worker_etait_vivant: bool
    arret_propre: bool
    secondes: float

    def __str__(self) -> str:
        if not self.worker_etait_vivant:
            return "worker OCR déjà arrêté : rien à libérer"
        voie = "SIGTERM" if self.arret_propre else "SIGKILL après escalade"
        return (
            f"worker OCR arrêté ({voie}) en {self.secondes:.1f} s : sa VRAM est rendue"
        )


def _arreter(proc: subprocess.Popen, *, rapport: bool) -> tuple[bool, float]:
    """Arrête un worker : SIGTERM, attente, puis SIGKILL si besoin.

    Renvoie `(arrêt_propre, secondes)`. Ne lève que si le process SURVIT au
    SIGKILL, auquel cas sa mémoire n'est pas rendue et l'appelant doit le dire.
    """
    depart = time.monotonic()
    if proc.poll() is not None:
        return True, 0.0
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=ARRET_GRACIEUX_S)
        return True, time.monotonic() - depart
    except subprocess.TimeoutExpired:
        pass
    if rapport:
        print(
            f"[paddle] worker OCR : SIGTERM ignoré après {ARRET_GRACIEUX_S:.0f} s, escalade SIGKILL",
            flush=True,
        )
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=ARRET_FORCE_S)
    except subprocess.TimeoutExpired as exc:
        raise LiberationImpossible(
            f"worker PaddleOCR-VL (pid {proc.pid}) survit à SIGKILL : sa VRAM (~1,9 Go) "
            f"n'est pas rendue, et la génération d'image ne peut pas être déclarée "
            f"conforme au budget. Arrêtez le process à la main puis réessayez."
        ) from exc
    return False, time.monotonic() - depart


def _worker_vivant() -> subprocess.Popen | None:
    with _lock:
        proc = _proc
        if proc is not None and proc.poll() is not None:
            return None
        return proc


def liberer_pour_diffusion(*, timeout_s: float = 5.0) -> RapportLiberation:
    """Rend la VRAM du worker OCR, et ne le fait QUE s'il ne travaille pas.

    QUI libère : ce service (image), pas l'ingestion. L'ingestion ne peut pas
    savoir qu'une image va démarrer, et arrêter le worker sur sa demande
    laisserait l'ingestion sans état reprenable.

    LE CAS DIFFICILE — une ingestion PDF EN COURS pendant la diffusion. Le worker
    est partagé, et le tuer en plein OCR perdrait des pages déjà payées (une page
    A4 à 600 dpi demande plusieurs minutes). On choisit donc, et on l'écrit :

        **L'INGESTION L'EMPORTE, et la génération est sérialisée derrière elle.**

    Trois raisons, dans l'ordre :
      1. le `ResourceCoordinator` rend l'exclusion mutuelle EXPLICITE — `parse_png`
         et `detect_regions` prennent le bail « ocr », `image_service.generate`
         prend le bail « image ». L'ordre d'arrivée est donc déjà géré, et
         l'ingestion ne peut pas être interrompue même par un bug d'ordonnancement ;
      2. le coût n'est pas symétrique : une image se régénère (idempotence déjà
         implémentée côté service), une ingestion en cours ne se reprend pas ;
      3. tuer un OCR en vol produirait un `RuntimeError` en aval, absorbé par un
         repli en « page sans texte » : le pire des verdicts, un document
         silencieusement amputé.

    DONC : si le bail « image » a pu être pris, aucune requête OCR n'est en vol
    (exclusion mutuelle), et l'arrêt est sûr. Si le bail n'a pas pu être pris,
    l'appelant doit REFUSER — c'est `image_service.generate` qui le fait, avec
    un motif qui nomme l'ingestion en cours.

    `timeout_s` borne l'attente du verrou interne du worker. On n'attend pas
    indéfiniment une ingestion qui prend 600 s : au-delà, le refus est plus
    honnête qu'un silence de dix minutes.
    """
    if not _lock.acquire(timeout=max(0.0, timeout_s)):
        raise WorkerOccupe(
            "worker PaddleOCR-VL occupé : une ingestion OCR est en cours (jusqu'à "
            f"{INFER_TIMEOUT_S} s par page). L'ingestion l'emporte — la génération "
            "d'image est refusée plutôt que d'interrompre un OCR à mi-page. "
            "Réessayez quand l'ingestion est terminée."
        )
    global _proc
    try:
        proc = _proc
        if proc is None or proc.poll() is not None:
            _proc = None
            return RapportLiberation(
                worker_etait_vivant=False, arret_propre=True, secondes=0.0
            )
        propre, secondes = _arreter(proc, rapport=True)
        return RapportLiberation(
            worker_etait_vivant=True, arret_propre=propre, secondes=secondes
        )
    finally:
        _proc = None
        _lock.release()


def _spawn() -> subprocess.Popen:
    """Lance le worker et attend son handshake. Lève RuntimeError si python/modèle KO."""
    global _proc
    racine = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    # Le worker importe `backend.config.paths`. Exécuter un script ne met que le
    # dossier du script dans sys.path, pas le répertoire courant : sans ce
    # PYTHONPATH, le worker démarre puis meurt sur un ModuleNotFoundError, ce
    # qui ressemble à tort à un modèle manquant.
    env = dict(os.environ)
    env["PYTHONPATH"] = racine + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, WORKER_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=racine,
            env=env,
        )
    except Exception as e:
        raise ModeleIndisponible(
            f"worker PaddleOCR-VL : python introuvable ({sys.executable}) : {e}"
        )
    assert proc.stdin is not None and proc.stdout is not None  # PIPE demandés ci-dessus
    import select

    deadline = time.time() + FIRST_LOAD_TIMEOUT_S
    while time.time() < deadline:
        r, _, _ = select.select([proc.stdout], [], [], 5.0)
        if not r:
            if proc.poll() is not None:
                raise ModeleIndisponible(
                    "worker PaddleOCR-VL : arrêt pendant le chargement (modèle absent ?)"
                )
            continue
        line = proc.stdout.readline()
        if not line:
            raise ModeleIndisponible("worker PaddleOCR-VL : stdout fermé au démarrage")
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("ready"):
            _proc = proc
            return proc
        raise ModeleIndisponible(
            f"worker PaddleOCR-VL : démarrage refusé : {msg.get('error', '?')}"
        )
    proc.kill()
    raise ModeleIndisponible("worker PaddleOCR-VL : timeout de chargement (>300s)")


def _ensure() -> subprocess.Popen:
    """Retourne un worker vivant (respawn si crash). Appeler sous _lock."""
    global _proc
    if _proc is not None and _proc.poll() is None:
        return _proc
    _proc = None
    return _spawn()


def _avec_lease_ocr(
    action: Callable[[], _T],
    *,
    timeout_s: float = 30.0,
    kind: str = "interactive",
    owner: str | None = None,
) -> _T:
    """Exécute une requête worker SOUS le bail « ocr » du coordinateur.

    Les DEUX points d'entrée passent par ici — `parse_png` ET `detect_regions`.
    Avant, seul `parse_png` prenait le bail : une découpe de mise en page pouvait
    donc démarrer APRÈS l'acquisition du bail « image », faire respawner le
    worker (1,9 Go) en pleine diffusion, et réintroduire sans bruit exactement la
    violation cumulée que `liberer_pour_diffusion` vient de réparer. Un bail
    pris par point d'entrée n'est pas une garantie ; c'est le chemin par
    lequel on y entre qui doit être instrumenté, donc c'est ICI qu'il est pris.

    CONTEXTE (Phase 3, tâche 3.4) : `kind` distingue un travail d'ingestion
    (priorité basse, aging) d'un travail interactif (priorité haute). `owner`
    identifie le demandeur : l'ingestion passe son `claim_token` (unique), pour
    que deux extractions ne se heurtent pas en `GPU_REENTRANT_ACQUIRE`. Un
    appelant interactif sans owner garde l'étiquette historique `paddle-ocr`.

    DISTINCTION AVEC LE CLAIM D'INGESTION (Phase 3, tâche 3.2) : ce bail « ocr »
    est celui du coordinateur GPU ; il sérialise l'accès au worker entre TOUS
    les appelants (ingestion ET chat). Il ne doit PAS être confondu avec le
    claim d'ingestion (`backend/ingestion/bail.py`), qui appartient à un
    artefact précis et empêche deux workers d'ingérer le même fichier.
    L'OCR interactif (chat) utilise donc le bail coordinateur mais PAS le claim
    d'ingestion, pour trois raisons localisées ici :
      1. chemins différents — le chat ne traverse pas `lancer_ingestion` ni le
         worker d'admission, il appelle directement `parse_png`/`detect_regions` ;
      2. pas de dette d'ingestion — une lecture interactive n'ouvre ni reprise,
         ni observation d'extraction, ni compteur d'essais ;
      3. éviter de marquer un artefact utilisateur comme « en cours d'ingestion » :
         ce serait un mensonge d'état qui fausserait `etat_admission` et le
         balayage de reprise.
    Si une intégration interactive devait un jour acquérir le claim d'ingestion,
    elle devrait d'abord rejoindre la file d'admission — pas le contourner.
    """
    if kind == "ingestion":
        from backend.services import activity

        if activity.tour_actif():
            from backend.inference.resource_coordinator import ResourceBusy

            raise ResourceBusy(
                "RESOURCE_BUSY:ocr (tour interactif actif — OCR d'ingestion différé)"
            )

    from backend.inference.resource_coordinator import get_resource_coordinator

    coordinator = get_resource_coordinator()
    lease = coordinator.acquire(
        "ocr", owner or "paddle-ocr", timeout_s=timeout_s, kind=kind
    )
    try:
        return action()
    finally:
        coordinator.release(lease)


def parse_png(
    png_bytes: bytes,
    *,
    task: str = "parse",
    max_tokens: int = 8192,
    timeout_s: int = INFER_TIMEOUT_S,
    kind: str = "interactive",
    owner: str | None = None,
) -> str:
    return _avec_lease_ocr(
        lambda: _parse_png_uncoordinated(
            png_bytes, task=task, max_tokens=max_tokens, timeout_s=timeout_s
        ),
        timeout_s=timeout_s,
        kind=kind,
        owner=owner,
    )


def _parse_png_uncoordinated(
    png_bytes: bytes,
    *,
    task: str = "parse",
    max_tokens: int = 8192,
    timeout_s: int = INFER_TIMEOUT_S,
) -> str:
    """Envoie une image au worker et retourne le texte. Lève RuntimeError sinon.

    `max_tokens` vaut 8192 par défaut, la valeur de production retenue par
    PaddleX. La valeur de 1024 de l'exemple officiel ne suffit qu'a la
    reconnaissance d'un element isole ; sur une page entiere elle tronque le
    document en cours de route, sans erreur visible.

    Le worker et la liste des taches sont les seules sources de verite sur ce
    qui est accepte : dupliquer cette liste ici l'avait deja desynchronisee une
    fois, lessix taches officielles etant alors refusees.
    """
    if not png_bytes:
        raise ValueError("image vide")
    reponse = _envoyer_locked(
        {
            "op": task,
            "image_b64": base64.b64encode(png_bytes).decode(),
            "max_tokens": max_tokens,
        },
        timeout_s=timeout_s,
    )
    return reponse.get("text", "")


def _reset_locked() -> None:
    """Abandonne le worker courant après un crash (sous `_lock`).

    Même chemin que la libération volontaire — `terminate()` d'abord — parce
    qu'un worker qui a crashé ne l'est pas forcément : il peut être figé sur un
    accès D2H, et dans ce cas `kill` seul peut laisser sa VRAM au pilote pour
    une durée indéterminée. Si même `kill` ne suffit pas, on le DIT (le log)
    au lieu de rendre la main en silence : un worker mort qui garderait 1,9 Go
    ferait échouer le budget de l'image suivante sans explication.
    """
    global _proc
    proc = _proc
    _proc = None
    if proc is None:
        return
    try:
        _arreter(proc, rapport=True)
    except LiberationImpossible as exc:
        print(f"[paddle] {exc}", flush=True)


def pil_to_png_bytes(image) -> bytes:
    """Convertit une PIL.Image en PNG bytes. Pur."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def check_available() -> tuple[bool, str]:
    """Sonde python + modèle sans charger (rapide). Retourne (ok, détail)."""
    from backend.config.paths import get_document_model_dir

    if sys.version_info < (3, 10):
        return (
            False,
            f"python >= 3.10 requis ({sys.version_info.major}.{sys.version_info.minor})",
        )
    if not os.path.isfile(WORKER_SCRIPT):
        return False, f"worker introuvable : {WORKER_SCRIPT}"
    try:
        ocr = get_document_model_dir("PaddleOCR-VL-1.6")
    except Exception as exc:
        return False, f"résolution du modèle impossible : {exc}"
    if not ocr.is_dir():
        return False, f"modèle absent : {ocr}"
    try:
        layout = get_document_model_dir("PP-DocLayoutV3")
    except Exception:
        layout = None
    detail = f"modèle OCR présent : {ocr}"
    if layout is not None and layout.is_dir():
        detail += f" | layout : {layout}"
    else:
        detail += " | layout absent (crops indisponibles, OCR de page intact)"
    return True, detail


def detect_regions(
    png_bytes: bytes,
    *,
    threshold: float = 0.3,
    timeout_s: int = INFER_TIMEOUT_S,
    kind: str = "interactive",
    owner: str | None = None,
) -> list[dict]:
    """Régions de mise en page d'une page, dans l'ordre de lecture.

    Câblé sur la chaîne documentaire : porte image (après la qualification VL,
    qui reste le seul routeur) et pages scannées de la chaîne PDF — les régions
    alimentent le routage OCR puis, lot suivant 4.4, le recadrage. Lève
    RuntimeError si le modèle de layout est absent : on préfère une erreur
    explicite à des crops vides qui passeraient pour une page sans image.

    `kind` et `owner` suivent le même contrat que `parse_png` (Phase 3, 3.4).
    """
    if not png_bytes:
        raise ValueError("image vide")

    def _demander() -> dict:
        return _envoyer_locked(
            {
                "op": "layout",
                "image_b64": base64.b64encode(png_bytes).decode(),
                "threshold": threshold,
            },
            timeout_s=timeout_s,
        )

    return _avec_lease_ocr(_demander, timeout_s=timeout_s, kind=kind, owner=owner).get(
        "regions", []
    )


def _envoyer_locked(charge: dict, *, timeout_s: int):
    """Envoie une requête au worker sous verrou et attend sa réponse."""
    global _req_id
    with _lock:
        proc = _ensure()
        assert proc.stdin is not None and proc.stdout is not None  # invariant _spawn
        _req_id += 1
        rid = _req_id
        req = json.dumps({"id": rid, **charge}, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(req)
            proc.stdin.flush()
        except Exception as e:
            _reset_locked()
            # L'entrée du worker a disparu : ce n'est pas le fichier qui est
            # fautif, c'est le worker. La dette reste rejouable.
            raise ModeleIndisponible(f"worker PaddleOCR-VL : écriture impossible : {e}")
        import select

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r, _, _ = select.select([proc.stdout], [], [], 5.0)
            if not r:
                if proc.poll() is not None:
                    _reset_locked()
                    raise ModeleIndisponible(
                        "worker PaddleOCR-VL : crash pendant l'inférence"
                    )
                continue
            line = proc.stdout.readline()
            if not line:
                _reset_locked()
                raise ModeleIndisponible(
                    "worker PaddleOCR-VL : stdout fermé pendant l'inférence"
                )
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") != rid:
                continue
            if msg.get("ok"):
                return msg
            raise RuntimeError(f"PaddleOCR-VL : {msg.get('error', 'échec inconnu')}")
        raise ModeleIndisponible("worker PaddleOCR-VL : timeout d'inférence")
