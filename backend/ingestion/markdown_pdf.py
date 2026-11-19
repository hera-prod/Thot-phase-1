# ingestion/markdown_pdf.py — Représentation Markdown structurée d'un PDF (tâche 4.7)
#
# FONDEMENT (docs/intentions, lecture seule — citations exactes) :
# - mecanismes/ingestion/pdf.md §1.1 : « Le pipeline rend un PDF exploitable
#   par les outils de Thot sous une représentation Markdown structurée,
#   accompagnée des localisations et limites nécessaires à la recherche et à
#   la lecture ciblée. »
# - pdf.md §3.6 : « La représentation suit l'ordre de lecture et conserve les
#   limites de pages. » avec l'arborescence (page 1 → texte / tables / figures
#   … , plus une couverture globale) et : « Le format physique des repères est
#   à définir » → conventions internes nommées ci-dessous, une seule fois.
# - pdf.md §4.1 (couverture réelle) : « Conserver pages attendues, pages
#   traitées, états locaux, régions non lues et causes connues. Une page
#   rendue n'est pas nécessairement une page entièrement reconnue ; un
#   document extrait n'est pas nécessairement totalement indexé. » → la
#   couverture rendue ICI est celle produite par l'extraction réelle (aucune
#   synthèse inventée) ; le titre ne sort que de `metadata.title` du PDF.
# - representations_exploitables.md §3.2 : « Un PDF reçoit une représentation
#   Markdown structurée. Celle-ci peut intégrer : Le texte natif lorsqu'il est
#   disponible. Le texte OCR des pages ou zones scannées. Les titres, sections
#   et pages. Les tableaux convertis en forme structurée. Les descriptions ou
#   analyses des figures pertinentes » (liste réelle de la fiche, ponctuation
#   des puces aplatie ici).
#
# CONVENTIONS INTERNES NOMMÉES (muets de la fiche, pour ne jamais en poser
# deux) :
# - en-tête de page : `## Page N` (N = numérotation physique à partir de un,
#   pdf.md §2.2) ;
# - couverture : un commentaire machine `<!-- thot:couverture chaine=…
#   pages_attendues=… pages_traitees=… [… ] -->` en tête de document ;
# - la `chaine` vaut la chaîne réellement suivie : `native` (aucun modèle),
#   `ocr` (toutes les pages ont demandé un modèle) ou `mixte` (pages natives
#   et/ou figures décrites par la vision dans le même document) — le mot du
#   retour de `extraire_texte`, tel quel.
# - la coupure par borne est DANS la couverture (pages_attendues >
#   pages_traitees + limite_pages) — plus jamais une ligne « (limite : …) »
#   glissée dans le corps où elle passerait pour du contenu du PDF.
# - une page dont la chaîne modèle a panne reste une page, avec sa mention
#   NOMMÉE `(page N non reconnue — cause)` rendue par
#   `mention_page_non_reconnue` (pdf.md §5.1 : « Échec d'une page |
#   Résultat partiel, autres pages conservées ») ;
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.ingestion import figures_pdf


@dataclass(frozen=True)
class BlocPage:
    """Un bloc de contenu d'une page, avec sa position de lecture.

    `type` : « texte », « table », « figure », « limite ».
    `contenu` : le rendu Markdown du bloc (déjà construit par les fonctions
    dédiées — ce module ne réinterprète rien).
    `page` : numéro physique de la page (1-based).
    `region` : index de la région dans l'ordre de lecture (1-based), ou None
    pour un bloc couvrant la page entière.
    `bbox` : (x0, y0, x1, y1) dans le repère pixel de la page rendue, ou None
    quand la position est inconnue.
    `ordre` : ordre de repli quand `bbox` est absent.

    L'ordre de lecture est reconstruit par `(y0, x0)` : les descriptions de
    figures s'intercalent à leur position, jamais en bloc de fin de page.
    """

    type: str
    contenu: str
    page: int
    region: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    ordre: int = 0


@dataclass(frozen=True)
class PageExtraite:
    """Une page réellement parcourue par l'extraction.

    `blocs`, quand il est fourni, porte l'ordre de lecture complet (texte,
    tables, figures, limites), avec les bbox nécessaires à l'insertion
    positionnelle. `texte`, `tables` et `figures` restent pour les chemins qui
    n'ont pas besoin de blocs ordonnés (page native sans figure, tableur,
    texte brut) : deux représentations d'une même page ne doivent pas
    coexister dans l'assemblage — `blocs` prime quand il est non vide.

    Une page sans élément extrait reste une page — traitée sans contenu, avec
    sa mention explicite (pdf.md §4.1 : « Une page blanche identifiée peut être
    traitée sans contenu »). Rien ne dit qu'elle est « blanche » : seule
    l'absence d'élément extrait est un fait, jamais son interprétation.
    """

    numero: int
    texte: str = ""
    tables: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()
    blocs: tuple[BlocPage, ...] = ()


MENTION_PAGE_SANS_TEXTE_NATIF = "(aucun texte natif reconnu sur cette page)"
MENTION_PAGE_SANS_TEXTE_OCR = "(aucun texte reconnu sur cette page)"

# Mention NOMMÉE d'une page dont la chaîne modèle a panne (pdf.md §5.1 :
# « Échec d'une page → Résultat partiel, autres pages conservées » ; §4.6 :
# « Les résultats aboutis restent utilisables avec leurs limites »). Rendue
# via `PageExtraite` : la page garde son rang physique dans le document, sa
# cause est citée, et les pages restantes continuent — même motif que les
# figures non réexaminées, qui rendent chacune leur cause. Le fail loud et
# le résultat partiel ne se contredisent pas : une mention est un aveu.
MENTION_PAGE_NON_RECONNUE = "(page {numero} non reconnue — {cause})"


def mention_page_non_reconnue(numero: int, cause: str) -> str:
    """La mention honnête d'une page non reconnue. UN format, une source. Pur."""
    return MENTION_PAGE_NON_RECONNUE.format(numero=numero, cause=cause)


def pages_traitees(markdown: str) -> int | None:
    """Nombre de pages réellement parcourues, relu dans la couverture. Pur.

    `None` si la couverture est absente ou illisible : on ne devine pas un
    total, sinon « zéro page reconnue » se confondrait avec « document dont on
    ignore la couverture ». La fonction ne lit QUE le format rendu ici.
    """
    motif = re.search(r"pages_traitees=(\d+)", markdown)
    return int(motif.group(1)) if motif else None


def pages_vides(markdown: str) -> int | None:
    """Nombre de pages sans texte, table ni figure, relu dans la couverture. Pur.

    `None` si la couverture ne porte pas la clé : une couverture ancienne ou
    absente ne doit pas se confondre avec « aucune page vide ». Une page sans
    `pages_vides` n'est pas déclarée vide par déduction — on ne devine pas un
    fait que l'extracteur n'a pas écrit.
    """
    motif = re.search(r"pages_vides=(\d+)", markdown)
    return int(motif.group(1)) if motif else None


def pages_restantes(markdown: str) -> int | None:
    """Nombre de pages non traitées à cause de la borne, relu dans la couverture. Pur.

    `None` si la couverture ne porte pas la clé : une couverture non tronquée
    ne raconte rien. Une valeur absente n'est PAS zéro — c'est « on ne sait pas
    si le document a été coupé ». L'état d'ingestion ne peut donc pas décider
    « tronqué » sur une couverture muette.
    """
    motif = re.search(r"pages_restantes=(\d+)", markdown)
    return int(motif.group(1)) if motif else None


def page_est_vide(page: PageExtraite) -> bool:
    """La page n'a-t-elle aucun contenu extrait ? Pur.

    `blocs` prime quand il est fourni : une page dont tous les blocs sont vides
    est vide, même si `texte` a été laissé pour compatibilité. Une page qui ne
    porte qu'une figure en limite n'est PAS vide — elle trace un contenu repéré.
    """
    if page.blocs:
        return not any(bloc.contenu.strip() for bloc in page.blocs)
    return not page.texte.strip() and not page.tables and not page.figures


def pages_non_reconnues(markdown: str) -> list[int]:
    """Les numéros de page en panne d'un Markdown rendu, dans l'ordre rendu. Pur.

    La mention est RELITE au format ci-dessus : l'état d'ingestion ne parse
    pas un document dont il ignore le vocabulaire — il relit celui que CE
    module rend, le seul qui le porte. Un numéro absent de la liste dit
    « tout est venu » : la fonction ne devine rien d'autre.
    """
    motif = re.compile(re.escape("(page ") + r"(\d+)" + re.escape(" non reconnue"))
    return [int(n) for n in motif.findall(markdown)]


def couverture(
    chaine: str,
    pages_attendues: int,
    pages: list[PageExtraite],
    *,
    limite_pages: int | None,
) -> str:
    """Ligne de couverture réelle de l'extraction, en tête de document. Pur.

    La borne ne figure que si elle a effectivement coupé (pdf.md §4.1 :
    « Conserver pages attendues, pages traitées, états locaux,
    régions non lues et causes connues ») — une borne non atteinte
    ne raconte rien.
    """
    entrees = [
        f"chaine={chaine}",
        f"pages_attendues={pages_attendues}",
        f"pages_traitees={len(pages)}",
    ]
    pages_vides = sum(1 for p in pages if page_est_vide(p))
    if pages_vides:
        entrees.append(f"pages_vides={pages_vides}")
    if limite_pages is not None and pages_attendues > limite_pages:
        entrees.append(f"limite_pages={limite_pages}")
        entrees.append(f"pages_restantes={pages_attendues - len(pages)}")
    return "<!-- thot:couverture " + " ".join(entrees) + " -->"


def bloc_texte(numero_page: int, index_region: int | None, texte: str) -> str:
    """Bloc Markdown d'un texte reconnu dans une région, avec son repère. Pur.

    Une région texte OCRisée garde son repère `page=N région=K` : la
    provenance d'un passage reste explicable, comme pour les tables et les
    figures. Une page entière (OCR de repli, texte natif) n'a pas de région à
    citer — le repère disparaît plutôt que d'inventer un index.
    """
    corps = (texte or "").strip()
    if index_region is None:
        return corps
    return f"<!-- thot:texte page={numero_page} région={index_region} -->\n{corps}"


def bloc_table(numero_page: int, index_region: int | None, texte_table: str) -> str:
    """Bloc Markdown d'une table reconnue par la tâche dédiée, avec repère. Pur.

    Le texte rendu par la tâche « table » du worker est retenu TEL QUEL :
    pdf.md §3.6 demande des « tables structurées et rendu lisible », et la
    retranscription de PaddleOCR-VL EST ce rendu ; ce module n'agrège rien et
    n'invente aucune cellule (tableaux.md §2.3 : « Les données OCR conservent
    leur statut d'extraction visuelle. La normalisation ne transforme pas une
    valeur ambiguë en nombre confirmé »).
    `index_region=None` : la page entière est venue de la tâche table, la
    région ne dit rien de localisable — le repère reste honnête.
    """
    debut = f"<!-- thot:table page={numero_page}"
    if index_region is not None:
        debut += f" région={index_region}"
    en_tete = debut + " -->"
    repere = (
        figures_pdf.repere_region(numero_page, index_region)
        if index_region is not None
        else f"page {numero_page}"
    )
    return f"{en_tete}\n**Tableau** ({repere}) :\n\n{texte_table.strip()}"


def _cle_bloc(bloc: BlocPage) -> tuple[float, float, int]:
    """Clé de tri d'un bloc dans l'ordre de lecture. Pure.

    `(y0, x0)` quand la position est connue. Un bloc sans bbox ne prétend pas
    être en haut de page : il est REPLIÉ EN FIN DE PAGE (`+inf`) et garde son
    ordre fourni entre blocs non positionnés. Le placer en `(0, 0)` l'aurait
    au contraire fait passer avant tout le contenu positionné.
    """
    if bloc.bbox is not None:
        try:
            return (float(bloc.bbox[1]), float(bloc.bbox[0]), bloc.ordre)
        except (TypeError, ValueError, IndexError):
            pass
    return (float("inf"), float("inf"), bloc.ordre)


class BlocFigureIntrouvable(ValueError):
    """Le placeholder d'une figure ne peut pas être remplacé sans deviner.

    Une représentation est une version : remplacer le premier bloc venu ou
    inventer une position produirait un Markdown qui décrit une autre zone que
    celle de la ligne `artifact_figures`. L'étape B s'arrête donc en `limite`,
    avec un motif, plutôt que de réparer à l'aveugle.
    """


def remplacer_bloc_figure(
    markdown: str,
    blocks: list[dict[str, Any]] | None,
    numero_page: int,
    index_region: int,
    nouveau_contenu: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Remplace le placeholder d'une figure dans le Markdown ET dans les blocs.

    Le placeholder est cherché par sa forme EXACTE (`bloc_figure_en_attente`),
    et doit apparaître une seule fois. Zéro occurrence ou plusieurs occurrences
    lèvent `BlocFigureIntrouvable` : l'ambiguïté est un fait à documenter, pas
    une invitation à choisir le premier bloc.

    `blocks_json` doit exister : le patch promet de mettre les deux vues à jour.
    Une représentation sans blocs ne peut pas tenir cette promesse pour une
    figure, elle lève donc plutôt que de ne patcher que le Markdown.
    """
    if not isinstance(markdown, str):
        raise TypeError("le Markdown d'une représentation doit être une chaîne")
    if blocks is None:
        raise BlocFigureIntrouvable(
            "blocs absents : le patch ne peut pas mettre `blocks_json` à jour"
        )

    placeholder = figures_pdf.bloc_figure_en_attente(numero_page, index_region)
    occurrences = markdown.count(placeholder)
    if occurrences == 0:
        raise BlocFigureIntrouvable(
            f"placeholder introuvable pour page {numero_page} région {index_region}"
        )
    if occurrences > 1:
        raise BlocFigureIntrouvable(
            f"placeholder ambigu pour page {numero_page} région {index_region} "
            f"({occurrences} occurrences)"
        )

    positions = [
        index
        for index, bloc in enumerate(blocks)
        if int(bloc.get("page", -1)) == int(numero_page)
        and bloc.get("region") is not None
        and int(bloc.get("region", -1)) == int(index_region)
        and str(bloc.get("type", "")).strip().lower() == "figure"
    ]
    if len(positions) != 1:
        raise BlocFigureIntrouvable(
            f"bloc figure {'introuvable' if not positions else 'ambigu'} pour "
            f"page {numero_page} région {index_region} ({len(positions)} bloc(s))"
        )

    index_bloc = positions[0]
    contenu_actuel = str(blocks[index_bloc].get("contenu", ""))
    if contenu_actuel.count(placeholder) != 1:
        raise BlocFigureIntrouvable(
            f"bloc figure page {numero_page} région {index_region} sans le "
            "placeholder attendu"
        )

    nouveaux_blocs = [dict(bloc) for bloc in blocks]
    nouveaux_blocs[index_bloc]["contenu"] = nouveau_contenu
    return markdown.replace(placeholder, nouveau_contenu, 1), nouveaux_blocs


def blocs_du_document(pages: list[PageExtraite]) -> list[dict[str, Any]]:
    """Blocs ordonnés de toutes les pages, prêts pour `blocks_json`. Pure.

    L'ordre est celui de `représentation_document` : `(y0, x0)` quand la bbox
    existe, repli en fin de page sinon. Le contenu est conservé tel quel — ce
    module ne réinterprète pas ce que les extracteurs ont rendu.
    """
    blocs: list[dict[str, Any]] = []
    for page in pages:
        if not page.blocs:
            # Page rendue sans structure de blocs (chemin natif pur, texte brut,
            # tableur) : son texte EST le bloc, à son rang physique. Ne rien
            # produire ici ferait retomber la représentation sur un bloc unique
            # contenant tout le document, et le patch futur des figures perdrait
            # la page de chaque passage.
            if page.texte.strip():
                blocs.append(
                    {
                        "page": page.numero,
                        "region": None,
                        "type": "texte",
                        "bbox": None,
                        "ordre": 0,
                        "contenu": page.texte,
                    }
                )
            continue
        for bloc in sorted(page.blocs, key=_cle_bloc):
            blocs.append(
                {
                    "page": bloc.page,
                    "region": bloc.region,
                    "type": bloc.type,
                    "bbox": list(bloc.bbox) if bloc.bbox is not None else None,
                    "ordre": bloc.ordre,
                    "contenu": bloc.contenu,
                }
            )
    return blocs


def représentation_document(
    titre: str | None,
    pages: list[PageExtraite],
    *,
    chaine: str,
    pages_attendues: int,
    limite_pages: int | None,
    mention_page_sans_texte: str,
) -> str:
    """Assemble le Markdown structuré du document. Pur.

    `titre` : celui extrait réellement (`metadata.title` du PDF) ou None —
    jamais une invention ; un titre VIDE ne défigure pas la couverture, il la
    simplifie.

    `blocs` représente l'ordre de lecture COMPLET d'une page : texte, tables
    et figures sont triés par `(y0, x0)` — une description de figure
    s'intercale donc entre les blocs de texte qui l'entourent, au lieu
    d'attendre la fin de page. Le repère `page=N région=K` reste porté par le
    bloc lui-même.

    L'ordre des pages est celui du parcours, déjà croissant ; ce module ne
    réordonne pas un ordre qui vient de l'extraction (pdf.md §3.6 : la
    représentation « suit l'ordre de lecture »), il reflète ce qui a été lu.
    """
    morceaux: list[str] = []
    if titre and titre.strip():
        morceaux.append(titre.strip())
    morceaux.append(
        couverture(chaine, pages_attendues, pages, limite_pages=limite_pages)
    )
    for page in pages:
        morceaux.append(f"## Page {page.numero}")
        if page.blocs:
            morceaux.extend(
                bloc.contenu.strip()
                for bloc in sorted(page.blocs, key=_cle_bloc)
                if bloc.contenu.strip()
            )
            if page_est_vide(page):
                morceaux.append(mention_page_sans_texte)
            continue
        morceaux.append(page.texte.strip() or mention_page_sans_texte)
        morceaux.extend(page.tables)
        morceaux.extend(page.figures)
    return "\n\n".join(morceaux).strip()
