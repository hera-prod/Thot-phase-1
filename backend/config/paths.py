# backend/config/paths.py — chemins Phase 1 (fusion v0.8 + notes FR scaffold)
# Notes FR : garde-fou CWD aléatoire du sidecar, absolu via Path.home() / tempfile
# But : résoudre data_dir, modèles, uploads temporaires sans .env — tout hardcodé
# Contrat portable (C19 / portabilite.md) : mode portable_strict si portable.json
# présent à côté du lanceur/bundle, sinon mode installé XDG.
#
# DEUXIÈME CONTRAT, §1.5 de la Bible : « une dépendance d'exécution est résolue par
# rapport à la racine de l'application ou à son dossier de données ». La seconde
# moitié du fichier est la machine qui applique ce mot. Elle est écrite ici, et pas
# dans chaque appelant, parce qu'une règle de résolution écrite deux fois diverge
# — et une règle divergente est une règle qui n'est plus une règle.
#
# `Path.cwd()` n'apparaît QUE dans `_model_search_bases(inclure_cwd=True)`, pour les
# chemins dont le contrat historique est inchangé. Les chemins qui doivent survivre à
# un transport, eux, ne le consultent pas.
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def is_frozen() -> bool:
    """True si exécuté depuis binaire gelé PyInstaller (Thot-phase-1 sidecar)."""
    return bool(getattr(sys, "frozen", False))


def is_packaged() -> bool:
    """True si l'application est DÉPLOYÉE, gelée ou autorisée à se dire telle.

    `is_frozen()` décrit le mécanisme (PyInstaller) ; `is_packaged()` décrit la
    situation de déploiement, et c'est celle-ci qui décide si le PATH est
    admissible. Les deux se confondent en production, mais pas en dev : on lance
    aujourd'hui Electron non empaqueté, et `app.isPackaged` mentait donc sur ce
    point. `THOT_PACKAGED` est l'affirmation du lanceur, propagée dans
    l'environnement du sidecar — c'est elle qui ferme le PATH côté Python quand
    l'app tourne en production, sans dépendre de la façon dont elle a été
    construite.
    """
    return is_frozen() or os.environ.get("THOT_PACKAGED") == "1"


def get_bundle_dir() -> Path:
    """
    Répertoire du bundle (lecture seule).
    - PyInstaller onefile : sys._MEIPASS
    - PyInstaller onedir : dossier du binaire
    - Dev : racine projet (parents de backend/config/paths.py)
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _find_portable_root(bundle: Path | None = None) -> Path | None:
    """Cherche portable.json (marqueur mode portable_strict) autour du bundle/exe."""
    # Env explicite prioritaire
    if os.environ.get("THOT_PORTABLE") == "1":
        # THOT_APP_DATA_DIR ou bundle parent sert de racine
        env_root = os.environ.get("THOT_APP_DATA_DIR") or os.environ.get(
            "THOT_DATA_DIR"
        )
        if env_root:
            return (
                Path(env_root).expanduser().resolve().parent
                if Path(env_root).name == "data"
                else Path(env_root).expanduser().resolve()
            )
    candidates: list[Path] = []
    if bundle is not None:
        candidates.append(bundle)
        candidates.append(bundle.parent)
    # Le répertoire courant est VOLONTAIREMENT absent de cette liste. Il rendait le
    # dossier de données fonction du dossier depuis lequel on avait lancé
    # l'application : un `.desktop` qui démarre dans `$HOME` adoptaba `$HOME` s'il
    # contenait un `portable.json`, et la base quittait la clé USB. La racine
    # portable se cherche à partir de l'exécutable et des variables explicites,
    # jamais de là où le processus se trouve par hasard.
    if is_frozen():
        b = get_bundle_dir()
        candidates.extend([b, b.parent])
    # Exe parent si frozen
    if is_frozen():
        candidates.append(Path(sys.executable).resolve().parent)
        candidates.append(Path(sys.executable).resolve().parent.parent)
    # En dev non-portable, on vérifie quand même si THOT_DATA_DIR/THOT_APP_DATA_DIR pointe vers portable
    # (cas sidecar lancé par Electron qui a déjà résolu la racine)
    for env_key in ("THOT_APP_DATA_DIR", "THOT_DATA_DIR"):
        env_val = os.environ.get(env_key)
        if env_val:
            p = Path(env_val).expanduser()
            # Un chemin relatif n'a pas de racine : le rattacher au CWD réintroduirait
            # exactement la dépendance qu'on supprime. On l'ignore donc, et la
            # détection continue sur les candidats vérifiables.
            if not p.is_absolute():
                log.warning(
                    "%s=%s est relatif : ignoré (les chemins doivent être absolus)",
                    env_key,
                    env_val,
                )
                continue
            candidates.append(p)
            if p.name == "data":
                candidates.append(p.parent)
            else:
                candidates.append(p / "data")
    seen: set[Path] = set()
    for base in candidates:
        try:
            base = base.resolve()
        except Exception:
            continue
        if base in seen:
            continue
        seen.add(base)
        if (base / "portable.json").exists():
            return base
        # Variante : data/portable.json (ancien)
        if (base / "data" / "portable.json").exists():
            return base
    return None


def is_portable_mode() -> bool:
    """True si portable_strict détecté (portable.json ou THOT_PORTABLE=1)."""
    if os.environ.get("THOT_PORTABLE") == "1":
        return True
    # THOT_DATA_DIR injecté par Electron en portable => on considère portable
    # mais on ne peut pas le déduire sans marqueur, donc check fichier
    return _find_portable_root() is not None


def get_portable_root() -> Path | None:
    """Racine du dossier portable (contient portable.json), ou None si mode installé."""
    return _find_portable_root()


def get_app_data_dir() -> Path:
    """
    Répertoire données utilisateur.
    - Si THOT_APP_DATA_DIR env (injecté par Electron) → priorité
    - Si portable_strict (portable.json trouvé) → <racine>/data (résolution depuis lanceur, pas CWD)
    - Sinon XDG/AppData (jamais CWD)
    """
    # 1. Env Electron prioritaire (source vérité)
    env_app = os.environ.get("THOT_APP_DATA_DIR")
    if env_app:
        return Path(env_app).expanduser().resolve()
    # 2. THOT_DATA_DIR direct (fallback Electron ancien)
    env_data = os.environ.get("THOT_DATA_DIR")
    if env_data:
        p = Path(env_data).expanduser().resolve()
        # Si env pointe vers data/, remonter au parent pour cohérence app_data
        if p.name == "data":
            return p.parent
        return p
    # 3. Portable strict : dossier qui contient portable.json
    portable = _find_portable_root()
    if portable is not None:
        return portable
    # 4. Mode installé XDG
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".local" / "share"
    return base / "thot"


def get_user_data_dir() -> Path:
    """Alias sémantique de get_app_data_dir()."""
    return get_app_data_dir()


def resolve_data_dir(value: Path | str | None) -> Path:
    """Résout data_dir en absolu. None → get_app_data_dir()/data."""
    if value is None:
        # get_app_data_dir() peut déjà être la racine portable ; data s'y trouve
        app = get_app_data_dir()
        # Si app contient déjà portable.json, data est app/data
        if (app / "portable.json").exists():
            return (app / "data").resolve()
        # Si app est déjà un dossier data (cas THOT_DATA_DIR)
        if app.name == "data":
            return app.resolve()
        return (app / "data").resolve()
    p = Path(value).expanduser()
    if not p.is_absolute():
        # Jamais `Path.cwd()` : un chemin relatif se rattache à l'APPLICATION, pas
        # au dossier depuis lequel elle a été lancée. Le CWD nefert qu'à donner
        # deux valeurs au même chemin selon le terminal utilisé.
        p = get_app_data_dir() / p
    return p.resolve()


def resolve_child_path(
    value: Path | str | None, default_name: str, data_dir: Path
) -> Path:
    """Sous-chemin relatif à data_dir (uploads, etc.)."""
    candidate = Path(value) if value is not None else Path(default_name)
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = data_dir / candidate
    return candidate.resolve()


def resolve_file_path(
    value: Path | str | None, default_name: str, data_dir: Path
) -> Path:
    """Fichier relatif à data_dir (thot-phase-1.db)."""
    candidate = Path(value) if value is not None else Path(default_name)
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = data_dir / candidate
    return candidate.resolve()


# --- Helpers portables ---
def get_data_dir() -> Path:
    """Raccourci : dossier data résolu selon mode."""
    return resolve_data_dir(None)


def get_temp_dir() -> Path:
    """
    Dossier temporaire :
    - portable_strict → <racine>/data/temp (reste sur même volume, pas d'écriture hors dossier)
    - sinon → /tmp/thot (ou %TEMP%/thot sur Windows)
    """
    if is_portable_mode():
        return get_data_dir() / "temp"
    return Path(tempfile.gettempdir()) / "thot"


def get_media_dir() -> Path:
    """Dossier blobs portables : data/blobs (nouveau) avec fallback data/media."""
    # Nouveau contrat : blobs/ ; ancien : media/ — on privilégie blobs
    d = get_data_dir()
    blobs = d / "blobs"
    media = d / "media"
    # Si media existe déjà et blobs non, garder media pour migration douce
    if media.exists() and not blobs.exists():
        return media
    return blobs


# --- Phase 1 : modèles locaux (hardcodé, pas BaseSettings) ---
def _model_search_bases(inclure_cwd: bool = True) -> list[Path]:
    """Bases de recherche modèles : portable > bundle > [CWD] > app_data.

    `inclure_cwd` vaut `True` pour les chemins historiques, dont le contrat
    n'a jamais été redefini. Il ne vaut `False` que pour les modèles dont la
    résolution est fermement celle d'une application portable : là, le dossier
    courant est une valeur de trop, parce qu'il ferait trouver un modèle au
    poste de développement et pas sur la clé.
    """
    bases: list[Path] = []
    portable = _find_portable_root()
    if portable is not None:
        bases.append(portable)
    bases.append(get_bundle_dir())
    # CWD seulement en dev non-frozen
    if inclure_cwd and not is_frozen():
        bases.append(Path.cwd())
    bases.append(get_app_data_dir())
    # Déduplique en conservant l'ordre
    seen: set[Path] = set()
    uniq: list[Path] = []
    for b in bases:
        try:
            r = b.resolve()
        except Exception:
            continue
        if r not in seen:
            seen.add(r)
            uniq.append(b)
    return uniq


def resoudre_fichier_modele(
    noms: Sequence[str], *, surcharge: str | Path | None = None
) -> Path:
    """Résout un fichier de modèle DANS l'application, jamais dans le CWD.

    `noms` sont des chemins relatifs au dossier `models/` ; plusieurs noms sont
    acceptés parce que les checkpoints sont publiés sous des noms voisins
    (`LLM/x.gguf` et `x.gguf`), et exiger l'un des deux ferait échouer une
    installation valide.

    `surcharge` est la variable d'environnement du modèle (ColBERT, le GGUF de
    discussion). Elle prime SI elle résout. Absente, c'est un niveau sauté, jamais
    une erreur : c'est la différence entre « je n'ai pas eu à le dire » et « il
    faut le dire pour démarrer ». POSÉE ET MORTE, elle ne gagne rien non plus —
    même règle que `THOT_LLAMA_SERVER`, parce que deux électrons ne font pas une
    règle — et le refus est journalisé.

    Le repli rendu est un chemin EXPLICITE sous le dossier de données : un
    appelant doit pouvoir afficher à l'utilisateur l'emplacement exact qui
    manque, plutôt que recevoir `None` et inventer un nom.
    """
    bases = _model_search_bases(inclure_cwd=False)
    if surcharge:
        p = Path(surcharge).expanduser()
        if p.is_absolute():
            if p.exists():
                return p
            ecart = f"{surcharge} ne désigne aucun fichier"
        else:
            trouve = next(
                (c for c in (base / "models" / p for base in bases) if c.exists()), None
            )
            if trouve is not None:
                return trouve
            ecart = f"{surcharge} ne désigne aucun fichier sous models/"
        log.warning("surcharge de modèle ignorée : %s", ecart)
    for base in bases:
        for nom in noms:
            candidat = base / "models" / nom
            if candidat.exists():
                return candidat
    return bases[0] / "models" / noms[0]


def resoudre_dossier_modele(nom: str, *, surcharge: str | Path | None = None) -> Path:
    """Comme `resoudre_fichier_modele`, pour un modèle fait d'un dossier entier.

    ColBERT est structurellement un dossier (`config.json`, poids, `1_Dense/`) :
    exiger un fichier serait demander un format que ce checkpoint n'a pas.
    """
    return resoudre_fichier_modele((nom,), surcharge=surcharge)


def get_document_model_dir(name: str) -> Path:
    """Répertoire d'un modèle documentaire (PaddleOCR-VL, PP-DocLayoutV3).

    Le nom est celui du dossier sous `models/`. On accepte aussi les variantes
    suffixées `_safetensors` : PaddlePaddle publie le même modèle sous deux
    noms, et exiger le nom exact ferait échouer une installation valide.

    Ces modèles sontructuralement différents des GGUF : un dossier complet de
    poids, pas un fichier. On rend donc un répertoire et on laisse l'appelant
    décider comment l'ouvrir, plutôt que de figer ici un format unique.
    """
    for base in _model_search_bases():
        for candidate in (name, f"{name}_safetensors"):
            path = base / "models" / candidate
            if path.is_dir():
                return path
    # Repli explicite : l'appelant doit pouvoir afficher le chemin Attempts
    # plutôt que d'inventer un emplacement silencieux qui "fonctionne".
    return _model_search_bases()[0] / "models" / name


def get_local_model_default_path() -> Path:
    """Chemin LFM2.5-8B principal — priorise portable/bundle/CWD/app_data."""
    for base in _model_search_bases():
        for name in [
            "LLM/LFM2.5-8B-A1B-UD-Q4_K_M.gguf",
            "LFM2.5-8B-A1B-UD-Q4_K_M.gguf",
        ]:
            cand = base / "models" / name
            if cand.exists():
                return cand
            direct = base / name
            if direct.exists():
                return direct
    # Fallback : portable/data ou app_data
    return get_app_data_dir() / "models" / "LFM2.5-8B-A1B-UD-Q4_K_M.gguf"


def get_vision_general_default_path() -> Path:
    for base in _model_search_bases():
        for cand in [
            base / "models" / "Vision-general" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf",
            base / "models" / "Vision-general" / "LFM2.5-3B-UD-Q4_K_M.gguf",
            base / "models" / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf",
        ]:
            if cand.exists():
                return cand
    return (
        get_app_data_dir()
        / "models"
        / "Vision-general"
        / "LFM2.5-VL-3B-UD-Q4_K_XL.gguf"
    )


def get_vision_document_default_path() -> Path:
    # Brouillon : document = PaddleOCR-VL-1.6 (dossier safetensors, worker transformers).
    # GLM-OCR (GGUF Vision-document) n'a jamais été livré ; chemins conservés
    # en repli ci-dessous au cas où le GGUF réapparaîtrait un jour.
    for base in _model_search_bases():
        for cand in [
            base / "models" / "PaddleOCR-VL-1.6",
            base / "models" / "Vision-document" / "GLM-OCR-f16.gguf",
            base / "models" / "Vision-document" / "GLM-OCR-Q4_K_M.gguf",
            base / "models" / "GLM-OCR-f16.gguf",
        ]:
            if cand.exists():
                return cand
    return get_app_data_dir() / "models" / "PaddleOCR-VL-1.6"


def resolve_model_path(value: str | Path | None) -> str:
    """Résout chemin modèle en absolu string.

    Un chemin relatif se rattache à l'APPLICATION (dossier de données), pas au
    répertoire courant : le même appel doit rendre le même chemin depuis n'importe
    quel terminal, sinon la résolution devient un canal caché du répertoire de
    travail de l'utilisateur.
    """
    if not value:
        return str(get_local_model_default_path().resolve())
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = get_app_data_dir() / p
    return str(p.resolve())


# ══════════════════════════════════════════════════════════════════════════════
# LES DÉPENDANCES D'EXÉCUTION — un exécutable est une PIÈCE DE L'APPLICATION
# ══════════════════════════════════════════════════════════════════════════════
#
# §1.5 de la Bible pose la contrainte, pas le confort : « presque tout ce qui suit
# en découle ». Un runtime d'inférence résolu par une variable d'environnement
# obligatoire n'est pas portatif — il est NATIF de la machine où quelqu'un l'a
# configurée une fois, et il devient une variable d'environnement obligatoire que
# personne ne lit. Sur une clé USB, l'application ne trouve pas son moteur, donc
# elle ne démarre pas, donc elle est transportée sans jamais avoir fonctionné.
#
# LA HIÉRARCHIE, et elle est la même pour tout le monde (voir `resoudre_executable`) :
#   1. un chemin ABSOLU connu de l'appelant — c'est le niveau des tests et de
#      l'outillage, il n'a rien à deviner ;
#   2. la variable d'environnement, SI elle est posée. Son ABSENCE n'est pas une
#      erreur : c'est un niveau sauté, et rien d'autre. Sa présence fausse, elle,
#      est consignée comme un écart et ne peut pas l'emporter sur un fichier
#      réellement présent ;
#   3. les racines de l'application : à côté de l'exécutable, la racine portable,
#      le dossier de données ;
#   4. le PATH, EN DERNIER RECOURS ET EN DÉVELOPPEMENT SEULEMENT.
#
# Le 4 est fermé par `est_mode_developpement()`. Ce n'est pas une précaution
# grammaticale, c'est la seule définition honnête de « développement » : un
# exécutable de l'application qui vient du PATH est un exécutable de quelqu'un
# d'autre, et une application qui l'utilise se dit fonctionnelle alors qu'elle
# n'exécute pas la sienne. Le manifeste (`distribution.json`) est ce qui permet de
# ne pas confondre les deux : ce qui est livré est DÉCLARÉ, et son `sha256` est
# VÉRIFIÉ. Sans manifeste et sans dossier, il n'y a rien à vérifier — et c'est
# précisément pourquoi le PATH n'est pas un niveau de production.

NOM_LLAMA_SERVER = "llama-server"
VARIABLE_LLAMA_SERVER = "THOT_LLAMA_SERVER"
# Les emplacements sont dans le CODE, pas dans le manifeste : ce sont des noms de
# fichiers par plate-forme, donc de la connaissance de construction. Le manifeste
# peut en ajouter d'autres (`search_dirs`), mais il ne peut pas les rendre
# nécessaires : `binary` reste le chemin qui fait foi.
EMPLACEMENTS_LLAMA_SERVER = ("bin/llama-server", "bin/llama-server.exe")

# `nvidia-smi` est un outil de PILOTE : il appartient au constructeur de la carte,
# pas à notre licence, et il ne peut ni être embarqué ni être transporté. C'est la
# seule dépendance d'exécution dont l'absence est un fait de la machine. Elle est
# donc un OUTIL SYSTÈME, résolu ailleurs (niveau 3 et 4 inclus), mais son absence
# doit se dire — voir `exiger_nvidia_smi`.
NOM_NVIDIA_SMI = "nvidia-smi"
VARIABLE_NVIDIA_SMI = "THOT_NVIDIA_SMI"
EMPLACEMENTS_NVIDIA_SMI = ("bin/nvidia-smi", "bin/nvidia-smi.exe")
# Les chemins du pilote : le paquet NVIDIA les installe ici, et WSL les expose sous
# `/usr/lib/wsl/lib`. Les nommer évite d'avoir à faire confiance au PATH pour un
# outil dont on sait où le constructeur le met.
CHEMINS_NVIDIA_SMI = (
    "/usr/bin/nvidia-smi",
    "/usr/local/bin/nvidia-smi",
    "/opt/nvidia/bin/nvidia-smi",
    "/usr/lib/wsl/lib/nvidia-smi",
)

MANIFESTE_NOM = "distribution.json"
_EMPREINTE = re.compile(r"^[0-9a-f]{64}$")


class DependanceIntrouvable(RuntimeError):
    """Une dépendance d'exécution n'a pas pu être résolue.

    Base commune des trois refus ci-dessous : ce qui intéresse l'appelant est la
    même chose partout — OÙ on a cherché et POURQUOI ça n'y est pas — pas la
    catégorie Python de l'exception.
    """


class ResolutionImpossible(DependanceIntrouvable):
    """Aucun niveau de résolution n'a produit le fichier attendu.

    Le message porte TOUS les écarts et tous les emplacements examinés. Un
    message qui dit seulement « introuvable » laisse deviner entre une variable
    mal posée, une clé USB mal branchée et un runtime jamais construit ; les trois
    se réparent différemment.
    """

    def __init__(
        self, nom: str, ecarts: Sequence[str], examines: Sequence[Path]
    ) -> None:
        lignes = [f"{nom} introuvable."]
        lignes += [f"  écarté : {e}" for e in ecarts]
        if examines:
            lignes.append(
                "  emplacements examinés : " + ", ".join(str(p) for p in examines)
            )
        super().__init__("\n".join(lignes))
        self.nom = nom
        self.ecarts = tuple(ecarts)


class IntegriteManifesteRompue(DependanceIntrouvable):
    """Le fichier trouvé n'est pas celui que le manifeste annonce.

    Une application portable qui lance un binaire sans en vérifier l'empreinte ne
    transporte pas son runtime : elle TRANSPORTE une confiance. Le sha256 est la
    seule chose qui distingue « le fichier que j'ai construit » de « le fichier
    qui est là », et c'est pour ça qu'il est obligatoire dès que le manifeste en
    déclare un.
    """

    def __init__(self, nom: str, chemin: Path, attendu: str, obtenu: str) -> None:
        super().__init__(
            f"intégrité rompue pour {nom} : {chemin}\n"
            f"  sha256 attendu (distribution.json) : {attendu}\n"
            f"  sha256 mesuré                    : {obtenu}\n"
            f"  Le fichier présent n'est pas le runtime construit. "
            f"Il est peut-être altéré, ou d'une autre version."
        )
        self.nom = nom
        self.chemin = chemin
        self.attendu = attendu
        self.obtenu = obtenu


class ManifesteIncoherent(RuntimeError):
    """`distribution.json` affirme quelque chose qu'il ne peut pas tenir.

    Un manifeste à moitié renseigné est plus dangereux qu'un manifeste absent :
    `enabled: true` avec `binary: null` promet un runtime livré que rien ne peut
    trouver. On refuse de démarrer sur cette promesse.
    """


class DependanceSystemeAbsente(DependanceIntrouvable):
    """Un outil du SYSTÈME manque, et ce qui en dépend ne peut pas fonctionner.

    Nom distinct de `ResolutionImpossible` parce que le remède n'est pas «
    réorganiser les dossiers de l'application » : c'est « installer le pilote » ou
    « nommer le chemin ». Confondre les deux ferait passer une carte graphique non
    détectée pour une installation portable ratée.
    """


@dataclass(frozen=True)
class Resolution:
    """Ce qui a été trouvé, ET d'où il vient.

    La provenance est PARTIE DU RÉSULTAT, pas une note de journal : un runtime
    trouvé dans le PATH et un runtime vérifié par son manifeste ne sont pas le
    même objet, et l'appelant doit pouvoir le dire à l'utilisateur ou l'écrire
    dans un rapport d'incident.
    """

    chemin: Path
    origine: str
    sha256_attendu: str | None = None
    sha256_verifie: bool = False
    ecarts: tuple[str, ...] = ()

    def __str__(self) -> str:
        integrite = (
            "sha256 vérifié" if self.sha256_verifie else "aucune empreinte déclarée"
        )
        return f"{self.chemin} [{self.origine}, {integrite}]"


def sha256_fichier(chemin: Path, *, bloc: int = 1 << 20) -> str:
    """sha256 d'un fichier, lu par blocs.

     Un runtime pèse plusieurs centaines de mégaoctets : le lire d'un seul tenant
     pour le hacher ferait doubler la mémoire de pointe au moment précis où l'on
    prêt à charger le modèle.
    """
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for morceau in iter(lambda: f.read(bloc), b""):
            h.update(morceau)
    return h.hexdigest()


def racines_application() -> list[Path]:
    """Racines où une pièce de l'application peut vivre, de la plus fiable à la
    moins.

    L'ordre est une hiérarchie de foi. À côté de l'exécutable gelé d'abord : le
    runtime est sa propriété, et il voyagera avec lui. La racine portable ensuite,
    parce qu'elle porte le marqueur qui en fait le dossier de l'application — et
    qu'en gelé, le bundle est une extraction temporaire (`_MEIPASS`), donc le
    mauvais endroit pour chercher un runtime. Le dossier de données enfin, parce
    que c'est là qu'un modèle se range sur une clé, et qu'un runtime pourrait y
    être déposé par un installateur.

    AUCUNE racine n'est `Path.cwd()`. Le répertoire courant décrit COMMENT on a
    lancé l'application, pas OÙ elle est : l'utiliser ferait de la plomberie
    portable une fonction du terminal.
    """
    racines: list[Path] = []
    if is_frozen():
        dossier_exe = Path(sys.executable).resolve().parent
        racines.extend([dossier_exe, dossier_exe.parent])
    portable = get_portable_root()
    if portable is not None:
        racines.append(portable)
    # En dev non gelé, `sys.executable` est l'interpréteur DU POSTE : `/usr/bin`
    # n'est pas une racine de Thot, l'y mettre ferait passer n'importe quel outil
    # du système pour une pièce de l'application. La racine de développement, c'est
    # le dépôt, et il se déduit du fichier source lui-même.
    racines.append(get_bundle_dir())
    racines.append(get_app_data_dir())

    vus: set[Path] = set()
    uniques: list[Path] = []
    for base in racines:
        try:
            resolu = base.resolve()
        except OSError:
            continue
        if resolu in vus:
            continue
        vus.add(resolu)
        uniques.append(base)
    return uniques


def est_mode_developpement() -> bool:
    """Vrai seulement pour un CHECKOUT SOURCE, où emprunter le PATH est honnête.

    Trois conditions, toutes nécessaires :

    * pas gelé et pas déclaré packagé — un binaire gelé n'a pas de « mode dev » ;
    * les deux marqueurs du dépôt (`sidecar.py`, `backend/main.py`) sont là. C'est
      la définition opérationnelle du mot « développement » : on ne la devine pas
      à partir d'un nom de dossier ;
    * rien ne déclare le contraire (`THOT_PACKAGED=1`).

    C'est cette fonction qui rend le 4e niveau de `resoudre_executable`
    inaccessible à une application déployée : `is_packaged()` suffit à le fermer.
    """
    if is_packaged():
        return False
    bundle = get_bundle_dir()
    return (bundle / "sidecar.py").is_file() and (
        bundle / "backend" / "main.py"
    ).is_file()


def _absolu_sans_cwd(chemin: Path, bases: Sequence[Path]) -> Path:
    """Rend absolu sans jamais consulter le répertoire courant.

    Un chemin relatif se rattache à la PREMIÈRE racine de l'application : c'est la
    seule interpretation qui ne dépende pas d'où l'on se trouve.
    """
    p = Path(chemin).expanduser()
    if p.is_absolute():
        return p
    return (bases[0] / p) if bases else p


def _verifier_empreinte(resolution: Resolution) -> Resolution:
    """Applique le `sha256` déclaré par le manifeste, s'il y en a un.

    Sans empreinte déclarée, la résolution reste POSSIBLE mais non VÉRIFIÉE, et le
    résultat le dit (`sha256_verifie=False`). Ce n'est pas une erreur : c'est la
    situation d'un poste de développement. C'est un mensonge de le présenter comme
    vérifié, d'où le drapeau.
    """
    if not resolution.sha256_attendu:
        return resolution
    obtenu = sha256_fichier(resolution.chemin)
    if obtenu != resolution.sha256_attendu:
        raise IntegriteManifesteRompue(
            resolution.origine, resolution.chemin, resolution.sha256_attendu, obtenu
        )
    return Resolution(
        resolution.chemin,
        resolution.origine,
        resolution.sha256_attendu,
        True,
        resolution.ecarts,
    )


def resoudre_executable(
    nom: str,
    *,
    explicite: str | Path | None = None,
    variable: str | None = None,
    emplacements: Sequence[str] = (),
    chemins_connus: Sequence[str] = (),
    sha256: str | None = None,
    racines: Sequence[Path] | None = None,
    autoriser_path: bool | None = None,
) -> Resolution:
    """Résout un exécutable selon la hiérarchie décrite en tête de section.

    Lève `ResolutionImpossible` — qui porte les écarts ET les emplacements
    examinés — si aucun niveau n'a produit de fichier. Ne rend jamais `None` :
    « pas trouvé » est un état qui doit être nommé, pas une valeur que l'appelant
    doit tester.

    `chemins_connus` est le niveau 3 bis, réservé aux outils SYSTÈME : des
    emplacements absolus que le fournisseur garantit (le pilote NVIDIA les
    installe à `/usr/bin`, WSL les expose sous `/usr/lib/wsl/lib`). Ils viennent
    après les racines de l'application et avant le PATH, parce que savoir où
    quelqu'un d'autre installe son outil vaut mieux que deviner dans `$PATH`.

    `autoriser_path` vaut par défaut `est_mode_developpement()`. Le paramètre
    n'existe que pour les tests, qui doivent pouvoir prouver que le PATH est
    fermé sans figer l'interpréteur de la machine.
    """
    bases = list(racines) if racines is not None else racines_application()
    ecarts: list[str] = []
    path_admissible = (
        est_mode_developpement() if autoriser_path is None else autoriser_path
    )

    def gagne(chemin: Path, origine: str) -> Resolution:
        """Une résolution réussie EMPORTE ses écarts.

        Les niveaux sautés avant elle font partie du résultat : « trouvé, et la
        variable qui pointait ailleurs a été écartée » est une information, et sans
        elle l'appelant ne peut ni journaliser ce qui a été ignoré ni l'expliquer.
        """
        return _verifier_empreinte(
            Resolution(chemin, origine, sha256, ecarts=tuple(ecarts))
        )

    # Niveau 1 — l'appelant SAIT où c'est. Il ne devine pas : s'il se trompe, il
    # faut le lui dire tout de suite plutôt que de chercher ailleurs en silence.
    if explicite:
        candidat = _absolu_sans_cwd(Path(explicite), bases)
        if candidat.is_file():
            return gagne(candidat, "chemin explicite")
        raise ResolutionImpossible(
            nom, [f"chemin explicite absent : {candidat}"], [candidat]
        )

    # Niveau 2 — la surcharge d'environnement. POSÉE, elle gagne, parce que
    # l'opérateur l'a demandée ; posée et fausse, elle ne gagne PAS, parce qu'un
    # chemin valide sur disque est meilleur qu'un pointeur mort, et surtout parce
    # qu'un pointeur mort qu'on respecte rend l'application inutilisable sur un poste
    # qui a oublié la variable. Ce refus est consigné et rendu visible.
    if variable:
        valeur = (os.environ.get(variable) or "").strip()
        if not valeur:
            ecarts.append(f"{variable} non posée (niveau sauté)")
        else:
            candidat = _absolu_sans_cwd(Path(valeur), bases)
            if candidat.is_file():
                return gagne(candidat, variable)
            ecarts.append(f"{variable}={valeur} ne désigne aucun fichier")

    # Niveau 3 — les racines de l'application. C'est le niveau qui rend le
    # produit transportable : le runtime est dans le dossier, avec l'application.
    examines: list[Path] = []
    for base in bases:
        for relatif in emplacements:
            candidat = base / relatif
            examines.append(candidat)
            if candidat.is_file():
                return gagne(candidat, f"application : {relatif}")
    if not emplacements and not chemins_connus:
        ecarts.append("aucun emplacement déclaré pour ce binaire")

    # Niveau 3 bis — les emplacements absolus que le FOURNISSEUR garantit.
    for connu in chemins_connus:
        candidat = Path(connu)
        examines.append(candidat)
        if candidat.is_file() and os.access(candidat, os.X_OK):
            return gagne(candidat, f"emplacement système : {connu}")
    if chemins_connus:
        ecarts.append("absent des emplacements système connus")

    # Niveau 4 — le PATH, et seulement en développement. Hors développement, on ne
    # le consulte même pas : chercher pour ne trouver qu'un exécutant tiers revient
    # à laisser l'application démarrer avec les outils du poste.
    if path_admissible:
        trouve = shutil.which(nom)
        if trouve:
            return gagne(Path(trouve), "PATH (développement)")
        ecarts.append("absent du PATH")
    else:
        ecarts.append(
            "PATH non consulté : application déployée, pas un checkout source"
        )

    raise ResolutionImpossible(nom, ecarts, examines)


def resoudre_outil_systeme(
    nom: str,
    *,
    variable: str | None = None,
    emplacements: Sequence[str] = (),
    chemins_connus: Sequence[str] = (),
) -> Path:
    """Résout un outil fourni par le système — donc, par construction, hors de
    l'application.

    Seule différence avec `resoudre_executable` : le PATH y est TOUJOURS
    admissible. Un outil de pilote n'a pas d'autre fournisseur, et exiger qu'il
    soit à côté de l'application reviendrait à interdire la machine. Ce qui change,
    c'est le REFUS : `DependanceSystemeAbsente`, distinct de l'échec de résolution
    ordinaire, parce que le remède n'est pas « réorganiser les dossiers » mais
    « installer le pilote ». Les deux messages ne doivent pas se confondre.
    """
    try:
        return resoudre_executable(
            nom,
            variable=variable,
            emplacements=emplacements,
            chemins_connus=chemins_connus,
            # Le PATH est le fournisseur LÉGITIME d'un outil de pilote : ici c'est un
            # choix documenté, pas le repli par défaut d'une dépendance applicative.
            autoriser_path=True,
        ).chemin
    except DependanceIntrouvable as exc:
        raise DependanceSystemeAbsente(
            f"{nom} est introuvable sur ce système.\n"
            f"  Emplacements examinés : {', '.join(chemins_connus) or '(aucun)'}\n"
            f"  Détail : {exc}"
        ) from exc


def exiger_nvidia_smi() -> Path:
    """Lève `DependanceSystemeAbsente` si `nvidia-smi` n'est pas résolvable.

    EXISTE POUR QUE LES PORTES DE MÉMOIRE AIENT QUOI APPELER. Le budget de la
    Bible est un budget de VRAM et de RAM, mesuré ; une porte qui n'a pas su
    mesurer ne doit pas rendre « conforme » : elle doit lever. `get_rss_mb()`
    avale l'absence de `/proc` et rend `(0, 0, 0)`, ce qui fait passer les cinq
    portes hors Linux — un budget qui ne peut pas échouer n'est pas un budget.

    Le message nomme la conséquence et le remède, parce qu'un refus sans issue
    se vit comme un défaut et pas comme une information.
    """
    try:
        return resoudre_outil_systeme(
            NOM_NVIDIA_SMI,
            variable=VARIABLE_NVIDIA_SMI,
            emplacements=EMPLACEMENTS_NVIDIA_SMI,
            chemins_connus=CHEMINS_NVIDIA_SMI,
        )
    except DependanceSystemeAbsente as exc:
        raise DependanceSystemeAbsente(
            f"nvidia-smi est introuvable : aucune VRAM ne peut être MESURÉE, et les "
            f"portes mémoire passeraient au vert sans avoir rien vérifié.\n"
            f"  Remèdes : installer le pilote NVIDIA de la carte, ou pointer "
            f"{VARIABLE_NVIDIA_SMI} sur le binaire s'il est installé ailleurs.\n"
            f"  {exc}"
        ) from exc


def racine_manifeste() -> Path:
    """Cherche `distribution.json` dans les racines de l'application.

    Jamais dans le répertoire courant, pour la même raison que partout ailleurs.
    En gelé, `_MEIPASS` est une extraction temporaire : le manifeste doit venir du
    dossier de l'application, pas de la variable d'extraction.
    """
    for base in racines_application():
        candidat = base / MANIFESTE_NOM
        if candidat.is_file():
            return candidat
    # Dernier resort, et il est explicite : la racine du dépôt en dev.
    return get_bundle_dir() / MANIFESTE_NOM


def lire_manifeste() -> dict:
    """Lit et PARSE `distribution.json`. Renvoie `{}` si absent ou illisible.

    Absent n'est pas une erreur : un poste de développement n'a pas de manifeste,
    et le refuser ferait dépendre le dev d'un fichier de release. Un manifeste
    illisible se dit dans le journal — on ne peut pas vérifier d'intégrité ce
    qu'on n'a pas lu, et il faut le savoir.
    """
    chemin = racine_manifeste()
    try:
        brut = chemin.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        charge = json.loads(brut)
    except ValueError as exc:
        log.warning(
            "%s illisible (%s) : aucune résolution ne sera vérifiée", chemin, exc
        )
        return {}
    if not isinstance(charge, dict):
        log.warning("%s ne porte pas un objet JSON : ignoré", chemin)
        return {}
    return charge


@dataclass(frozen=True)
class DeclarationRuntime:
    """Ce que `distribution.json` AFFIRME transporter, une fois analysé.

    Analysée ici, donc jamais revalidée ailleurs : un appelant qui reçoit cette
    structure travaille sur des champs typés, et n'a plus à savoir d'où ils
    viennent.
    """

    livre: bool
    relatif: str | None = None
    sha256: str | None = None
    emplacements: tuple[str, ...] = EMPLACEMENTS_LLAMA_SERVER


def declaration_runtime_inference() -> DeclarationRuntime:
    """Lit `distribution.json` et en tire la déclaration du runtime d'inférence.

    `enabled: true` avec `binary: null` lève : c'est un manifeste qui promet un
    runtime livré que rien ne peut trouver, et démarrer dessus produirait exactement
    le symptôme que la Bible interdit — une application qui ne marche pas sans
    qu'on sache pourquoi.

    `search_dirs` est FACULTATIF et s'ajoute aux emplacements du code, même quand
    `binary` est nul : c'est le cas réel de ce dépôt (`enabled: false`, aucun
    runtime construit), et un manifeste qui décrit où le runtime SERA posé doit
    être lu dans tous les cas. `binary` reste le chemin qui fait foi.
    """
    manifeste = lire_manifeste()
    outil = (manifeste.get("tool_runtime") or {}).get("llama_server")
    if not isinstance(outil, dict):
        return DeclarationRuntime(livre=False)

    emplacements = EMPLACEMENTS_LLAMA_SERVER
    additionnels = outil.get("search_dirs")
    if isinstance(additionnels, list):
        for nom in additionnels:
            if not isinstance(nom, str) or not nom or nom in emplacements:
                # Un doublon ferait sonder deux fois le même chemin et gonflerait le
                # message de refus : on garde l'ordre, pas la répétition.
                continue
            if Path(nom).is_absolute():
                # Même règle que `binary` : un manifeste ne nomme pas une machine.
                raise ManifesteIncoherent(
                    f"{MANIFESTE_NOM} déclare un emplacement ABSOLU ({nom}) : la "
                    f"résolution se fait sous les racines de l'application, jamais ailleurs."
                )
            emplacements = (*emplacements, nom)

    livre = bool(outil.get("enabled"))
    relatif = outil.get("binary")
    if not isinstance(relatif, str) or not relatif.strip():
        if livre:
            raise ManifesteIncoherent(
                f"{MANIFESTE_NOM} déclare tool_runtime.llama_server.enabled=true "
                f"sans binaire : rien ne pourra être trouvé, donc rien ne démarrera."
            )
        return DeclarationRuntime(livre=False, emplacements=emplacements)
    if Path(relatif).is_absolute():
        raise ManifesteIncoherent(
            f"{MANIFESTE_NOM} déclare un binaire ABSOLU ({relatif}) : un manifeste "
            f"relatif à l'application ne peut pas nommer un chemin d'une autre machine."
        )
    empreinte = outil.get("sha256")
    if empreinte is not None:
        if not isinstance(empreinte, str) or not _EMPREINTE.match(
            empreinte.strip().lower()
        ):
            raise ManifesteIncoherent(
                f"{MANIFESTE_NOM} déclare un sha256 illisible pour {relatif} : "
                f"une empreinte non conforme ne vérifie rien et laisse croire que si."
            )
        empreinte = empreinte.strip().lower()
    return DeclarationRuntime(livre, relatif.strip(), empreinte, emplacements)


def resoudre_runtime_inference(
    explicite: str | Path | None = None,
    *,
    declaration: DeclarationRuntime | None = None,
) -> Resolution:
    """Le runtime d'inférence, trouvé À CÔTÉ DE L'APPLICATION, ou rien.

    `THOT_LLAMA_SERVER` n'est plus une condition de démarrage : c'est une
    surcharge. `THOT_LLAMA_SERVER_ALLOW_UNPACKAGED` disparaît avec elle, parce
    qu'elle ne faisait que décrire ce que fait maintenant la structure — un binaire
    non packagé est celui qu'on trouve dans le dossier, et le Path n'est admis que
    si l'on est honnêtement en développement.

    Si le binaire trouvé n'est pas DÉCLARÉ et qu'on n'est pas en développement,
    on le dit dans le journal : une application déployée qui utilise un runtime
    qu'elle ne déclare pas ne peut ni le mettre à jour ni le vérifier.
    """
    d = declaration if declaration is not None else declaration_runtime_inference()
    emplacements = (d.relatif, *d.emplacements) if d.relatif else d.emplacements
    resolution = resoudre_executable(
        NOM_LLAMA_SERVER,
        explicite=explicite,
        variable=VARIABLE_LLAMA_SERVER,
        emplacements=emplacements,
        sha256=d.sha256,
    )
    if not d.livre and resolution.origine != "chemin explicite":
        log.warning(
            "runtime d'inférence trouvé par %s alors que %s ne le déclare pas : "
            "aucune empreinte ne sera vérifiée.",
            resolution.origine,
            MANIFESTE_NOM,
        )
    return resolution


# --- Temporaire éphémère (RGPD) ---
# Compat : constante historique ; préférer get_temp_dir() pour le portable
THOT_TMP = (
    Path(tempfile.gettempdir()) / "thot"
)  # /tmp/thot — nettoyé systemd 10j + cleanup_request()
# Alias dynamique (utiliser get_temp_dir() dans le nouveau code)


__all__ = [
    "THOT_TMP",
    "DeclarationRuntime",
    "DependanceIntrouvable",
    "DependanceSystemeAbsente",
    "IntegriteManifesteRompue",
    "ManifesteIncoherent",
    "Resolution",
    "ResolutionImpossible",
    "declaration_runtime_inference",
    "est_mode_developpement",
    "exiger_nvidia_smi",
    "get_app_data_dir",
    "get_bundle_dir",
    "get_data_dir",
    "get_media_dir",
    "get_portable_root",
    "get_temp_dir",
    "get_local_model_default_path",
    "get_vision_document_default_path",
    "get_vision_general_default_path",
    "get_user_data_dir",
    "is_frozen",
    "is_packaged",
    "is_portable_mode",
    "lire_manifeste",
    "racines_application",
    "resoudre_dossier_modele",
    "resoudre_executable",
    "resoudre_fichier_modele",
    "resoudre_runtime_inference",
    "resolve_child_path",
    "resolve_data_dir",
    "resolve_file_path",
    "resolve_model_path",
    "sha256_fichier",
]
