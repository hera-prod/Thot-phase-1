# runner/mempwatch.py — Le PIC d'un scénario nommé, et la convention qui le rend comparable
#
# POURQUOI CE MODULE, À CÔTÉ DE `memprof` : `memprof.RunRecord` photographie un
# INSTANT. Un instant ne prouve rien — un run qui monte à 9 Go entre deux pics
# puis redescend à 1 Go affiche 1 Go, et le rapport dit « conforme ». Ce module
# mesure un SCÉNARIO (une suite d'étapes dont on connaît l'ordre) et en rend le
# PIC, avec l'instant où il a eu lieu. Une mesure qui ne dit pas QUAND ne prouve
# rien : on ne peut ni la comparer à une autre version du pipeline, ni savoir quel
# étage la mandate — et le_pdf de la Bible parle de « pic », pas de « moyenne ».
#
# IL REFUSE DE RAPPORTER UN CHIFFRE QU'IL N'A PAS MESURÉ. Une source illisible
# ne devient pas un zéro : un zéro est une mesure (celle d'une machine sans GPU),
# et le confondre avec « je n'ai pas su » produit exactement le faux PASS que la
# Bible §1.4 interdit — un budget qui ne peut pas échouer n'est pas un budget.

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager

try:
    import psutil
except Exception:  # pragma: no cover - psutil est optionnel hors Linux
    psutil = None
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

MIB = 1024 * 1024
GIB = 1024 * MIB

# ----------------------------------------------------------------------------
# LA CONVENTION DE MESURE — Bible des intentions §1.4, « pic inférieur à 8 Go ».
# Elle est écrite ICI, en un seul endroit, parce qu'une convention qu'on récite à
# chaque implémentation finit par diverger entre deux fichiers. Le code ci-dessous
# ne fait rien d'autre que l'appliquer.
#
#   RAM  = RssAnon          mémoire anonyme : ce que le process a réellement alloué
#   VRAM = torch.cuda.max_memory_reserved()   haut du réservoir du cacheur CUDA
#   TOTAL RETENU = RssAnon + VRAM pic
#
#   RssFile et VmSwap sont EXCLUS du cumul. Ils restent deux portes SÉPARÉES et
#   NON ADDITIVES, jamais une somme de plus.
#
#   POURQUOI RssFile est exclu du cumul, et non simplement ignoré : un `mmap` du
#   fichier de poids et la déquantisation de ce même bloc en VRAM sont deux états
#   SUCCESSIFS du MÊME octet. À l'instant de la déquantisation la page fichier est
#   lue puis abandonnée ; après, elle ne l'est plus. Les deux mesures sont
#   pourtant toutes deux vraies, et les additionner revient à payer deux fois le
#   fichier de poids — ce qui ferait échouer le budget PAR CONSTRUCTION, puisque
#   la taille sur disque des poids est précisément ce que le budget ne doit pas
#   compter. C'est pourquoi la preuve de la convention est un test : deux
#   fichiers de poids de tailles différentes, même total.
#
#   POURQUOI JAMAIS `psutil.rss` : le RSS inclut RssFile. C'est exactement le
#   double compte ci-dessus, avec un nom qui ne dit pas ce qu'il mesure.
#   `RunRecord.ram_mb` portait ce mensonge ; il porte maintenant le total de la
#   convention.
#
#   POURQUOI `reserved` ET NON `allocated` : le cacheur CUDA ne rend pas ce qu'il
#   n'a pas reservé. Le budget doit couvrir ce que le process TIENT sur la carte,
#   pas ce qu'il utilisationne en ce moment — sans quoi un simple `empty_cache`
#   ferait disparaître le pic du rapport.
# ----------------------------------------------------------------------------
CONVENTION_MESURE = (
    "RAM=RssAnon ; VRAM=torch.cuda.max_memory_reserved() ; total=RssAnon+VRAM_pic ; "
    "RssFile et VmSwap exclus du cumul (portes séparées, non additives) ; "
    "jamais psutil.rss."
)

MESURE_RAM_ANON = "rss_anon"
MESURE_RSS_FILE = "rss_file"
MESURE_VM_SWAP = "vm_swap"
MESURE_VRAM = "vram"

# Les deux mesures qui composent le total. Une seule des deux manquantes et le
# total n'est pas un total : `PicMemoire.total_mb` lève plutôt que deAdditionner
# un trou.
MESURES_DU_CUMUL = (MESURE_RAM_ANON, MESURE_VRAM)

#: Champs de `/proc/<pid>/status` dont on a besoin, et leur unité : kilo-octets.
CHAMPS_STATUS_PROC = {
    "RssAnon": MESURE_RAM_ANON,
    "RssFile": MESURE_RSS_FILE,
    "VmSwap": MESURE_VM_SWAP,
}


class MesureNonDisponible(RuntimeError):
    """Une mesure du budget n'a pas pu être prise.

    Distincte d'une mesure à zéro : zéro est un résultat, ceci est une absence.
    Le message nomme CE QUI MANQUE et OÙ, parce qu'un refus sans issue se vit
    comme un défaut et pas comme une information.
    """


def chemin_status_proc() -> Path:
    """Chemin du `status` du process courant. Point d'entrée pour les tests.

    Une fonction, et non une constante calculée à l'import : un test doit pouvoir
    désigner un `/proc` inexistant pour prouver le refus, sans tuer le process qui
    mesure.
    """
    return Path("/proc") / str(os.getpid()) / "status"


def lire_memoire_ram() -> dict[str, float]:
    """RssAnon, RssFile et VmSwap en Mo. LÈVE `MesureNonDisponible` si on ne peut pas.

    Plus de `(0, 0, 0)` : c'est ce triplet qui faisait passer les cinq portes
    hors Linux, parce que `0 ≤ 3072`, `0 == 0` et `0 < RSS_FILE_BASE_MB` sont trois
    verdicts favorables. Un budget qui ne peut pas échouer n'est pas un budget.
    """
    if not sys.platform.startswith("linux"):
        if psutil is None:
            raise MesureNonDisponible(
                f"RAM non mesurable : psutil est indisponible sur cette plateforme "
                f"(« {sys.platform} »). RssAnon/RssFile/VmSwap n'ont pas "
                "d'équivalent fiable hors Linux sans psutil."
            )
        try:
            processus = psutil.Process(os.getpid())
            info = processus.memory_info()
        except Exception as exc:
            raise MesureNonDisponible(
                f"RAM non mesurable : lecture psutil impossible ({exc})."
            ) from exc
        # Hors Linux, on ne sait pas séparer l'anonime du fichier : psutil.rss
        # est traité comme RssAnon (convention conservatrice, comme indiqué).
        return {MESURE_RAM_ANON: float(info.rss) / MIB}
    chemin = chemin_status_proc()
    try:
        texte = chemin.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MesureNonDisponible(
            f"RAM non mesurable : lecture de {chemin} impossible ({exc}). "
            "Sans RssAnon/RssFile/VmSwap, aucune porte RAM ne peut être jugée."
        ) from exc
    valeurs: dict[str, float] = {}
    for ligne in texte.splitlines():
        cle, _, reste = ligne.partition(":")
        nom = CHAMPS_STATUS_PROC.get(cle)
        if nom is None:
            continue
        chiffres = reste.split()
        if not chiffres:
            continue
        try:
            valeurs[nom] = int(chiffres[0]) / 1024
        except ValueError as exc:
            raise MesureNonDisponible(
                f"RAM non mesurable : « {ligne.strip()} » illisible dans {chemin} ({exc})."
            ) from exc
    manquantes = [nom for nom in CHAMPS_STATUS_PROC.values() if nom not in valeurs]
    if manquantes:
        raise MesureNonDisponible(
            f"RAM partiellement non mesurable : {chemin} ne porte pas "
            f"{', '.join(manquantes)} (noyau inhabituel ?). Une porte qui porte sur "
            "une mesure absente ne peut pas être tenue pour franchie."
        )
    return valeurs


def lire_vram_reservee_mb() -> float:
    """Haut du réservoir CUDA en Mo. LÈVE `MesureNonDisponible` s'il n'y a rien à mesurer.

    Import de torch différé : le module doit s'importer et se tester sans GPU,
    et torch ne sert qu'à cette lecture.
    """
    try:
        import torch
    except Exception as exc:  # pragma: no cover — torch est une dépendance
        raise MesureNonDisponible(
            f"VRAM non mesurable : torch absent ({exc})."
        ) from exc
    cuda = getattr(torch, "cuda", None)
    if cuda is None or not getattr(cuda, "is_available", lambda: False)():
        raise MesureNonDisponible(
            "VRAM non mesurable : aucun GPU CUDA visible depuis ce process. "
            "C'est une absence de matériel, pas une VRAM de 0 Go — les deux ne "
            "donnent pas le même verdict de porte."
        )
    return float(cuda.max_memory_reserved()) / MIB


def lire_vram_carte_gb() -> tuple[float | None, float | None, str | None]:
    """(VRAM libre, VRAM totale) en Go via l'outil du CONSTRUCTEUR de la carte.

    Renvoie `(None, None, cause)` quand la mesure est impossible. La cause vient
    de `paths.exiger_nvidia_smi()` : c'est elle qui sait nommer l'absence et son
    remède, donc on ne réécrit pas un message concurrent qui dirait « nvidia-smi
    introuvable » sans dire quoi faire ensuite.

    POURQUOI CET OUTIL ET PAS CELUI DE TORCH : torch répond sur SON contexte
    (réservoir du cacheur de ce process), pas sur ce que la carte accorde. Or la
    question des portes est « la carte a-t-elle de la place, TOUT OCCUPÉANT
    COMPRIS ? » — un worker OCR dans un autre process en occupe 1,9 Go, et seule
    la carte le sait. Une mesure qui l'ignore ferait passer une porte qui
    devrait tomber.
    """
    from backend.config import paths

    try:
        binaire = paths.exiger_nvidia_smi()
    except paths.DependanceSystemeAbsente as exc:
        return None, None, str(exc)
    try:
        sortie = subprocess.check_output(
            [
                str(binaire),
                "--query-gpu=memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        )
        libre, total = [
            valeur.strip() for valeur in sortie.strip().splitlines()[0].split(",")
        ]
        return round(int(libre) / 1024, 2), round(int(total) / 1024, 2), None
    except Exception as exc:
        # L'outil est résolvable mais ne parle pas : installation NVIDIA
        # cassée, permission, pilote muet. Distinct de « absent », et le
        # message le dit — un 0 Go ici ferait tomber une porte par accident de
        # parsing.
        return (
            None,
            None,
            f"nvidia-smi résolu ({binaire}) mais muet ou illisible : {exc}",
        )


@dataclass(frozen=True)
class PicMemoire:
    """Le PIC d'un scénario : le cumul le plus haut, ET l'instant où il a eu lieu.

    Les composantes sont `None` quand elles n'ont pas été mesurées — jamais 0.0.
    `total_mb` refuse d'y additionner un trou.
    """

    scenario: str
    instant: str
    rss_anon_mb: float | None
    rss_file_mb: float | None
    vm_swap_mb: float | None
    vram_mb: float | None
    instant_rss_anon: str
    instant_vram: str
    echantillons: int
    mesures_absentes: tuple[tuple[str, str], ...] = ()

    @property
    def total_mb(self) -> float:
        """`RssAnon + VRAM pic`. LÈVE si l'un des deux n'a pas été mesuré."""
        if self.rss_anon_mb is None or self.vram_mb is None:
            absentes = ", ".join(nom for nom, _ in self.mesures_absentes)
            raise MesureNonDisponible(
                f"scénario « {self.scenario} » : total non rapportable, il manque "
                f"{absentes or 'une mesure du cumul'}. Un total calculé sur une "
                "mesure absente n'est pas un total, c'est un nombre qui a l'air "
                "d'en être un."
            )
        return self.rss_anon_mb + self.vram_mb

    @property
    def complet(self) -> bool:
        return self.rss_anon_mb is not None and self.vram_mb is not None

    def __str__(self) -> str:
        cumul = f"{self.total_mb:.0f} Mo" if self.complet else "NON RAPPORTABLE"
        fenetre = f"{self.instant} (sur {self.echantillons} échantillons)"
        return (
            f"[pic] {self.scenario} : cumul {cumul} à « {fenetre}» | "
            f"RssAnon {self._ou(self.rss_anon_mb)} (pic à « {self.instant_rss_anon}») | "
            f"VRAM {self._ou(self.vram_mb)} (pic à « {self.instant_vram}») | "
            f"portes séparées RssFile {self._ou(self.rss_file_mb)}, "
            f"VmSwap {self._ou(self.vm_swap_mb)} — hors cumul"
            + (
                f" | non mesuré : {', '.join(nom for nom, _ in self.mesures_absentes)}"
                if self.mesures_absentes
                else ""
            )
        )

    @staticmethod
    def _ou(valeur: float | None) -> str:
        return "n.d." if valeur is None else f"{valeur:.0f} Mo"


class SuiviMemoire:
    """Suit UN scénario nommé et retient son pic, pas son dernier instant.

    `observer(instant)` prend un échantillon et le nomme. Le pic rapporté est le
    maximum du CUMUL (`RssAnon + VRAM`) parmi tous les échantillons, avec
    l'étiquette de l'échantillon qui l'a atteint : c'est ce qui permet de dire
    « c'est l'assemblage du transformer qui mandate », et pas seulement « ça
    dépasse ».
    """

    def __init__(
        self,
        scenario: str,
        *,
        lire_ram: Callable[[], Mapping[str, float]] | None = None,
        lire_vram: Callable[[], float] | None = None,
    ) -> None:
        if not str(scenario).strip():
            raise ValueError(
                "un scénario sans nom ne prouve rien : il n'y a rien à comparer"
            )
        self.scenario = str(scenario)
        # Les sources par défaut sont résolues ICI, pas en valeur par défaut du
        # paramètre : une liaison figée à l'import rendrait impossible de brancher
        # une mesure simulée, donc de prouver quoi que ce soit sans carte.
        self._lire_ram = lire_ram or lire_memoire_ram
        self._lire_vram = lire_vram or lire_vram_reservee_mb
        self._mesures_absentes: list[tuple[str, str]] = []
        self._cumul: tuple[float, str, dict[str, float]] | None = None
        self._pics: dict[str, tuple[float, str]] = {}
        self._echantillons = 0

    @property
    def mesures_absentes(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._mesures_absentes)

    def observer(self, instant: str) -> None:
        """Prend un échantillon et le rattache à un instant nommé du scénario.

        Le cumul suivi est la somme des mesures du cumul PRÉSENTES. Un scénario
        dont la VRAM n'a jamais pu être lue est donc suivi sur sa seule part
        mesurée, et `PicMemoire.total_mb` refuse de le rendre : on ne comble
        jamais un trou par un zéro.
        """
        if not str(instant).strip():
            raise ValueError(
                "un échantillon sans nom d'instant ne dit pas QUAND il a eu lieu"
            )
        valeurs: dict[str, float] = {}
        for nom, lire in ((MESURE_RAM_ANON, self._ram), (MESURE_VRAM, self._vram)):
            try:
                valeurs.update(lire())
            except MesureNonDisponible as exc:
                self._absente(nom, str(exc))
        if not valeurs:
            # Rien n'a été mesuré : c'est un instant SANS VALEUR, pas un pic à zéro.
            return
        self._echantillons += 1
        for nom, valeur in valeurs.items():
            self._garder_pic(nom, valeur, str(instant))
        cumul = sum(valeurs[nom] for nom in MESURES_DU_CUMUL if nom in valeurs)
        if self._cumul is None or cumul > self._cumul[0]:
            self._cumul = (cumul, str(instant), dict(valeurs))

    def pic(self) -> PicMemoire:
        """Le pic du scénario. LÈVE si aucun échantillon n'a pu être pris."""
        if self._cumul is None:
            causes = " ; ".join(
                f"{nom} : {raison}" for nom, raison in self._mesures_absentes
            )
            raise MesureNonDisponible(
                f"scénario « {self.scenario} » : aucun pic rapporté, aucune mesure "
                f"n'a abouti. {causes or 'Cause inconnue.'}"
            )
        return PicMemoire(
            scenario=self.scenario,
            instant=self._cumul[1],
            rss_anon_mb=_valeur_ou_none(self._pics, MESURE_RAM_ANON),
            rss_file_mb=_valeur_ou_none(self._pics, MESURE_RSS_FILE),
            vm_swap_mb=_valeur_ou_none(self._pics, MESURE_VM_SWAP),
            vram_mb=_valeur_ou_none(self._pics, MESURE_VRAM),
            instant_rss_anon=self._pics[MESURE_RAM_ANON][1]
            if MESURE_RAM_ANON in self._pics
            else "",
            instant_vram=self._pics[MESURE_VRAM][1]
            if MESURE_VRAM in self._pics
            else "",
            echantillons=self._echantillons,
            mesures_absentes=self.mesures_absentes,
        )

    def _ram(self) -> dict[str, float]:
        return dict(self._lire_ram())

    def _vram(self) -> dict[str, float]:
        return {MESURE_VRAM: self._lire_vram()}

    def _absente(self, nom: str, raison: str) -> None:
        if all(existant != nom for existant, _ in self._mesures_absentes):
            self._mesures_absentes.append((nom, raison))

    def _garder_pic(self, nom: str, valeur: float, instant: str) -> None:
        if nom not in self._pics or valeur > self._pics[nom][0]:
            self._pics[nom] = (valeur, instant)


def _valeur_ou_none(pics: Mapping[str, tuple[float, str]], nom: str) -> float | None:
    """La valeur d'un pic, ou `None` si cette mesure n'a jamais été prise."""
    entree = pics.get(nom)
    return None if entree is None else entree[0]


@contextmanager
def mesurer(
    scenario: str,
    *,
    lire_ram: Callable[[], Mapping[str, float]] | None = None,
    lire_vram: Callable[[], float] | None = None,
) -> Iterator[SuiviMemoire]:
    """Suit un scénario et garantit un échantillon à l'entrée comme à la sortie.

    Les deux extrémités sont échantillonnées même si le corps lève : un run qui
    explose après son pic doit quand même avoir mesuré ce pic, sinon le journal
    ne portera que l'échec.
    """
    suivi = SuiviMemoire(scenario, lire_ram=lire_ram, lire_vram=lire_vram)
    suivi.observer("entrée du scénario")
    try:
        yield suivi
    finally:
        suivi.observer("sortie du scénario")
