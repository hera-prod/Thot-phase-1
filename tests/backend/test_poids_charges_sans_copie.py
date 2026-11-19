"""tests/backend/test_poids_charges_sans_copie.py — Les poids restent sur disque.

CE QUE CE FICHIER PROUVE, ET POURQUOI C'EST UNE PREUVE ET NON UN CONSTAT
========================================================================
`RawVRAMIndex` servait les poids quantifiés depuis `torch.from_numpy(arr.copy())` :
une copie des 5,32 Go du fichier de poids est de la MÉMOIRE ANONYME, donc du
`RssAnon`, donc du cumul de la Bible §1.4. Mesuré le 05/10/2026 sur
`flux2-klein-9b-kv-Q4_K_M.gguf` : `RssAnon` 315 Mo → 5 769 Mo, soit exactement la
taille du fichier, tandis que `RssFile` ne bougeait pas.

La mesure se fait DANS UN ENFANT, pas dans le process de pytest : deux mesures de
`RssAnon` dans le même process se contaminent (le `RssFile` du premier mapping est
encore là quand on mesure le second), et un test qui ne prouve rien laisse passer
une régression qu'un test qui prouve quelque chose attrape. Le fichier de poids
est un GGUF jetable de quelques dizaines de Mo écrit dans `tmp_path` : aucun test
ne suppose un modèle, une carte, ni la présence de la base réelle.

LES TROIS PREUVES, ET LEUR RÔLE CHACUN :

* la RAM ANONYME ne bouge pas à l'indexation, à une fraction de la taille du
  fichier — c'est la régression visée ;
* les pages fichier, elle, AUGMENTENT quand on lit les tenseurs, et les octets
  servis sont ceux du fichier BIT POUR BIT — sans quoi « la mémoire anonyme n'a
  pas monté » pourrait aussi bien vouloir dire « les poids ont disparu » ;
* la déquantisation rend le MÊME résultat qu'avant, sinon ce serait un échange, et
  un échange de chiffres contre de la mémoire n'est pas une correction.

AUCUN TEST N'ÉCRIT SUR LA DONNÉE RÉELLE, et le fichier jetable n'est jamais
modifié : c'est vérifié.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from backend.image_engine.runner import mempwatch

RACINE = Path(mempwatch.__file__).resolve().parents[3]
MO = 1024 * 1024

#: Q8_0 = 34 octets pour 32 éléments. On fabrique les octets nous-mêmes : c'est
#: ce qui rend le test indépendant d'un encodeur quantifié, et c'est ce qui permet
#: d'affirmer « le déquantiseur rend ce qu'il rendait » sur des blocs qu'on
#: connaît par construction.
ELEMENTS_PAR_BLOC, OCTETS_PAR_BLOC = 32, 34
Q8_0 = 8

#: Un tenseur d'environ 4 Mo, huit fois : un fichier de ≈32 Mo. Assez gros pour
#: qu'une copie soit VISIBLE, assez petit pour tenir dans le conteneur d'un test.
OCTETS_PAR_TENSEUR = 4 * MO
NB_TENSEURS = 8
TAILLE_FICHIER_MO = OCTETS_PAR_TENSEUR * NB_TENSEURS / MO

#: Ce que l'indexation a le droit de coûter en RAM anonyme. Volontairement très
#: bas : une indexation qui ne copie rien n'alloue que des objets Python. Le seuil
#: est le HUITIÈME du fichier, donc une copie le rate très large.
MARGE_ANON_MB = TAILLE_FICHIER_MO / 8.0


def ecrire_un_gguf_jetable(chemin: Path, nb_tenseurs: int = NB_TENSEURS) -> list[bytes]:
    """Un GGUF VRAI (lisible par `GGUFReader`) dont on connaît chaque octet.

    On écrit des blocs Q8_0 à la main — un échelle `float16` puis 32 entiers
    signés — parce que `gguf.quants.quantize` ne sait pas encoder le Q8_0 sur
    cette version : le test ne doit pas dépendre d'une capacité de l'encodeur
    qu'on ne teste pas.
    """
    import numpy as np
    from gguf import GGUFReader, GGUFWriter
    from gguf.constants import GGMLQuantizationType

    rng = np.random.default_rng(20261005)
    n_elements = (OCTETS_PAR_TENSEUR // OCTETS_PAR_BLOC) * ELEMENTS_PAR_BLOC
    attendu: list[bytes] = []
    writer = GGUFWriter(chemin, arch="klein")
    for indice in range(nb_tenseurs):
        valeurs = rng.integers(-8, 8, size=n_elements).astype(np.int8)
        echelle = np.float16(0.05 + 0.01 * indice)
        blocs = np.empty((n_elements // ELEMENTS_PAR_BLOC, OCTETS_PAR_BLOC), dtype=np.uint8)
        blocs[:, :2] = np.frombuffer(echelle.tobytes(), dtype=np.uint8)
        blocs[:, 2:] = valeurs.reshape(-1, ELEMENTS_PAR_BLOC)
        brut = blocs.reshape(-1).copy()
        writer.add_tensor(
            f"blocs.{indice}.weight",
            brut.reshape(-1),
            # Pour un tenseur quantifié, `raw_shape` est une forme EN OCTETS : la
            # dernière dimension doit être un multiple de la taille de bloc, sinon
            # l'écrivain refuse de fabriquer une ligne qui ne tient pas.
            raw_shape=(1, brut.nbytes),
            raw_dtype=GGMLQuantizationType.Q8_0,
        )
        attendu.append(brut.tobytes())
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    # Le fichier doit être relisible par le lecteur du produit : c'est la même
    # entrée que le vrai fichier de poids, pas un imitation.
    lecteur = GGUFReader(chemin)
    assert len(lecteur.tensors) == nb_tenseurs
    del lecteur
    return attendu


# ---------------------------------------------------------------------------
# 1. LA MESURE, DANS UN ENFANT.
# ---------------------------------------------------------------------------

_ENFANT = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from backend.image_engine.runner import mempwatch
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex

    chemin = sys.argv[2]

    def lire():
        ram = mempwatch.lire_memoire_ram()
        return round(ram["rss_anon"], 1), round(ram["rss_file"], 1)

    # torch est importé AVANT la référence : son contexte fait plusieurs centaines
    # de Mo d'anonyme, et ce n'est pas le poids qu'on veut mesurer.
    import torch  # noqa: F401

    avant = lire()
    # `device="cpu"` ET `storage_device="cpu"` : cet enfant ne touche pas CUDA, donc
    # ce qu'il mesure ne dépend d'aucune carte.
    index = RawVRAMIndex(chemin, device="cpu", storage_device="cpu")
    apres_index = lire()
    # On LIT tout : c'est ce qui distingue « rien n'a été copié » de « les poids
    # ont été perdus ». Chaque tenseur est empreinté au fil de la lecture.
    empreintes = []
    for nom in sorted(index.raw):
        vue = index.raw[nom]
        empreintes.append((nom, int(vue[0]), int(vue[-1]), int(vue.sum()), int(vue.numel())))
    n_tenseurs = len(index.meta)
    # `vue` est une vue VIVANTE sur la mapping : la garder ferait échouer le
    # `close()` de `release()` avec un BufferError, et la pages resteraient résidentes
    # alors que le test mesurerait l'échec de sa propre mesure.
    del vue
    apres_lecture = lire()
    index.release()
    apres_release = lire()
    print(json.dumps({
        "avant": avant, "apres_index": apres_index,
        "apres_lecture": apres_lecture, "apres_release": apres_release,
        "n_tenseurs": n_tenseurs, "empreintes": empreintes,
    }))
""")


def _mesurer_dans_un_enfant(tmp_path: Path, chemin: Path) -> dict:
    resultat = subprocess.run(
        [sys.executable, "-c", _ENFANT, str(RACINE), str(chemin)],
        capture_output=True, text=True, timeout=600, cwd=str(tmp_path),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert resultat.returncode == 0, resultat.stderr
    return json.loads(resultat.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# 2. LES PREUVES.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="le test porte sur /proc")
def test_indexer_un_poids_de_plusieurs_mo_ne_copie_pas_dans_la_ram_anonyme(tmp_path) -> None:
    """LA PREUVE CENTRALE : l'index n'ajoute pas de mémoire anonyme.

    Ce que l'ancien `torch.from_numpy(arr.copy())` faisait, et que ce test rend
    impossible : indexer un fichier de 32 Mo y mettait 32 Mo de `RssAnon`. Le
    seuil est le huitième du fichier, donc la régression ne passerait pas « un
    peu » non plus.
    """
    chemin = tmp_path / "poids_jetables.gguf"
    ecrire_un_gguf_jetable(chemin)

    mesure = _mesurer_dans_un_enfant(tmp_path, chemin)

    avant_anon, _ = mesure["avant"]
    index_anon, _ = mesure["apres_index"]
    cout = index_anon - avant_anon
    assert cout < MARGE_ANON_MB, (
        f"l'indexation a ajouté {cout:.1f} Mo de RAM anonyme pour un fichier de "
        f"{TAILLE_FICHIER_MO:.0f} Mo (seuil {MARGE_ANON_MB:.1f} Mo) : les poids ont "
        f"été RECOPIÉS, et cette copie est due au cumul du budget 8 Go."
    )
    assert mesure["n_tenseurs"] == NB_TENSEURS


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="le test porte sur /proc")
def test_les_pages_lues_viennent_du_fichier_et_non_dune_autre_memoire(tmp_path) -> None:
    """La contre-preuve : ce n'est pas « les poids ont disparu », c'est « ils sont sur disque ».

    Une RAM anonyme stable ne prouverait rien si la lecture ne touchait rien.
    On exige donc que la LECTURE fasse monter `RssFile` de la taille du fichier,
    et que `release()` le fasse redescendre : les octets sont dans le mapping,
    le mapping se démonte, et les octets d'avant étaient bien les mêmes.
    """
    chemin = tmp_path / "poids_jetables.gguf"
    attendu = ecrire_un_gguf_jetable(chemin)

    mesure = _mesurer_dans_un_enfant(tmp_path, chemin)

    _, fichier_avant = mesure["avant"]
    _, fichier_apres_index = mesure["apres_index"]
    _, fichier_apres_lecture = mesure["apres_lecture"]
    _, fichier_apres_release = mesure["apres_release"]
    assert fichier_apres_index - fichier_avant < TAILLE_FICHIER_MO, (
        "l'indexation a déjà fait entrer le fichier en pages résidentes : elle a "
        "donc touché les octets, et RssFile ne peut plus servir de témoin"
    )
    montee = fichier_apres_lecture - fichier_apres_index
    assert montee >= 0.9 * TAILLE_FICHIER_MO, (
        f"la lecture n'a fait monter RssFile que de {montee:.1f} Mo pour "
        f"{TAILLE_FICHIER_MO:.0f} Mo de poids : les tenseurs servis ne sont pas "
        f"les pages du fichier, donc la RAM anonyme stable ne prouve rien"
    )
    assert fichier_apres_release <= fichier_avant + 0.25 * TAILLE_FICHIER_MO, (
        f"après release(), RssFile reste à {fichier_apres_release:.1f} Mo : "
        f"release() ne démonte plus la mapping, donc la libération est décorative"
    )
    # Et l'empreinte, relue, est celle du fichier écrit — pas celle d'une relique.
    for (nom, premier, dernier, somme, numel), octets in zip(mesure["empreintes"], attendu):
        assert numel == len(octets), nom
        assert (premier, dernier, somme) == (
            octets[0], octets[-1], sum(octets),
        ), f"{nom} : les octets servis par l'index ne sont pas ceux du fichier"


def test_le_fichier_de_poids_n_est_jamais_ecrit(tmp_path) -> None:
    """La promesse du MAP_PRIVATE, tenue : lire ne réécrit pas, écrire ne salit pas.

    `ACCESS_READ` aurait donné un tableau que numpy déclare non inscriptible, donc
    une écriture sur le tenseur aurait été un SIGSEGV — pas une erreur Python, un
    arrêt du process. `ACCESS_COPY` (MAP_PRIVATE) donne les mêmes octets, accessibles
    en écriture, et le fichier reste intact : c'est ce qui rend le choix sûr, sans
    payer la copie de plusieurs Go que l'Autre voie rendait obligatoire.
    """
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex

    chemin = tmp_path / "poids_jetables.gguf"
    ecrire_un_gguf_jetable(chemin)
    avant = chemin.read_bytes()

    index = RawVRAMIndex(chemin, device="cpu", storage_device="cpu")
    nom = sorted(index.raw)[0]
    vue = index.raw[nom]
    assert vue.is_contiguous() and vue.dtype.__str__() == "torch.uint8"
    vue[0] = (int(vue[0]) + 1) % 256
    assert chemin.read_bytes() == avant, "le mapping a réécrit le fichier de poids"

    index.release()


def test_release_ferme_le_mapping_et_vide_l_index(tmp_path) -> None:
    """`release()` fait ce que son nom dit — y compris quand on l'appelle deux fois.

    Le double appel est un cas réel : `transformer_lazy` libère son chargeur dans
    un `finally`, et un index libéré à tort doit rester inerte, pas lever.
    """
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex

    chemin = tmp_path / "poids_jetables.gguf"
    ecrire_un_gguf_jetable(chemin, nb_tenseurs=2)

    index = RawVRAMIndex(chemin, device="cpu", storage_device="cpu")
    assert len(index.raw) == 2 and index.meta

    index.release()

    assert index.raw == {} and index.meta == {}
    assert index._mapping is None
    index.release()


def test_relacher_les_pages_ferme_le_fichier_sans_perdre_un_octet(tmp_path) -> None:
    """`relacher_les_pages()` : le fichier cesse d'être facturé, et reste lisible.

    C'est le remède MESURÉ à la porte « RssFile ~baseline », qui ne peut pas être
    satisfaite tant que les poids restent mmapés (voir l'en-tête de `raw_vram.py`).
    On exige ici les deux propriétés qui le rendent acceptable : les octets
    survivent au relâchement, et le fichier n'a pas bougé d'un bit.
    """
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex

    chemin = tmp_path / "poids_jetables.gguf"
    ecrire_un_gguf_jetable(chemin, nb_tenseurs=2)
    avant = chemin.read_bytes()

    index = RawVRAMIndex(chemin, device="cpu", storage_device="cpu")
    nom = sorted(index.raw)[0]
    empreinte_avant = (int(index.raw[nom][0]), int(index.raw[nom][-1]), int(index.raw[nom].sum()))

    index.relacher_les_pages()

    # Relâché ne veut pas dire perdu : la vue sert toujours les mêmes octets.
    assert (int(index.raw[nom][0]), int(index.raw[nom][-1]), int(index.raw[nom].sum())) == empreinte_avant
    assert chemin.read_bytes() == avant, "relâcher les pages a modifié le fichier de poids"

    # Et c'est idempotent : un double appel, ou un appel après `release()`, ne lève pas.
    index.relacher_les_pages()
    index.release()
    index.relacher_les_pages()


def test_un_fichier_de_poids_absent_est_nomme(tmp_path) -> None:
    """Un chemin faux se dit, il ne se découvre pas au milieu de la construction."""
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex

    with pytest.raises(FileNotFoundError, match="POIDS_ABSENTS"):
        RawVRAMIndex(str(tmp_path / "absent.gguf"), device="cpu", storage_device="cpu")


# ---------------------------------------------------------------------------
# 3. LA PARITÉ : la correction n'a pas changé un chiffre.
# ---------------------------------------------------------------------------


def test_la_dequantisation_rend_toujours_le_meme_resultat(tmp_path) -> None:
    """La parité qui décide de tout : mémoire gagnée, résultats inchangés.

    Le déquantiseur GPU du produit est comparé à la référence CPU `gguf`, sur les
    MÊMES octets, via l'index. C'est ce test — et non le gain de mémoire — qui
    autorise à dire que la correction ne fait qu'échanger de la mémoire contre du
    chiffre : ici il n'y a pas d'échange, il y a zéro écart.
    """
    import numpy as np
    import torch
    import gguf
    from gguf.quants import dequantize as dequant_cpu

    from backend.image_engine.loaders.dequant_gpu import dequantize_gpu
    from backend.image_engine.loaders.raw_vram import RawVRAMIndex

    chemin = tmp_path / "poids_jetables.gguf"
    ecrire_un_gguf_jetable(chemin, nb_tenseurs=3)

    index = RawVRAMIndex(chemin, device="cpu", storage_device="cpu")
    qtype = gguf.GGMLQuantizationType.Q8_0
    for nom in sorted(index.raw):
        forme, type_gguf = index.meta[nom]
        assert type_gguf == "Q8_0"
        brut = index.raw[nom]
        reference = np.frombuffer(
            dequant_cpu(brut.numpy(), qtype).astype(np.float32).tobytes(), dtype=np.float32
        ).reshape(forme)
        obtenu = dequantize_gpu(brut, qtype, forme, dtype=torch.float32).numpy()
        ecart = float(np.abs(obtenu - reference).max())
        echelle = float(np.abs(reference).max()) + 1e-8
        assert ecart == 0.0, (
            f"{nom} : la déquantisation rend {ecart:.3e} (relatif {ecart / echelle:.3e}) "
            f"au lieu de zéro. Une correction mémoire qui change un chiffre de "
            f"sortie n'est pas une correction."
        )
    index.release()