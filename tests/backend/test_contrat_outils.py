"""Le contrat des six outils de l'orchestrateur, mesuré contre une table de référence.

CE QUE CE FICHIER RÉPARE. L'ancien `test_prompt_tool_parity.py` — renommé
`test_prompt_orchestrateur.py` — épinglait les divergences : il exigeait
`text` de `find_files` et `artifact_ids` de `ask_files` — le CODE, pas la fiche.
Le test passait parce que l'écart était là, et aurait continué de passer après
le renommage seul. Un garde-fou qui protège l'état qu'une phase doit changer
est pire que l'absence de garde-fou : il donne une fausse assurance.

CE QUE CE FICHIER FAIT. Il compare le code livré à une table unique
(`tests/fixtures/outils_signatures.json`), qui n'est qu'une lecture machine des
six fiches d'`docs/intentions/outils/`. Une divergence constatée est classée en
deux catégories, et la catégorie décide du verdict :

  — CONNUE, c'est-à-dire déclarée dans `_ECARTS_CONNUS` avec un identifiant
    stable et le numéro de la phase qui la ferme. Elle est signalée — le
    résumé de `_ECARTS_CONNUS` est émis en avertissement à chaque exécution —
    mais elle ne fait pas échouer la suite : la phase 2 du plan la ferme, et un
    test rouge en permanence est du bruit qu'on apprend à ignorer.
  — INCONNUE, c'est-à-dire constatée et non déclarée. ÉCHEC DUR, avec un
    message qui nomme l'outil, l'argument, les deux valeurs et la ligne à
    écrire pour la déclarer.

CE QUE CE FICHIER MESURE, ET CE QU'IL NE MESURE PAS. Il mesure trois
choses, et trois seulement : le NOM d'un argument, son EXIGENCE, et son TYPE.
Il ne mesure donc ni les valeurs autorisées d'une énumération (celles de
`freshness`, les ratios de `generate_image`, les sources de `web_research`),
ni les codes d'erreur, ni les enveloppes de résultat. Ces écarts sont réels et
portés par `docs/etat_reel_vs_spec.md`, mais aucune de ces trois dimensions
n'est un nom, une obligation ou un type : les revendiquer ici serait faux, et
un test qui promet plus qu'il ne mesure ment deux fois.

Le verrou est à sens double. Une déclaration qui ne correspond plus à aucune
divergence fait aussi échouer la suite : sinon la liste s'encombre de lignes
mortes et cesse de prouver quoi que ce soit. Fermer un écart oblige donc à
retirer sa déclaration.

D'où vient l'état du code. Cinq des six outils viennent de
`registry.definitions()`. `submit_brief` n'y est pas : c'est un handoff, pas un
outil exécutable, et son schéma est écrit à la main dans
`backend/prompts/orchestrator.py`. Les six sont donc lus là où ils sont
réellement publiés au modèle.
"""

from __future__ import annotations

import importlib
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.prompts.orchestrator import SUBMIT_BRIEF_TOOL
from backend.tools.registry import get_default_registry

RACINE = Path(__file__).resolve().parents[2]
TABLE = RACINE / "tests/fixtures/outils_signatures.json"

# Vocabulaire de types fermé : les types de base qu'un schéma JSON peut publier.
# `boolean` et `number` n'apparaissent dans aucune des six fiches — ils sont là
# pour que le typage d'un argument publié reste lisible au lieu de lever sur une
# valeur qu'on ne sait pas nommer. Un type hors de cette liste lève : le parseur
# le dit au lieu de rendre un `str` que l'on comparerait à rien.
TYPES_PUBLIES = ("string", "array", "integer", "number", "boolean", "object")

# Les phases du plan de mise en conformité, et rien d'autre. Une déclaration
# qui porte un autre numéro ne renvoie à aucune phase et n'est pas un suivi.
PHASES_DU_PLAN = (2, 3, 4, 5, 6, 7)


class EcartsAssumes(UserWarning):
    """Les écarts de contrat déclarés, recomptés à chaque exécution.

    Un `Warning`, pas une exception : un écart déclaré ne doit pas faire échouer
    la suite — la phase qui le ferme le fera — mais il ne doit pas disparaître
    non plus. Avertissement et non `print` : pytest le regroupé dans son résumé,
    donc il survit au `-q` et au rapport de CI, là où un affichage silencieux ne
    survit à rien.
    """


# ---------- La table de référence ----------


@dataclass(frozen=True)
class ArgumentAttendu:
    nom: str
    type: str
    requis: bool


@dataclass(frozen=True)
class OutilAttendu:
    nom: str
    fiche: str
    arguments: tuple[ArgumentAttendu, ...]


def _argument_attendu(outil: str, brut: Any) -> ArgumentAttendu:
    """Un argument de la table, ou rien. Une clé inconnue lève : une table
    qu'on lit sans vérifier ses clés laisse passer la faute qu'elle porte."""
    attendu = {"nom", "type", "requis"}
    if not isinstance(brut, dict) or set(brut) != attendu:
        raise AssertionError(
            f"[{outil}] argument de la table mal formé : {brut!r}. "
            f"Clés attendues, et elles seules : {sorted(attendu)}."
        )
    if brut["type"] not in TYPES_PUBLIES:
        raise AssertionError(
            f"[{outil}] type hors vocabulaire pour {brut['nom']!r} : "
            f"{brut['type']!r}. Vocabulaire : {list(TYPES_PUBLIES)}."
        )
    if not isinstance(brut["requis"], bool):
        raise AssertionError(
            f"[{outil}] « requis » doit être un booléen pour {brut['nom']!r} : "
            f"{brut['requis']!r}."
        )
    return ArgumentAttendu(nom=brut["nom"], type=brut["type"], requis=brut["requis"])


def charger_table() -> dict[str, OutilAttendu]:
    """La table de référence, analysée en un état de confiance. Pure.

    Analyse et non validation : le retour ne contient que des dataclasses, et
    une table mal formée lève avant qu'un test ait pu la comparer à quoi que ce
    soit — donc avant qu'un test ait pu passer faute d'arguments.
    """
    brut = json.loads(TABLE.read_text(encoding="utf-8"))
    outils = brut.get("outils")
    if not isinstance(outils, dict) or not outils:
        raise AssertionError(f"{TABLE.name} : clé « outils » absente ou vide.")
    lues: dict[str, OutilAttendu] = {}
    for nom, corps in outils.items():
        if not isinstance(corps, dict) or set(corps) != {"fiche", "arguments"}:
            raise AssertionError(
                f"[{nom}] outil de la table mal formé : clés attendues, et elles "
                f"seules : ['arguments', 'fiche']."
            )
        arguments = corps["arguments"]
        if not isinstance(arguments, list) or not arguments:
            raise AssertionError(
                f"[{nom}] la table ne déclare aucun argument. Un outil sans "
                f"signature ne vérifie rien : le test passerait au hasard."
            )
        lues[nom] = OutilAttendu(
            nom=nom,
            fiche=corps["fiche"],
            arguments=tuple(_argument_attendu(nom, argument) for argument in arguments),
        )
    return lues


@pytest.fixture(scope="session")
def table() -> dict[str, OutilAttendu]:
    return charger_table()


# ---------- L'état réel du code ----------


@dataclass(frozen=True)
class SignaturePubliee:
    """Ce que le modèle voit : les propriétés, et ce que le schéma en exige."""

    proprietes: dict[str, Any]
    requis: frozenset[str]


def _type_publie(nom: str, schema: Any) -> str:
    """Ramène un nœud de schéma JSON à l'un des quatre types de la table. Pure.

    `Optional[X]` arrive en pydantic v2 sous forme de `anyOf` — ou de `type`
    en liste — dont une branche `null`. On garde la branche informative : ce
    qui est comparé au type de la fiche est le contenu, pas la nullabilité,
    qu'aucune fiche n'énonce.
    """
    if not isinstance(schema, dict):
        raise AssertionError(f"[{nom}] schéma illisible : {schema!r}")
    variantes = schema.get("anyOf") or schema.get("oneOf")
    if variantes:
        retenues = [
            v for v in variantes if isinstance(v, dict) and v.get("type") != "null"
        ]
        if not retenues:
            raise AssertionError(
                f"[{nom}] schéma sans branche que « null » : {schema!r}"
            )
        return _type_publie(nom, retenues[0])
    declare = schema.get("type")
    if isinstance(declare, list):
        retenus = [t for t in declare if t != "null"]
        if not retenus:
            raise AssertionError(
                f"[{nom}] type « null » seul dans son schéma : {schema!r}"
            )
        declare = retenus[0]
    if declare not in TYPES_PUBLIES:
        raise AssertionError(
            f"[{nom}] type publié hors vocabulaire : {declare!r}. "
            f"Vocabulaire : {list(TYPES_PUBLIES)}."
        )
    return declare


def _chemin_de(module: str) -> str:
    """`backend.tools.search_info` → `backend/tools/search_info.py`. Pure."""
    fichier = Path(importlib.import_module(module).__file__ or "")
    return str(fichier.relative_to(RACINE))


# `submit_brief` n'est pas au registre : il est cité par les deux endroits qui
# le publient réellement — le schéma que le modèle reçoit, et la liste des
# clés que le validateur admet. Citer l'un sans l'autre laisserait passer une
# divergence qui n'existe que dans l'un des deux.
_HORS_REGISTRE = {
    "submit_brief": "backend/prompts/orchestrator.py:SubmitBriefArgs, "
    "backend/inference/brief.py:_ALLOWED_BRIEF_ARGS",
}


def signatures_publiees() -> dict[str, SignaturePubliee]:
    """Les six signatures, lues là où elles sont réellement publiées. Pure."""
    outils = get_default_registry().all_tools()
    publiees: dict[str, SignaturePubliee] = {}
    for outil in outils:
        parametres = outil.definition()["function"]["parameters"]
        publiees[outil.name] = SignaturePubliee(
            proprietes=dict(parametres.get("properties") or {}),
            requis=frozenset(parametres.get("required") or ()),
        )
    parametres = SUBMIT_BRIEF_TOOL["function"]["parameters"]
    publiees[SUBMIT_BRIEF_TOOL["function"]["name"]] = SignaturePubliee(
        proprietes=dict(parametres.get("properties") or {}),
        requis=frozenset(parametres.get("required") or ()),
    )
    return publiees


def source_de(outil: str) -> str:
    """La citation `fichier:symbole` où l'écart se loge. Pure."""
    if outil in _HORS_REGISTRE:
        return _HORS_REGISTRE[outil]
    reel = next(
        (t for t in get_default_registry().all_tools() if t.name == outil), None
    )
    if reel is None:
        return "outil absent du registre"
    modele = reel.input_model
    return f"{_chemin_de(modele.__module__)}:{modele.__qualname__}"


# ---------- Les écarts ----------


@dataclass(frozen=True)
class Divergence:
    identifiant: str
    outil: str
    argument: str | None
    nature: str
    attendu: str
    observe: str
    # Contrepartie nommée quand elle est unique : sans elle, le lecteur doit
    # deviner quel argument le code a mis à la place de celui qui manque.
    contrepartie: str = ""

    def en_ligne(self) -> str:
        lignes = [
            f"  {self.identifiant}",
            f"      outil     {self.outil}"
            + (f" · argument {self.argument}" if self.argument else ""),
            f"      nature    {self.nature}",
            f"      spécifié  {self.attendu}",
            f"      observé   {self.observe}",
            f"      source    {source_de(self.outil)}",
        ]
        if self.contrepartie:
            lignes.append(f"      probable  {self.contrepartie}")
        return "\n".join(lignes)


def divergences(
    table: dict[str, OutilAttendu], publiees: dict[str, SignaturePubliee]
) -> list[Divergence]:
    """Tout ce qui sépare le code de la table, sans exception ni arrangement.

    L'unité est le triplet (outil, argument, nature) : une divergence n'est
    jamais regroupée avec une autre. Un nom qui manque et le nom qui le remplace
    sont deux constats distincts — le second est un argument que le modèle voit
    et que la fiche ne nomme jamais, et le dissimuler dans le premier serait
    perdre exactement ce qu'on cherche à voir.
    """
    trouvees: list[Divergence] = []
    for outil in sorted(table):
        attendu = table[outil]
        if outil not in publiees:
            trouvees.append(
                Divergence(
                    identifiant=f"{outil}.outil_absent",
                    outil=outil,
                    argument=None,
                    nature="outil absent — la fiche expose un outil que le code ne publie pas",
                    attendu=f"{outil} publié au modèle",
                    observe="absent du registre",
                )
            )
            continue
        publie = publiees[outil]
        nom_attendus = {argument.nom for argument in attendu.arguments}
        absents = nom_attendus - set(publie.proprietes)
        inconnus = set(publie.proprietes) - nom_attendus
        # Un nom absent n'a de correspondant lisible que s'il n'y en a qu'un
        # candidat : au-delà, l'appariement serait une devinette, pas un constat.
        appaires = {
            nom: ", ".join(
                f"{autre}.{nom}.argument_absent" for autre in sorted(absents)
            )
            for nom in sorted(inconnus)
            if len(absents) == 1
        }

        for argument in attendu.arguments:
            if argument.nom not in absents:
                continue
            trouvees.append(
                Divergence(
                    identifiant=f"{outil}.{argument.nom}.argument_absent",
                    outil=outil,
                    argument=argument.nom,
                    nature="argument absent — la table le nomme, le code publié ne le porte pas",
                    attendu=f"{argument.nom} : {argument.type}, "
                    f"{'requis' if argument.requis else 'optionnel'}",
                    observe=f"propriétés publiées : {sorted(publie.proprietes)}",
                )
            )
        for nom in sorted(inconnus):
            trouvees.append(
                Divergence(
                    identifiant=f"{outil}.{nom}.argument_inconnu",
                    outil=outil,
                    argument=nom,
                    nature="argument inconnu — le code le publie, la table ne le déclare pas",
                    attendu=f"propriétés déclarées : {sorted(nom_attendus)}",
                    observe=f"{nom} : {_type_publie(nom, publie.proprietes[nom])}",
                    contrepartie=appaires.get(nom, ""),
                )
            )
        for argument in attendu.arguments:
            if argument.nom in absents:
                continue  # l'obligation et le type de ce nom sont déjà dans `absent`
            if argument.requis != (argument.nom in publie.requis):
                trouvees.append(
                    Divergence(
                        identifiant=f"{outil}.{argument.nom}.obligation",
                        outil=outil,
                        argument=argument.nom,
                        nature="obligation — l'argument existe des deux côtés, pas avec la même exigence",
                        attendu=f"requis : {argument.requis}",
                        observe=f"requis : {argument.nom in publie.requis} "
                        f"(required du schéma : {sorted(publie.requis)})",
                    )
                )
            type_observe = _type_publie(argument.nom, publie.proprietes[argument.nom])
            if argument.type != type_observe:
                trouvees.append(
                    Divergence(
                        identifiant=f"{outil}.{argument.nom}.type",
                        outil=outil,
                        argument=argument.nom,
                        nature="type — même nom, type différent",
                        attendu=argument.type,
                        observe=type_observe,
                    )
                )
    return trouvees


# ---------- Les écarts connus ----------

# Chaque entrée est un écart constaté, assumé, et rattaché à la phase qui le
# ferme. L'identifiant est construit, jamais écrit à la main : il retombe sur
# lui-même tant que la divergence est la même, et le devient faux dès qu'elle
# change de nature — ce qui est exactement ce qu'on veut d'un identifiant.
#
# Chaque ligne porte aussi, dans `docs/etat_reel_vs_spec.md`, les deux citations
# que ce document exige de tout écart. La déclaration ci-dessous dit QUOI et
# PAR QUELLE PHASE ; la ligne du tableau d'écart dit OÙ, avec les preuves.
# Les écarts ont été fermés un à un au fil des phases ; le lot C (tâche 2.13)
# a fait passer `submit_brief` à la signature `{"brief": …}` de la fiche v1.2,
# et ses huit déclarations ont été retirées dans le même commit que leur code.
# La liste reste : la mécanique des deux verrous (écart non déclaré / déclaration
# sans cause) doit survivre à une table vide — c'est elle qui protège, pas ce
# qu'elle contenait. « 0 = succès » : rien n'est à déclarer, rien n'est déclaré.
_ECARTS_CONNUS: dict[str, int] = {}


def _resume_des_ecarts_connus() -> str:
    par_phase: dict[int, list[str]] = {}
    for identifiant, phase in sorted(_ECARTS_CONNUS.items()):
        par_phase.setdefault(phase, []).append(identifiant)
    corps = "\n".join(
        f"  phase {phase} — {len(identifiants)} écart(s)\n"
        + "\n".join(f"      {identifiant}" for identifiant in identifiants)
        for phase, identifiants in sorted(par_phase.items())
    )
    return (
        f"{len(_ECARTS_CONNUS)} écart(s) de contrat CONNU(S) entre le code livré et "
        f"la table de référence. Ils ne sont pas tolérés : ils sont déclarés, "
        f"comptés, et rattachés à la phase qui les ferme.\n{corps}\n"
        f"  Retirer une entrée dès que la phase la ferme — le test échoue alors sur "
        f"une déclaration sans cause."
    )


# ---------- 1. Conformité du code à la table ----------


def test_le_code_ne_diverge_du_contrat_sur_un_ecart_non_declare(table):
    """Le verdict. Rouge dès qu'une divergence n'est pas déclarée, vert sinon."""
    constates = divergences(table, signatures_publiees())
    inconnues = [d for d in constates if d.identifiant not in _ECARTS_CONNUS]
    assert not inconnues, "\n\n".join(
        [
            f"ÉCART DE CONTRAT NON DÉCLARÉ — {len(inconnues)} divergence(s) constatée(s) "
            f"que rien n'assume. Le code diverge de la table de référence, et aucune "
            f"phase du plan ne porte cet écart.\n",
            *(d.en_ligne() for d in inconnues),
            "",
            "Pour les déclarer, ajouter dans tests/backend/test_contrat_outils.py :\n"
            + "\n".join(
                f'    _ECARTS_CONNUS["{d.identifiant}"] = 2,' for d in inconnues
            )
            + "\n\net porter la ligne dans docs/etat_reel_vs_spec.md (§ Écarts / Phase 2), "
            "avec ses deux citations.\n"
            "Si un écart ne doit pas être corrigé, il n'est pas un écart de code : la "
            "fiche est rectifiée, et la ligne du tableau d'écart est supprimée.",
        ]
    )
    # Déclaré n'est pas « toléré en silence » : chaque écart connu est recompté,
    # rattaché à une phase, et publié à chaque exécution.
    warnings.warn(_resume_des_ecarts_connus(), EcartsAssumes, stacklevel=1)


def test_une_declaration_sans_cause_fait_echouer_la_suite(table):
    """Le verrou inverse : une déclaration qui ne correspond plus à rien échoue.

    Sans ce test, la liste s'encombre de lignes mortes au fil des phases et
    cesse de prouver quoi que ce soit : le garde-fou deviendrait un mémorial.
    Fermer un écart oblige donc à retirer sa déclaration, dans le même commit.
    """
    constates = {d.identifiant for d in divergences(table, signatures_publiees())}
    orphelines = sorted(set(_ECARTS_CONNUS) - constates)
    assert not orphelines, "\n\n".join(
        [
            f"DÉCLARATION SANS CAUSE — {len(orphelines)} écart(s) déclaré(s) ne sont plus "
            f"constaté(s). Une déclaration qui survit à sa cause prouve que la suite "
            f"n'a rien regardé.\n",
            *(
                f'  _ECARTS_CONNUS["{identifiant}"] = {_ECARTS_CONNUS[identifiant]},'
                f"  aucun écart correspondant"
                for identifiant in orphelines
            ),
            "",
            "Soit l'écart est résorbé : retirez la déclaration, et fermez la ligne de "
            "docs/etat_reel_vs_spec.md avec la citation du code désormais conforme.",
        ]
    )


def test_chaque_ecart_connu_renvoie_a_une_phase_du_plan():
    """Une déclaration sans phase n'est pas un suivi : c'est une excuse.

    Depuis le lot C, la liste est vide : tous les écarts de la phase 2 sont
    fermés, et « 0 = succès » est désormais le verdict attendu de ce registre.
    Le test reste pour sa partie contraignante intacte — si quelqu'un déclare
    un écart avec un numéro hors plan, il est attrapé ; une liste vide n'est
    plus une faute, c'est l'état nominal.
    """
    hors_plan = {i: p for i, p in _ECARTS_CONNUS.items() if p not in PHASES_DU_PLAN}
    assert not hors_plan, (
        f"écarts déclarés hors plan de mise en conformité : {hors_plan}. "
        f"Phases valides : {list(PHASES_DU_PLAN)}."
    )


# ---------- 2. Dérive de la table par rapport aux fiches ----------

# Les fiches d'intention sont la documentation INTERNE du projet : le dossier
# `docs/` n'est pas livré dans un checkout public. Les deux tests de cette
# section comparent la table aux fiches ; sans les fiches, ils ne comparent
# rien. Ils SAUTENT alors nommément — un échec sur une documentation non
# livrée accuserait le checkout, pas le code. `signature_de_la_fiche`, lui,
# reste exigeant : c'est le test qui saute quand la livraison change, jamais
# le parseur qui s'absente.
RAISON_FICHE_NON_LIVREE = (
    "fiche de contrat d'outil non livrée (documentation interne) — "
    "test ignoré sur un checkout public"
)


def _exiger_les_fiches(table: dict[str, OutilAttendu]) -> None:
    """Saute le test appelant si les fiches de la table ne sont pas livrées."""
    if any(not (RACINE / outil.fiche).is_file() for outil in table.values()):
        pytest.skip(RAISON_FICHE_NON_LIVREE)


_ENTETE_ARGUMENT = re.compile(r"^\|\s*Argument\s*\|")
_LIGNE_SEPARATION = re.compile(r"^\|[\s\-|]+\|$")
_ACCOLADES = re.compile(r"[`*\s]")


def obligation_declaration(texte: str, fiche: str, argument: str) -> bool:
    """La colonne « Obligation » d'une fiche, lue. Pure, ou levée.

    Cinq formulations coexistent dans les six fiches — `Obligatoire`,
    `Optionnel`, `Oui`, `Non`, `Facultative`. Toute autre formulation lève :
    deviner ce qu'un mot voulait dire, c'est lire une fiche à travers soi.
    """
    valeur = texte.strip().lower()
    if valeur.startswith("obligatoire") or valeur == "oui":
        return True
    if (
        valeur.startswith("optionnel")
        or valeur.startswith("facultat")
        or valeur == "non"
    ):
        return False
    raise AssertionError(
        f"{fiche} : obligation illisible pour l'argument {argument!r} — {texte.strip()!r}. "
        f"Formes lues : Obligatoire, Oui / Optionnel, Non, Facultative."
    )


def signature_de_la_fiche(fiche: str) -> list[tuple[str, bool]]:
    """Les (nom, requis) que la fiche déclare. Pure, ou levée.

    Une fiche sans tableau d'arguments, un tableau sans ligne de données, un
    tableau d'arguments en double : tout cela lève. Un parsing qui ne trouve
    rien ne doit JAMAIS produire une liste vide que l'on comparerait à une table
    et qui passerait par défaut — c'est le mode de défaillance que ce test
    existe pour rendre impossible.
    """
    chemin = RACINE / fiche
    if not chemin.is_file():
        raise AssertionError(f"fiche introuvable dans le dépôt : {fiche}")
    lignes = chemin.read_text(encoding="utf-8").splitlines()
    entetes = [
        index for index, ligne in enumerate(lignes) if _ENTETE_ARGUMENT.match(ligne)
    ]
    if len(entetes) != 1:
        raise AssertionError(
            f"{fiche} : {len(entetes)} tableau(x) d'arguments trouvé(s), un attendu "
            f"(en-tête « | Argument | »). Le parsing ne peut pas trancher, donc il "
            f"ne rend aucune signature : c'est un échec, pas une absence."
        )
    lus: list[tuple[str, bool]] = []
    for ligne in lignes[entetes[0] + 1 :]:
        if not ligne.startswith("|"):
            break
        if _LIGNE_SEPARATION.match(ligne):
            continue
        cellules = [cellule.strip() for cellule in ligne.strip().strip("|").split("|")]
        if len(cellules) < 3:
            raise AssertionError(
                f"{fiche} : ligne de tableau à moins de trois colonnes, "
                f"le parsing ne sait pas lire : {ligne!r}"
            )
        nom = _ACCOLADES.sub("", cellules[0])
        if not nom:
            raise AssertionError(
                f"{fiche} : ligne de tableau sans nom d'argument : {ligne!r}"
            )
        lus.append((nom, obligation_declaration(cellules[2], fiche, nom)))
    if not lus:
        raise AssertionError(
            f"{fiche} : le tableau d'arguments ne contient aucune ligne de données. "
            f"Un test qui passe parce qu'il n'a rien trouvé est pire qu'un test absent."
        )
    return lus


def test_le_parsing_des_fiches_produit_une_signature_pour_les_six_outils(table):
    """Le parsing se vérifie avant d'être utilisé. S'il faiblit, il se voit ici.

    Isolé de la comparaison pour qu'un parsing devenu permissif — qui rendrait
    une liste vide, ou absorberait la première fiche venue — ne puisse pas être
    déguisé en « aucun écart » dans le test suivant.
    """
    _exiger_les_fiches(table)
    assert len(table) == 6, f"la table déclare {len(table)} outil(s), six attendus."
    for outil in sorted(table):
        lus = signature_de_la_fiche(table[outil].fiche)
        assert len({nom for nom, _ in lus}) == len(lus), (
            f"{outil} : le tableau d'arguments répète un nom — {lus}."
        )
        for nom, requis in lus:
            assert nom.replace("_", "").isalnum(), (
                f"{outil} : nom d'argument extrait illisible : {nom!r} (lu dans "
                f"{table[outil].fiche})."
            )
            assert isinstance(requis, bool)


def test_la_table_de_reference_ne_diverge_des_fiches(table):
    """La table est une lecture des fiches, pas une seconde spécification."""
    _exiger_les_fiches(table)
    problemes: list[str] = []
    for outil in sorted(table):
        attendu = table[outil]
        lus = signature_de_la_fiche(attendu.fiche)
        declares = {argument.nom for argument in attendu.arguments}
        ranges = {nom for nom, _ in lus}
        par_nom = {argument.nom: argument for argument in attendu.arguments}
        for nom in sorted(ranges - declares):
            problemes.append(
                f"  {outil} · {nom}\n"
                f"      fiche   {attendu.fiche} — « {nom} » y est déclaré\n"
                f"      table   absent — la signature lisible du modèle ne le portera pas"
            )
        for nom in sorted(declares - ranges):
            argument = par_nom[nom]
            problemes.append(
                f"  {outil} · {nom}\n"
                f"      fiche   {attendu.fiche} — la fiche ne nomme pas {nom}\n"
                f"      table   le déclare : {nom}, type {argument.type}, "
                f"{'requis' if argument.requis else 'optionnel'}"
            )
        table_requis = {
            argument.nom for argument in attendu.arguments if argument.requis
        }
        fiche_requis = {nom for nom, requis in lus if requis}
        for nom in sorted(table_requis ^ fiche_requis):
            present_fiche = nom in fiche_requis
            problemes.append(
                f"  {outil} · {nom}\n"
                f"      fiche   {attendu.fiche} — {('requis' if present_fiche else 'optionnel')}\n"
                f"      table   {('requis' if nom in table_requis else 'optionnel')}"
            )
    assert not problemes, "\n".join(
        [
            f"LA TABLE DE RÉFÉRENCE DIVERGE DES FICHES — {len(problemes)} écart(s). "
            f"Une table qui ne suit plus sa fiche n'est plus une spécification, c'est "
            f"une troisième source de vérité en concurrence avec les deux autres.",
            "",
            *problemes,
            "",
            "Corriger la table, ou corriger la fiche — et ce ne sont pas deux versions "
            "du même geste : la fiche décide, la table suit.",
        ]
    )
