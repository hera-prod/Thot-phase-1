"""tests/backend/test_portes_memoire.py — Les portes du budget 8 Go, PRUVÉES appliquées.

CE QUE CE FICHIER PROUVE, ET POURQUOI CE SONT DES PREUVES ET NON DES CONSTATS :

* hors `/proc`, ou sans `nvidia-smi`, les portes ÉCHOUENT et nomment leur cause.
  Avant, `get_rss_mb()` avalait l'absence de `/proc` et rendait `(0, 0, 0)` :
  `0 ≤ 3072`, `0 == 0`, `0 < 1000` sont trois verdicts FAVORABLES, donc les cinq
  portes passaient hors Linux. Un budget qui ne peut pas échouer n'est pas un
  budget ;
* une porte franchie EMPÊCHE le pipeline image de démarrer, et le test le prouve
  en observant le refus — pas en lisant le code qui l'appelle ;
* la convention RAM/VRAM ne fait pas de double compte : deux fichiers de poids de
  tailles différentes, même total ;
* `mempwatch` refuse de rapporter un chiffre qu'il n'a pas mesuré ;
* le seuil d'admission de la VRAM est DÉRIVÉ du pic mesuré, et le worker OCR est
  libéré avant la diffusion, avec un refus franc s'il ne peut pas l'être.

AUCUN TEST N'ÉCRIT SUR LA DONNÉE RÉELLE : bases, fichiers de poids et workers de
mesure vont dans `tmp_path` ou sont des enfants jetables. Aucun test ne suppose
une carte graphique, un pilote, ni la présence d'un modèle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from backend.config import paths
from backend.image_engine.runner import mempwatch
from backend.image_engine.runner.memprof import (
    MESURE_VRAM_LIBRE,
    PorteMemoireFranchie,
    RunRecord,
)

RACINE = Path(mempwatch.__file__).resolve().parents[3]
MO = 1024 * 1024


# ---------------------------------------------------------------------------
# Outillage : rendre une mesure IMPOSSIBLE, et rendre une mesure donnée.
# ---------------------------------------------------------------------------


def sans_proc(monkeypatch, tmp_path: Path) -> None:
    """Le process courant n'a pas de `/proc` — donc aucune mesure RAM.

    On désigne un `status` inexistant plutôt que de simuler une plateforme : le
    refus doit être le même sur une machine sans `/proc` ET sur un macOS, parce
    que c'est le même motif — RssAnon n'a pas d'équivalent ailleurs.
    """
    monkeypatch.setattr(
        mempwatch, "chemin_status_proc", lambda: tmp_path / "absent" / "status"
    )


def sans_nvidia_smi(monkeypatch, tmp_path: Path) -> None:
    """Le pilote NVIDIA n'est pas résolvable sur cette machine.

    On vide le PATH et on coupe les emplacements connus : c'est exactement ce que
    fait `resoudre_outil_systeme` sur un poste sans pilote. Le motif attendu vient
    alors de `paths.exiger_nvidia_smi()` — on ne l'écrit pas dans le test, on le
    vérifie.
    """
    monkeypatch.delenv(paths.VARIABLE_NVIDIA_SMI, raising=False)
    monkeypatch.setattr(paths, "EMPLACEMENTS_NVIDIA_SMI", ())
    monkeypatch.setattr(
        paths, "CHEMINS_NVIDIA_SMI", (str(tmp_path / "absent" / "nvidia-smi"),)
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin-vide"))


def vram_non_mesurable() -> float:
    raise mempwatch.MesureNonDisponible("aucun GPU CUDA visible depuis ce process")


def releve_sain(
    mode: str = "full_gpu_phased", *, vram_peak_mb: float = 7000.0
) -> RunRecord:
    """Un relevé conforme — la référence contre laquelle « tout échoue » se lit.

    Nombres SOUS les seuils du rapport, toutes mesures présentes. Sans cette
    référence, les tests de refus ne prouveraient rien : ils passeraient aussi
    bien avec des seuils cassés.
    """
    return RunRecord(
        rss_anon_mb=1200.0,
        rss_file_mb=120.0,
        vm_swap_mb=0.0,
        vram_peak_mb=vram_peak_mb,
        ram_mb=1200.0 + vram_peak_mb,
        backend="cuda",
        mode=mode,
        free_vram_gb=10.6,
        vram_total_gb=11.99,
        ram_free_gb=20.0,
    )


def releve_vram_libre(
    mode: str, *, free_vram_gb: float, vram_peak_mb: float = 7000.0
) -> RunRecord:
    """Le relevé sain dont on ne change que la VRAM libre — la porte seule en jeu."""
    return replace(
        releve_sain(mode=mode, vram_peak_mb=vram_peak_mb), free_vram_gb=free_vram_gb
    )


# ---------------------------------------------------------------------------
# 1. MESURE IMPOSSIBLE ⇒ PORTE FAUSSE. Jamais de vert par défaut.
# ---------------------------------------------------------------------------


def test_portes_saines_sont_toutes_vertes() -> None:
    """Cinq portes, cinq mesures, cinq verts. La référence."""
    portes = releve_sain().portes()

    assert [porte.nom for porte in portes] == [
        "RAM anon ≤3 Go",
        "VRAM ≤8.5 Go",
        "VRAM ≤ free-0.5",
        "swap ≤0.16 Go",
        "RssFile ~baseline",
    ]
    assert all(porte.franchie for porte in portes), portes
    assert releve_sain().exiger_conforme(etape="contrôle").gates_ok()


def test_hors_proc_les_cinq_portes_echouent(monkeypatch, tmp_path) -> None:
    """Le faux PASS d'origine, qui rendait les cinq portes vraies hors Linux.

    C'est le test à faire échouer volontairement pour prouver qu'il n'est pas
    vide : avec l'ancien `get_rss_mb()` — `except: pass`, retour `(0, 0, 0)` —
    ce relevé donnait `RssAnon 0 ≤ 3072`, `swap 0 == 0`, `RssFile 0 < RSS_FILE_BASE_MB`,
    donc `gates_ok()` plein de `True` et un `PASS` affiché. Les trois portes RAM
    doivent donc tomber, et nommer `/proc`.
    """
    sans_proc(monkeypatch, tmp_path)

    releve = RunRecord.capture(mode="full_gpu_phased", vram_peak_mb=7000.0)

    verdict = releve.gates_ok()
    assert verdict["RAM anon ≤3 Go"] is False
    assert verdict["swap ≤0.16 Go"] is False
    assert verdict["RssFile ~baseline"] is False
    assert not all(verdict.values()), "un relevé sans /proc ne peut pas être un PASS"
    causes = " ".join(raison for _, raison in releve.mesures_absentes)
    assert "status" in causes and "non mesurable" in causes
    with pytest.raises(PorteMemoireFranchie, match="PORTES_MEMOIRE"):
        releve.exiger_conforme(etape="démarrage")


def test_hors_proc_la_porte_du_mode_cpu_tombe_elle_aussi(monkeypatch, tmp_path) -> None:
    """Le cas le plus discret du défaut : une seule porte, donc un mode « vert ».

    Le mode `cpu` n'a que la porte swap ; avec `(0, 0, 0)`, elle passait. Sur une
    machine sans `/proc`, elle doit tomber comme les autres.
    """
    sans_proc(monkeypatch, tmp_path)

    releve = RunRecord.capture(mode="cpu")

    assert releve.gates_ok() == {"swap ≤0.5 Go": False}
    with pytest.raises(PorteMemoireFranchie) as echec:
        releve.exiger_conforme()
    assert "non mesurable" in str(echec.value)


def test_plateforme_non_linux_refuse_la_mesure_ram(monkeypatch) -> None:
    """Hors Linux, `/proc/self/status` n'a pas d'équivalent qui sépare l'anonime.

    `psutil.rss` et `task_info` confondent l'anonime et le fichier, donc le double
    compte que la convention exclut redeviendrait invisible. Mieux vaut un refus
    qu'une mesure qui a l'air bonne.
    """

    class _SysFaux:
        platform = "darwin"

    monkeypatch.setattr(mempwatch, "sys", _SysFaux)
    monkeypatch.setattr(mempwatch, "psutil", None)

    with pytest.raises(mempwatch.MesureNonDisponible) as echec:
        mempwatch.lire_memoire_ram()

    assert "psutil" in str(echec.value)


def test_sans_nvidia_smi_la_porte_vram_libre_echoue_avec_le_motif_du_resolveur(
    monkeypatch, tmp_path
) -> None:
    """L'absence d'outil se dit AVEC SON REMÈDE, et ne passe pas pour un 0 Go.

    Le motif attendu n'est pas recopié ici : c'est celui de
    `paths.exiger_nvidia_smi()`, repris tel quel. Un test qui recopierait le
    message validerait un double diagnostic — celui du résolveur et celui du
    propriétaire de la porte.
    """
    sans_nvidia_smi(monkeypatch, tmp_path)

    releve = RunRecord.capture(mode="full_gpu_phased", vram_peak_mb=7000.0)

    porte = next(p for p in releve.portes() if p.nom == "VRAM ≤ free-0.5")
    assert porte.franchie is False
    assert MESURE_VRAM_LIBRE in porte.mesures
    assert "MESURÉE" in (porte.raison or "")
    assert paths.VARIABLE_NVIDIA_SMI in (porte.raison or "")
    with pytest.raises(PorteMemoireFranchie) as echec:
        releve.exiger_conforme(etape="démarrage")
    # Le refus porte la porte ET sa cause : un refus qui n'explique pas quoi
    # installer se vit comme une panne.
    assert "VRAM ≤ free-0.5" in str(echec.value)
    assert "nvidia-smi" in str(echec.value)


def test_les_autres_portes_restent_jugees_quand_la_vram_libre_est_absente(
    monkeypatch, tmp_path
) -> None:
    """Une mesure absente ne contamine que la porte qui la consomme.

    Sinon le premier outil absent ferait tomber tout le budget, et le remède
    deviendrait « installez nvidia-smi » même pour un problème de RAM.
    """
    sans_nvidia_smi(monkeypatch, tmp_path)

    verdict = RunRecord.capture(mode="full_gpu_phased", vram_peak_mb=7000.0).gates_ok()

    assert verdict["RAM anon ≤3 Go"] is True
    assert verdict["VRAM ≤8.5 Go"] is True
    assert verdict["swap ≤0.16 Go"] is True
    assert verdict["VRAM ≤ free-0.5"] is False


def test_le_mode_spike_applique_la_porte_vram_libre() -> None:
    """Le worker `spike` doit opposer son pic à la VRAM réellement libre.

    La branche par défaut de `_criteres` est celle du worker T2I : elle doit donc
    porter « VRAM ≤ free-0.5 » comme les branches `full_gpu_*`. Sans elle, un pic
    de 7 000 Mo (≈ 6,84 Gio) passait sur une carte qui n'en offrait que 6,5 de
    marge. Deux relevés identiques, une seule différence — la VRAM libre.
    """
    passe = releve_vram_libre("spike", free_vram_gb=7.5)
    tombe = releve_vram_libre("spike", free_vram_gb=7.0)

    assert passe.gates_ok()["VRAM ≤ free-0.5"] is True
    assert tombe.gates_ok()["VRAM ≤ free-0.5"] is False


def test_aucune_valeur_par_defaut_ne_rend_une_porte_verte(
    monkeypatch, tmp_path
) -> None:
    """Le principe, énoncé une fois pour toutes.

    Un relevé pris sans AUCUNE mesure ne doit produire aucune porte verte,
    quel que soit son nom. C'est cette règle qui rend tous les autres refus
    crédibles.
    """
    sans_proc(monkeypatch, tmp_path)
    monkeypatch.setattr(mempwatch, "lire_vram_reservee_mb", vram_non_mesurable)
    sans_nvidia_smi(monkeypatch, tmp_path)

    releve = RunRecord.capture(mode="full_gpu_phased")

    assert releve.gates_ok() == dict.fromkeys(releve.gates_ok(), False)


# ---------------------------------------------------------------------------
# 2. LES PORTES BLOQUENT : le pipeline image refuse de démarrer.
# ---------------------------------------------------------------------------


def _spike_prepare(monkeypatch, tmp_path, *, rss_anon_mb: float) -> Path:
    """Monte le worker T2I jusqu'au préflight, la RAM anonyme étant le paramètre.

    Rien n'est chargé et rien ne touche CUDA : `_reset_peak` devient inerte,
    `build_lazy_transformer` lève, et le chargement du TOKENIZER est coupé — c'est
    la toute première chose après le préflight. Sans cette dernière coupure, le
    test « le relevé sain passe » chargerait un encodeur 8B pour de vrai, quatre
    minutes de suite, sur une machine qui a les poids.
    """
    from backend.image_engine import spike_t2i as spike
    from backend.image_engine.runtime.device_probe import DeviceInfo

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("un papillon sur une fleur", encoding="utf-8")
    sortie = tmp_path / "sortie.png"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "spike_t2i",
            "--prompt-file",
            str(prompt),
            "--width",
            "512",
            "--height",
            "512",
            "--seed",
            "1",
            "--output",
            str(sortie),
        ],
    )
    monkeypatch.setattr(
        spike,
        "probe_device",
        lambda: DeviceInfo(
            backend="cuda",
            name="test-cuda",
            total_vram_gb=11.99,
            free_vram_gb=10.6,
            unified_memory=False,
            ram_total_gb=32.0,
            ram_free_gb=20.0,
            device_index=0,
        ),
    )
    monkeypatch.setattr(
        mempwatch,
        "lire_memoire_ram",
        lambda: {
            "rss_anon": float(rss_anon_mb),
            "rss_file": 120.0,
            "vm_swap": 0.0,
        },
    )
    monkeypatch.setattr(mempwatch, "lire_vram_reservee_mb", lambda: 400.0)
    monkeypatch.setattr(mempwatch, "lire_vram_carte_gb", lambda: (10.6, 11.99, None))
    monkeypatch.setattr(spike, "_reset_peak", lambda device: None)
    monkeypatch.setattr(spike, "build_lazy_transformer", _poids_charges_interdits)
    monkeypatch.setattr("tokenizers.Tokenizer.from_file", _arret_apres_preflight)
    return sortie


def _poids_charges_interdits(*args, **kwargs):
    raise AssertionError(
        "aucun poids ne doit être chargé tant qu'une porte est franchie"
    )


def _arret_apres_preflight(*args, **kwargs):
    raise RuntimeError(
        "ARRÊT DE TEST : le préflight est passé, on n'ira pas charger les poids"
    )


def test_une_porte_franchie_empeche_le_demarrage_du_pipeline_image(
    monkeypatch, tmp_path
) -> None:
    """LA PREUVE CENTRALE : un budget non établi interdit de commencer.

    RssAnon est mesuré à 5 Go, au-dessus de la porte des 3 Go du rapport. Le
    worker doit refuser — et refuser SANS avoir chargé le transformer.
    """
    from backend.image_engine import spike_t2i as spike

    sortie = _spike_prepare(monkeypatch, tmp_path, rss_anon_mb=5000.0)

    with pytest.raises(PorteMemoireFranchie) as echec:
        spike.main()

    message = str(echec.value)
    assert "RAM anon ≤3 Go" in message
    assert "démarrage" in message
    assert "5000" in message.replace(" ", " ") or "5 000" in message
    assert not sortie.exists(), "aucune image ne doit être produite"


def test_le_preflight_laisse_passer_un_releve_sain(
    monkeypatch, tmp_path, capsys
) -> None:
    """Le refus doit venir de la porte, pas d'un `raise` oublié.

    Même montage, RAM anonyme sous le seuil : le préflight est passé et affiché,
    et le run va jusqu'au chargement du tokenizer, où le test l'arrête. Sans cette
    contre-épreuve, « le pipeline refuse » se vérifierait trivially et ne
    prouverait rien sur les seuils.
    """
    from backend.image_engine import spike_t2i as spike

    _spike_prepare(monkeypatch, tmp_path, rss_anon_mb=1200.0)

    with pytest.raises(RuntimeError, match="le préflight est passé") as echec:
        spike.main()

    assert not isinstance(echec.value, PorteMemoireFranchie)
    journal = capsys.readouterr().out
    assert "preflight" in journal and "PASS" in journal


def test_la_convention_est_ecrite_dans_le_code() -> None:
    """La convention doit être dans le code, pas dans l'intention.

    `mempwatch` en est l'unique porteur : deux fichiers qui chacun récitent la
    convention finissent par diverger, et c'est le calcul du budget qui diverge
    sans que personne ne le voie.
    """
    from backend.image_engine.runner import memprof

    assert mempwatch.CONVENTION_MESURE == memprof.CONVENTION_MESURE
    for fragment in ("RssAnon", "max_memory_reserved", "jamais psutil.rss"):
        assert fragment in mempwatch.CONVENTION_MESURE


# ---------------------------------------------------------------------------
# 3. LA CONVENTION : le total ne bouge pas avec la taille du fichier de poids.
# ---------------------------------------------------------------------------


def test_le_total_ne_compte_pas_les_pages_fichier() -> None:
    """L'arithmétique de la convention, sans machine et sans GPU.

    900 Mo de pages fichier pour 100 Mo d'anonime et 50 Mo de VRAM : le total
    retenu vaut 150 Mo. Si quelqu'un réintroduit `psutil.rss` ou `RssFile` dans
    le cumul, ce test tombe — et il tombe AVANT la mesure sur machine réelle.
    """
    suivi = mempwatch.SuiviMemoire(
        "convention",
        lire_ram=lambda: {"rss_anon": 100.0, "rss_file": 900.0, "vm_swap": 0.0},
        lire_vram=lambda: 50.0,
    )
    suivi.observer("après chargement")

    pic = suivi.pic()

    assert pic.total_mb == pytest.approx(150.0)
    assert pic.rss_file_mb == pytest.approx(900.0)
    assert "hors cumul" in str(pic)


# L'enfant mesure UN scénario dans UN process : deux mesures dans le même process
# se contaminent (le RssFile du premier mapping est encore là quand on mesure le
# second), et le test ne prouverait alors rien.
_ENFANT = textwrap.dedent("""
    import json, mmap, sys
    sys.path.insert(0, sys.argv[1])
    from backend.image_engine.runner import mempwatch

    chemin, octets = sys.argv[2], int(sys.argv[3])
    with open(chemin, "wb") as f:
        f.truncate(octets)
    with open(chemin, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        lus = 0
        for i in range(0, len(m), 4096):
            lus += m[i]
        # VRAM nulle PAR CONSTRUCTION ici : cet enfant ne teste que la
        # convention RAM, et importer torch y créerait un contexte CUDA de
        # plusieurs centaines de Mo sans rien prouver de plus.
        suivi = mempwatch.SuiviMemoire("poids", lire_vram=lambda: 0.0)
        suivi.observer("fichier de poids resident")
        pic = suivi.pic()
        print(json.dumps({
            "lus": lus,
            "rss_anon_mb": pic.rss_anon_mb,
            "rss_file_mb": pic.rss_file_mb,
            "total_mb": pic.total_mb,
        }))
""")


def _mesurer_poids_dans_un_enfant(tmp_path: Path, mo: int) -> dict:
    poids = tmp_path / f"poids_{mo}.bin"
    resultat = subprocess.run(
        [sys.executable, "-c", _ENFANT, str(RACINE), str(poids), str(mo * MO)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert resultat.returncode == 0, resultat.stderr
    return json.loads(resultat.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="le test porte sur /proc"
)
def test_doubler_le_fichier_de_poids_ne_change_pas_le_total(tmp_path) -> None:
    """LE TEST DE CONVENTION. Il prouve l'absence de double compte, sur pièces.

    Le raisonnement : un `mmap` du fichier de poids et la déquantisation de ce
    même bloc en VRAM sont deux états successifs du MÊME octet. Additionner
    `RssFile` au cumul revient donc à payer deux fois le fichier — et le budget
    échouerait PAR CONSTRUCTION, puisque la taille sur disque des poids est
    précisément ce que la Bible refuse de compter.

    Les deux moitiés de la preuve sont vérifiées, sinon le test peut passer pour
    aucune raison :
      * le fichier lu DEUX FOIS PLUS GROSSE occupe bien plus de pages fichier, ce
        qui prouve que les pages sont réellement résidentes ;
      * le total retenu ne bouge pas d'un centime.
    """
    petit = _mesurer_poids_dans_un_enfant(tmp_path, 64)
    grand = _mesurer_poids_dans_un_enfant(tmp_path, 128)

    assert petit["lus"] == 0 and grand["lus"] == 0  # fichier creux, lu page à page
    assert grand["rss_file_mb"] - petit["rss_file_mb"] == pytest.approx(
        64.0, abs=8.0
    ), (
        "les pages du fichier de poids ne sont pas résidentes : le test ne "
        "prouverait rien du double compte"
    )
    assert grand["total_mb"] == pytest.approx(petit["total_mb"], abs=1.0), (
        "doubler le fichier de poids a changé le total : les pages fichier "
        "entrent dans le cumul, donc le budget est payé deux fois"
    )


# ---------------------------------------------------------------------------
# 4. `mempwatch` : un pic, son instant, et rien de plus.
# ---------------------------------------------------------------------------


def test_mempwatch_rend_un_pic_et_son_instant_pas_un_instantane() -> None:
    """Un relevé final ne prouve rien ; le pic et son moment, si.

    Le scénario monte puis redescend : le dernier échantillon est le plus bas,
    donc un `RunRecord` pris à la fin dirait « 300 Mo » alors que le cumul a
    atteint 1 200 Mo. `mempwatch` rend le maximum ET l'étiquette de l'étape qui
    l'a atteint — c'est ce qui permet de dire « c'est l'assemblage du
    transformer qui mandate », au lieu de « ça dépasse, sans savoir où ».
    """
    etape = {"nom": ""}
    valeurs = {"entrée": 100.0, "pic": 1200.0, "sortie": 300.0}

    def lire_ram() -> dict[str, float]:
        return {"rss_anon": valeurs[etape["nom"]], "rss_file": 0.0, "vm_swap": 0.0}

    suivi = mempwatch.SuiviMemoire(
        "assemblage", lire_ram=lire_ram, lire_vram=lambda: 0.0
    )
    for nom in ("entrée", "pic", "sortie"):
        etape["nom"] = nom
        suivi.observer(nom)

    pic = suivi.pic()

    assert pic.instant == "pic"
    assert pic.rss_anon_mb == pytest.approx(1200.0)
    assert pic.echantillons == 3


def test_mempwatch_refuse_de_rapporter_un_pic_quil_n_a_pas_mesure() -> None:
    """Aucun échantillon, aucun chiffre. Jamais 0, jamais « environ 0 »."""
    with pytest.raises(mempwatch.MesureNonDisponible) as echec:
        mempwatch.SuiviMemoire("jamais mesuré").pic()

    assert "aucun pic rapporté" in str(echec.value)


def test_mempwatch_refuse_un_pic_ou_la_mesure_a_echoue() -> None:
    """Une source muette donne un relevé INCOMPLET, et le cumul se refuse.

    Le point délicat : ici la VRAM serait 0,0 Go (pas de GPU dans l'environnement
    du test). Additionner l'anonime à ce zéro donnerait un total qui a l'air
    complet — c'est exactement le mensonge du `(0, 0, 0)` qu'on répare ailleurs.
    """
    suivi = mempwatch.SuiviMemoire(
        "ram seule",
        lire_ram=lambda: {"rss_anon": 800.0, "rss_file": 400.0, "vm_swap": 0.0},
        lire_vram=vram_non_mesurable,
    )
    suivi.observer("après chargement")

    pic = suivi.pic()

    assert pic.rss_anon_mb == pytest.approx(800.0)
    assert pic.vram_mb is None
    assert pic.complet is False
    assert [nom for nom, _ in pic.mesures_absentes] == ["vram"]
    with pytest.raises(mempwatch.MesureNonDisponible) as echec:
        _ = pic.total_mb
    assert "vram" in str(echec.value)


def test_mempwatch_refuse_un_scenario_sans_nom() -> None:
    """Un pic qu'on ne peut pas nommer n'est comparable à aucun autre."""
    with pytest.raises(ValueError):
        mempwatch.SuiviMemoire("  ")


def test_mempwatch_refuse_un_instant_sans_nom() -> None:
    """Un échantillon sans instant ne dit pas QUAND le pic a eu lieu."""
    with pytest.raises(ValueError):
        mempwatch.SuiviMemoire("scénario").observer("")


# ---------------------------------------------------------------------------
# 5. Le seuil d'admission, et la libération de l'OCR avant la diffusion.
# ---------------------------------------------------------------------------


def test_seuil_vram_derive_du_pic_mesure() -> None:
    """Le seuil doit être le pic MESURÉ plus la marge que la porte applique déjà.

    L'invariant vérifié ici est la FORMULE, pas un chiffre : `MIN_FREE ==
    PIC + MARGE`. Le littéral qui suit est le pic que le banc a mesuré le 05/10/2026
    à 1024² sur ce code (`torch.cuda.max_memory_reserved()` = 3 134 Mo, deux rejets
    identiques) ; il était à 7,9 Go, valeur reprise de `models/README.md` et
    reprodite par aucun run de ce dépôt. Un seuil qui n'est adossé à aucune mesure
    ne mérite pas d'être figé : s'il change, ce test doit changer avec la mesure,
    pas la contestantsur un souvenir.

    Plus haut que le pic + marge, l'admission contredirait la porte ; plus bas, elle
    admettrait des cartes qui finiraient en OOM.
    """
    from backend.services import flux_spike

    assert flux_spike.MIN_FREE_VRAM_GB == pytest.approx(
        flux_spike.PIC_VRAM_MESURE_GB + flux_spike.MARGE_ADMISSION_GB
    )
    assert flux_spike.PIC_VRAM_MESURE_GB == pytest.approx(3.06, abs=0.05)
    assert flux_spike.MIN_FREE_VRAM_GB < 9.0


def test_le_refus_de_vram_dit_d_ou_vient_le_seuil(monkeypatch) -> None:
    """Le refus doit être actionnable : il nomme la mesure dont il déduit.

    Un seuil écrit en dur dans un refus se conteste sans preuve ; « pic mesuré
    3,1 Go + 0,5 Go de marge » se discute sur la mesure.
    """
    from backend.services import flux_spike

    monkeypatch.setattr(
        flux_spike,
        "probe",
        lambda: (True, {"cuda": True, "free_vram_gb": 0.5, "files": {}}),
    )

    ok, raison = flux_spike.check_vram()

    assert ok is False
    assert "pic mesuré" in raison
    assert f"{flux_spike.MIN_FREE_VRAM_GB:.1f}" in raison


def test_le_worker_ocr_est_libere_avant_la_sonde_de_marge(monkeypatch) -> None:
    """L'ordre est la correction : libérer, PUIS sonder.

    Sonder d'abord mesurerait l'occupant (≈ 1,9 Go du worker) au lieu du besoin —
    l'erreur déjà commise pour le LLM, et laissée ouverte pour l'OCR.
    """
    from backend.services import image_service as svc

    etapes: list[str] = []
    monkeypatch.setattr(svc, "check_install", lambda: (True, "stub"))
    monkeypatch.setattr(
        svc, "check_vram", lambda: (etapes.append("sonde"), (False, "arrêt ici"))[1]
    )
    monkeypatch.setattr(
        svc,
        "_rendre_la_vram_du_worker_ocr",
        lambda: (etapes.append("libération"), "")[1],
    )

    with pytest.raises(RuntimeError, match="OUT_OF_MEMORY"):
        svc.generate(None, "run-ordre", "un chat")

    assert etapes == ["libération", "sonde"]


def test_generation_refusee_si_le_worker_ocr_ne_se_libere_pas(monkeypatch) -> None:
    """Pas de « on diffuse et on verra » : un occupant qu'on ne connaît pas, on refuse.

    Le refus ne repose plus sur une addition cumulée — depuis le 05/10/2026 le pic
    de diffusion mesuré est 3,06 Go, donc 1,9 + 3,06 + 0,5 tient sous les 8 Go. Il
    repose sur ce qui reste vrai quand l'addition passe : « un modèle lourd en VRAM
    à la fois » est une RÈGLE de séquence, et un worker qui survit à SIGKILL est un
    occupant dont personne ne connaît la taille. Diffuser quand même produirait une
    image non conforme ; on refuse, et on dit pourquoi.
    """
    from backend.ingestion import paddle_client
    from backend.services import flux_spike
    from backend.services import image_service as svc

    monkeypatch.setattr(svc, "check_install", lambda: (True, "stub"))
    monkeypatch.setattr(svc, "check_vram", lambda: (True, "stub"))

    def liberation_bousillee() -> paddle_client.RapportLiberation:
        raise paddle_client.LiberationImpossible("worker PaddleOCR-VL survit à SIGKILL")

    monkeypatch.setattr(paddle_client, "liberer_pour_diffusion", liberation_bousillee)
    diffuse: list[bool] = []
    monkeypatch.setattr(flux_spike, "run_t2i", lambda *a, **k: diffuse.append(True))

    with pytest.raises(RuntimeError) as echec:
        svc.generate(None, "run-refus", "un chat")

    assert "SERVICE_UNAVAILABLE" in str(echec.value)
    assert "SIGKILL" in str(echec.value)
    assert not diffuse, "rien ne doit être lancé après un refus de libération"


def test_generation_refusee_quand_une_ingestion_ocr_tient_le_bail(monkeypatch) -> None:
    """Le cas difficile, au niveau du service : le bail OCR tenu refuse l'image.

    L'exclusion mutuelle vit au `ResourceCoordinator` ; le service ne fait que la
    traduire en refus lisible. On ne simule pas le texte de l'erreur : on fait
    lever la VRAIE `ResourceBusy`, comme le fait un bail « ocr » tenu.
    """
    from backend.inference import resource_coordinator
    from backend.services import image_service as svc

    def refuse_apres_attente(*args, **kwargs):
        raise resource_coordinator.ResourceBusy("RESOURCE_BUSY:ocr")

    monkeypatch.setattr(svc, "check_install", lambda: (True, "stub"))
    monkeypatch.setattr(
        resource_coordinator,
        "get_resource_coordinator",
        lambda: type(
            "CoordinateurOccupe", (), {"acquire": staticmethod(refuse_apres_attente)}
        )(),
    )

    with pytest.raises(RuntimeError) as echec:
        svc.generate(None, "run-bail", "un chat")

    assert "SERVICE_UNAVAILABLE" in str(echec.value)
    assert "tâche de fond" in str(echec.value)
    assert "Réessayez" in str(echec.value)


def test_liberer_pour_diffusion_arrete_le_worker_proprement(monkeypatch) -> None:
    """Un `terminate` propre, et le rapport qui dit ce qui a été fait.

    On mesure ici le MÉCANISME, pas la VRAM : un vrai worker Paddle est hors de
    portée d'un test sans modèle, mais le signal envoyé et la mort du process
    s'observent sur n'importe quel enfant Python.
    """
    from backend.ingestion import paddle_client

    enfant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        monkeypatch.setattr(paddle_client, "_proc", enfant)
        rapport = paddle_client.liberer_pour_diffusion(timeout_s=1.0)

        assert rapport.worker_etait_vivant is True
        assert rapport.arret_propre is True, (
            "un worker vivant part sur SIGTERM, pas sur SIGKILL"
        )
        assert enfant.poll() is not None
        assert "SIGTERM" in str(rapport)
    finally:
        if enfant.poll() is None:  # pragma: no cover — filet de sécurité
            enfant.kill()
            enfant.wait(timeout=5)


def test_liberer_pour_diffusion_refuse_un_worker_qui_travaille() -> None:
    """Un worker occupé n'est PAS tué, et le refus nomme la cause.

    « Rien ne s'est passé » est le pire des verdicts pour quelqu'un qui a lancé
    une génération ; le motif doit donc dire ce qui bloque et quoi en faire.
    """
    from backend.ingestion import paddle_client

    verrou = paddle_client._lock
    verrou.acquire()
    try:
        with pytest.raises(paddle_client.WorkerOccupe) as echec:
            paddle_client.liberer_pour_diffusion(timeout_s=0.05)
    finally:
        verrou.release()

    assert "ingestion OCR est en cours" in str(echec.value)
    assert "l'emporte" in str(echec.value)


# ---------------------------------------------------------------------------
# 6. LA PORTE `RssFile` : RECALIBRÉE SUR UNE MESURE, ET ELLE MORD ENCORE.
# ---------------------------------------------------------------------------

# LES DEUX MESURES dont le seuil sort. Elles ne sont pas des bornes choisies pour
# faire passer : ce sont les deux `RssFile` du BILAN d'un run image réel identique
# (RTX 4070 SUPER, `flux2-klein-9b-kv-Q4_K_M`, 4 pas, 1024×1024, 06/10/2026), la
# seule différence étant le point d'appel de `RawVRAMIndex.relacher_les_pages()` :
#
#     point d'appel ACTIF   → RssFile  261 Mo, cinq portes vertes, PNG produit
#     point d'appel RETIRÉ  → RssFile 5 047 Mo, porte franchie, PNG EFFACÉ
#
# La valeur sans relâchement n'est pas une constante de physique : un second run a
# donné 5 275 Mo. Elle dépend de l'état du cache de pages, pas du code, et c'est
# justement pourquoi le seuil est posé à 700 Mo — très en dessous des DEUX, et très
# au-dessus des 261.
#
# Écrire ces deux nombres ici, et non « la valeur observée », est ce qui distingue
# une porte d'un décor : si l'une des deux change, ce sont ces deux lignes qui
# doivent changer avec elle.
RSS_FILE_AVEC_RELECHEMENT_MB = 261.0
RSS_FILE_SANS_RELECHEMENT_MB = 5047.0

#: Le fichier de poids que ces mesures concernent, en Mo. C'est l'ÉCHELLE à
#: laquelle « on paie le fichier » se lit : un seuil qui n'est pas très en dessous
#: de cette taille est un seuil qu'on peut satisfaire sans rien éviter.
TAILLE_FICHIER_POIDS_MB = 5452.0


def _releve_rss_file(rss_file_mb: float) -> RunRecord:
    """Le relevé sain dont on ne change que `RssFile` — la porte seule en jeu."""
    sain = releve_sain()
    return RunRecord(
        rss_anon_mb=sain.rss_anon_mb,
        rss_file_mb=rss_file_mb,
        vm_swap_mb=sain.vm_swap_mb,
        vram_peak_mb=sain.vram_peak_mb,
        ram_mb=sain.ram_mb,
        backend=sain.backend,
        mode=sain.mode,
        free_vram_gb=sain.free_vram_gb,
        vram_total_gb=sain.vram_total_gb,
        ram_free_gb=sain.ram_free_gb,
    )


def test_le_seuil_rss_file_encadre_les_deux_mesures_et_ignore_le_fichier() -> None:
    """La forme du seuil : entre les deux mesures, et loin du fichier.

    Trois exigences, chacune avec sa raison :

    * `RSS_FILE_BASE_MB` doit être PLUS GRAND que la mesure avec relâchement —
      sinon il juge le comportement correct comme un défaut, et le premier import
      supplémentaire fera tomber un run conforme ;
    * il doit être PLUS PETIT que la mesure sans relâchement — c'est là qu'il
      mord, et sans ça c'est un seuil qui ne détecte plus rien ;
    * il doit être TRÈS EN DESSOUS de la taille du fichier (ici au plus un quart) —
      sinon il pourrait être satisfait en payant le fichier, et une porte qui se
      laisse satisfaire par le défaut qu'elle traque n'est pas une porte.
    """
    from backend.image_engine.runner.memprof import RSS_FILE_BASE_MB

    assert RSS_FILE_BASE_MB > RSS_FILE_AVEC_RELECHEMENT_MB
    assert RSS_FILE_BASE_MB < RSS_FILE_SANS_RELECHEMENT_MB
    assert RSS_FILE_BASE_MB <= TAILLE_FICHIER_POIDS_MB / 4, (
        f"{RSS_FILE_BASE_MB:.0f} Mo est trop près des {TAILLE_FICHIER_POIDS_MB:.0f} Mo "
        "du fichier de poids : payer le fichier passerait la porte"
    )


def test_un_rssfile_eleve_fait_encore_echouer_le_bilan() -> None:
    """LA CONTRE-ÉPREUVE : le seuil recalibré n'a pas cessé de mordre.

    On rejoue la MESURE obtenue sans le point d'appel — 5 047 Mo — dans un relevé
    par ailleurs parfaitement sain. Seule la porte `RssFile` doit tomber, et le
    refus doit nommer la porte ET sa valeur. Sans ce test, « le seuil vient d'une
    mesure » ne serait qu'une intention : un seuil posé sur la valeur observée
    passe toujours, et ne prouve jamais qu'il sait échouer.
    """
    releve = _releve_rss_file(RSS_FILE_SANS_RELECHEMENT_MB)

    verdict = releve.gates_ok()
    assert verdict["RssFile ~baseline"] is False
    # Une seule porte tombe : les quatre autres ne doivent pas être contaminées
    # par le défaut d'une seule mesure.
    assert all(vrai for nom, vrai in verdict.items() if nom != "RssFile ~baseline")

    with pytest.raises(PorteMemoireFranchie) as echec:
        releve.exiger_conforme(etape="bilan")
    message = str(echec.value)
    assert "RssFile ~baseline" in message
    assert "5047" in message.replace(" ", " "), "le refus doit nommer la valeur fautive"


def test_le_rssfile_mesure_avec_relechement_passe() -> None:
    """L'autre moitié de la preuve : le seuil ne mord pas ce qui est conforme.

    Un seuil recalibré trop bas est aussi un seuil menteur — il ferait jeter des
    images bonnes. On rejoue donc la mesure du run réellement passé (261 Mo).
    """
    releve = _releve_rss_file(RSS_FILE_AVEC_RELECHEMENT_MB)

    assert releve.gates_ok()["RssFile ~baseline"] is True
    assert releve.exiger_conforme(etape="bilan").gates_ok()["RssFile ~baseline"] is True


# ---------------------------------------------------------------------------
# 6 bis. LA PORTE SWAP GPU : CALIBRÉE ENTRE LE RUN SAIN ET LE DÉFAUT CONNU.
# ---------------------------------------------------------------------------

# LES DEUX MESURES dont le seuil sort. Ce ne sont pas des bornes choisies pour
# faire passer : la première est le run conforme q-r du 01/11/2026, refusé par
# l'ancienne porte binaire `swap =0` ; la seconde est le plus petit swap d'un
# VRAI défaut mesuré dans la référence (double allocation, 23 Go de RssAnon).
SWAP_RUN_SAIN_MB = 114.0
SWAP_DEFAUT_MESURE_MB = 231.0

#: Les modes GPU qui portent la porte swap. `spike` est la branche par défaut du
#: worker T2I ; les deux autres sont ses variantes pleine carte et hybride. Une
#: contre-épreuve qui ne couvrirait que la variante laisserait deux branches
#: libres de dériver — or c'est la même constante qui les juge.
MODES_SWAP = ("spike", "hybrid", "full_gpu_phased")


def _releve_swap(vm_swap_mb: float, mode: str = "full_gpu_phased") -> RunRecord:
    """Le relevé sain dont on ne change que `VmSwap` — la porte seule en jeu."""
    sain = releve_sain(mode=mode)
    return replace(sain, vm_swap_mb=vm_swap_mb)


def test_le_seuil_swap_encadre_le_run_sain_et_le_defaut_connu() -> None:
    """La forme du seuil : strictement entre 114 Mo et 231 Mo.

    Trop haut, il laisse passer le défaut réellement mesuré ; trop bas, il rejette
    le run conforme. Les deux erreurs sont aussi graves, et ce test les nomme.
    """
    from backend.image_engine.runner.memprof import SWAP_GPU_MAX_MB

    assert SWAP_RUN_SAIN_MB < SWAP_GPU_MAX_MB < SWAP_DEFAUT_MESURE_MB, (
        f"{SWAP_GPU_MAX_MB:.0f} Mo est hors de l'intervalle "
        f"({SWAP_RUN_SAIN_MB:.0f}, {SWAP_DEFAUT_MESURE_MB:.0f}) : un seuil hors de "
        "cet intervalle soit laisse passer le défaut connu, soit rejette le run sain"
    )


@pytest.mark.parametrize("mode", MODES_SWAP)
def test_un_swap_de_114_mo_laisse_passer_le_bilan(mode: str) -> None:
    """La contre-épreuve du run conforme : 114 Mo ne font plus tomber la porte."""
    releve = _releve_swap(SWAP_RUN_SAIN_MB, mode)

    assert releve.gates_ok()["swap ≤0.16 Go"] is True
    assert releve.exiger_conforme(etape="bilan").gates_ok()["swap ≤0.16 Go"] is True


@pytest.mark.parametrize("mode", MODES_SWAP)
def test_un_swap_de_231_mo_fait_encore_echouer_le_bilan(mode: str) -> None:
    """LA CONTRE-ÉPREUVE du défaut : le seuil calibré n'a pas cessé de mordre.

    Le swap du défaut connu tombe, et LUI SEUL : les autres portes ne doivent pas
    être contaminées par le défaut d'une seule mesure.
    """
    releve = _releve_swap(SWAP_DEFAUT_MESURE_MB, mode)

    verdict = releve.gates_ok()
    assert verdict["swap ≤0.16 Go"] is False
    assert all(vrai for nom, vrai in verdict.items() if nom != "swap ≤0.16 Go")

    with pytest.raises(PorteMemoireFranchie) as echec:
        releve.exiger_conforme(etape="bilan")
    message = str(echec.value)
    assert "swap ≤0.16 Go" in message
    assert "231" in message.replace(" ", ""), "le refus doit nommer la valeur fautive"


def test_la_borne_du_seuil_swap_est_incluse() -> None:
    """`≤` et non `<` : 160 Mo passe, 161 Mo tombe."""
    from backend.image_engine.runner.memprof import SWAP_GPU_MAX_MB

    assert _releve_swap(SWAP_GPU_MAX_MB).gates_ok()["swap ≤0.16 Go"] is True
    assert _releve_swap(SWAP_GPU_MAX_MB + 1.0).gates_ok()["swap ≤0.16 Go"] is False


# ---------------------------------------------------------------------------
# 7. LE POINT D'APPEL : ce qui fait passer la porte, et ce qui la fait tomber.
# ---------------------------------------------------------------------------


class _IndexFactice:
    """Un `RawVRAMIndex` de banque, sans fichier, sans carte, sans Go.

    On ne teste pas ici la mémoire — cela se mesure dans un enfant, sur le fichier
    réel (`test_poids_charges_sans_copie.py`). On teste l'ORDRE des opérations dans
    `LazyLinear.forward`, qui est la seule chose qui décide si les pages sont
    relâchées avant ou après le produit dont elles portent les octets.
    """

    def __init__(self, forme: tuple[int, ...]) -> None:
        import torch

        self.meta = {"blocs.0.weight": (forme, "Q8_0")}
        self.raw = {"blocs.0.weight": torch.zeros(16, dtype=torch.uint8)}
        self.relechements = 0

    def relacher_les_pages(self) -> None:
        self.relechements += 1


def _lazy_linear_de_banc(monkeypatch):
    """Un `LazyLinear` CPU dont la déquantisation est un tenseur float fixé.

    `dequantize_gpu` est remplacé PARCE QUE son comportement est mesuré ailleurs
    (parité bit à bit contre la référence `gguf`). Ce qu'on observe ici, c'est
    l'appel du relâchement et sa position — deux choses que la déquantisation
    réelle n'observerait pas mieux, et qui coûteraient un GPU.
    """
    import torch
    from backend.image_engine.loaders import dequant_gpu
    from backend.image_engine.models.transformer import LazyLinear

    index = _IndexFactice(forme=(4,))
    poids = torch.ones(4, dtype=torch.float32)
    monkeypatch.setattr(dequant_gpu, "dequantize_gpu", lambda *a, **k: poids.clone())
    return LazyLinear(index, "blocs.0.weight"), index


def test_le_relachement_a_lieu_une_seule_fois_par_produit(monkeypatch) -> None:
    """Un produit, un relâchement. Ni avant, ni deux fois, ni jamais après.

    Le compte est la preuve que le point d'appel est branché ET qu'il est unique :
    `LazyLinear.forward` est appelé 121 fois par pas de diffusion, donc 484 fois
    par run, et un appel en double ferait 968 `MADV_DONTNEED` sur 5,45 Go.
    """
    import torch

    couche, index = _lazy_linear_de_banc(monkeypatch)

    sortie = couche(torch.ones(1, 2, 4))

    assert sortie.shape == (1, 2), "le produit doit avoir eu lieu avant le relâchement"
    assert index.relechements == 1


def test_le_relachement_vient_apres_le_produit_et_pas_avant(monkeypatch) -> None:
    """Si le calcul n'a pas produit de résultat, aucune page n'est relâchée.

    `F.linear` est asynchrone côté CUDA : relâcher les pages entre la
    déquantisation et la multiplication laisserait une copie en vol lire un
    mapping vidé. L'ordre s'observe par l'échec : on casse la multiplication, et
    on exige qu'aucun relâchement n'ait eu lieu. Un relâchement AVANT le produit
    passerait ce test ; c'est exactement ce qu'il est là pour attraper.
    """
    import torch
    from backend.image_engine.models import transformer as modele

    couche, index = _lazy_linear_de_banc(monkeypatch)

    def multiplication_cassee(*args, **kwargs):
        raise RuntimeError("[TEST] multiplication impossible")

    monkeypatch.setattr(modele.F, "linear", multiplication_cassee)

    with pytest.raises(RuntimeError, match="multiplication impossible"):
        couche(torch.ones(1, 2, 4))

    assert index.relechements == 0
