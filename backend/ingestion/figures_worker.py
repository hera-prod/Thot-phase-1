"""Worker dédié à la description des figures (étape B1).

ARCHITECTURE — un THREAD DAEMON séparé, réveillé par `threading.Event` :

    ingestion étape A commitée ──reveiller_worker_figures()──┐
                                                             ▼
    boucle : wait(intervalle) / debounce court ──> _traiter_pending()
                                                             │
                        texte prioritaire ? chat actif ? ────┤ (diffère, sans claim)
                        claim atomique d'UNE figure ─────────┤
                        crop PDF → LFM2-VL (kind="ingestion") ┘
                        patch représentation + audit + statut

PRIORITÉ TEXTE D'ABORD : avant chaque lot, la file d'admission d'ingestion
texte et tout artefact `en_cours` font différer le worker. Le chat est encore
plus prioritaire : `tour_actif()` rend la main **sans réclamer**, donc sans
consommer d'essai. Le worker ne prend AUCUN verrou global : les appels modèle
et OCR passent `kind="ingestion"` au coordinateur, qui sert d'abord
l'interactif. Une figure en cours de crop ne peut donc pas bloquer un chat.

UN APPEL VLM À LA FOIS : le claim est unitaire et la boucle traite une figure
avant de réclamer la suivante. Aucun lot multi-images dans un prompt.

ARRÊT : `arreter_worker_figures()` pose le stop ET le réveil, pour ne pas
attendre l'intervalle complet ; le thread est joint cinq secondes.
"""

from __future__ import annotations

import io
import logging
import os
import threading
from typing import Any

from backend.ingestion.erreurs import BailPerdu

log = logging.getLogger(__name__)

_VOIE = "thot-figures-worker"
_stop = threading.Event()
_reveil = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_execution_lock = threading.Lock()

try:
    from backend.config.defaults import FIGURES_LIMITS as _LIMITES

    _INTERVALLE_S = int(_LIMITES.get("sweep_interval_s", 60))
    _DEBOUNCE_S = float(_LIMITES.get("debounce_s", 1.0))
except Exception:  # pragma: no cover
    _INTERVALLE_S = 60
    _DEBOUNCE_S = 1.0


def reveiller_worker_figures() -> None:
    """Signale qu'une figure est en attente, sans jamais bloquer ni lever."""
    try:
        _reveil.set()
    except Exception:  # pragma: no cover — Event.set ne lève pas
        pass


def _ingestion_texte_prioritaire() -> bool:
    """Vrai si du texte attend/est en cours : les figures passent après.

    Deux sources : la file d'admission LOCALE (`en_file`/`en_cours`) et la base
    (`ingest_state='en_cours'`), qui couvre un autre processus ou une reprise.
    Le worker figures n'écrit aucun état d'ingestion texte ; il ne fait que
    constater la dette et rendre la main.
    """
    try:
        from backend.ingestion import attachment_ingest as _ai

        etat = _ai.etat_admission()
        if int(etat.get("en_file", 0) or 0) > 0:
            return True
        if int(etat.get("en_cours", 0) or 0) > 0:
            return True
    except Exception:
        pass

    from backend.db.database import SessionLocal
    from backend.db.models import Artifact

    db = None
    try:
        db = SessionLocal()
        return (
            db.query(Artifact.id).filter(Artifact.ingest_state == "en_cours").first()
            is not None
        )
    except Exception:
        # Une lecture impossible ne doit pas bloquer les figures : le worker
        # consulte aussi la file locale, et le prochain balayage réessaiera.
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _chat_actif() -> bool:
    try:
        from backend.services.activity import tour_actif

        return bool(tour_actif())
    except Exception:
        return False


class _Battement(threading.Thread):
    """Heartbeat 60 s d'une figure réclamée, pendant l'appel VLM.

    Un thread plutôt qu'un simple horodatage avant/après : un crop difficile
    peut dépasser le TTL si le heartbeat n'est renouvelé qu'à la fin. `perdu`
    est posé dès qu'une écriture conditionnelle ne trouve plus le token : le
    worker n'écrit alors aucun résultat.
    """

    def __init__(self, figure) -> None:
        super().__init__(name=f"figures-heartbeat-{figure.figure_id}", daemon=True)
        self.figure = figure
        self.perdu = threading.Event()
        self._arreter = threading.Event()

    def run(self) -> None:
        from backend.db.database import SessionLocal
        from backend.ingestion import figures_etape as _etape

        while not self._arreter.wait(_etape.HEARTBEAT_S):
            db = SessionLocal()
            try:
                if not _etape.battre_coeur_figure(
                    db, self.figure.figure_id, self.figure.claim_token
                ):
                    self.perdu.set()
                    return
            except Exception as exc:  # noqa: BLE001 — le fond ne casse pas le worker
                log.warning(
                    "[figures] heartbeat impossible pour %s : %s: %s",
                    self.figure.figure_id,
                    type(exc).__name__,
                    exc,
                )
            finally:
                try:
                    db.close()
                except Exception:
                    pass

    def arreter(self) -> None:
        self._arreter.set()
        self.join(2)


def _session():
    from backend.db.database import SessionLocal

    return SessionLocal()


def _appliquer_description(
    figure,
    *,
    description: str,
    crop_sha256: str,
) -> None:
    """Patch Markdown + blocs, observation d'audit et statut — UNE transaction.

    L'ordre est : vérifier la représentation courante → patcher (Markdown +
    `blocks_json`) → insérer l'observation → clore la figure. Tout tombe
    ensemble : soit la représentation est enrichie et la figure `done`, soit
    rien n'est écrit. Un placeholder introuvable est une LIMITE, jamais un
    patch deviné.
    """
    from backend.ingestion import figures_etape as _etape
    from backend.ingestion import figures_pdf as _fig
    from backend.ingestion import representations as _reps
    from backend.search import chunks as _chunks
    from backend.search.observations import record_figure_observation

    db = _session()
    try:
        representation = _etape.representation_courante(db, figure.representation_id)
        if representation is None:
            _etape.marquer_superseded(db, figure, commit=True)
            return

        try:
            revision = _reps.patcher_figure(
                db,
                representation,
                figure.page,
                figure.region,
                _fig.bloc_figure(figure.page, figure.region, description),
                commit=False,
            )
        except _reps.RepresentationPerimee:
            db.rollback()
            _etape.marquer_superseded(db, figure, commit=True)
            return
        except _reps.PatchFigureImpossible as exc:
            db.rollback()
            _clore_limite(figure, f"placeholder non remplacé : {exc}")
            return

        # Les fragments déjà indexés portent encore le bloc d'attente. On les
        # patche DANS LA MÊME TRANSACTION que la représentation, mais derrière
        # un savepoint : un échec d'index ne doit pas annuler la description
        # déjà appliquée. La figure devient alors `limite` pour l'index.
        try:
            with db.begin_nested():
                resultat = _chunks.mettre_a_jour_chunks_figures(
                    db,
                    figure.artifact_id,
                    figure.representation_id,
                    [
                        {
                            "figure_id": figure.figure_id,
                            "page": figure.page,
                            "region": figure.region,
                            "description": description,
                        }
                    ],
                    figures_revision=revision,
                )
        except Exception as exc:  # noqa: BLE001 — l'index est une conséquence
            resultat = _chunks.ResultatPatch(
                figures_limite=(
                    _chunks.FigureLimiteIndex(
                        figure.figure_id,
                        figure.page,
                        figure.region,
                        f"patch des fragments impossible : {type(exc).__name__}: {exc}",
                    ),
                )
            )
        limite_index = next(
            (
                limite
                for limite in resultat.figures_limite
                if limite.figure_id == figure.figure_id
            ),
            None,
        )

        observation = record_figure_observation(
            figure.artifact_id,
            figure.figure_id,
            description,
            crop_sha256=crop_sha256,
            prompt_version=_fig.PROMPT_FIGURE_REEXAMINEE,
            representation_id=figure.representation_id,
            page=figure.page,
            region=figure.region,
            claim_token=figure.claim_token,
            db=db,
        )
        if observation is None:
            db.rollback()
            return
        if limite_index is not None:
            # La description EST dans la représentation, mais un ou plusieurs
            # fragments n'ont pas pu être patchés : la figure n'est pas
            # « done » pour l'index. Le texte n'est pas perdu.
            reussi = _etape.marquer_limite(
                db,
                figure,
                "description appliquée mais index des fragments incomplet : "
                f"{limite_index.cause}",
                description=description,
                description_sha256=_reps.sha256_texte(description),
                observation_id=observation.id,
                commit=False,
            )
        else:
            reussi = _etape.marquer_succes(
                db,
                figure,
                description=description,
                description_sha256=_reps.sha256_texte(description),
                observation_id=observation.id,
                applied_revision=revision,
                commit=False,
            )
        if not reussi:
            db.rollback()
            return
        db.commit()
    except BailPerdu:
        try:
            db.rollback()
        except Exception:
            pass
        log.info(
            "[figures] claim perdu avant l'application de %s : aucun écrit",
            figure.figure_id,
        )
        return
    except Exception as exc:  # noqa: BLE001 — reprise par le balayage
        try:
            db.rollback()
        except Exception:
            pass
        log.warning(
            "[figures] application impossible pour %s : %s: %s",
            figure.figure_id,
            type(exc).__name__,
            exc,
        )
        _echec_reessayable(figure, f"écriture du résultat impossible : {exc}")
    finally:
        try:
            db.close()
        except Exception:
            pass


def _clore_limite(figure, cause: str) -> None:
    """Clôt en `limite` et remplace le placeholder par un bloc motivé.

    Si le placeholder lui-même est introuvable/ambigu, le statut et la cause
    sont quand même écrits : la ligne `artifact_figures` reste la source de
    vérité de la limite, et le Markdown n'est pas deviné.
    """
    from backend.ingestion import figures_etape as _etape
    from backend.ingestion import figures_pdf as _fig
    from backend.ingestion import representations as _reps

    db = _session()
    try:
        representation = _etape.representation_courante(db, figure.representation_id)
        if representation is None:
            _etape.marquer_superseded(db, figure, commit=True)
            return
        try:
            _reps.patcher_figure(
                db,
                representation,
                figure.page,
                figure.region,
                _fig.bloc_figure_limite(figure.page, figure.region, cause),
                commit=False,
            )
        except _reps.RepresentationPerimee:
            db.rollback()
            _etape.marquer_superseded(db, figure, commit=True)
            return
        except _reps.PatchFigureImpossible as exc:
            db.rollback()
            cause = f"{cause} ; placeholder non remplacé : {exc}"
        if not _etape.marquer_limite(db, figure, cause, commit=True):
            log.info(
                "[figures] claim perdu avant la limite de %s : aucun écrit",
                figure.figure_id,
            )
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:
            pass
        log.warning(
            "[figures] limite non écrite pour %s : %s: %s",
            figure.figure_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def _echec_reessayable(figure, cause: str) -> None:
    """Planifie un nouvel essai, ou bascule en limite au plafond."""
    from backend.ingestion import figures_etape as _etape

    if int(figure.attempts) >= _etape.MAX_ESSAIS:
        _clore_limite(
            figure,
            f"{cause} (plafond de {_etape.MAX_ESSAIS} exécutions atteint)",
        )
        return
    db = _session()
    try:
        _etape.marquer_echec_reessayable(db, figure, cause, commit=True)
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:
            pass
        log.warning(
            "[figures] reprise non planifiée pour %s : %s: %s",
            figure.figure_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def _differe(figure, cause: str) -> None:
    """Rend la figure pending SANS consommer d'essai (chat/texte/GPU occupé).

    Un différé n'est PAS un échec : il ne laisse aucun `last_error`, donc
    `recalculer_etat_figures` garde `en_attente`/`en_cours`. La cause reste
    lisible dans le journal, pas dans une colonne qui ferait croire à un essai
    raté.
    """
    from backend.ingestion import figures_etape as _etape

    log.info("[figures] %s différée : %s", figure.figure_id, cause)
    db = _session()
    try:
        _etape.marquer_differe(db, figure, cause, commit=True)
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:
            pass
        log.warning(
            "[figures] différé non écrit pour %s : %s: %s",
            figure.figure_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def _executer(figure) -> None:
    """Traite UNE figure réclamée : crop → VLM → patch → audit → statut."""
    from backend.ingestion import figures_etape as _etape
    from backend.ingestion import figures_pdf as _fig
    from backend.ingestion import gate as _gate
    from backend.ingestion import vision_appel as _vision
    from backend.inference.resource_coordinator import ResourceBusy

    if figure.epuise:
        _clore_limite(
            figure,
            f"plafond de {_etape.MAX_ESSAIS} exécutions atteint sans description",
        )
        return

    db = _session()
    try:
        representation = _etape.representation_courante(db, figure.representation_id)
        if representation is None:
            _etape.marquer_superseded(db, figure, commit=True)
            return
        chemin = _etape.chemin_blob(db, figure.artifact_id)
    finally:
        try:
            db.close()
        except Exception:
            pass

    if not chemin:
        _clore_limite(figure, "blob du document introuvable : crop impossible")
        return

    try:
        crop, cause = _fig.crop_depuis_pdf(
            chemin,
            figure.page,
            figure.region,
            bbox_json=figure.bbox_json,
            label=figure.label,
            score=figure.score,
            dpi=_gate.PDF_RENDER_DPI_CONFIG,
            kind="ingestion",
        )
    except _fig.RecadrageDiffere as exc:
        # Re-détection impossible pour une cause TRANSITOIRE (GPU/OCR occupé,
        # modèle indisponible) : ce n'est pas une dérive de layout, donc pas une
        # limite terminale. On rend la figure due plus tard, sans essai consommé.
        _differe(figure, str(exc))
        return
    if cause:
        _clore_limite(figure, cause)
        return
    if crop is None:  # pragma: no cover — crop_depuis_pdf nomme toutes les causes
        _clore_limite(figure, "crop introuvable sans cause nommée")
        return

    try:
        relpath, sha, largeur, hauteur = _etape.ecrire_crop_png(crop)
        tampon = io.BytesIO()
        crop.save(tampon, format="PNG")
        pixels = tampon.getvalue()
    except Exception as exc:  # noqa: BLE001
        _echec_reessayable(figure, f"persistance du crop impossible : {exc}")
        return

    db = _session()
    try:
        if not _etape.enregistrer_crop(
            db,
            figure.figure_id,
            figure.claim_token,
            crop_relpath=relpath,
            crop_sha256=sha,
            largeur=largeur,
            hauteur=hauteur,
        ):
            return
    finally:
        try:
            db.close()
        except Exception:
            pass

    battement = _Battement(figure)
    battement.start()
    try:
        try:
            description = _fig.reexaminer_figure(
                pixels, _vision.appeler_vision_generale
            )
        except ResourceBusy as exc:
            battement.arreter()
            _differe(figure, f"GPU occupé (chat prioritaire) : {exc}")
            return
        except _vision.VisionIndisponible as exc:
            battement.arreter()
            # `couche_managere` lève VisionIndisponible AUSSI pour une réponse
            # vide. Ce n'est pas une indisponibilité transitoire : aucune
            # description n'est récupérable, c'est une limite nommée.
            if "réponse vide" in str(exc).lower():
                _clore_limite(figure, f"description vide rendue par la vision : {exc}")
            else:
                _differe(figure, f"vision indisponible : {exc}")
            return
        except ValueError as exc:
            battement.arreter()
            _clore_limite(figure, f"description vide ou invalide : {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            battement.arreter()
            _echec_reessayable(figure, f"appel vision impossible : {exc}")
            return
    finally:
        battement.arreter()

    if battement.perdu.is_set():
        log.info(
            "[figures] claim perdu pendant la description de %s : aucun écrit",
            figure.figure_id,
        )
        return
    if not (description or "").strip():
        _clore_limite(figure, "description vide rendue par la vision")
        return

    _appliquer_description(
        figure,
        description=description,
        crop_sha256=sha,
    )


def _traiter_pending(stop_event: threading.Event) -> None:
    """Une passe bornée : une figure à la fois, priorité texte/chat respectée.

    Aucun verrou global n'est tenu ici. Le chat peut démarrer à tout moment :
    `_chat_actif()` empêche seulement de RÉCLAMER une nouvelle figure. Pendant
    un crop déjà commencé, la priorité interactive est portée par le
    coordinateur (`kind="ingestion"`), pas par une réservation binaire qui
    bloquerait le chat.
    """
    while not stop_event.is_set():
        if _ingestion_texte_prioritaire():
            return
        if _chat_actif():
            return
        db = _session()
        try:
            from backend.ingestion import figures_etape as _etape

            figure = _etape.reclamer_figure(db)
        finally:
            try:
                db.close()
            except Exception:
                pass
        if figure is None:
            return
        try:
            _executer(figure)
        except Exception as exc:  # noqa: BLE001 — le fond ne casse pas le worker
            log.warning(
                "[figures] exécution interrompue pour %s : %s: %s",
                figure.figure_id,
                type(exc).__name__,
                exc,
            )
            _echec_reessayable(figure, f"worker interrompu : {exc}")


def _boucle(stop_event: threading.Event) -> None:
    """Réveil immédiat + filet périodique, avec debounce court."""
    while not stop_event.is_set():
        reveille = _reveil.wait(_INTERVALLE_S)
        _reveil.clear()
        if stop_event.is_set():
            return
        if reveille:
            if stop_event.wait(_DEBOUNCE_S):
                return
            # Les réveils arrivés pendant le debounce sont couverts par CETTE
            # passe : les laisser armés produirait un second tour inutile.
            _reveil.clear()
        if not _execution_lock.acquire(blocking=False):
            continue
        try:
            _traiter_pending(stop_event)
        except Exception as exc:  # noqa: BLE001
            log.warning("[figures] passe impossible : %s: %s", type(exc).__name__, exc)
        finally:
            _execution_lock.release()


def demarrer_worker_figures() -> dict[str, Any]:
    """Démarre le thread daemon, une seule fois. Désactivable par env."""
    global _thread, _stop
    if os.environ.get("THOT_DISABLE_FIGURES_WORKER") == "1":
        return {"ok": True, "statut": "desactive"}
    with _lock:
        if _thread is not None and _thread.is_alive():
            return {"ok": True, "statut": "deja_demarre"}
        _stop = threading.Event()
        _reveil.clear()
        _thread = threading.Thread(
            target=_boucle, args=(_stop,), name=_VOIE, daemon=True
        )
        _thread.start()
    return {"ok": True, "statut": "demarre"}


def arreter_worker_figures() -> dict[str, Any]:
    """Arrête et joint le thread. Réveille le `wait` pour ne pas attendre 60 s."""
    global _thread

    _stop.set()
    _reveil.set()
    with _lock:
        thread = _thread
    if thread is not None:
        thread.join(5)
    with _lock:
        if thread is not None and _thread is thread and not thread.is_alive():
            _thread = None
    _reveil.clear()
    return {"ok": True, "statut": "arrete"}


__all__ = [
    "arreter_worker_figures",
    "demarrer_worker_figures",
    "reveiller_worker_figures",
]
