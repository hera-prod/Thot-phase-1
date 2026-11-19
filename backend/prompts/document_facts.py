# prompts/document_facts.py — contrat LLM de l'extraction documentaire (Phase 8.4).
#
# D'OÙ VIENT CE FICHIER : l'extraction déterministe de `memoire_documentaire`
# ne voit que des phrases déclaratives repérables par regex. Un document qui
# affirme sans verbe de la liste, ou qui répartit un fait sur plusieurs phrases,
# ne produit rien. Le LLM structuré comble ce trou — mais il ne décide jamais de
# la preuve : il propose des faits CITÉS, le backend vérifie chaque citation
# contre le fragment qu'il désigne.
#
# CE QUI EST FIGÉ ICI :
#   - un OUTIL UNIQUE (`submit_document_facts`) et un schéma strict
#     (`extra="forbid"`) : aucun champ libre, aucune sortie hors schéma ;
#   - au plus 5 faits par document ;
#   - une citation d'au moins 12 caractères, copie exacte d'un fragment.
#
# Le prompt ne contient que des règles de sortie. Les invariants de provenance
# (chunk réel, citation contenue, revision courante) sont vérifiés en Python :
# un prompt n'est pas une frontière de sécurité.
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.services.memoire import (
    _MEMORY_AFFIRMATION_STATUSES,
    _MEMORY_CATEGORIES,
)

# Bornes du contrat 8.4. Elles sont aussi celles du repli déterministe pour le
# nombre de faits ; la longueur de citation est propre au LLM.
MAX_FAITS_LLM = 5
MIN_CITATION_CHARS = 12
MAX_ENTREE_LLM_CHARS = 16_000
MAX_CONTENU_FAIT_CHARS = 300

# UNE SEULE SOURCE pour le vocabulaire : le schéma LLM ne recopie pas les
# énumérations de `memoire`. Une valeur ajoutée d'un côté devient visible de
# l'autre sans dérive silencieuse.
_CATEGORIES = _MEMORY_CATEGORIES
_STATUTS_AFFIRMATION = _MEMORY_AFFIRMATION_STATUSES


class FaitSchema(BaseModel):
    """Un fait proposé par le LLM, avec sa citation et son fragment.

    `extra="forbid"` est la loi « parse, don't validate » : un champ inconnu
    n'est pas ignoré en silence, il invalide la sortie et le repli déterministe
    s'applique. Le modèle ne peut donc pas glisser une métadonnée d'exécution
    (version, provenance, statut de fiche) dans son JSON.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    category: str
    affirmation_status: str
    quote: str
    chunk_id: str

    @field_validator("content", "quote", "chunk_id")
    @classmethod
    def _non_vide(cls, value: str) -> str:
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            raise ValueError("[FAIT_VIDE] un champ requis est vide")
        return text

    @field_validator("content")
    @classmethod
    def _longueur_contenu(cls, value: str) -> str:
        if len(value) > MAX_CONTENU_FAIT_CHARS:
            raise ValueError(
                f"[FAIT_TROP_LONG] content est borné à "
                f"{MAX_CONTENU_FAIT_CHARS} caractères"
            )
        return value

    @field_validator("quote")
    @classmethod
    def _citation_suffisante(cls, value: str) -> str:
        if len(value) < MIN_CITATION_CHARS:
            raise ValueError(
                f"[CITATION_TROP_COURTE] quote doit faire au moins "
                f"{MIN_CITATION_CHARS} caractères"
            )
        return value

    @field_validator("category")
    @classmethod
    def _categorie_connue(cls, value: str) -> str:
        if value not in _CATEGORIES:
            raise ValueError(f"[ENUM_INVALID] catégorie inconnue : {value}")
        return value

    @field_validator("affirmation_status")
    @classmethod
    def _statut_connu(cls, value: str) -> str:
        if value not in _STATUTS_AFFIRMATION:
            raise ValueError(f"[ENUM_INVALID] statut d'affirmation inconnu : {value}")
        return value


class FaitsDocumentArgs(BaseModel):
    """Enveloppe d'appel de l'outil : UNE liste de faits, au plus 5.

    La liste vide est légale : un document peut ne contenir aucun fait
    mémorable. Elle produit alors zéro fait et un statut `done` motivé, jamais
    un échec — l'absence de fait n'est pas une panne de l'extracteur.
    """

    model_config = ConfigDict(extra="forbid")

    # `faits` est REQUIS : une enveloppe vide n'est pas « zéro fait », c'est une
    # sortie hors schéma. Le modèle doit dire explicitement `faits: []`.
    faits: list[FaitSchema] = Field(..., max_length=MAX_FAITS_LLM)


SYSTEM_DOCUMENT_FACTS = """Tu extrais des faits durables depuis un document.

Règles absolues :
- Tu réponds UNIQUEMENT en appelant l'outil submit_document_facts.
- Tu produis au plus 5 faits. La liste vide est autorisée.
- Chaque fait contient :
  * content : le fait en une phrase autonome, 300 caractères maximum ;
  * category : contraintes, decisions, contexte_stable, preferences,
    connaissances_de_travail ou reperes_documentaires ;
  * affirmation_status : decision, hypothese, observation ou
    information_a_verifier ;
  * quote : une copie EXACTE d'au moins 12 caractères prise dans le fragment ;
  * chunk_id : l'identifiant exact du fragment qui porte la citation.
- Ne recopie jamais un identifiant de fragment absent du message.
- N'invente aucune valeur, date, nom ou chiffre absent du document.
- Si le document ne contient aucun fait mémorable, renvoie faits=[].
"""


EXTRACTION_DOCUMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_document_facts",
        "description": (
            "Transmets les faits mémorables extraits du document. Chaque fait "
            "doit citer un fragment existant par son chunk_id et une quote "
            "exacte d'au moins 12 caractères. Maximum 5 faits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "faits": {
                    "type": "array",
                    "maxItems": MAX_FAITS_LLM,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "maxLength": MAX_CONTENU_FAIT_CHARS,
                                "description": "Fait autonome, 300 caractères max.",
                            },
                            "category": {
                                "type": "string",
                                "enum": list(_CATEGORIES),
                            },
                            "affirmation_status": {
                                "type": "string",
                                "enum": list(_STATUTS_AFFIRMATION),
                            },
                            "quote": {
                                "type": "string",
                                "minLength": MIN_CITATION_CHARS,
                                "description": "Copie exacte d'un passage du fragment.",
                            },
                            "chunk_id": {
                                "type": "string",
                                "description": "Identifiant exact d'un fragment fourni.",
                            },
                        },
                        "required": [
                            "content",
                            "category",
                            "affirmation_status",
                            "quote",
                            "chunk_id",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["faits"],
            "additionalProperties": False,
        },
    },
}


def construire_message_utilisateur(
    fragments: list[tuple[str, int, str]],
    *,
    max_chars: int = MAX_ENTREE_LLM_CHARS,
) -> str:
    """Assemble les fragments persistés sous la borne d'entrée du LLM.

    Les fragments sont la SEULE source citable : le prompt ne contient pas le
    texte brut séparément, sinon le modèle pourrait citer une phrase qu'aucun
    fragment ne porte. Le budget inclut l'en-tête : le message rendu ne dépasse
    jamais `max_chars`.
    """
    entete = "FRAGMENTS PERSISTÉS (cite un chunk_id exact) :\n"
    budget = max(0, int(max_chars) - len(entete))
    blocs: list[str] = []
    for chunk_id, ordinal, texte in fragments:
        bloc = f"[chunk_id={chunk_id} ordinal={ordinal}]\n{texte}"
        separateur = "\n\n" if blocs else ""
        if len(bloc) + len(separateur) > budget:
            break
        blocs.append(bloc)
        budget -= len(bloc) + len(separateur)
    return entete + "\n\n".join(blocs)
