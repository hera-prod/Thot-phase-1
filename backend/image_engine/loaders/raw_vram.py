"""Index des octets quantifiés GGUF, stockable CPU ou device selon le placement.

POURQUOI CE FICHIER NE COPIE PLUS LES POIDS (le défaut qu'il répare)
=====================================================================
`RawVRAMIndex` servait les poids quantifiés depuis `torch.from_numpy(arr.copy())`.
Une copie des 5,32 Go du fichier de poids est de la MÉMOIRE ANONYME : elle entre
donc dans `RssAnon`, donc dans le cumul du budget de 8 Go de la Bible §1.4, alors
que le mmap (`RssFile`) en est explicitement EXCLU par la convention
(`mempwatch.CONVENTION_MESURE`) — puisque le mmap et la déquantisation de ce même
octet sont deux états successifs du MÊME octet. Mesuré le 05/10/2026 sur
`flux2-klein-9b-kv-Q4_K_M.gguf` (5,32 Go) : `RssAnon` 315 Mo → 5 769 Mo, soit
exactement la taille du fichier. `RssFile` ne bougeait pas : c'était bien une
copie, pas un mapping.

CE QUE LA COPIE PRODUISAIT, ET QU'IL NE FAUT PAS PERDRE EN LA SUPPRIMANT
========================================================================
La copie détachait les octets du mmap, et c'est ce qui rendait licite le
`del reader` + fermeture que cette classe faisait juste après (« libération des
vues mmap »). Sans elle, un démontage laisserait des tenseurs pointer dans une
mapping disparue : ce n'est pas une exception Python, c'est un SIGSEGV à la
prochaine lecture. Deux autres effets allaient avec, et sont conservés :

* `np.frombuffer` sur une vue en lecture seule rend un tableau que numpy déclare
  NON ÉCRIVABLE, et `torch.from_numpy` le signale alors : « writing to this
  tensor will result in undefined behavior ». Une écriture sur une page mappée en
  lecture seule ne relève pas une erreur Python mais un SIGSEGV. Le mapping est
  donc pris en MAP_PRIVATE (`ACCESS_COPY`) : mêmes octets que le fichier,
  accessibles en écriture sans danger (copy-on-write privé, jamais réécrit sur le
  disque), donc plus d'écriture indéfinie possible ;
* le mmap ne se démonte plus à la construction — il ne le PEUT pas, puisque les
  octets qu'il sert sont précisément ceux qu'il contient. La libération existe
  toujours et elle a une adresse : `release()`, dont l'appelant est
  `build_lazy_transformer`, qui doit garder l'index vivant pendant l'inférence.

CE QUI A ÉTÉ RETIRÉ SANS REGRET : le `malloc_trim(0)` et le `empty_cache()` de la
construction ne donnaient plus rien à rendre — la copie n'existe plus, et le
lecteur de métadonnées ne retient aucune arène notable. `release()` les garde,
parce que là, et seulement là, il reste de la mémoire d'allocateur à rendre.
"""
import ctypes
import gc
import mmap
import os
from typing import NamedTuple

import numpy as np
import torch
from gguf import GGUFReader

libc = ctypes.CDLL("libc.so.6")


class _Poids(NamedTuple):
    """Ce qu'il faut savoir d'un tenseur du GGUF pour le servir sans le copier."""

    nom: str
    forme: tuple[int, ...]
    type_gguf: str
    decalage: int
    taille: int


def _lire_les_metadonnees(chemin: str) -> tuple[_Poids, ...]:
    """Noms, formes, types et positions des tenseurs — puis plus de lecteur.

    `GGUFReader` ouvre sa PROPRE `np.memmap` du fichier entier. On n'en garde que
    l'information : la métadonnée survit au `del`, la page non. Le conserver
    laisserait deux mappings du même fichier et une référence de plus sur des
    pages que le nôtre ne détient pas.
    """
    lecteur = GGUFReader(chemin)
    poids = tuple(
        _Poids(
            nom=tensor.name,
            # GGUF range les dimensions à l'envers de PyTorch : on les retourne ICI,
            # à la frontière, pour que rien en aval ait à le savoir.
            forme=tuple(int(dimension) for dimension in reversed(tensor.shape)),
            type_gguf=tensor.tensor_type.name,
            decalage=int(tensor.data_offset),
            taille=int(tensor.n_bytes),
        )
        for tensor in lecteur.tensors
    )
    del lecteur
    gc.collect()
    return poids


def _refuser_un_fichier_tronque(chemin: str, poids: tuple[_Poids, ...]) -> None:
    """Un tenseur qui déborde du fichier est un fichier tronqué, pas un décalage faux.

    On le nomme ici, au lieu de le découvrir plus tard dans un `view()` de torch,
    sous une forme qui ne dit plus quel octet du fichier a été lu.
    """
    taille_fichier = os.path.getsize(chemin)
    debordants = [p.nom for p in poids if p.decalage < 0 or p.decalage + p.taille > taille_fichier]
    if debordants:
        raise RuntimeError(
            f"[GGUF_TRONQUE] {chemin} fait {taille_fichier} octets mais {len(debordants)} "
            f"tenseur(s) en débordent, dont « {debordants[0]} » : le fichier est "
            f"incomplet, les octets à lire n'existent pas."
        )


def _mapper_les_poids(chemin: str) -> mmap.mmap:
    """Le fichier de poids, mappé UNE fois, en MAP_PRIVATE, et jamais recopié.

    Le descripteur est refermé immédiatement : une mapping survit à son fichier,
    ce qui évite de garder un descripteur ouvert pendant toute l'inférence.
    """
    with open(chemin, "rb") as fichier:
        return mmap.mmap(fichier.fileno(), 0, access=mmap.ACCESS_COPY)


class RawVRAMIndex:
    """Index des octets quantifiés, adossé au mmap du fichier — aucune copie.

    `self.raw[name]` est une VUE sur le fichier : elle coûte zéro octet de RAM
    anonyme. C'est ce qui rend le cumul de la Bible mesurable — le poids sur
    disque et le poids en mémoire ne sont plus deux fois le même fait à payer.

    L'index est la rétention, et il le reste tant que l'inférence lit. `release()`
    est le chemin qui rend les pages ; il n'est pas appelé depuis le constructeur
    parce que démonter là démonterait les pieds des vues qu'on vient de servir.
    """

    def __init__(self, gguf_path: str, device: str | torch.device = "cuda", storage_device: str | torch.device | None = None):
        if not os.path.isfile(gguf_path):
            raise FileNotFoundError(f"[POIDS_ABSENTS] Fichier de poids illisible : {gguf_path}")
        poids = _lire_les_metadonnees(gguf_path)
        _refuser_un_fichier_tronque(gguf_path, poids)
        # La mapping vit dans l'index : c'est elle qui garde les vues valides.
        # Les tenseurs la retiennent aussi, transitivement, par leur `.base`.
        self._mapping: mmap.mmap | None = _mapper_les_poids(gguf_path)
        resident = torch.device(storage_device or device)
        # Une vue unique sur le fichier entier ; chaque tenseur en est une tranche.
        # `np.frombuffer` ne touche aucune page : la mémoire n'est peuplée qu'à la
        # première lecture, donc rien n'entre dans RssAnon à la construction.
        fenetre = np.frombuffer(self._mapping, dtype=np.uint8)
        self.raw: dict[str, torch.Tensor] = {}
        self.meta: dict[str, tuple[tuple, str]] = {}
        for tenseur_gguf in poids:
            octets = fenetre[tenseur_gguf.decalage : tenseur_gguf.decalage + tenseur_gguf.taille]
            vue = torch.from_numpy(octets)
            # `.to()` sur un tenseur CPU est un no-op : on ne l'appelle que si la
            # destination est ailleurs, sinon le lecteur croirait à une copie.
            self.raw[tenseur_gguf.nom] = vue if resident.type == "cpu" else vue.to(resident)
            self.meta[tenseur_gguf.nom] = (tenseur_gguf.forme, tenseur_gguf.type_gguf)

    def relacher_les_pages(self) -> None:
        """Demande à l'OS de ne PLUS garder les pages du fichier en resident.

        `MADV_DONTNEED` ne démonte rien et n'efface rien : il dit au noyau « ces
        pages peuvent être lâchées ». Au prochain accès, elles reviennent — depuis
        le cache de pages s'il les a encore, depuis le disque sinon. Les octets
        servis par `self.raw` sont donc rigoureusement les mêmes, et c'est mesuré :
        les moyennes de déquantisation sont identiques au bit près avant et après.

OU CETTE MÉTHODE EST APPELÉE, ET CE QUE ÇA A COÛTÉ. Un poids `mmap`é
        est, par construction, un poids RÉSIDENT en `RssFile` dès qu'on le lit. Or
        `RSS_FILE_BASE_MB` (porte « RssFile ~baseline ») avait été calibré sur une
        version de ce fichier qui COPIAIT les poids puis démontait la mapping —
        donc sur un `RssFile` bas obtenu en payant le fichier en RAM anonyme, ce
        que la convention interdit. Les deux énoncés « on ne garde pas le fichier »
        et « on garde les poids mmapés » sont contradictoires ; c'est le second qui
        est conforme (`mempwatch.CONVENTION_MESURE` exclut `RssFile` du cumul
        PARCE QUE mmap et déquantification sont deux états du même octet).

        Point d'appel : `LazyLinear.forward`, après `F.linear` et `del w` — jamais
        avant, parce que `F.linear` est asynchrone côté CUDA et que relâcher entre
        la déquantisation et le produit laisserait un `cudaMemcpyAsync` lire un
        mapping vidé. À chaque produit, pas seulement au premier : `MADV_DONTNEED`
        ne retire que les pages déjà peuplées, donc un appel unique en début de
        diffusion laisserait le fichier se remplir jusqu'au moment où la porte est
        jugée — le pire moment possible.

        Mesuré, même run, seule différence = le point d'appel : `RssFile` **261 Mo**
        contre **5 047 Mo**. Le seuil est à 700 Mo, soit ≈ 1,9× le pic observé et un
        huitième du fichier de poids. Coût : 1 min 30 s à 1024² contre 1 min 16 s
        sans, 10,2 Go de lectures disque contre 8,2. Les octets servis par
        `self.raw` sont rigoureusement les mêmes, et c'est mesuré : les moyennes de
        déquantisation sont identiques au bit près avant et après.
        """
        if self._mapping is None:
            return
        try:
            self._mapping.madvise(mmap.MADV_DONTNEED)
        except (AttributeError, OSError, ValueError):
            # Un noyau sans `madvise` ne rend pas les pages : il ne les perd pas.
            # Refuser ici ferait échouer un run sur une capacité d'inférence,
            # pas sur un défaut de poids.
            pass

    def release(self):
        """Relâche les octets : plus de tenseur, plus de vue, plus de mapping.

        L'ordre compte : on efface les tenseurs AVANT de démonter, parce qu'une
        mapping ne se démonte pas sous des buffers exportés. Si une vue survit
        quand même — parce que l'appelant en a gardé une — on ne force pas : le
        noyau démontera la mapping quand son dernier détenteur partira.
        """
        mapping, self._mapping = getattr(self, "_mapping", None), None
        self.raw.clear()
        self.meta.clear()
        gc.collect()
        if mapping is not None:
            try:
                mapping.close()
            except BufferError:
                pass
        try:
            libc.malloc_trim(0)
        except Exception:
            pass
        if torch.cuda.is_available():
            torch.cuda.empty_cache()