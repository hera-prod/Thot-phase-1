"""Tests de la mise en page du module spike T2I (régression NameError)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_build_cond_refs_defined_before_main_and_guard_last():
    root = Path(__file__).resolve().parents[2]
    source = (root / "backend" / "image_engine" / "spike_t2i.py").read_text(
        encoding="utf-8"
    )
    pos_refs = source.find("def build_cond_refs")
    pos_main = source.find("def main")
    pos_guard = source.find('if __name__ == "__main__":')
    assert pos_refs != -1
    assert pos_main != -1
    assert pos_guard != -1
    assert pos_refs < pos_main < pos_guard

    # Le guard __main__ est la dernière instruction non vide du fichier.
    call_index = source.find("main()", pos_guard)
    assert call_index != -1
    trailing = source[call_index + len("main()") :]
    assert trailing.strip() == ""


def test_le_bilan_mesure_avant_le_teardown_complet():
    """Le bilan doit mesurer l'ÉTAT DE FIN DE RUN, avant que le teardown ne lisse.

    Test d'ORDRE sur le source : il cible des sous-chaînes stables (nom d'appel
    et étiquette de porte), pas la mise en forme. Rendre `img, lat, vae` avant le
    verdict ferait du bilan une mesure de RÉSIDU : la VRAM libre serait gonflée
    et la porte « VRAM ≤ free−0.5 » relâcherait sa contrainte. Le teardown vit
    donc dans le `finally` du bilan — après la mesure, mais dans tous les cas.
    """
    root = Path(__file__).resolve().parents[2]
    source = (root / "backend" / "image_engine" / "spike_t2i.py").read_text(
        encoding="utf-8"
    )

    pos_bilan = source.find('rec.exiger_conforme(etape="bilan")')
    assert pos_bilan != -1

    # L'index mmap n'est plus lu après la diffusion : il est relâché avant le
    # bilan. C'est une vraie libération, pas une façon de passer la porte.
    pos_index = source.find("index.release()")
    assert pos_index != -1 and pos_index < pos_bilan

    # Le sampling rend ses tenseurs AVANT de relâcher l'index : relâcher d'abord
    # laisserait l'index vidé sous un produit encore en vol.
    pos_sampling_del = source.find("del v, taps, tr, sched")
    assert pos_sampling_del != -1 and pos_sampling_del < pos_index

    # Le teardown complet est APRÈS le bilan, et il est dans son `finally`.
    pos_del = source.find("del img, lat, vae")
    assert pos_del != -1 and pos_del > pos_bilan
    pos_finally = source.find("finally:", pos_bilan)
    assert pos_finally != -1 and pos_bilan < pos_finally < pos_del

    # Le teardown est BEST-EFFORT : il est enveloppé pour qu'une erreur de
    # nettoyage ne remplace pas la `PorteMemoireFranchie` d'origine. On vérifie
    # la sous-chaîne stable du journal, pas la mise en forme du try/except.
    pos_teardown_try = source.find("try:", pos_finally)
    assert pos_teardown_try != -1 and pos_teardown_try < pos_del
    pos_teardown_log = source.find("teardown incomplet", pos_finally)
    assert pos_teardown_log != -1 and pos_teardown_log > pos_del

    # Le `malloc_trim` du teardown accompagne donc ces libérations, après le bilan.
    pos_trim = source.rfind("rendre_la_memoire_anonyme()")
    assert pos_trim != -1 and pos_trim > pos_bilan

    # Le bilan reste une mesure de la mémoire RÉSIDUELLE : aucune porte, aucun
    # seuil de swap n'est réécrit dans le worker.
    assert "vm_swap" not in source
    assert "SWAP" not in source


def test_spike_help_smoke():
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "backend/image_engine/spike_t2i.py", "--help"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, combined
    assert "NameError" not in combined
