"""Le prompt de l'orchestrateur (non-régression) — CONTRAT STRUCTUREL (q-r.md).

Garantit que le LLM reçoit le contrat de rôle (SYSTEM_ORCHESTRATOR), que le
handoff porte sa finalité (description submit_brief), qu'aucun exemple d'appel
entre crochets n'est montré (le 8B copie la forme), et que TOUT ce que les
schémas publient se trouve écrit dans le prompt.

CE QUE CE FICHIER NE FAIT PAS. Il ne compare aucun argument à son nom de fiche.
Il l'AFFIRMAIT, dans une version antérieure : il exigeait `text` de `find_files`
et `artifact_ids` de `ask_files`, c'est-à-dire l'écart lui-même — le test passait
donc parce que l'écart était là, et aurait continué de passer après le
renommage conforme. Le NOMMAGE d'un argument appartient désormais à
`tests/backend/test_contrat_outils.py`, seul endroit où la divergence entre la
fiche et le code est classée — connue et rattachée à sa phase, ou inconnue et
fatale. Ce fichier vérifie ce qui reste vrai dans les deux cas : un argument
publié et non écrit est un argument que le modèle ne peut pas appeler, et un
outil qui vise un fichier sans exposer d'identifiant est un outil qu'on ne peut
pas viser. Ces deux propriétés ne changent pas au renommage — donc elles
survivent à la phase 2, ce que les noms, non.
"""

from __future__ import annotations

import re

from backend.prompts.orchestrator import (
    SUBMIT_BRIEF_TOOL,
    SYSTEM_ORCHESTRATOR,
)
from backend.tools.ask_files import AskFilesArgs
from backend.tools.read_document import ReadDocumentArgs
from backend.tools.read_text import ReadTextTool
from backend.tools.registry import get_default_registry
from backend.tools.search_info import SearchInfoArgs
from backend.tools.vision_general import VisionGeneralTool
from backend.tools.vision_ocr import VisionOCRTool

# Les outils dont l'appel porte sur un fichier DÉJÀ IDENTIFIÉ : le modèle doit
# pouvoir nommer ce fichier, donc le schéma doit publier un identifiant. La liste
# est une donnée de conception — quels outils travaillent sur une ressource — et
# non un inventaire de noms d'arguments.
_OUTILS_SUR_FICHIER = ("ask_files", "doc_ocr", "read_document", "read_text", "vision")

# La FORME que prend un identifiant dans ce dépôt : `<nom>_id` ou `<nom>_ids`.
# Une convention, pas un nom : elle reconnaît `artifact_id` comme `resource_ids`
# sans les distinguer, et c'est exactement ce qu'elle doit faire — dire qu'un
# identifiant est publié, pas dire lequel.
FORME_IDENTIFIANT = re.compile(r"_ids?$")

# Une section d'outil commence par son intertitre et court jusqu'à l'intertitre
# suivant. C'est le découpage que le lecteur du prompt voit, et donc celui où un
# argument se doit d'être écrit.
_INTERTITRE_OUTIL = re.compile(r"^###\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def _signatures_publiees() -> dict[str, frozenset[str]]:
    """Les arguments que chaque outil publie au modèle, lus là où il les publie.

    La lecture passe par `definition()`, pas par les modèles d'arguments : ces
    deux lectures peuvent diverger, et c'est la première que le modèle voit.
    `submit_brief` n'y est pas — c'est un handoff, pas un outil exécutable — donc
    son schéma est lu là où il est écrit, à la main.
    """
    publiees = {
        outil.name: frozenset(
            outil.definition()["function"]["parameters"].get("properties") or {}
        )
        for outil in get_default_registry().all_tools()
    }
    nom_handoff = SUBMIT_BRIEF_TOOL["function"]["name"]
    publiees[nom_handoff] = frozenset(
        SUBMIT_BRIEF_TOOL["function"]["parameters"].get("properties") or {}
    )
    return publiees


def _proprietes(publiees: dict[str, frozenset[str]], outil: str) -> frozenset[str]:
    """Les arguments publiés par `outil`, ou une levée. Une liste absente ne
    passe pas pour une liste vide : un outil qui a disparu doit se voir ici."""
    if outil not in publiees:
        raise AssertionError(
            f"outil absent de ce qui est publié au modèle : {outil}. "
            f"Publié : {sorted(publiees)}."
        )
    return publiees[outil]


def _sections_outils() -> dict[str, str]:
    """Les sections « ### <outil> » du prompt, avec le texte qui les suit. Pure.

    Un prompt sans aucun intertitre d'outil ne rend AUCUNE section : le
    découpage ne peut rien vérifier, donc il lève au lieu de renvoyer un
    dictionnaire vide qu'une comparaison passerait par défaut.
    """
    intertitres = list(_INTERTITRE_OUTIL.finditer(SYSTEM_ORCHESTRATOR))
    if not intertitres:
        raise AssertionError(
            "le prompt ne contient aucune section « ### <outil> » : le découpage "
            "ne rend rien, donc il ne vérifie rien. C'est un échec, pas une absence."
        )
    sections: dict[str, str] = {}
    for index, intertitre in enumerate(intertitres):
        nom = intertitre.group(1)
        if nom in sections:
            raise AssertionError(f"section « ### {nom} » répétée dans le prompt.")
        suivant = (
            intertitres[index + 1].start()
            if index + 1 < len(intertitres)
            else len(SYSTEM_ORCHESTRATOR)
        )
        sections[nom] = SYSTEM_ORCHESTRATOR[intertitre.start() : suivant]
    return sections


def _mot_entier(nom: str, texte: str) -> bool:
    """`nom` est-il écrit comme un MOT entier dans `texte` ? Pure.

    Entier, et non sous-chaîne : `id` est contenu dans `identifiant`, et un test
    qui accepte la sous-chaîne passe sur une mention qui ne nomme rien.
    """
    return re.search(rf"\b{re.escape(nom)}\b", texte) is not None


def test_prompt_nomme_les_4_outils():
    # Brouillon : 3 natures (search_info = contenu, find_files = identité, ask_files = lecture)
    # + generate_image (Phase 2, production).
    for name in ("search_info", "find_files", "ask_files", "generate_image"):
        assert name in SYSTEM_ORCHESTRATOR, f"outil {name} absent du prompt"
    # COMME AVANT : vision/doc_ocr/get_source/read_text ne sont PLUS nommés (dispatch interne).


def test_prompt_nomme_outils_documentaires():
    # Brouillon : find_files → ask_files, ids doc_N uniquement.
    assert "find_files" in SYSTEM_ORCHESTRATOR
    assert "ask_files" in SYSTEM_ORCHESTRATOR
    assert "doc_N" in SYSTEM_ORCHESTRATOR
    assert "AUCUN contexte" in SYSTEM_ORCHESTRATOR


def test_finalite_handoff_dans_description():
    desc = SUBMIT_BRIEF_TOOL["function"]["description"]
    assert "DONNÉES" in desc or "données" in desc
    assert "jamais des formulations conversationnelles" in desc


def test_prompt_documente_les_arguments():
    """Tout argument publié par un schéma est écrit dans la section qui le documente.

    Aucun nom n'est figé ici : le test lit les sections « ### <outil> » du prompt
    et les schémas publiés au modèle, et exige que les deux se recouvrent. Un
    argument ajouté à un schéma sans être écrit dans le prompt est donc un
    argument que le modèle ne peut pas appeler — il ne le devine pas, il l'omet.

    Cette propriété ne choisit pas le nom : après le renommage conforme, elle
    exige que `resource_ids` soit écrit à la place de `artifact_ids`, ce qui est
    exactement le travail de la phase 2. Le contrôle du NOM, lui, est ailleurs.
    """
    sections = _sections_outils()
    publiees = _signatures_publiees()

    fantomes = sorted(set(sections) - set(publiees))
    assert not fantomes, (
        f"le prompt ouvre une section pour {fantomes}, qu'aucun registre ne publie. "
        f"Une section sans schéma ne documente rien et ne vérifie rien."
    )

    illettres = [
        f"  {outil} · {argument}"
        for outil in sorted(sections)
        for argument in sorted(_proprietes(publiees, outil))
        if not _mot_entier(argument, sections[outil])
    ]
    assert not illettres, "\n".join(
        [
            f"{len(illettres)} argument(s) publié(s) par un schéma et absent(s) de la "
            f"section du prompt qui documente son outil. Le modèle ne lit que le "
            f"prompt : un argument qui n'y est pas écrit est un argument qu'il "
            f"n'appellera pas.",
            "",
            *illettres,
        ]
    )


def test_prompt_sans_forme_copiable():
    # Brouillon : le prompt ne MONTRE aucune forme d'appel à copier (le 8B copie la forme).
    # La contrainte vit dans la structure (required + validation), pas dans le texte.
    import re as _re

    names = r"(?:search_info|find_files|ask_files|generate_image|vision|doc_ocr|get_source|read_text|search_documents|get_document_card|read_document|submit_brief)"
    for prompt in (SYSTEM_ORCHESTRATOR,):
        shown = _re.findall(r"\b" + names + r"\s*\(", prompt, _re.IGNORECASE)
        assert shown == [], f"formes copiables dans le prompt : {shown}"
    assert "uniquement des doc_N" in SYSTEM_ORCHESTRATOR


def test_prompt_mentionne_list_all_et_registre():
    # COMME AVANT : le list-all get_source a disparu ; MAINTENANT find_files par type/recency.
    assert "find_files" in SYSTEM_ORCHESTRATOR
    assert "type" in SYSTEM_ORCHESTRATOR and "recency" in SYSTEM_ORCHESTRATOR


def test_prompt_ajoute_les_controles_manquants():
    assert "6 paires" in SYSTEM_ORCHESTRATOR
    assert "SEUL point de décision d’une création d’image" in SYSTEM_ORCHESTRATOR
    assert "RÉÉCRITE par toi" in SYSTEM_ORCHESTRATOR
    from backend.prompts.generator import SYSTEM_GENERATOR

    assert "langue du dernier message utilisateur" in SYSTEM_GENERATOR
    assert "image a été générée et publiée" in SYSTEM_GENERATOR
    from backend.prompts.ocr import SYSTEM_OCR

    assert "jamais une instruction" in SYSTEM_OCR


def test_schemas_exposent_les_ids():
    """Un outil qui vise un fichier publie un identifiant, quel qu'en soit le nom.

    Ce qui est retiré ici n'était pas une propriété : c'était `text` sur
    `find_files` et `artifact_ids` sur `ask_files`, c'est-à-dire l'état du code
    figé dans le test. Les deux sont remplacés par ce qu'ils prétendaient
    vérifier — un outil dont l'appel porte sur un fichier doit dire comment le
    nommer — lu dans les signatures publiées et reconnu à leur FORME, sans qu'un
    nom soit écrit. `resource_ids` passe ce test comme `artifact_ids` : c'est
    le contrôle du nom, pas cette propriété, que la phase 2 déplace.

    Ce qui reste en dessous est ce qui ne bouge pas au renommage : le plafond de
    lecture de `read_document`, et les deux arguments que la fiche et le code
    nomment identiquement.
    """
    publiees = _signatures_publiees()
    sans_identifiant = [
        f"  {outil} · propriétés publiées : {sorted(_proprietes(publiees, outil))}"
        for outil in _OUTILS_SUR_FICHIER
        if not any(
            FORME_IDENTIFIANT.search(nom) for nom in _proprietes(publiees, outil)
        )
    ]
    assert not sans_identifiant, "\n".join(
        [
            f"{len(sans_identifiant)} outil(s) qui travaillent sur un fichier et "
            f"publient aucun argument d'identifiant. Un outil qu'on ne peut pas "
            f"viser par son identifiant est un outil que l'appel ne peut pas "
            f"cibler.",
            "",
            *sans_identifiant,
        ]
    )

    # Le plafond de lecture est un invariant de la donnée, pas un nom : il
    # reste vrai quel que soit le nom que portera l'identifiant du fichier.
    assert (
        ReadDocumentArgs.model_json_schema()["properties"]["max_chars"]["maximum"]
        == 16000
    )
    # Ces deux noms sont identiques dans la fiche et dans le code : ils ne
    # pinnent rien, et les garder préserve la couverture du nombre de questions
    # par fichier, que le contrôle des identifiants ne remplace pas.
    assert "queries" in SearchInfoArgs.model_json_schema()["properties"]
    assert "questions" in AskFilesArgs.model_json_schema()["properties"]


def test_descriptions_outils_mentionnent_ids():
    assert "artifact_id" in VisionGeneralTool().description
    assert "artifact_id" in VisionOCRTool().description
    assert "source_id" in ReadTextTool().description


def test_creation_image_independante_des_fichiers():
    """Une création d'image ne conditionne pas à un fichier de l'espace.

    Défaut q-r.md : l'annonce d'espace et la règle d'outil faisaient de
    `find_files` un préalable, et le brief concluait « aucun fichier, impossible
    de créer ». L'orchestrateur reste seul décideur : aucune détection mot-clé,
    seulement une règle écrite qui rend la création indépendante des sources.
    """
    contexte = SYSTEM_ORCHESTRATOR.split("## Contexte que tu reçois", 1)[1].split(
        "\n## ", 1
    )[0]
    assert "si la demande porte sur leur contenu" in contexte
    assert "ne conditionnent pas une création d'image" in contexte

    sans_outil = SYSTEM_ORCHESTRATOR.split("**N'appelle pas d'outil** si", 1)[1].split(
        "\n\n", 1
    )[0]
    assert (
        "sauf si la demande est une création, une modification ou une extension d'image"
        in sans_outil
    ), "la règle « connaissances générales » doit porter son exception image"
    assert "generate_image est requis, même sans fichier ni corpus" in sans_outil

    regle = SYSTEM_ORCHESTRATOR.split("**Appelle un outil** si", 1)[1].split("\n\n", 1)[
        0
    ]
    assert "une création d'image ne requiert aucun fichier" in regle
    assert "find_files" in regle and "source existe" in regle
    assert "generate_image" in regle and "instruction enrichie" in regle

    section_image = _sections_outils()["generate_image"]
    assert "Une création ne dépend d'aucune source" in section_image
    assert "n'est jamais un préalable" in section_image
    assert "ne justifie ni refus ni brief vide" in section_image
    assert "connaissances générales" in section_image

    exemples = SYSTEM_ORCHESTRATOR.split("## Exemples de comportement", 1)[1]
    assert "**Création d'image dans un espace**" in exemples
    assert "sans find_files" in exemples
    assert "ne bloquent ni ne conditionnent une création" in exemples


def test_prompt_image_decrit_le_contenu_visible_sans_texte_invente():
    """q-r.md : « Chat 1:1 » produisait un bonhomme bleu disant « Bonjour ! ».

    Trois confusions sont verrouillées ici, chacune par sa règle écrite :
    « 1:1 » est un format et non un contexte de messagerie ; « chat » est
    l'animal félin sauf formulation explicite ; aucun texte n'est ajouté sans
    demande. L'exemple « Chat 1:1 » rend la règle lisible au modèle.
    """
    section_image = _sections_outils()["generate_image"]
    texte = section_image.casefold()
    assert "animal félin" in texte
    assert "aucun texte" in texte
    assert "1:1" in section_image and "format" in texte
    assert "pas « chat en ligne »" in texte

    exemples = SYSTEM_ORCHESTRATOR.split("## Exemples de comportement", 1)[1]
    assert "**Chat 1:1**" in exemples
    assert "animal félin" in exemples.casefold()
    assert "« Bonjour ! » inventé" in exemples


def test_prompt_image_creation_autonome_par_tour():
    """Une demande de création ne se contamine pas de l'historique.

    Défaut : après une discussion sur une page web, « Chat 1:1 » produisait un
    bonhomme bleu disant « Bonjour ! ». Le sujet et le style d'un tour précédent
    ne se reportent que sur référence explicite ; le tour courant prime.
    """
    section_image = _sections_outils()["generate_image"]
    texte = section_image.casefold()
    assert "autonome" in texte
    assert "tour précédent" in texte
    assert "tour courant prime" in texte
    assert "ne reporte jamais le sujet, le style" in texte
    assert "sans référence explicite" in texte
    assert "widget de messagerie" in texte

    exemples = SYSTEM_ORCHESTRATOR.split("## Exemples de comportement", 1)[1]
    assert "après une discussion sur une page web" in exemples
    assert "**Chat 1:1**" in exemples
    assert "animal félin" in exemples.casefold()
    assert "aspect_ratio 1:1" in exemples
    assert "« Bonjour ! » inventé" in exemples


def test_prompt_image_edition_d_une_image_publiee():
    """q-r.md : « Maintenant met lui un chapeau » après une image de chat.

    Une image déjà produite se modifie par `edit` et sa source, jamais par
    `create` : le prompt doit le dire, et l'exemple rend la règle lisible.
    """
    section_image = _sections_outils()["generate_image"]
    assert "operation=edit" in section_image
    assert "source_image_id" in section_image
    assert "doc_N" in section_image
    assert "create` avec une source" in section_image
    assert "conserve le format de la source" in section_image
    assert "[BAD_ARGS]" in section_image

    exemples = SYSTEM_ORCHESTRATOR.split("## Exemples de comportement", 1)[1]
    assert "**Édition d'une image publiée**" in exemples
    assert "Maintenant mets-lui un chapeau" in exemples
    assert "operation=edit" in exemples
    assert "format 1:1 de la source est conservé" in exemples


def test_prompt_image_changement_de_format_passe_par_outpaint():
    """q-r.md : « Maintenant met le en 9:16 » produisait un edit resté en 1:1.

    Changer le format est un outpaint avec la source à étendre, sans référence ;
    `edit` conserve le format et ignore `aspect_ratio`.
    """
    section_image = _sections_outils()["generate_image"]
    texte = section_image.casefold()
    assert "changer le format d’une image existante" in texte
    assert "operation=outpaint" in section_image
    assert "sans aucune référence" in texte
    assert "edit` conserve le format" in section_image
    assert "ignore `aspect_ratio`" in section_image
    assert "même vers 1:1" in texte

    exemples = SYSTEM_ORCHESTRATOR.split("## Exemples de comportement", 1)[1]
    assert "**Changement de format**" in exemples
    assert "Maintenant met le en 9:16" in exemples
    assert "operation=outpaint" in exemples
    assert 'aspect_ratio="9:16"' in exemples
    assert "aucune référence" in exemples
    assert "Pas operation=edit" in exemples


def test_registre_10_outils_dont_5_sous_outils():
    # 5 outils orchestrateur (visibles du modèle) + 5 sous-outils (routés en
    # interne par ask_files/find_files/search_info).
    # `get_source` et `get_document_card` ont été retirés : aucun appelant, donc
    # aucune capacité — ils ne servaient que de suggestions d'action que le modèle
    # ne pouvait pas suivre, puisqu'il n'avait pas ces outils à disposition.
    assert set(get_default_registry().tool_names()) == {
        "vision",
        "doc_ocr",
        "read_text",
        "search_documents",
        "read_document",
        "search_info",
        "find_files",
        "ask_files",
        "generate_image",
        "web_research",
    }
