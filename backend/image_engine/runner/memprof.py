# runner/memprof.py — Les portes du budget 8 Go, APPLIQUÉES (Bible §1.4)
#
# CE QUE CE MODULE REFUSE DE FAIRE, ET POURQUOI :
#
#   1. Il ne rend JAMAIS une mesure absente sous forme de 0. Avant, `get_rss_mb()`
#      avalait l'absence de `/proc` et renvoyait `(0, 0, 0)` ; or `0 ≤ 3072`,
#      `0 == 0` et `0 < RSS_FILE_BASE_MB` sont trois verdicts FAVORABLES. Les cinq
#      portes passaient donc hors Linux : un faux PASS, c'est-à-dire exactement le
#      mensonge que la Bible interdit (« la taille des modèles sur disque ne
#      suffit pas à démontrer le respect de cette limite »). Une porte qui n'a
#      pas su mesurer est maintenant une porte fausse, qui nomme sa cause.
#
#   2. Il ne se contente plus d'imprimer. `gates_ok()` était sans appelant :
#      `orchestrator`, `spike_t2i` et `dev/server` n'appelaient que `capture()`,
#      qui journalise. Une mesure AFFICHÉE n'est pas une mesure OPPOSÉE. Le
#      verdict est rendu par `exiger_conforme()`, qui refuse. Les seuils, eux,
#      ne sont pas rediscutés : ils sont calibrés (rapport_gestion_memoire §5,
#      7,6-8 Go au pic) et seule leur APPLICATION était fausse — SAUF celui de
#      `RssFile`, qui était calibré sur un bug et l'est aujourd'hui sur une
#      mesure (voir `RSS_FILE_BASE_MB`). Un seuil issu d'un défaut ment sur ce
#      défaut ; il n'a été repris que parce que le défaut qu'il mesurait a été
#      réparé, et parce que la question qu'il pose a maintenant une bonne réponse.
#
# LA MESURE, ELLE, EST DÉFINIE DANS `mempwatch` (convention RAM/VRAM, preuve par
# test). Ce module ne fait que la constater et la confronter aux portes.
from __future__ import annotations

from dataclasses import dataclass

from backend.config import paths

from . import mempwatch
from .mempwatch import (
    CONVENTION_MESURE,
    MIB,
    MESURE_RAM_ANON,
    MESURE_RSS_FILE,
    MESURE_VRAM,
    MESURE_VM_SWAP,
    MesureNonDisponible,
    PicMemoire,
    mesurer,
)

#: La mesure de VRAM libre ne vient pas de torch mais de l'outil du CONSTRUCTEUR
#: de la carte : c'est lui qui sait ce qu'une application tient réellement sur la
#: carte, y compris ce qu'un AUTRE process (le worker OCR, par exemple) y occupe.
MESURE_VRAM_LIBRE = "vram_libre"

# Seuils du rapport_gestion_memoire §5. CONSTANTES NOMMÉES, pour qu'une
# recalibration se voie dans une diff au lieu de se diluer dans un littéral.
RAM_ANON_PLEIN_MB = 3072.0  # full_gpu_* : RssAnon ≤ 3 Go
RAM_ANON_HYBRIDE_MB = 6144.0  # hybrid : le texte reste chargé en CPU mmap
VRAM_PIC_PLEIN_MB = 8500.0  # pic VRAM ≤ 8,5 Go
SWAP_CPU_MAX_MB = 512.0  # cpu : swap ≤ 0,5 Go

# LA PORTE SWAP GPU, CALIBRÉE SUR DEUX MESURES — comme `RSS_FILE_BASE_MB`.
# ==============================================================================
# La porte était binaire (`swap = 0`) : elle jugeait un run ENTIER sur un unique
# octet swappé, alors que le cumul du même run tenait sous les 3,6 Go. Un run
# conforme a été refusé pour 114 Mo de swap.
#
# Les deux mesures dont le seuil sort, prises sur cette machine :
#
#     run conforme q-r du 01/11/2026   VmSwap 114 Mo  (RssAnon 1045 Mo, VRAM pic 2618 Mo)
#                                       → image valide produite, refusée par « swap =0 »
#     plus petit swap d'un VRAI défaut  VmSwap 231 Mo  (double allocation : 23 Go RssAnon)
#     mesuré dans la référence
#
# 160 Mo est AU-DESSUS de 114 et EN DESSOUS de 231 : il ne baisse pas le seuil
# pour faire passer une mesure, il le place entre la mesure saine et le plus petit
# défaut réel connu. La contre-épreuve est dans
# `tests/backend/test_portes_memoire.py` : 114 passe, 231 échoue.
SWAP_GPU_MAX_MB = 160.0  # full_gpu_* et hybride : swap ≤ 0,16 Go
MARGE_VRAM_LIBRE_GB = 0.5  # pic ≤ libre − 0,5

# LA PORTE `RssFile`, ET LA QUESTION QU'ELLE POSE — qui n'est pas « le cumul tient-il ? »
# ==============================================================================
# `RssFile` n'est pas un terme du cumul (`mempwatch.CONVENTION_MESURE` : un `mmap`
# du fichier de poids et la déquantisation de ce même bloc sont deux états
# SUCCESSIFS du MÊME octet, les additionner ferait payer le fichier deux fois). Elle
# est donc une porte SÉPARÉE et NON ADDITIVE, et sa question est une autre :
#
#     « est-on en train de payer le fichier en mémoire résidente,
#       alors qu'on pourrait l'évicter ? »
#
# LE 1 000 Mo HISTORIQUE ÉTAIT UN SEUIL MENTÉ. Il venait d'une version de
# `loaders/raw_vram.py` qui RECOPIAIT les 5,45 Go du fichier de poids en mémoire
# anonyme (`torch.from_numpy(arr.copy())`) puis démontait le mmap : `RssFile`
# plafonnait à 213 Mo, non parce que le fichier était évité, mais parce qu'il
# était payé ailleurs — en `RssAnon`, donc dans le cumul, où il pesait 5,45 Go.
# Un seuil calibré sur ce défaut mesure le défaut et rien d'autre : il passe au
# moment exact où il faudrait qu'il tombe.
#
# LA QUESTION A MAINTENANT UNE BONNE RÉPONSE, ET ELLE EST « NON ». Le poids est
# mmapé en MAP_PRIVATE, donc résident dès la première lecture, et
# `LazyLinear.forward` appelle `RawVRAMIndex.relacher_les_pages()` après la
# déquantisation de chaque tenseur : le processus cesse de se facturer un fichier
# qu'il ne fait que lire. Le seuil doit donc dire « pas le fichier », et rester
# capable de mordre si ça casse.
#
# MESURÉ LE 06/10/2026, RTX 4070 SUPER, `flux2-klein-9b-kv-Q4_K_M` (fichier de
# 5 452 Mo), 4 pas de diffusion, 1024×1024, ce code :
#
#     point d'appel ACTIF     RssFile 261 Mo au bilan, pic de scénario 365 Mo
#     point d'appel RETIRÉ    RssFile 5 047 Mo (5 275 sur un second run : la valeur
#                             suit le cache de pages, pas le code) → porte
#                             FRANCHIE, image effacée, run refusé
#
# 700 Mo, c'est ≈ 1,9× le pic observé et ≈ un huitième du fichier de poids :
# assez large pour ne pas trembler au premier import supplémentaire, et 7× trop
# bas pour être satisfait en payant le fichier. La contre-épreuve n'est pas une
# phrase : elle est dans `tests/backend/test_portes_memoire.py`, qui rejoue les
# 5 047 Mo mesurés sans le point d'appel et exige le refus.
RSS_FILE_BASE_MB = 700.0


class PorteMemoireFranchie(RuntimeError):
    """Le budget de 8 Go n'est pas établi : une porte est franchie, ou non mesurable.

    Le refus est le but. Le message nomme CHAQUE porte fautive, et — une fois
    chacune, pas une fois par porte — la cause de chaque mesure manquante : un
    refus sans diagnostic se vit comme une panne.
    """


@dataclass(frozen=True)
class Porte:
    """Une porte du budget : son nom, les mesures dont elle dépend, son verdict.

    `franchie` vaut FAUX dès qu'une mesure dont la porte dépend est absente. Un
    champ `mesures` explicite, c'est ce qui rend cette règle Applicable
    porte par porte au lieu d'être une phrase d'intention.
    """

    nom: str
    mesures: tuple[str, ...]
    franchie: bool
    detail: str
    raison: str | None = None

    def __str__(self) -> str:
        if self.raison is not None:
            # La cause est dans le bloc « Causes » du refus, une seule fois par
            # mesure : la répéter sous chaque porte noierait le diagnostic dans
            # le même paragraphe trois fois.
            return f"    ✗ {self.nom} — NON VÉRIFIABLE ({', '.join(self.mesures)})"
        marque = "✓" if self.franchie else "✗"
        return f"    {marque} {self.nom} — {self.detail}"


@dataclass
class RunRecord:
    """Un relevé de mémoire, et le verdict qu'il permet — ou refuse de porter.

    `mesures_absentes` est la partie honnête du relevé : la liste des
    (mesure, cause) que ce relevé n'a PAS su prendre. Les champs numériques
    restent à 0 pour ces mesures, et c'est pourquoi AUCUNE porte ne se fie à eux
    sans consulter `mesures_absentes` : le 0 y est un trou, pas une valeur.
    """

    rss_anon_mb: float = 0.0
    rss_file_mb: float = 0.0
    vm_swap_mb: float = 0.0
    vram_alloc_mb: float = 0.0
    vram_peak_mb: float = 0.0
    # TOTAL DE LA CONVENTION (RssAnon + VRAM pic). Ne PLUS y mettre psutil.rss :
    # le RSS inclut RssFile, donc il ferait payer deux fois le fichier de poids.
    ram_mb: float = 0.0
    backend: str = ""
    mode: str = ""
    free_vram_gb: float = 0.0
    vram_total_gb: float = 0.0
    ram_free_gb: float = 0.0
    mesures_absentes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def capture(
        cls, dev=None, mode: str = "", vram_peak_mb: float | None = None
    ) -> "RunRecord":
        """Relève la mémoire selon la convention, en notant ce qu'il n'a pas su mesurer.

        `vram_peak_mb` est prioritaire quand l'appelant l'a mesuré lui-même :
        `spike_t2i` remet le compteur CUDA à zéro entre deux phases, et c'est
        donc lui qui détient le pic de son scénario.
        """
        absentes: list[tuple[str, str]] = []
        ram = _mesurer_ram(absentes)
        vram_peak = _mesurer_vram_peak(vram_peak_mb, absentes)
        vram_libre, vram_total, cause_libre = mempwatch.lire_vram_carte_gb()
        if cause_libre is not None:
            absentes.append((MESURE_VRAM_LIBRE, _cause_vram_libre(cause_libre, dev)))
        rec = cls(
            rss_anon_mb=ram.get(MESURE_RAM_ANON, 0.0),
            rss_file_mb=ram.get(MESURE_RSS_FILE, 0.0),
            vm_swap_mb=ram.get(MESURE_VM_SWAP, 0.0),
            vram_alloc_mb=_vram_alloue_mb(),
            vram_peak_mb=vram_peak,
            ram_mb=ram.get(MESURE_RAM_ANON, 0.0) + vram_peak,
            vram_total_gb=vram_total or 0.0,
            free_vram_gb=vram_libre or 0.0,
            mesures_absentes=tuple(absentes),
        )
        if dev is not None:
            rec.backend = dev.backend
            rec.mode = mode or ""
            rec.ram_free_gb = dev.ram_free_gb
        elif mode:
            rec.mode = mode
        return rec

    def _criteres(self) -> tuple[tuple[str, tuple[str, ...], bool, str], ...]:
        """(nom, mesures, la porte tient-elle, détail lisible) — rapport §5.

        Les seuils sont ceux du rapport ; ils ne sont pas rediscutés ici, à
        l'exception de `SWAP_GPU_MAX_MB`, calibré sur deux mesures (voir sa
        constante). Ce qui change, c'est l'absence de mesure : elle est traduite
        en porte fausse par `_porter`, et la porte « VRAM ≤ free-0.5 » ne peut
        plus se résoudre par « pas de VRAM libre connue ⇒ conforme », qui était
        son mensonge.
        """
        vram_libre_ok = (
            self.free_vram_gb > 0
            and self.vram_peak_mb / 1024 <= self.free_vram_gb - MARGE_VRAM_LIBRE_GB
        )
        detail_libre = (
            f"pic {self.vram_peak_mb / 1024:.2f} Go, libre {self.free_vram_gb:.2f} Go "
            f"(marge {MARGE_VRAM_LIBRE_GB:.1f} Go)"
        )
        if self.mode in ("full_gpu_simult", "full_gpu_phased"):
            return (
                (
                    "RAM anon ≤3 Go",
                    (MESURE_RAM_ANON,),
                    self.rss_anon_mb <= RAM_ANON_PLEIN_MB,
                    f"{self.rss_anon_mb:.0f} Mo ≤ {RAM_ANON_PLEIN_MB:.0f} Mo",
                ),
                (
                    "VRAM ≤8.5 Go",
                    (MESURE_VRAM,),
                    self.vram_peak_mb <= VRAM_PIC_PLEIN_MB,
                    f"{self.vram_peak_mb:.0f} Mo ≤ {VRAM_PIC_PLEIN_MB:.0f} Mo",
                ),
                (
                    "VRAM ≤ free-0.5",
                    (MESURE_VRAM, MESURE_VRAM_LIBRE),
                    vram_libre_ok,
                    detail_libre,
                ),
                (
                    "swap ≤0.16 Go",
                    (MESURE_VM_SWAP,),
                    self.vm_swap_mb <= SWAP_GPU_MAX_MB,
                    f"{self.vm_swap_mb:.0f} Mo ≤ {SWAP_GPU_MAX_MB:.0f} Mo",
                ),
                (
                    "RssFile ~baseline",
                    (MESURE_RSS_FILE,),
                    self.rss_file_mb < RSS_FILE_BASE_MB,
                    f"{self.rss_file_mb:.0f} Mo < {RSS_FILE_BASE_MB:.0f} Mo",
                ),
            )
        if self.mode == "hybrid":
            return (
                (
                    "RAM anon ≤6 Go",
                    (MESURE_RAM_ANON,),
                    self.rss_anon_mb <= RAM_ANON_HYBRIDE_MB,
                    f"{self.rss_anon_mb:.0f} Mo ≤ {RAM_ANON_HYBRIDE_MB:.0f} Mo",
                ),
                (
                    "VRAM ≤8.5 Go",
                    (MESURE_VRAM,),
                    self.vram_peak_mb <= VRAM_PIC_PLEIN_MB,
                    f"{self.vram_peak_mb:.0f} Mo ≤ {VRAM_PIC_PLEIN_MB:.0f} Mo",
                ),
                (
                    "swap ≤0.16 Go",
                    (MESURE_VM_SWAP,),
                    self.vm_swap_mb <= SWAP_GPU_MAX_MB,
                    f"{self.vm_swap_mb:.0f} Mo ≤ {SWAP_GPU_MAX_MB:.0f} Mo",
                ),
            )
        if self.mode == "cpu":
            return (
                (
                    "swap ≤0.5 Go",
                    (MESURE_VM_SWAP,),
                    self.vm_swap_mb <= SWAP_CPU_MAX_MB,
                    f"{self.vm_swap_mb:.0f} Mo ≤ {SWAP_CPU_MAX_MB:.0f} Mo",
                ),
            )
        # BRANCHE PAR DÉFAUT : c'est le mode `spike` du worker T2I, et il applique
        # les CINQ portes. La porte « VRAM ≤ free-0.5 » manquait ici alors qu'elle
        # est la seule à confronter le pic à ce que la carte a RÉELLEMENT de libre
        # au moment du run : sans elle, un pic de 7 Go passait sur une carte qui
        # n'en offrait que 6,5. Elle est la même expression que dans `full_gpu_*`.
        return (
            (
                "RAM anon ≤3 Go",
                (MESURE_RAM_ANON,),
                self.rss_anon_mb <= RAM_ANON_PLEIN_MB,
                f"{self.rss_anon_mb:.0f} Mo ≤ {RAM_ANON_PLEIN_MB:.0f} Mo",
            ),
            (
                "VRAM ≤8.5 Go",
                (MESURE_VRAM,),
                self.vram_peak_mb <= VRAM_PIC_PLEIN_MB,
                f"{self.vram_peak_mb:.0f} Mo ≤ {VRAM_PIC_PLEIN_MB:.0f} Mo",
            ),
            (
                "VRAM ≤ free-0.5",
                (MESURE_VRAM, MESURE_VRAM_LIBRE),
                vram_libre_ok,
                detail_libre,
            ),
            (
                "swap ≤0.16 Go",
                (MESURE_VM_SWAP,),
                self.vm_swap_mb <= SWAP_GPU_MAX_MB,
                f"{self.vm_swap_mb:.0f} Mo ≤ {SWAP_GPU_MAX_MB:.0f} Mo",
            ),
            (
                "RssFile ~baseline",
                (MESURE_RSS_FILE,),
                self.rss_file_mb < RSS_FILE_BASE_MB,
                f"{self.rss_file_mb:.0f} Mo < {RSS_FILE_BASE_MB:.0f} Mo",
            ),
        )

    def portes(self) -> tuple[Porte, ...]:
        """Les portes du mode, avec leur verdict ET la cause d'un verdict impossible."""
        return tuple(self._porter(*critere) for critere in self._criteres())

    def _porter(
        self, nom: str, mesures: tuple[str, ...], franchie: bool, detail: str
    ) -> Porte:
        for mesure in mesures:
            for nom_mesure, cause in self.mesures_absentes:
                if nom_mesure == mesure:
                    # MESURE ABSENTE ⇒ PORTE FAUSSE. Jamais « on ne sait pas,
                    # donc ça ne peut pas être grave » : c'est l'inverse.
                    return Porte(nom, mesures, False, detail, raison=cause)
        return Porte(nom, mesures, franchie, detail)

    def gates_ok(self) -> dict[str, bool]:
        """Verdict par porte. Une porte non mesurable est FAUSSE, jamais un vert par défaut."""
        return {porte.nom: porte.franchie for porte in self.portes()}

    def exiger_conforme(self, *, etape: str = "") -> "RunRecord":
        """REFUSE le run si une porte est franchie ou n'a pas pu être jugée.

        C'est l'appelant qui bloque, et il bloque ICI, avant de charger 5,7 Go de
        poids : un refus au démarrage coûte une seconde, un refus après une
        diffusion minuteuse coûte l'image entière.
        """
        fautives = tuple(porte for porte in self.portes() if not porte.franchie)
        if not fautives:
            return self
        raise PorteMemoireFranchie(self._refus(etape, fautives))

    def _refus(self, etape: str, fautives: tuple[Porte, ...]) -> str:
        """Le refus, avec le diagnostic complet : portes fautives ET causes.

        Les causes des mesures absentes sont listées une fois chacune, avec les
        lignes du message d'outil conservées : `exiger_nvidia_smi` y écrit les
        emplacements examinés et le remède, et tronquer ça reviendrait à
        substituer notre propre texte à celui qui sait.
        """
        ou = f" au {etape}" if etape else ""
        causes = "".join(
            f"\n    - {nom} : {ligne}"
            for nom, raison in self.mesures_absentes
            for ligne in raison.splitlines()
        )
        return "\n".join(
            [
                f"[PORTES_MEMOIRE] Budget 8 Go non établi{ou} sur « {self.mode or 'mode inconnu'} » "
                f"({self.backend or 'backend inconnu'}) : le pipeline image est refusé.",
                f"  Mesures : RssAnon {self.rss_anon_mb:.0f} Mo | RssFile {self.rss_file_mb:.0f} Mo "
                f"| VmSwap {self.vm_swap_mb:.0f} Mo | VRAM pic {self.vram_peak_mb:.0f} Mo "
                f"| VRAM libre {self.free_vram_gb:.2f}/{self.vram_total_gb:.2f} Go",
                "  Portes :",
                *(str(porte) for porte in fautives),
                f"  Convention : {CONVENTION_MESURE}",
                "  Remède : deux causes distinctes, deux remèdes distincts. Si la mémoire "
                "est occupée par un autre run, libérez-la et relancez. Si elle est RETENUE "
                "par ce run (poids qui ne sont pas rendus), c'est un défaut de libération : "
                "la ligne « PIC SCENARIO » du journal nomme l'étape qui l'a gardée, et le "
                "remède est de corriger cette étape — jamais de baisser le seuil pour faire "
                "passer la mesure.",
            ]
        ) + (f"\n  Mesures absentes :{causes}" if causes else "")

    def __str__(self) -> str:
        portes = self.portes()
        ok = all(porte.franchie for porte in portes)
        status = "✅ PASS" if ok else "❌ FAIL"
        absentes = (
            f" | NON MESURÉ : {', '.join(nom for nom, _ in self.mesures_absentes)}"
            if self.mesures_absentes
            else ""
        )
        extra = (
            f" | backend {self.backend} mode {self.mode} freeRAM {self.ram_free_gb:.1f}"
            if self.backend
            else ""
        )
        return (
            f"[RunRecord] {status} | RssAnon {self.rss_anon_mb:.0f} MB | RssFile {self.rss_file_mb:.0f} MB "
            f"| Swap {self.vm_swap_mb:.0f} | VRAM pic {self.vram_peak_mb:.0f} MB "
            f"| total convention {self.ram_mb:.0f} MB | VRAM libre {self.free_vram_gb:.2f} Go{extra}{absentes} "
            f"| gates {self.gates_ok()}"
        )


def _mesurer_ram(absentes: list[tuple[str, str]]) -> dict[str, float]:
    """Les trois mesures `/proc`, ou trois absences motivées."""
    try:
        return mempwatch.lire_memoire_ram()
    except MesureNonDisponible as exc:
        for nom in (MESURE_RAM_ANON, MESURE_RSS_FILE, MESURE_VM_SWAP):
            absentes.append((nom, str(exc)))
        return {}


def _mesurer_vram_peak(
    vram_peak_mb: float | None, absentes: list[tuple[str, str]]
) -> float:
    if vram_peak_mb is not None:
        return vram_peak_mb
    try:
        return mempwatch.lire_vram_reservee_mb()
    except MesureNonDisponible as exc:
        absentes.append((MESURE_VRAM, str(exc)))
        return 0.0


def _vram_alloue_mb() -> float:
    """VRAM *allouée* en Mo — AFFICHAGE seul, aucune porte ne s'y fie.

    Distinguée du pic (`max_memory_reserved`) parce que les deux ne répondent pas
    à la même question ; confondre les deux ferait baisser le pic au moment où
    le cacheur relâche, ce qui est précisément l'instant où il ne faut rien
    relâcher.
    """
    try:
        import torch
    except Exception:
        return 0.0
    if not torch.cuda.is_available():
        return 0.0
    return float(torch.cuda.memory_allocated()) / MIB


def _cause_vram_libre(cause: str, dev) -> str:
    """La cause de l'absence de nvidia-smi, dite pour CETTE machine.

    Sur ROCm il n'y a pas de nvidia-smi, et les portes sont calibrées dessus :
    l'absence est donc un fait de calibration, pas une panne du poste. Le dire
    évite un remède qui ne fonctionnerait pas.
    """
    if dev is not None and getattr(dev, "backend", "") == "rocm":
        return (
            f"{cause} Cette machine expose « rocm » : les portes mémoire sont calibrées "
            "sur nvidia-smi, donc la porte de VRAM libre n'a pas d'outil étalonné ici."
        )
    return cause


__all__ = [
    "CONVENTION_MESURE",
    "MIB",
    "MesureNonDisponible",
    "Porte",
    "PorteMemoireFranchie",
    "PicMemoire",
    "RunRecord",
    "mesurer",
    "MesurePhase",
    "mesurer_phase",
    "pic_simultane_ram_vram",
]


class MesurePhase:
    def __init__(self, nom):
        self.nom = str(nom)

    def __enter__(self):
        self.debut = RunRecord.capture(dev=None, mode=f"{self.nom}:debut")
        return self

    def __exit__(self, type_exc, exc, tb):
        self.fin = RunRecord.capture(dev=None, mode=f"{self.nom}:fin")
        return False


def mesurer_phase(nom):
    return MesurePhase(nom)


def pic_simultane_ram_vram(*, vram_peak_mb=None):
    absentes = []
    ram = _mesurer_ram(absentes)
    vram_peak = _mesurer_vram_peak(vram_peak_mb, absentes)
    rss_anon = ram.get(MESURE_RAM_ANON)
    cumul = rss_anon + vram_peak if rss_anon is not None else None
    return {
        "rss_anon_mb": rss_anon,
        "vram_peak_mb": vram_peak,
        "cumul_mb": cumul,
    }
