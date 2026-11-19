# backend/ingestion/sheets.py — Pipeline tableur, point de convergence unique (spec 03)
# Origines : xlsx/ods/csv natifs, tableau PDF (pdfplumber/Camelot ou fallback PaddleOCR-VL),
# tableau image/figure via gate. Aucun modèle ici : extraction déterministe + chunking.
# Schéma -> registre (find_files) ; chunks enrichis en-têtes+valeurs -> index ColBERT (search_info).
# ask_files prend le relais pour l'extraction précise à la demande sur les gros fichiers.
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from backend.config.defaults import INPUT_LIMITS
from backend.utils.binary_guard import TAILLE_ECHANTILLON, raison_binaire

_ERREURS_XML_DURCI: tuple[type[Exception], ...] = ()
try:  # Défense XML : parseur durci quand la dépendance est présente.
    from defusedxml import ElementTree as _ET_DURCI
    from defusedxml.common import DefusedXmlException

    _ERREURS_XML_DURCI = (DefusedXmlException,)
except ImportError:  # pragma: no cover - dépend de l'environnement
    _ET_DURCI = None  # type: ignore[assignment]

# ── Bornes des conteneurs tableurs (Phase 7.1) ───────────────────────────────
# Un `.xlsx`/`.ods` est une archive ZIP. Avant `load_workbook` (ou pandas), on
# lit l'ANNUAIRE de l'archive — jamais son contenu — pour refuser une bombe de
# décompression. Ces valeurs viennent du point unique INPUT_LIMITS.
MAX_ZIP_DECOMPRESSE_OCTETS = int(INPUT_LIMITS["zip_max_decompressed_bytes"])
MAX_ZIP_RATIO = int(INPUT_LIMITS["zip_max_ratio"])
MAX_ZIP_MEMBRES = int(INPUT_LIMITS["zip_max_members"])
MAX_ZIP_PROFONDEUR = int(INPUT_LIMITS["zip_max_depth"])
MAX_FEUILLES = int(INPUT_LIMITS["sheet_max_sheets"])
MAX_LIGNES_FEUILLE = int(INPUT_LIMITS["sheet_max_rows"])
MAX_CELLULES_TOTAL = int(INPUT_LIMITS["sheet_max_cells"])

# Un DTD/ENTITY peut apparaître n'importe où dans le prologue, pas seulement
# dans les premiers Ko. Le préfixe ci-dessous reste une détection RAPIDE (il
# évite un parsing complet quand le cas est évident), mais il n'est PAS la
# barrière de sécurité : celle-ci est `defusedxml`, qui analyse tout le flux
# (voir `_iterparse_xml`). Une déclaration placée après 64 Kio lui échappe.
MAX_PREFIXE_XML = 64 * 1024
_DÉCLARATIONS_XML_INTERDITES = (b"<!DOCTYPE", b"<!ENTITY")


class TableurRefuse(ValueError):
    """Un tableur refusé par une BORNE, pas par un moteur en panne.

    La distinction compte : `lire_classeur_feuillet` a un repli pandas, et un
    refus de borne ne doit jamais être retenté par un moteur qui matérialiserait
    précisément ce qu'on vient de refuser. Cette exception traverse les replis.
    """


def _refuser_déclarations_xml(
    archive: zipfile.ZipFile, membre: str, contexte: str
) -> None:
    """Refuse DOCTYPE/ENTITY tôt, sur un préfixe borné — PAS la seule garantie.

    Une entité interne s'expanse en mémoire et les bornes ZIP ne mesurent que la
    taille décompressée déclarée. Ce contrôle ne lit que les 64 premiers Kio :
    une déclaration placée plus loin dans le prologue lui échappe. C'est un refus
    PRÉCOCE et peu coûteux, pas la barrière de sécurité — celle-ci est
    `_iterparse_xml`, qui utilise `defusedxml` sur le flux complet.
    """
    with archive.open(membre) as flux:
        prefixe = flux.read(MAX_PREFIXE_XML).upper()
    for déclaration in _DÉCLARATIONS_XML_INTERDITES:
        if déclaration in prefixe:
            raise TableurRefuse(
                f"{contexte} refusé : le XML contient "
                f"{déclaration.decode('ascii')}. Thot n'expanse jamais "
                "d'entités XML ; les bornes d'archive ne bornent pas la "
                "mémoire d'une expansion."
            )


def _iterparse_xml(flux: Any, events: Any):
    """`iterparse` durci par `defusedxml` — OBLIGATOIRE, sans repli stdlib.

    Le préfixe borné (`_refuser_déclarations_xml`) ne voit que les 64 premiers
    Kio : un DTD/ENTITY placé après cette fenêtre lui échappe. La seule garantie
    indépendante de la position est le parseur `defusedxml` (DTD, entités et
    références externes interdites pendant TOUT le flux). Si `defusedxml` manque,
    on refuse explicitement plutôt que de parser avec la stdlib, qui expanserait
    précisément ce qu'on veut interdire.
    """
    if _ET_DURCI is None:
        raise TableurRefuse(
            "Lecture XML impossible : `defusedxml` est absent. Thot refuse de "
            "parser un flux XML de classeur avec la bibliothèque standard, qui "
            "expanse les DTD/entités. Installez `defusedxml`."
        )
    return _ET_DURCI.iterparse(
        flux,
        events=events,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )


def _inspecter_zip_tableur(chemin: str) -> None:
    """Refuse une archive ZIP démesurée AVANT toute lecture de contenu.

    `zipfile` lit les en-têtes de l'annuaire central ; `file_size` est la taille
    DÉCOMPRESSÉE déclarée. On n'extrait rien, on ne lit aucun membre. Trois
    gardes : taille décompressée totale, ratio de compression et nombre de
    membres (plus la profondeur des noms, qui n'a pas de sens pour un classeur
    sain et borne les archives construites pour piéger un extracteur).
    """
    try:
        taille_fichier = max(1, os.path.getsize(chemin))
    except OSError as exc:
        raise TableurRefuse(
            f"Classeur illisible sur le disque : {exc.strerror or exc}."
        ) from exc

    try:
        if not zipfile.is_zipfile(chemin):
            # Un fichier qui commence par `PK` sans être une archive valide
            # n'est pas une bombe : c'est un fichier corrompu, et les lecteurs
            # doivent le nommer avec leur propre message (ODS sans odfpy,
            # openpyxl en échec, etc.). La garde de décompression ne s'applique
            # qu'aux archives réellement lisibles.
            return
        with zipfile.ZipFile(chemin) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBRES:
                raise TableurRefuse(
                    f"Classeur refusé : {len(infos)} membres dans l'archive "
                    f"(maximum {MAX_ZIP_MEMBRES})."
                )
            total = 0
            profondeur = 0
            for info in infos:
                if info.is_dir():
                    continue
                total += int(info.file_size)
                profondeur = max(profondeur, len(Path(info.filename).parts))
            if profondeur > MAX_ZIP_PROFONDEUR:
                raise TableurRefuse(
                    f"Classeur refusé : chemin interne trop profond "
                    f"({profondeur} niveaux, maximum {MAX_ZIP_PROFONDEUR})."
                )
            if total > MAX_ZIP_DECOMPRESSE_OCTETS:
                raise TableurRefuse(
                    f"Classeur refusé : {total // (1024 * 1024)} Mo décompressés "
                    f"(maximum {MAX_ZIP_DECOMPRESSE_OCTETS // (1024 * 1024)} Mo)."
                )
            ratio = total / taille_fichier
            if ratio > MAX_ZIP_RATIO:
                raise TableurRefuse(
                    f"Classeur refusé : ratio de compression {ratio:.0f}:1 "
                    f"(maximum {MAX_ZIP_RATIO}:1)."
                )
    except zipfile.BadZipFile as exc:
        raise TableurRefuse("Classeur illisible : archive ZIP corrompue.") from exc


@dataclass
class _CompteurFeuilles:
    """Compte les lignes et cellules MATÉRIALISÉES, feuille par feuille.

    Le lecteur read-only d'openpyxl génère les lignes une à une ; compter avant
    d'ajouter à la grille permet de s'arrêter sans avoir construit une liste
    plus grosse que la borne.
    """

    cellules: int = 0

    def ajouter_ligne(self, feuille: str, nb_cellules: int, nb_lignes: int) -> None:
        if nb_lignes > MAX_LIGNES_FEUILLE:
            raise TableurRefuse(
                f'Feuille "{feuille}" refusée : plus de {MAX_LIGNES_FEUILLE} '
                "lignes matérialisées."
            )
        self.cellules += nb_cellules
        if self.cellules > MAX_CELLULES_TOTAL:
            raise TableurRefuse(
                f"Classeur refusé : plus de {MAX_CELLULES_TOTAL} cellules "
                "matérialisées."
            )


if TYPE_CHECKING:
    # Les stubs typeshed de openpyxl classent un classeur lu (read_only, sans
    # write_only) comme « WriteOnlyWorksheet » et privent la branche de lecture
    # de ses API — à l'exécution ce sont bien des Worksheet. Ce nom sert aux
    # casts de reconnaissance de `_lire_xlsx_cellules`.
    from openpyxl.worksheet.worksheet import Worksheet


def _tete_octets(path: str, taille: int = TAILLE_ECHANTILLON) -> bytes:
    """Les premiers octets du fichier.

    Le jugement « ceci est-il du texte » se fait ICI, sur les octets : c'est le
    dernier moment où ils sont encore des octets. Un peu plus loin, dans le
    lecteur de texte, ils seront des caractères — et un caractère ne dit plus
    rien de ce qu'il portait.
    """
    try:
        with open(path, "rb") as f:
            return f.read(taille)
    except OSError as exc:
        raise ValueError(f"Lecture impossible : {exc.strerror or exc}.") from exc


def _module_present(nom: str) -> bool:
    """Un moteur de lecture est-il installé ? Distinguer « absent » de « cassé »."""
    import importlib.util

    try:
        return importlib.util.find_spec(nom) is not None
    except (ImportError, ValueError):
        return False


def _get_limits() -> dict:
    try:
        from backend.config.defaults import TOOL_LIMITS as _L

        return dict(_L)
    except Exception:
        return {
            "sheet_small_max_lines": 500,
            "sheet_small_max_sheets": 5,
            "sheet_sample_chunks": 75,
        }


@dataclass
class SheetSchema:
    sheets: list[str]
    headers: dict[str, list[str]]
    col_types: dict[str, list[str]]
    n_rows: dict[str, int]
    n_cols: dict[str, int]
    origin: str
    confidence: float | None
    caption: str


@dataclass
class CelluleFeuillet:
    """Une cellule telle que la lecture l'a rendue, ET son statut de vérité.

    La fiche « mecanismes/ingestion/tableaux.md » §3.3 fixe le vocabulaire :
    Formule source = expression enregistrée ; Résultat en cache = valeur déjà
    stockée, fraîcheur non garantie par la lecture seule ; Résultat recalculé =
    produit par un moteur de calcul identifié — moteur que Thot n'ajoute pas
    (§3.4), donc ce statut n'est JAMAIS produit ici. L'ancien lecteur xlsx faisait
    une seule lecture `data_only=True` : le résultat en cache était rendu sans
    dire qu'une formule l'avait produit. La distinction exige DEUX passages sur
    le même blob (formules, puis valeurs en cache) et c'est ce que fait
    `lire_classeur_feuillet`. Une valeur absente du cache reste une FORMULE sans
    cache — jamais une cellule vide (fiche §3.3) et jamais un zéro inventé (§3.4).
    """

    valeur: object  # résultat en cache (xlsx) ou valeur native (csv/ods)
    formule: str | None = (
        None  # expression enregistrée ("=SUM(...)") ; None = valeur native
    )


@dataclass
class FeuilletLu:
    """Le résultat de la LECTURE réelle d'un fichier tableur, prêt à représenter.

    `minima` porte, par feuille, la dimension déclarée (min_row, min_col) telle
    que le classeur l'expose — elle atteste que le moteur CONNAÎT des adresses
    natives, et `(1, 1)` vaut pour un lecteur qui n'en a pas. Elle ne sert JAMAIS
    à calculer une adresse : la grille d'`iter_rows` est complétée depuis A1, et
    une position y vaut `position + 1` (voir `_lignes_données`). `repères_natifs`
    enregistre la distinction imposée par la fiche §2.5 : « La provenance distingue
    repère natif et coordonnées de reconstruction » — un CSV n'a pas d'adresse de
    cellule native, ses repères sont donc SIGNALÉS reconstruits au niveau du schéma
    (une mention honnête au titre de feuille, pas un mensonge répété ligne à ligne).
    """

    tables: dict[str, list[list[object]]]
    cellules: dict[str, list[list[CelluleFeuillet]]] | None
    minima: dict[str, tuple[int, int]]
    repères_natifs: dict[str, bool]
    lecteur: str


def _cellule_non_vide(valeur: object) -> bool:
    """Une cellule porte-t-elle une valeur ? `None` est VIDE, pas le texte "None".

    `str(None).strip()` vaut `"None"` — un mensonge qui rendait une ligne de
    cellules nulles « non vide » et la faisait élire comme en-tête. `"="` est une
    formule littéralement vide : elle ne porte aucun contenu non plus.
    """
    if valeur is None:
        return False
    texte = str(valeur).strip()
    return texte != "" and texte != "="


def _formule_porteuse(cellule: CelluleFeuillet) -> bool:
    """La cellule porte-t-elle une formule qui exprime quelque chose ?

    `"="` est enregistré comme formule mais ne calcule rien : il n'occupe pas sa
    ligne, exactement comme la valeur `"="` dans `_cellule_non_vide`.
    """
    return cellule.formule is not None and cellule.formule.strip() != "="


def _ligne_occupée(
    ligne: list[object],
    cellules: list[CelluleFeuillet] | None = None,
) -> bool:
    """Une ligne est occupée si elle porte une VALEUR ou une FORMULE.

    tableaux.md §3.3 : une formule sans cache est une limitation de valeur,
    jamais une cellule vide. Une ligne composée uniquement de telles formules
    a donc `[None, …]` pour valeurs mais reste une ligne réelle du classeur :
    les valeurs seules la feraient disparaître du rendu et du schéma. En
    revanche, une formule littéralement vide (`"="`) n'occupe rien.
    """
    if any(_cellule_non_vide(c) for c in ligne):
        return True
    return cellules is not None and any(_formule_porteuse(c) for c in cellules)


def infer_col_types(rows: list[list[object]], limit: int = 100) -> list[str]:
    """Heuristique simple sur les 100 premières lignes : texte|nombre|date|booléen. Pur."""
    import re as _re

    sample = (rows or [])[: max(1, limit)]
    if not sample:
        return []
    ncols = max(len(r) for r in sample)
    out: list[str] = []
    for c in range(ncols):
        vals = [
            str(r[c]).strip() for r in sample if len(r) > c and _cellule_non_vide(r[c])
        ]
        if not vals:
            out.append("texte")
            continue
        if all(
            v.lower()
            in ("vrai", "faux", "true", "false", "oui", "non", "yes", "no", "0", "1")
            for v in vals
        ):
            out.append("booléen")
        elif all(_re.fullmatch(r"-?\d+([.,]\d+)?([ €%]*)?", v) for v in vals):
            out.append("nombre")
        elif all(
            _re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{2}-\d{2}-\d{4}", v)
            for v in vals
        ):
            out.append("date")
        else:
            out.append("texte")
    return out


def extract_schema(
    tables: dict[str, list[list[object]]],
    *,
    origin: str = "natif",
    confidence: float | None = None,
    cellules: dict[str, list[list[CelluleFeuillet]]] | None = None,
) -> SheetSchema:
    """Extrait schéma registre : feuilles, en-têtes (1re ligne non vide), types, dimensions, caption auto. Pur.

    `cellules`, quand il est fourni, applique la même règle d'occupation que
    `_lignes_données` : une formule sans cache occupe sa ligne (tableaux.md
    §3.3). `n_rows` et la caption décrivent alors EXACTEMENT les lignes que le
    rendu produit — plus de ligne fantôme comptée mais jamais affichée.
    """
    sheets = list(tables.keys())
    headers: dict[str, list[str]] = {}
    col_types: dict[str, list[str]] = {}
    n_rows: dict[str, int] = {}
    n_cols: dict[str, int] = {}
    for name, rows in tables.items():
        grille_cellules = (cellules or {}).get(name)
        head: list[str] = []
        data: list[list[object]] = []
        for position, r in enumerate(rows or []):
            ligne_cellules = (
                grille_cellules[position]
                if grille_cellules is not None and position < len(grille_cellules)
                else None
            )
            if not head and _ligne_occupée(r, ligne_cellules):
                # `None` reste une cellule VIDE (chaîne vide) : la ligne
                # d'en-tête garde l'alignement de la grille — une feuille
                # B2:C2 a donc un A2 vide devant « Nom » et « Valeur ».
                head = ["" if c is None else str(c).strip() for c in r]
                continue
            if head and _ligne_occupée(r, ligne_cellules):
                data.append(list(r))
        headers[name] = head
        col_types[name] = infer_col_types(data)
        n_rows[name] = len(data)
        n_cols[name] = len(head)
    total_rows = sum(n_rows.values())
    # Caption auto : concaténation en-têtes + stats (spec 03)
    parts = []
    for name in sheets:
        cols = headers.get(name, [])
        main = ", ".join(cols[:4])
        parts.append(
            f"{name} : {n_cols.get(name, 0)} colonnes, {n_rows.get(name, 0)} lignes — colonnes principales : {main}"
        )
    caption = " ; ".join(parts)[:400] or f"Tableur {total_rows} lignes"
    return SheetSchema(
        sheets=sheets,
        headers=headers,
        col_types=col_types,
        n_rows=n_rows,
        n_cols=n_cols,
        origin=origin,
        confidence=confidence,
        caption=caption,
    )


def _lettre_colonne(index: int) -> str:
    """Lettre de colonne 1-based : 1 → A, 2 → B, 26 → Z, 27 → AA (convention tableur)."""
    lettres = ""
    while index > 0:
        index, reste = divmod(index - 1, 26)
        lettres = chr(ord("A") + reste) + lettres
    return lettres or "A"


def naturalize_row(
    sheet: str,
    headers: list[str],
    row: list[object],
    cellules: list[CelluleFeuillet] | None = None,
    *,
    repere: str | None = None,
    ligne: int | None = None,
) -> tuple[str, str]:
    """Retourne (texte_affiché brut, texte_embeddé naturalisé phrase minimale). Pur.

    Affiché (snippet orchestrateur) : Feuille: Factures — Factures!A2:C2 /
    Date=2026-03-15, Client=Dubois SARL, ...
    Embeddé (encodé ColBERT) : Ligne de la feuille Factures : Date 2026-03-15,
    Client Dubois SARL. [Factures!A2:C2]
    Chunks textuels/documentaires : identiques (pas de distinction).

    La représentation structurée (tâche 4.5) y ajoute :
      - la provenance de cellule — tableaux.md §2.5 : « Conserver la localisation :
        feuille/cellule/plage pour un tableur » — portée par `repere` (plage
        `Feuille!A3:C3` calculée par `build_chunks` à partir du rang natif) ;
      - la distinction formule / résultat en cache — tableaux.md §3.4 : « Indexer
        la valeur utile avec son statut si elle existe ; sinon conserver une
        limitation, sans inventer le résultat ». Le « sans inventer » se dit
        « résultat en cache absent, jamais recalculé par Thot » : le moteur de
        recalcul est hors périmètre (§3.4), le statut « résultat recalculé »
        n'est jamais produit ici. La cellule d'une formule reçoit en plus son
        repère unitaire (C3) — c'est la cellule dont on citait la valeur.
    Une ligne sans `cellules` (CSV, ODS) ne porte AUCUN statut : la phrase reste
    sémantiquement propre et ne parle d'aucune formule qui n'existe pas.

    La colonne native d'un élément est `index + 1` : la grille est complétée
    depuis A1 (seule origine que le module produit, voir `_lignes_données`).
    """
    pairs_levés: list[str] = []
    pairs_naturalisés: list[str] = []
    for index, (header, valeur) in enumerate(zip(headers, row)):
        nom = str(header).strip()
        cellule = None
        if cellules is not None and index < len(cellules):
            cellule = cellules[index]
        if cellule is not None and _formule_porteuse(cellule):
            adresse = _lettre_colonne(index + 1)
            if ligne is not None:
                adresse = f"{adresse}{ligne}"
            if cellule.valeur is None:
                état = "résultat en cache absent, jamais recalculé par Thot"
            else:
                état = f"résultat en cache {cellule.valeur}"
            pairs_levés.append(
                f'{nom} ({adresse}) = formule "{cellule.formule}" ({état})'
            )
            pairs_naturalisés.append(f'{nom} (formule "{cellule.formule}", {état})')
            continue
        brut = "" if valeur is None else str(valeur).strip()
        if brut in ("", "="):
            continue
        pairs_levés.append(f"{nom}={brut}")
        pairs_naturalisés.append(f"{nom} {brut}")
    affiché = (
        f"Feuille: {sheet}"
        + (f" — {repere}" if repere else "")
        + "\n"
        + ", ".join(pairs_levés)
    )
    embeddé = f"Ligne de la feuille {sheet} : " + ", ".join(pairs_naturalisés) + "."
    if repere:
        embeddé += f" [{repere}]"
    return affiché, embeddé


def _lignes_données(
    rows: list[list[object]],
    cellules: list[list[CelluleFeuillet]] | None = None,
) -> list[tuple[int, int, list[object]]]:
    """Les lignes de données d'une feuille APRÈS l'en-tête (première ligne occupée).

    "Occupée" suit `_ligne_occupée` : une valeur OU une formule. Une ligne dont
    toutes les cellules sont des formules sans cache vaut `[None, …]` pour les
    valeurs, mais le classeur la déclare — elle doit être rendue, pas sautée
    (tableaux.md §3.3 : formule sans cache = limitation, pas cellule vide).

    CONVENTION D'ORIGINE, unique pour tout le module : `iter_rows(values_only=True)`
    rend une grille TOUJOURS complétée depuis `(1, 1)`, même quand la dimension
    déclarée commence en `B2` (la ligne et la colonne A sont alors des `None` de
    remplissage). Une position de liste est donc 0-based depuis A1 : son rang
    natif vaut `position + 1`. `calculate_dimension()` donne `min_row/min_col` en
    A1 1-based, mais ces deux origines ne s'additionnent JAMAIS — c'est l'erreur
    qui rendait une feuille B2:C2 « ligne 4 » au lieu de « ligne 3 ».

    Chaque entrée est (numéro de ligne natif, position, ligne) : la position sert
    à aligner la grille `cellules` — deux vues du même fichier, jamais deux
    lectures.
    """
    données: list[tuple[int, int, list[object]]] = []
    vu_en_tête = False
    for position, ligne_grille in enumerate(rows):
        ligne_cellules = (
            cellules[position]
            if cellules is not None and position < len(cellules)
            else None
        )
        occupée = _ligne_occupée(ligne_grille, ligne_cellules)
        if not vu_en_tête:
            if occupée:
                vu_en_tête = True
            continue
        if not occupée:
            continue  # ligne vide : rien à indexer, mais elle garde son rang
        ligne = list(ligne_grille)
        # Une ligne portée seulement par une formule peut revenir plus courte
        # que la grille `cellules` : compléter en `None` garde l'alignement et
        # permet à `naturalize_row` de citer la formule (valeur absente).
        if ligne_cellules is not None and len(ligne) < len(ligne_cellules):
            ligne.extend([None] * (len(ligne_cellules) - len(ligne)))
        données.append((position + 1, position, ligne))
    return données


def _repère_ligne(
    sheet: str,
    row: list[object],
    num_ligne: int,
    cellules: list[CelluleFeuillet] | None = None,
) -> str | None:
    """`Factures!A3:C3` : la plage NATIVE qu'un lecteur a déclarée pour la ligne.

    Convention de repère (interne, nommée) : `Feuille!<plage>` en notation A1.
    Les index de `row` sont 0-based depuis A1 (voir `_lignes_données`) : la
    colonne native vaut `index + 1`. Une ligne entièrement vide n'a pas de
    plage : elle ne porte aucun repère.

    Une cellule PORTEUSE DE FORMULE occupe l'étendue de la ligne même quand son
    résultat en cache est absent (fiche §3.3) : la cellule C2 existe dans le
    classeur — le fichier la déclare —, seule SA VALEUR manque. Restreindre la
    plage à `A2:B2` pour ce seul motif ferait varier le repère d'un même fichier
    selon que Excel avait ou non calculé la formule avant l'enregistrement.
    """
    longueur = max(len(row), len(cellules) if cellules is not None else 0)

    def occupée(index: int) -> bool:
        if index < len(row) and _cellule_non_vide(row[index]):
            return True
        return (
            cellules is not None
            and index < len(cellules)
            and _formule_porteuse(cellules[index])
        )

    colonnes = [index for index in range(longueur) if occupée(index)]
    if not colonnes:
        return None
    de = _lettre_colonne(colonnes[0] + 1)
    à = _lettre_colonne(colonnes[-1] + 1)
    plage = f"{de}{num_ligne}" if de == à else f"{de}{num_ligne}:{à}{num_ligne}"
    return f"{sheet}!{plage}"


def _chunk_de_ligne(
    sheet: str,
    head: list[str],
    entrée: tuple[int, int, list[object]],
    cellules: dict[str, list[list[CelluleFeuillet]]] | None,
) -> dict:
    """Le chunk d'UNE ligne de données : naturalisation + provenance de cellule."""
    num_ligne, position, row = entrée
    ligne_cellules: list[CelluleFeuillet] | None = None
    if cellules is not None:
        grille = cellules.get(sheet)
        if grille and position < len(grille):
            ligne_cellules = grille[position]
    repere = _repère_ligne(sheet, row, num_ligne, ligne_cellules)
    affiché, embeddé = naturalize_row(
        sheet,
        head,
        row,
        ligne_cellules,
        repere=repere,
        ligne=num_ligne,
    )
    return {
        "sheet": sheet,
        "ligne": num_ligne,
        "repere": repere or "",
        "texte_affiche": affiché,
        "texte_embedde": embeddé,
    }


def build_chunks(
    tables: dict[str, list[list[object]]],
    schema: SheetSchema | None = None,
    *,
    cellules: dict[str, list[list[CelluleFeuillet]]] | None = None,
) -> list[dict]:
    """Chunks enrichis en-têtes+valeurs. Petit (≤500 lignes, ≤5 feuilles) : complet ; grand : échantillon 50-100.

    Échantillon (ordre priorité spec 03) : 10-20 premières, 10-20 dernières, 10-20 extrêmes
    numériques, 10-20 motifs rares. Pur.

    La représentation structurée (tâche 4.5) y joint, PAR CHUNK, la provenance de
    cellule : le rang natif de la ligne (`ligne`, tableaux.md §2.5 « Conserver la
    localisation : feuille/cellule/plage pour un tableur ») et la plage repérée
    nativement (`repere`) — les cellules du classeur portent, avec `cellules`,
    leur distinction formule / résultat en cache (fiche §3.3).

    L'origine des adresses est UNIQUE : la grille est complétée depuis A1 (voir
    `_lignes_données`). Aucune dimension déclarée n'entre dans le calcul.
    """
    lim = _get_limits()
    small_lines = int(lim.get("sheet_small_max_lines", 500))
    small_sheets = int(lim.get("sheet_small_max_sheets", 5))
    target = int(lim.get("sheet_sample_chunks", 75))
    schema = schema or extract_schema(tables, cellules=cellules)
    total = sum(schema.n_rows.values())
    is_small = total <= small_lines and len(schema.sheets) <= small_sheets
    chunks: list[dict] = []
    for sheet in schema.sheets:
        head = schema.headers.get(sheet, [])
        grille_cellules = cellules.get(sheet) if cellules is not None else None
        data = _lignes_données(tables.get(sheet, []), grille_cellules)
        if is_small:
            for entrée in data:
                chunks.append(_chunk_de_ligne(sheet, head, entrée, cellules))
            continue
        # Grand : échantillon représentatif
        per = max(10, min(20, target // 4))
        first = data[:per]
        last = data[-per:] if len(data) > per else []
        # Extrêmes numériques : max/min sur 1re colonne numérique
        extremes: list[tuple[int, int, list[object]]] = []
        try:
            num_idx = next(
                (
                    i
                    for i, t in enumerate(schema.col_types.get(sheet, []))
                    if t == "nombre"
                ),
                None,
            )
            if num_idx is not None:
                colonnes = num_idx  # la colonne numérique, prise en closure

                def _num(entrée: tuple[int, int, list[object]]) -> float | None:
                    row = entrée[2]
                    try:
                        return float(
                            str(row[colonnes])
                            .replace(" ", "")
                            .replace(",", ".")
                            .rstrip("€%")
                        )
                    except Exception:
                        return None

                mensurations: list[tuple[float, tuple[int, int, list[object]]]] = []
                for entrée_num in data:
                    valeur_num = _num(entrée_num)
                    if valeur_num is not None:
                        mensurations.append((valeur_num, entrée_num))
                mensurations.sort(key=lambda paire: paire[0])
                scored = mensurations
                extremes = [e for _, e in scored[: per // 2]] + [
                    e for _, e in scored[-per // 2 :]
                ]
        except Exception:
            extremes = []
        # Motifs rares : lignes avec valeur apparaissant une seule fois (1re colonne texte)
        rare: list[tuple[int, int, list[object]]] = []
        try:
            from collections import Counter as _C

            c0 = _C(str(e[2][0]).strip() for e in data if e[2] and str(e[2][0]).strip())
            rare = [e for e in data if e[2] and c0.get(str(e[2][0]).strip(), 0) == 1][
                :per
            ]
        except Exception:
            rare = []
        seen: set[str] = set()
        for entrée in first + last + extremes + rare:
            key = "|".join(str(c) for c in entrée[2])
            if key in seen:
                continue
            seen.add(key)
            chunks.append(_chunk_de_ligne(sheet, head, entrée, cellules))
            if len(chunks) >= target:
                break
        # Complétion : si les priorités se recouvrent (extrêmes/rares déjà dans
        # premières/dernières), complète au milieu pour tenir 50-100 (spec 03).
        if len(chunks) < 50 and len(data) > len(chunks):
            # max(1, …) : un `sheet_sample_chunks` sous la borne de
            # complétion (50) peut amener target == len(chunks) ; une
            # division par zéro ici serait un crash de lecteur réussi.
            step = max(1, len(data) // max(1, target - len(chunks)))
            for entrée in data[per::step]:
                key = "|".join(str(c) for c in entrée[2])
                if key in seen:
                    continue
                seen.add(key)
                chunks.append(_chunk_de_ligne(sheet, head, entrée, cellules))
                if len(chunks) >= target:
                    break
    return chunks


def read_csv_tables(path: str) -> dict[str, list[list[object]]]:
    """CSV : détection encodage (UTF-8, Latin-1) + séparateur (, ; |) sur 100 premières lignes (spec 03).

    LE DÉFAUT QUE LA GARDE PRÉALABLE SUPPRIME. La boucle d'encodage s'arrêtait au
    PREMIER qui aboutissait — or `latin-1` aboutit TOUJOURS : il associe un
    caractère à chacun des 256 octets possibles, donc `UnicodeDecodeError` — le
    SEUL motif d'échec de cette boucle — ne peut pas être levé. Aucun fichier ne
    pouvait jamais être refusé par cette voie, et le `latin-1` de secours servait
    donc à contourner la détection au lieu de l'appliquer.

    Concrètement : un `.ods` est une archive ZIP. Il passait la porte « texte »,
    n'était coupé que par les quelques octets `0x0A` de son contenu compressé,
    revenait comme une feuille unique de plus de cent mille caractères, et l'ingestion
    enregistrait 16 000 octets `PK\x03\x04...` comme une extraction RÉUSSIE. La
    réparation n'était donc pas de mieux deviner un encodage — il n'y a rien à
    deviner — mais de refuser ce que cette boucle, par construction, ne pouvait
    pas refuser. D'où le test de la signature avant la boucle.
    """
    import csv as _csv

    motif = raison_binaire(_tete_octets(path))
    if motif:
        raise ValueError(
            f"Contenu binaire dans un CSV ({motif}) : ce fichier n'est pas un "
            "tableur texte. Le décoder ne donnerait pas des données mais des "
            "octets illisibles — mieux vaut un refus qu'une fausse extraction."
        )
    for enc in ("utf-8", "latin-1"):
        try:
            with open(path, encoding=enc, newline="") as f:
                sample = "".join(f.readline() for _ in range(100))
            counts = {sep: sample.count(sep) for sep in (",", ";", "|")}
            sep = max(counts, key=lambda k: counts[k]) or ","
            rows: list[list[object]] = []
            cellules = 0
            with open(path, encoding=enc, newline="") as f:
                for rangée in _csv.reader(f, delimiter=sep):
                    if len(rows) >= MAX_LIGNES_FEUILLE:
                        raise TableurRefuse(
                            f"CSV refusé : plus de {MAX_LIGNES_FEUILLE} lignes "
                            "matérialisées."
                        )
                    cellules += len(rangée)
                    if cellules > MAX_CELLULES_TOTAL:
                        raise TableurRefuse(
                            f"CSV refusé : plus de {MAX_CELLULES_TOTAL} cellules "
                            "matérialisées."
                        )
                    rows.append(list(rangée))
            return cast("dict[str, list[list[object]]]", {"Feuille 1": rows})
        except TableurRefuse:
            raise
        except Exception:
            continue
    return {"Feuille 1": []}


_NS_ODF_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_NS_ODF_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def _compter_feuilles_ods(chemin: str) -> int | None:
    """Compte les `<table:table>` d'un ODS SANS passer par `pd.ExcelFile`.

    `pd.ExcelFile` matérialise tout le DOM ODF AVANT qu'on puisse constater
    qu'il y a trop de feuilles. Ici `content.xml` est parcouru en flux : on
    compte les tables directement filles d'`office:spreadsheet`, et seules les
    balises de structure sont vues — aucune cellule, aucun DataFrame.

    Rend `None` si le conteneur n'est pas inspectable (pas de `content.xml`,
    XML illisible) : pandas produira alors son propre message, plutôt qu'un
    refus de borne fondé sur une lecture incomplète. Un XML à DTD/entités, lui,
    est REFUSÉ (tableaux.md §3.3 : une limite n'est pas un contenu à expanser).
    """
    contexte = f'Feuille ODS "{Path(chemin).name}"'
    try:
        with zipfile.ZipFile(chemin) as archive:
            try:
                _refuser_déclarations_xml(archive, "content.xml", contexte)
            except KeyError:
                return None
            with archive.open("content.xml") as flux:
                pile: list[str] = []
                nombre = 0
                for évènement, element in _iterparse_xml(flux, ("start", "end")):
                    if évènement == "start":
                        if (
                            element.tag == f"{{{_NS_ODF_TABLE}}}table"
                            and pile
                            and pile[-1] == f"{{{_NS_ODF_OFFICE}}}spreadsheet"
                        ):
                            nombre += 1
                        pile.append(element.tag)
                    else:
                        if pile:
                            pile.pop()
                        element.clear()
                return nombre
    except TableurRefuse:
        raise
    except _ERREURS_XML_DURCI as exc:
        raise TableurRefuse(
            f"{contexte} refusé : entités XML interdites ({exc})."
        ) from exc
    except (zipfile.BadZipFile, ET.ParseError, OSError):
        return None


def _feuilles_par_pandas(path: str) -> dict[str, list[list[object]]]:
    """Repli pandas (moteurs ods/xlsx/xls). Tout en texte : pandas ne rend pas les types.

    La liste des feuilles est lue AVANT toute matérialisation : refuser APRÈS un
    `read_excel(sheet_name=None)` reviendrait à charger toutes les feuilles pour
    constater qu'il y en a trop. Pour un ODS, la liste est même comptée AVANT
    `pd.ExcelFile`, qui construit tout le DOM ODF : `_compter_feuilles_ods` lit
    `content.xml` en flux. Chaque feuille est ensuite lue seule, avec `nrows`
    comme barrière de lignes, et les cellules sont comptées au fur et à mesure —
    la borne s'applique à chaque étape, pas après coup.
    """
    import pandas as _pd

    if Path(str(path).lower()).suffix == ".ods":
        nombre_feuilles = _compter_feuilles_ods(path)
        if nombre_feuilles is not None and nombre_feuilles > MAX_FEUILLES:
            raise TableurRefuse(
                f"Classeur refusé : {nombre_feuilles} feuilles "
                f"(maximum {MAX_FEUILLES})."
            )

    classeur = _pd.ExcelFile(path)
    try:
        noms = list(classeur.sheet_names)
        if len(noms) > MAX_FEUILLES:
            raise TableurRefuse(
                f"Classeur refusé : {len(noms)} feuilles (maximum {MAX_FEUILLES})."
            )
        feuilles: dict[str, list[list[object]]] = {}
        cellules = 0
        for nom in noms:
            # `sheet_name` est une chaîne : pandas rend UN DataFrame, mais ses
            # stubs typent aussi le cas `None` (dict). L'annotation explicite
            # garde le type checker aligné sur l'appel réel.
            table: Any = classeur.parse(
                nom,
                header=None,
                dtype=str,
                nrows=MAX_LIGNES_FEUILLE + 1,
            )
            if len(table) > MAX_LIGNES_FEUILLE:
                raise TableurRefuse(
                    f'Feuille "{nom}" refusée : plus de {MAX_LIGNES_FEUILLE} lignes '
                    "matérialisées."
                )
            cellules += int(table.shape[0]) * max(1, int(table.shape[1]))
            if cellules > MAX_CELLULES_TOTAL:
                raise TableurRefuse(
                    f"Classeur refusé : plus de {MAX_CELLULES_TOTAL} cellules "
                    "matérialisées."
                )
            feuilles[str(nom)] = [
                [("" if _pd.isna(c) else str(c)) for c in row]
                for row in table.values.tolist()
            ]
        return feuilles
    finally:
        classeur.close()


def _motif_classeur_illisible(suffixe: str, erreurs: list[str]) -> str:
    """Le message qu'un classeur qu'on n'a pas su ouvrir doit laisser.

    Un `{}` ne dit rien : il est indiscernable d'un classeur réellement vide, et
    l'appelant — qui teste « le rendu est-il non vide ? » — ne peut pas le
    distinguer d'une réussite. Le message, lui, dit ce qui a été tenté, ce qui
    manque, et ce que l'utilisateur peut faire.
    """
    detail = " ; ".join(erreurs) or "aucun moteur n'a produit de feuille"
    if suffixe == ".xls":
        return (
            f"Format .xls (Excel 97-2003) non lisible : c'est un conteneur OLE2, "
            f"que ni openpyxl ni pandas sans le module `xlrd` n'ouvrent ({detail}). "
            "La lecture d'un .xls n'est pas une capacité de Thot : rouvrez le "
            "fichier et ré-enregistrez-le en .xlsx ou .ods."
        )
    if suffixe == ".ods":
        return (
            f"Format .ods non lisible ({detail}). Sa lecture exige le module "
            "`odfpy` : sans lui, aucun moteur ne sait ouvrir le classeur."
        )
    return f"Classeur non lisible ({suffixe or 'sans extension'}) : {detail}"


def _dimension_feuille(dimension: str | None) -> tuple[int, int]:
    """(min_row, min_col) déclarés par le classeur, ou (1, 1).

    La dimension openpyxl (`A1:D40`, `B2:K19`) atteste que le classeur PORTE des
    adresses natives. Elle est retournée en A1 1-based, mais elle ne s'additionne
    JAMAIS aux positions de la grille d'`iter_rows` (0-based depuis A1) : c'est
    l'addition de deux origines qui produisait une ligne 4 pour B3. Sinon — la
    fiche tableaux.md §2.5 interdit de présenter une adresse générée comme une
    adresse réelle — on retombe sur (1, 1) et la feuille sera marquée « repères
    construits » par le lecteur.
    """
    import re as _re

    trouvé = _re.fullmatch(
        r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", (dimension or "").strip()
    )
    if not trouvé:
        return 1, 1

    def _colonne(les: str) -> int:
        valeur = 0
        for ch in les:
            valeur = valeur * 26 + ord(ch) - ord("A") + 1
        return valeur

    return int(trouvé.group(2)), _colonne(trouvé.group(1))


_CELL_REF_RE = re.compile(r"^([A-Za-z]+)(\d+)$")
_NS_SPREADSHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _coordonnees_cellule(ref: str | None) -> tuple[int, int] | None:
    """(ligne, colonne) absolues d'une référence A1, ou None si illisible."""
    match = _CELL_REF_RE.match((ref or "").strip())
    if match is None:
        return None
    colonne = 0
    for ch in match.group(1).upper():
        colonne = colonne * 26 + (ord(ch) - ord("A") + 1)
    return int(match.group(2)), colonne


_REF_CELLULE_FORMULE = re.compile(
    r"(?<![A-Za-z0-9_.])(\$?)([A-Za-z]{1,3})(\$?)(\d+)(?![A-Za-z0-9_])"
)

# Bornes de la grille Excel (A1:XFD1048576). Une translation qui les dépasse
# écrit `#REF!`, comme Excel — et non `XFE1` ou `A1048577`, qui n'existent pas.
_MAX_COLONNES_EXCEL = 16384
_MAX_LIGNES_EXCEL = 1_048_576


def _formule_avec_egal(texte: str) -> str:
    """La forme normalisée d'une formule : elle commence toujours par `=`."""
    return texte if texte.startswith("=") else "=" + texte


def _colonne_depuis_lettres(lettres: str) -> int:
    colonne = 0
    for ch in lettres.upper():
        colonne = colonne * 26 + ord(ch) - ord("A") + 1
    return colonne


def _lettres_depuis_colonne(colonne: int) -> str | None:
    if colonne < 1:
        return None
    lettres = ""
    while colonne:
        colonne, reste = divmod(colonne - 1, 26)
        lettres = chr(ord("A") + reste) + lettres
    return lettres


def _plages_noms_feuilles(texte: str) -> list[tuple[int, int]]:
    """Les intervalles des noms de feuilles entre apostrophes (`'Ma Feuille'`).

    Excel isole un nom de feuille contenant espaces ou caractères spéciaux par
    des apostrophes, et double l'apostrophe interne (`'L''Equipe'`). Ces
    intervalles ne contiennent pas des adresses : un nom comme `'Q1'` ne doit
    pas être décalé en `'R1'`.
    """
    plages: list[tuple[int, int]] = []
    position = 0
    while position < len(texte):
        if texte[position] != "'":
            position += 1
            continue
        debut = position
        position += 1
        while position < len(texte):
            if texte[position] != "'":
                position += 1
                continue
            if position + 1 < len(texte) and texte[position + 1] == "'":
                position += 2
                continue
            position += 1
            break
        plages.append((debut, position))
    return plages


def _traduire_references_relatives(
    texte: str, decalage_ligne: int, decalage_colonne: int
) -> str:
    """Décale les références A1 RELATIVES d'un fragment de formule.

    Les `$` figent la ligne ou la colonne qu'ils précèdent, exactement comme une
    recopie Excel. Une référence sortie de la grille devient `#REF!` — c'est ce
    qu'Excel écrit, et l'inventer autrement serait faux.

    Un NOM DE FEUILLE pouvant ressembler à une adresse (`Q1`, `S1`, `Tab1`,
    `QTR1`) n'est PAS une cellule : il est suivi de `!` (ou encadré
    d'apostrophes) et reste intact. Sans cette distinction, `Q1!A1` décalé
    d'une colonne devenait `R1!B1` au lieu de `Q1!B1`.
    """
    noms_feuilles = _plages_noms_feuilles(texte)

    def _dans_nom_feuille(position: int) -> bool:
        return any(debut <= position < fin for debut, fin in noms_feuilles)

    def _remplacer(match: re.Match[str]) -> str:
        # `LOG10(` est une fonction, pas la cellule LOG10 : ne pas la décaler.
        if match.end() < len(texte) and texte[match.end()] == "(":
            return match.group(0)

        # Un token suivi de `!` est un nom de feuille (`Sheet1!A1`) : même
        # s'il ressemble à une adresse, ce n'est pas une cellule.
        if match.end() < len(texte) and texte[match.end()] == "!":
            return match.group(0)
        if _dans_nom_feuille(match.start()):
            return match.group(0)

        col_absolu, col_lettres, ligne_absolue, ligne_chiffres = match.groups()
        colonne = _colonne_depuis_lettres(col_lettres)
        ligne = int(ligne_chiffres)
        if not col_absolu:
            colonne += decalage_colonne
        if not ligne_absolue:
            ligne += decalage_ligne
        lettres = _lettres_depuis_colonne(colonne)
        if (
            lettres is None
            or ligne < 1
            or colonne > _MAX_COLONNES_EXCEL
            or ligne > _MAX_LIGNES_EXCEL
        ):
            return "#REF!"
        return f"{col_absolu}{lettres}{ligne_absolue}{ligne}"

    return _REF_CELLULE_FORMULE.sub(_remplacer, texte)


def _traduire_formule_partagee(
    texte: str, decalage_ligne: int, decalage_colonne: int
) -> str:
    """Traduit une formule partagée pour la cellule dépendante.

    Les références situées DANS une chaîne littérale (« A1 ») ne sont pas des
    adresses : on ne décale que le texte hors guillemets.
    """
    if decalage_ligne == 0 and decalage_colonne == 0:
        return texte

    morceaux: list[str] = []
    debut = 0
    position = 0
    while position < len(texte):
        if texte[position] != '"':
            position += 1
            continue

        morceaux.append(
            _traduire_references_relatives(
                texte[debut:position], decalage_ligne, decalage_colonne
            )
        )
        fin = position + 1
        while fin < len(texte):
            if texte[fin] != '"':
                fin += 1
                continue
            if fin + 1 < len(texte) and texte[fin + 1] == '"':
                fin += 2
                continue
            fin += 1
            break
        morceaux.append(texte[position:fin])
        debut = fin
        position = fin

    morceaux.append(
        _traduire_references_relatives(texte[debut:], decalage_ligne, decalage_colonne)
    )
    return "".join(morceaux)


def _formules_xlsx(
    chemin: str, chemins: dict[str, str]
) -> dict[str, dict[tuple[int, int], str]]:
    """Formules enregistrées, lues dans le XML du classeur SANS second chargement.

    openpyxl expose soit la formule, soit la valeur en cache selon `data_only`,
    jamais les deux. Le second `load_workbook` décodait tout le classeur une
    deuxième fois uniquement pour relire des formules que le ZIP contient déjà :
    ici le XML est parcouru en flux, et seules les cellules portant `<f>` sont
    retenues.

    Une formule RECOPIÉE n'est écrite qu'une fois dans un vrai xlsx :

        <f t="shared" ref="B1:B2" si="0">A1*2</f>   ← maître, en B1
        <f t="shared" si="0"/>                      ← dépendant, en B2

    Le dépendant n'a pas de texte : son texte se reconstitue en décalant les
    références relatives du maître de l'écart entre les deux cellules. C'est ce
    que faisait le second passage openpyxl ; le refaire ici garde l'équivalence
    sans recharger le classeur. Un dépendant sans maître retrouvable reste sans
    formule, mais sa cellule n'est pas perdue. Un XML illisible rend une feuille
    sans formules — jamais une lecture qui casse.
    """
    formules: dict[str, dict[tuple[int, int], str]] = {titre: {} for titre in chemins}
    try:
        with zipfile.ZipFile(chemin) as archive:
            for titre, chemin_xml in chemins.items():
                if not chemin_xml:
                    continue
                maitres: dict[str, tuple[tuple[int, int], str, str]] = {}
                dependants: list[tuple[tuple[int, int], str]] = []
                try:
                    _refuser_déclarations_xml(
                        archive, chemin_xml, f'Feuille xlsx "{titre}"'
                    )
                    with archive.open(chemin_xml) as flux:
                        for _, element in _iterparse_xml(flux, ("end",)):
                            if element.tag.rsplit("}", 1)[-1] != "c":
                                continue
                            coordonnees = _coordonnees_cellule(element.get("r"))
                            if coordonnees is not None:
                                balise = element.find(_NS_SPREADSHEET + "f")
                                if balise is not None:
                                    texte = (balise.text or "").strip()
                                    si = balise.get("si")
                                    if texte:
                                        formules[titre][coordonnees] = (
                                            _formule_avec_egal(texte)
                                        )
                                        if (
                                            balise.get("t") == "shared"
                                            and si is not None
                                        ):
                                            maitres[si] = (
                                                coordonnees,
                                                texte,
                                                balise.get("ref") or "",
                                            )
                                    elif si is not None:
                                        # Dépendant `shared` — et, s'il en
                                        # existe, `array` porteur d'un `si` :
                                        # la résolution est la même.
                                        dependants.append((coordonnees, si))
                            element.clear()
                except TableurRefuse:
                    raise
                except _ERREURS_XML_DURCI as exc:
                    raise TableurRefuse(
                        f'Feuille xlsx "{titre}" refusée : entités XML '
                        f"interdites ({exc})."
                    ) from exc
                except Exception:
                    continue
                for coordonnees, si in dependants:
                    maitre = maitres.get(si)
                    if maitre is None:
                        # Pas de maître : ne pas inventer une formule. La
                        # cellule existe déjà dans la grille ; elle garde sa
                        # valeur de cache (ou None).
                        continue
                    (ligne_maitre, colonne_maitre), texte_maitre, _ = maitre
                    formules[titre][coordonnees] = _formule_avec_egal(
                        _traduire_formule_partagee(
                            texte_maitre,
                            coordonnees[0] - ligne_maitre,
                            coordonnees[1] - colonne_maitre,
                        )
                    )
    except TableurRefuse:
        raise
    except Exception:
        return {titre: {} for titre in chemins}
    return formules


def _lire_grille_bornee(feuille, compteur: _CompteurFeuilles) -> list[list[object]]:
    """Matérialise une feuille read-only LIGNE PAR LIGNE, sous les bornes.

    Compter AVANT d'ajouter est ce qui arrête la lecture sans construire une
    grille plus grosse que `MAX_LIGNES_FEUILLE` / `MAX_CELLULES_TOTAL`.
    """
    grille: list[list[object]] = []
    for rangée in feuille.iter_rows(values_only=True):
        compteur.ajouter_ligne(feuille.title, len(rangée), len(grille) + 1)
        grille.append(list(rangée))
    return grille


def _fusionner_cellules(
    grille: list[list[object]],
    formules: dict[tuple[int, int], str],
    minimum: tuple[int, int],
) -> list[list[CelluleFeuillet]]:
    """Une cellule = valeur en cache (ou native) + formule enregistrée si elle existe.

    `grille` est la sortie d'`iter_rows(values_only=True)` : elle est TOUJOURS
    complétée depuis (1, 1), même quand la plage déclarée commence en B2 (la
    première ligne/colonne est alors vide). C'est pourquoi l'appelant passe
    (1, 1), et non le `min_row/min_col` de `calculate_dimension()` — celui-ci
    ne sert qu'à restituer les adresses A1 réelles dans la représentation.
    """
    min_row, min_col = minimum
    lignes: list[list[CelluleFeuillet]] = []
    for position_ligne, rangée in enumerate(grille):
        cellules_ligne: list[CelluleFeuillet] = []
        for position_colonne, valeur in enumerate(rangée):
            formule = formules.get(
                (min_row + position_ligne, min_col + position_colonne)
            )
            cellules_ligne.append(
                CelluleFeuillet(valeur=valeur, formule=formule)
                if formule
                else CelluleFeuillet(valeur=valeur)
            )
        lignes.append(cellules_ligne)
    return lignes


def _lire_xlsx_cellules(chemin: str) -> FeuilletLu:
    """UNE lecture openpyxl + formules lues dans le XML : plus de double chargement.

    La fiche tableaux.md §3.3 exige la DISTINCTION entre formule enregistrée et
    résultat en cache. La version précédente la produisait en chargeant le
    classeur DEUX fois (`data_only=False` puis `data_only=True`), ce qui
    matérialisait toutes les lignes deux fois. Ici :

      - `data_only=True` fournit la valeur native ou le résultat en cache ;
      - le ZIP, déjà inspecté contre les bombes, fournit les formules `<f>` en
        flux.

    Une formule sans cache reste une formule avec valeur `None` — jamais une
    cellule vide. AUCUN recalcul : le moteur de calcul est hors périmètre
    (§3.4), donc le statut « résultat recalculé » n'est JAMAIS produit ici.
    """
    import openpyxl as _oxl

    # `lire_classeur_feuillet` a déjà inspecté le ZIP avant de nous appeler :
    # ce lecteur privé n'a qu'un seul appelant, on ne refait pas le parcours
    # de l'annuaire une seconde fois.
    tables: dict[str, list[list[object]]] = {}
    minima: dict[str, tuple[int, int]] = {}
    chemins: dict[str, str] = {}
    wb = _oxl.load_workbook(chemin, data_only=True, read_only=True)
    try:
        feuilles = list(wb.worksheets)
        if len(feuilles) > MAX_FEUILLES:
            raise TableurRefuse(
                f"Classeur refusé : {len(feuilles)} feuilles (maximum {MAX_FEUILLES})."
            )
        compteur = _CompteurFeuilles()
        for ws in feuilles:
            # Cast de reconnaissance : les stubs typeshed de openpyxl classent
            # `load_workbook` comme Workbook | WriteOnlyWorksheet et privent la
            # branche de lecture de ses API — à l'exécution ce sont bien des
            # Worksheet.
            feuille = cast("Worksheet", ws)
            chemins[feuille.title] = str(getattr(feuille, "_worksheet_path", "") or "")
            tables[feuille.title] = _lire_grille_bornee(feuille, compteur)
            try:
                minima[feuille.title] = _dimension_feuille(
                    feuille.calculate_dimension()
                )
            except Exception:  # noqa: BLE001 — dimension mal déclarée → (1, 1) (§2.5)
                minima[feuille.title] = (1, 1)
    finally:
        try:
            wb.close()
        except Exception:
            pass
    formules = _formules_xlsx(chemin, chemins)
    cellules = {
        # La grille d'`iter_rows` est indexée depuis (1, 1), pas depuis la
        # dimension déclarée : c'est le repère de l'ancien merge par index.
        titre: _fusionner_cellules(grille, formules.get(titre, {}), (1, 1))
        for titre, grille in tables.items()
    }
    return FeuilletLu(
        tables=tables,
        cellules=cellules,
        minima=minima,
        repères_natifs={nom: True for nom in tables},
        lecteur="openpyxl (lecture unique ; formules lues dans le XML)",
    )


def lire_classeur_feuillet(chemin: str) -> FeuilletLu:
    """XLSX/ODS/XLS : la lecture RÉELLE du classeur, cellules avec leur statut.

    Deux lecteurs, un contrat de réussite : openpyxl seul suffit pour xlsx
    (fiche §2.2 : « Pour XLSX, openpyxl est un candidat de lecture structurée »,
    avec la lecture unique qui porte formules et résultats en cache) ; les
    autres profils passent par pandas, dont le moteur `ods` EXIGE le module
    `odfpy`. Pandas ne conserve PAS les formules enregistrées : un classeur ods
    revient en valeurs natives, sans distinction de statut — une limitation
    assumée (§2.2 : « Le parseur ODS et les autres profils restent à choisir »),
    documentée dans la mesure du schéma plutôt que dissimulée.

    L'inspection ZIP est faite AVANT tout moteur : un conteneur bombe est
    refusé sans ouvrir le classeur. Un `TableurRefuse` traverse les replis —
    retenter avec pandas reviendrait à matérialiser ce qu'on vient de refuser.

    Lève RuntimeError/ValueError NOMMÉE si aucun moteur ne sait lire — jamais
    un silence, jamais `{}` : un classeur illisible n'est pas un classeur vide.
    """
    suffixe = Path(str(chemin).lower()).suffix
    if suffixe in (".xlsx", ".ods"):
        _inspecter_zip_tableur(chemin)
    if suffixe == ".xlsx":
        try:
            return _lire_xlsx_cellules(chemin)
        except TableurRefuse:
            raise
        except Exception as exc:
            erreurs = [f"openpyxl : {type(exc).__name__}: {exc}"]
    else:
        erreurs = []
    if suffixe == ".ods" and not _module_present("odf"):
        raise RuntimeError(
            "Lecture d'un .ods impossible : le module `odfpy` est absent "
            "(`pip install odfpy`). Le fichier reste déposé et consultable, mais "
            "rien n'a été extrait de lui — ce qui n'est pas la même chose que "
            "l'avoir lu."
        )
    try:
        tables = _feuilles_par_pandas(chemin)
        if tables:
            return FeuilletLu(
                tables=tables,
                cellules=None,
                minima={nom: (1, 1) for nom in tables},
                repères_natifs={nom: False for nom in tables},
                lecteur="pandas (valeur native : les formules du classeur ne sont pas conservées)",
            )
        erreurs.append("pandas n'a rendu aucune feuille")
    except TableurRefuse:
        raise
    except Exception as exc:
        erreurs.append(f"pandas : {type(exc).__name__}: {exc}")
    raise RuntimeError(_motif_classeur_illisible(suffixe, erreurs))


def lire_csv_feuillet(chemin: str) -> FeuilletLu:
    """La lecture du CSV, un tableUR SANS formule : tout est valeur native.

    Un CSV n'a ni formule ni résultat en cache (fiche §3.3 : les deux statuts
    décrivent un classeur xlsx) — ses cellules sont marquées natives, et ses
    repères de cellule sont CONSTRUITS : un CSV n'a pas d'adresse A1, la
    convention (feuille 1, colonne A) est déclarée à la fiche, ligne à ligne
    elle reste honnête (fiche §2.5).
    """
    tables = read_csv_tables(chemin)
    return FeuilletLu(
        tables=tables,
        cellules=None,
        minima={nom: (1, 1) for nom in tables},
        repères_natifs={nom: False for nom in tables},
        lecteur="csv (stdlib)",
    )


def read_workbook_tables(path: str) -> dict[str, list[list[object]]]:
    """XLSX/ODS/XLS : schéma complet, valeurs calculées (pas formules brutes). openpyxl requis, pandas en repli.

    NE RENVOIE PLUS `{}`. Ce module promet un schéma de registre ; un classeur que
    personne n'a su ouvrir n'est pas un classeur vide, c'est un échec — et un
    échec rendu en `{silence}` se combinait avec le repli CSV pour produire une
    extraction « réussie » de 16 000 octets. Le vide et la réussite ne sont pas
    le même état, et seul le second mérite un statut d'ingestion réussie.

    LES MOTEURS, et ce qu'ils exigent :
      - `.xlsx` : openpyxl seul suffit (il décode le ZIP et les valeurs mises en
        cache par le tableur — donc les résultats calculés, pas les formules).
      - `.ods`  : passe par pandas, dont le moteur `ods` EXIGE le module `odfpy`.
        Sans lui, pandas lève `ImportError`, ce module le rendait muet, et le
        fichier revenait vide : d'où un test d'absence explicite, pour que la
        cause réelle soit nommée au lieu d'être déduite.
      - `.xls`  : ni lu ni déclaré. Voir `_motif_classeur_illisible`.

    Depuis la représentation structurée (tâche 4.5), la lecture réelle vit dans
    `lire_classeur_feuillet` (qui porte en plus, cellule par cellule, la
    distinction formule / résultat en cache) ; cette fonction en est la vue
    « tables brutes », pour les lecteurs qui ne veulent que des grilles de
    valeurs (`ask_files`).
    """
    return dict(lire_classeur_feuillet(path).tables)


def validate_table(rows: list[list[object]]) -> bool:
    """Validation structurelle extraction PDF (spec 03) : nb colonnes cohérent, en-tête identifiable, cellules vides sous seuil. Pur."""
    data = [r for r in (rows or []) if any(str(c).strip() for c in r)]
    if len(data) < 2:
        return False
    widths = [len(r) for r in data]
    if max(widths) - min(widths) > 1:
        return False
    head = data[0]
    if not any(str(c).strip() for c in head):
        return False
    empty = sum(1 for r in data for c in r if not str(c).strip())
    total = sum(len(r) for r in data) or 1
    return (empty / total) < 0.4


# La phrase d'un lecteur qui a ABOUTI et n'a rien trouvé à lire, dite ICI
# une fois : l'écran, l'outil et l'observation recevront le même MOT, et le
# silence d'un `{}` ne sert plus de langue commune.
PHRASE_FEUILLET_VIDE = "le fichier est lisible mais vide"


class FeuilletVide(RuntimeError):
    """La lecture a ABOUTI et le fichier ne contient aucune ligne de données.

    texte.md §2.3 : « Un fichier vide peut être une ressource valide sans
    passage indexable ; ce n'est pas nécessairement une panne. » Cette
    classe existe pour que CET état ne soit jamais rendu comme une
    défaillance de lecteur : un classeur réduit à son en-tête, un CSV sans
    ligne, sont des FICHIERS réels et l'ingestion doit en tirer le mot
    `vide` de etat.py — pas `echec`, qui dirait « quelque chose est cassé »
    alors que rien ne l'est. Un `RuntimeError` brouillait ces deux états :
    une lecture réussie revenait consignée comme une panne.
    """

    pass


def représenter_feuillet(feuillet: FeuilletLu) -> str:
    """Assemble la REPRÉSENTATION STRUCTURÉE du tableur réellement lu (tâche 4.5).

    Elle remplace l'aplatissement TSV : l'ancien rendu (`Feuille 1\\nLigne1\\tLigne2`)
    livrait des tabulations que personne ne contextualisait — ni en-tête ni
    provenance ni statut des cellules. La nouvelle structure sert les TROIS
    consumers du chemin de tableur (observation/read_document, index de
    recherche, fiche find_files) et suit la fiche tableaux.md :

      1. LE SCHÉMA EN TÊTE (§4.1 : « La fiche de ressource comporte les noms de
         feuilles ou tables, leurs thèmes fondés, en-têtes et informations de volume
         fiables disponibles ») — lecteur, adresses natives ou reconstruites (§2.5),
         noms de feuilles, en-têtes, volumes, types ;
      2. LE SIGNAL D'INDEXATION quand le lecteur dépasse les limites (§4.4 :
         « Une indexation initiale échantillonnée est possible pour de grands
         fichiers, mais elle doit être signalée ») — l'échantillon signé spec 03
         passe AU-DESSUS des lignes restantes, pour être les premières du stock ;
      3. LES CHUNKS de `build_chunks` — naturalisés (phrase minimale), chargés de
         leur repère de cellule et du statut formule / résultat en cache (§3.3,
         §3.4) — puis, pour les GRANDS fichiers, la SUITE de lecture : toutes les
         lignes restantes, naturalisées à l'identique (§4.3 : « On peut lire des
         métadonnées complètes tout en n'indexant qu'une partie des données » —
         la lecture porte tout, c'est la borne d'observation qui borne).

    Rend "" si rien n'est extractible : ce vide, rendu par une lecture
    RÉUSSIE, est un état du FICHIER et non une défaillance — l'appelant
    (`représentation_feuillet`) le rejoue en `FeuilletVide`, que l'ingestion
    écrit `vide` de etat.py, jamais `echec`.
    """
    schema = extract_schema(feuillet.tables, cellules=feuillet.cellules)
    chunks = build_chunks(
        feuillet.tables,
        schema,
        cellules=feuillet.cellules,
    )
    lignes_données: dict[str, list[tuple[int, int, list[object]]]] = {}
    for nom in schema.sheets:
        grille_cellules = (
            feuillet.cellules.get(nom) if feuillet.cellules is not None else None
        )
        lignes_données[nom] = _lignes_données(
            feuillet.tables.get(nom, []), grille_cellules
        )
    non_vides = sum(len(l) for l in lignes_données.values())
    if non_vides == 0:
        # Lecture ABOUTIE, zéro passage indexable (texte.md §2.3) : un état,
        # pas une panne. La montée en `FeuilletVide` découlera de ce retour.
        return ""
    index_complet = len(chunks) >= non_vides

    sortie: list[str] = []
    # 1. Le schéma (fiche find_files ; lecteur + adresses, tableaux.md §5.2 « Mesures »).
    formules = 0
    sans_cache = 0
    if feuillet.cellules:
        for grille in feuillet.cellules.values():
            for rangée in grille:
                for cellule in rangée:
                    if cellule.formule:
                        formules += 1
                        if cellule.valeur is None:
                            sans_cache += 1
    parties = [f"lecteur : {feuillet.lecteur}"]
    if feuillet.cellules:
        parties.append(
            f"formules : {formules}, dont {sans_cache} sans résultat en cache "
            "(arbitrage : jamais recalculé par Thot)"
        )
    elif feuillet.lecteur.strip().lower().startswith("csv"):
        # Un CSV ne porte NI formule NI résultat en cache (fiche §3.3 : ces
        # statuts décrivent un classeur) : la question ne lui est PAS posée.
        # Répondre « aucune » serait trancher une question de catégorie
        # erronée ; la propriété est celle du FORMAT, et la sémantique propre
        # des lignes naturalisées n'a rien à signaler dessus.
        pass
    else:
        parties.append("formules : aucune (le lecteur ne conserve pas de formule)")
    sortie.append("Tableur — " + " ; ".join(parties) + ".")
    for nom in schema.sheets:
        head = schema.headers.get(nom, [])
        types = schema.col_types.get(nom, [])
        principal = ", ".join(head[:4])
        native = feuillet.repères_natifs.get(nom, False)
        adresse = (
            "repères A1 natifs (dimension du classeur)"
            if native
            else "repères de cellule reconstruits (un CSV et les lectures pandas n'ont pas d'adresse native ; convention : colonne A, rang 1)"
        )
        sortie.append(
            f'Feuille "{nom}" : {len(head)} colonne(s) ({principal}), '
            f"{len(lignes_données.get(nom, []))} ligne(s) de données"
            + (f" ; types : {', '.join(types)}" if types else "")
            + f" ; {adresse}."
        )
    # 2. Le signal d'indexation (fiche §4.4 « signalée ») : la borne d'AUDIT.
    if index_complet:
        sortie.append(f"Index : lecture complète ({non_vides} lignes).")
    else:
        sortie.append(
            f"Index : échantillon signalé (spec 03) — {len(chunks)} lignes "
            f"représentatives sur {non_vides} "
            "(premières, dernières, extrêmes numériques, motifs rares) ; "
            "lecture complète : ask_files."
        )
    # 3a. Les chunks indexés : naturalisation + provenance + statut.
    sortie.extend(chunk["texte_embedde"] for chunk in chunks)
    # 3b. La suite de lecture : les lignes HORS échantillon, même naturalisation.
    if not index_complet:
        enchunkées: dict[str, set[int]] = {}
        for chunk in chunks:
            enchunkées.setdefault(chunk["sheet"], set()).add(chunk["ligne"])
        sortie.append(
            "Suite de lecture (lignes hors échantillon, extraites en totalité) :"
        )
        for nom in schema.sheets:
            head = schema.headers.get(nom, [])
            déja = enchunkées.get(nom, set())
            rangée_cellues = (feuillet.cellules or {}).get(nom)
            for num_ligne, position, ligne in lignes_données.get(nom, []):
                if num_ligne in déja:
                    continue
                cellules_ligne = (
                    rangée_cellues[position]
                    if rangée_cellues and position < len(rangée_cellues)
                    else None
                )
                repère = _repère_ligne(nom, ligne, num_ligne, cellules_ligne)
                _, embeddé = naturalize_row(
                    nom,
                    head,
                    ligne,
                    cellules_ligne,
                    repere=repère,
                    ligne=num_ligne,
                )
                sortie.append(embeddé)
    return "\n".join(sortie)


def représentation_feuillet(chemin: str, kind: str) -> str:
    """La représentation structurée du tableur déposé : LIRE le vrai fichier.

    L'ancien `_texte_feuillet` aplatait : un TSV courant d'onglet en onglet, sans
    en-tête contextualisé, sans provenance de cellule, sans statut des formules —
    « l'ingestion aplatisse encore les tableurs en TSV » (tâche 4.5). Il est
    remplacé ici, dans le point de convergence, par l'enchaînement lecteur réel →
    `extract_schema` + `build_chunks` → `représenter_feuillet`.

    Le filet d'ordre est conservé : un `csv` tente LES DEUX lecteurs, un classeur
    en tente deux, et chaque erreur est ramassée pour être citée dans le message
    final — l'appelant doit savoir si son fichier a été refusé par le format ou
    par le moteur. LE VIDE EST DISTINGUÉ DE LA PANNE (texte.md §2.3 :
    « Un fichier vide peut être une ressource valide sans passage
    indexable ; ce n'est pas nécessairement une panne ») : quand au moins un lecteur a réussi à lire
    sans rien rendre, la levée est le nom `FeuilletVide` — un état du
    FICHIER que l'ingestion écrit `vide` — et non l'erreur d'un lecteur
    mêlée aux vraies défaites. Le message reste honnête : chaque lecteur,
    réussi ou en panne, y est cité avec sa phrase.
    """
    erreurs: list[str] = []
    vides: list[str] = []  # lecteurs qui ONT LU — et n'ont rien trouvé à lire
    lecteurs = (
        (lire_csv_feuillet, lire_classeur_feuillet)
        if kind == "csv"
        else (lire_classeur_feuillet, lire_csv_feuillet)
    )
    for lecteur in lecteurs:
        try:
            feuillet = lecteur(chemin)
        except TableurRefuse:
            # Une borne n'est pas une panne de lecteur : passer au repli
            # reviendrait à retenter exactement ce qu'on vient de refuser.
            raise
        except Exception as exc:  # noqa: BLE001 — l'erreur du lecteur est résorbée POUR être citée nommément dans le rendu final
            erreurs.append(f"{lecteur.__name__}: {exc}")
            continue
        rendu = représenter_feuillet(feuillet)
        if rendu.strip():
            return rendu
        vides.append(f"{lecteur.__name__}: {PHRASE_FEUILLET_VIDE}")
    if vides:
        # Une lecture RÉUSSIE a dit son état : `FeuilletVide` (voir classe).
        # Les lecteurs en panne, toujours cités derrière, n'y transforment
        # pas le fichier en panne.
        raise FeuilletVide(
            f"{PHRASE_FEUILLET_VIDE} — la lecture a abouti sans trouver une "
            "ligne de données (" + "; ".join(vides + erreurs) + ")"
        )
    raise RuntimeError(
        "aucun lecteur de tableur n'a produit de contenu (" + "; ".join(erreurs) + ")"
    )
