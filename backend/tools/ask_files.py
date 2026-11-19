# tools/ask_files.py — Question ciblée à des fichiers (spec 01/02)
# Schéma : questions 1-4 x ≤300 factuelles autonomes, resource_ids 1-3 (doc_N), paires ≤6. Ids jamais inventés.
# `scope` (fiche §3.1/§3.3) : ciblage facultatif {pages, sheets, ranges, regions} —
# présent, il réduit la lecture aux parties demandées ; invalide, il est REFUSÉ
# en [INVALID_SCOPE], jamais appliqué à moitié en silence.
#
# D'OÙ VIENT CE FICHIER : il interroge la REPRÉSENTATION D'INGESTION d'un fichier déjà
# identifié, et rien d'autre. La Bible §2.5 (l. 163) : « il s'appuie sur le fichier
# d'ingestion associé au document ». §2.6 (l. 189) : « L'ingestion initiale doit être
# effectuée à l'ajout du fichier, sans recommencer systématiquement l'analyse complète
# à chaque question. » Le chemin vivant de la boucle (`loop._execute_ask_files`) faisait
# l'inverse : il routait par type — vision sur les pixels, OCR sur les pages, repli
# lecture — donc CHAQUE question relançait le travail, et répondait depuis un modèle
# plutôt que depuis la donnée.
#
# CE QUE CE FICHIER CONSOMME, et ne recalcule jamais :
#   - `ingestion.etat` : l'ÉTAT d'un artefact, son point d'écriture unique et la seule
#     formulation des motifs. Un fichier `binaire_refuse` n'a rien à interroger, un
#     fichier `en_cours` n'est pas encore interrogeable : les deux se disent avec le
#     MOTIF du dépôt, jamais avec un « information absente » fabriqué sur place. Trois
#     lecteurs qui formulent l'explication différemment produisent trois verdicts sur un
#     même fichier.
#   - `ingestion.etat.texte_ingeste` : la LECTURE UNIQUE de « ce fichier a-t-il été lu ».
#     Elle est appelée UNE FOIS PAR FICHIER, pas une fois par paire question-fichier.
#
# LE PLAFOND EST DANS LA DONNÉE, ET ANNONCÉ. `Observation.payload_ref` conserve au plus
# `observations.PAYLOAD_CHARS` caractères d'une extraction qui en a peut-être produit
# dix fois plus. Servir cette moitié comme si c'était le document rend la réponse FAUSSE
# SANS LE DIRE. L'écart est mesurable : `Observation.result_json` porte `chars_total`, la
# longueur que l'extraction avait produite. C'est le seul endroit du dépôt où l'on peut
# comparer « ce qui a été extrait » à « ce qui a été conservé » — donc une réponse servie
# sur une représentation bornée porte TOUJOURS sa limite, et une absence y est formulée
# comme une absence dans la partie lisible, jamais comme une absence du document.
#
# Modes de rendu : 1 question × n fichiers = transversal ; plusieurs questions = matrice.
from __future__ import annotations

import json as _json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backend.config.defaults import TOOL_LIMITS
from backend.tools.base import BaseTool, ToolContext, ToolResult

_DOC_RE = re.compile(r"^doc_\d+$")
_REPLAGE_RE = re.compile(r"^[A-Za-z]{1,3}\d+:[A-Za-z]{1,3}\d+$")

# Le ciblage §3.3 associé à chaque nature de ressource. La fiche donne la grille
# (pages → PDF documents paginés, sheets/ranges → tableurs, regions → images ou
# pages visualisées) : un ciblage qui ne peut pas s'appliquer à la nature lue est
# REFUSÉ — l'appliquer sans effet serait ignorer silencieusement la demande.
_CIBLES_PAR_NATURE: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pages", "regions"}),
    "sheet": frozenset({"sheets", "ranges"}),
    "image": frozenset({"regions"}),
    "text": frozenset(),
}


class AskFilesScope(BaseModel):
    """Le ciblage facultatif de la fiche §3.1 — un objet, quatre clés, au mot.

    La fiche donne la forme exacte : `scope` est UN objet dont les clés
    énumèrent les pages (entiers), les feuilles (noms), les plages (`A2:D50`)
    et les zones (descriptions). Chaque clé est une liste, chacune facultative ;
    l'objet lui-même est facultatif (tableau d'arguments, obligation « Non »).
    Les limites chiffrées du ciblage sont un point ouvert assumé de la fiche
    (§9), donc aucune borne de longueur n'est inventée ici.
    """

    model_config = ConfigDict(
        extra="forbid",
        # La description publiée au modèle reste courte : le docstring est la
        # note de conception interne, pas la phrase que le 8B lit.
        json_schema_extra={
            "description": "Ciblage optionnel de la lecture : pages (entiers, à partir de 1), "
            "sheets (noms de feuilles), ranges (plages type A2:D50), regions "
            "(descriptions de zones). Une cible non résoluble est refusée en "
            "INVALID_SCOPE, jamais ignorée.",
        },
    )

    pages: list[int] | None = None
    sheets: list[str] | None = None
    ranges: list[str] | None = None
    regions: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _cible_inconnue_invalide(cls, data: object) -> object:
        # §3.3 : un ciblage invalide ne doit pas ignorer silencieusement la
        # demande. Une clé mal écrite refluerait sinon par `extra="forbid"`
        # comme un argument inconnu générique — ici la faute est NOMMÉE comme
        # un ciblage faux, sa clé citée, avant même que le champ ne soit posé.
        if isinstance(data, Mapping):
            inconnues = sorted(set(data) - {"pages", "sheets", "ranges", "regions"})
            if inconnues:
                raise ValueError(
                    "[INVALID_SCOPE] Clé de ciblage inconnue : "
                    + ", ".join(inconnues)
                    + ". "
                    "Le scope ne connaît que quatre clés : pages, sheets, ranges, regions."
                )
        return data

    @field_validator("pages")
    @classmethod
    def _pages_en_entiers(cls, v: list[int] | None) -> list[int] | None:
        """Les pages sont des entiers numérotés à partir de 1, comme la fiche : [4]."""
        if not v:
            return None
        if any(not isinstance(p, int) or isinstance(p, bool) or p < 1 for p in v):
            raise ValueError(
                "[INVALID_SCOPE] pages : liste d'entiers numérotés à partir de 1 (pages [4] = quatrième page)."
            )
        return sorted(dict.fromkeys(v))

    @field_validator("sheets")
    @classmethod
    def _feuilles(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        noms = [(s or "").strip() for s in v]
        if not all(noms):
            raise ValueError(
                "[INVALID_SCOPE] sheets : liste de noms de feuilles non vides."
            )
        return list(dict.fromkeys(noms))

    @field_validator("ranges")
    @classmethod
    def _plages(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        nettoyees = [(r or "").strip() for r in v]
        if not all(_REPLAGE_RE.fullmatch(r) for r in nettoyees):
            raise ValueError(
                "[INVALID_SCOPE] ranges : plages de la forme A2:D50 — deux cellules "
                "(lettre de colonne + numéro de ligne) reliées par deux-points."
            )
        return list(dict.fromkeys(nettoyees))

    @field_validator("regions")
    @classmethod
    def _zones(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        descriptions = [(s or "").strip() for s in v]
        if not all(descriptions):
            raise ValueError(
                "[INVALID_SCOPE] regions : liste de descriptions de zones non vides."
            )
        return list(dict.fromkeys(descriptions))

    @model_validator(mode="after")
    def _cibler_au_moins_une_dimension(self) -> "AskFilesScope":
        # Un scope PRÉSENT qui ne cible rien est un ciblage invalide : c'est la
        # demande d'un filtrage que l'on refuse de poser faussement sur « tout lire ».
        actifs = [
            nom
            for nom in ("pages", "sheets", "ranges", "regions")
            if getattr(self, nom)
        ]
        if not actifs:
            raise ValueError(
                "[INVALID_SCOPE] Un scope présent cible au moins une dimension : "
                "pages, sheets, ranges ou regions. Sans ciblage, omet le champ."
            )
        return self


# Longueur d'une phrase servie comme réponse. Au-delà, c'est une COUPE : elle se dit,
# sinon le lecteur — l'orchestrateur, puis l'utilisateur — croit avoir la phrase
# entière. Une borne nommée vaut mieux qu'un `[:500]` anonyme : c'est elle qu'on annonce.
REPONSE_CHARS = 500


class AskFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[str]
    resource_ids: list[str]
    # La fiche §3.1 : ciblage `scope` optionnel, objet à quatre clés. Optionnel
    # ET exigeant : présent, sa composition est valide ou l'appel est refusé —
    # jamais un ciblage à moitié porté.
    scope: AskFilesScope | None = None

    @field_validator("questions")
    @classmethod
    def _trim_questions(cls, v: list[str]) -> list[str]:
        lim = int(
            TOOL_LIMITS["question_max_chars"]
        )  # 300 — questions, pas queries ColBERT
        qmax = int(TOOL_LIMITS["questions_max"])  # 4
        raw = list(v or [])
        if len(raw) > qmax:
            raise ValueError(
                "[TOO_MANY] Un appel est limité à 4 questions de 300 caractères maximum."
            )
        out = []
        for q in raw:
            t = (q or "").strip()
            if not t:
                continue
            if len(t) > lim:
                raise ValueError(
                    "[TOO_LONG] Un appel est limité à 4 questions de 300 caractères maximum."
                )
            out.append(t)
        dedup = list(
            dict.fromkeys(out)
        )  # questions identiques : déduplication silencieuse
        if not dedup:
            raise ValueError(
                "[EMPTY_QUERY] Écris 1 à 4 questions factuelles et autonomes."
            )
        return dedup

    @field_validator("resource_ids")
    @classmethod
    def _check_ids(cls, v: list[str]) -> list[str]:
        amax = int(TOOL_LIMITS["artifact_ids_max"])  # 3
        raw = list(v or [])
        dedup = list(
            dict.fromkeys([(s or "").strip() for s in raw if (s or "").strip()])
        )
        if not dedup:
            raise ValueError(
                "[MISSING_REQUIRED] Précise au moins un identifiant de ressource (doc_N)."
            )
        if len(raw) > amax or len(dedup) > amax:
            raise ValueError("[TOO_MANY] Un appel est limité à 3 fichiers maximum.")
        for aid in dedup:
            if not _DOC_RE.fullmatch(aid):
                raise ValueError(
                    f"[ID_UNKNOWN] L'id {aid} n'existe pas dans le registre. Retrouve le bon id avec find_files."
                )
        return dedup

    @model_validator(mode="before")
    @classmethod
    def _exiger_des_paires(cls, data: object) -> object:
        # Obligation explicite des deux listes : un validateur de champ ne
        # s'exécute pas sur une valeur ABSENTE, donc un appel sans `questions`
        # inventerait un « mode transversal, 1 question × 0 fichiers » — un faux
        # succès construit sur rien. Un appel sans cible valide échoue ici, en
        # nommant ce qui manque.
        if isinstance(data, Mapping):
            absents = [nom for nom in ("questions", "resource_ids") if nom not in data]
            if absents:
                raise ValueError(
                    f"[MISSING_REQUIRED] {absents[0]} est obligatoire : questions et resource_ids "
                    "vont toujours par paires (au moins une question, au moins un fichier)."
                )
        return data

    @model_validator(mode="after")
    def _check_pairs(self):
        if len(self.questions) * len(self.resource_ids) > int(TOOL_LIMITS["pairs_max"]):
            raise ValueError(
                "[TOO_MANY] Trop de paires question-fichier (max 6). Divise en plusieurs appels."
            )
        return self


def _chunk_evidence_id(db, doc_label: str, artifact_id: str, quote: str) -> str | None:
    """``doc_N:k`` adossé au fragment PERSISTÉ qui contient l'extrait, sinon ``None``.

    Une preuve doit pouvoir être relue : sans ligne `artifact_chunks` derrière elle,
    elle ne vaut rien — mieux vaut « preuve indisponible » qu'un rang fabriqué.
    """
    from backend.search import chunks as _chunks

    try:
        ordinal = _chunks.ordinal_of_quote(db, artifact_id, quote)
    except Exception:
        return None
    return f"{doc_label}:{ordinal}" if ordinal is not None else None


@dataclass(frozen=True)
class Representation:
    """Ce que l'ingestion a produit pour UN fichier, et ce qu'elle ne couvre pas.

    Les trois champs sont les trois issues possibles d'une question, jamais deux à la
    fois : un `texte` à interroger, un `motif_absence` qui dit POURQUOI il n'y en a pas,
    une `limite` qui dit ce que le `texte` ne contient pas. Un fichier sans `texte` n'a
    pas une « réponse vide » : il a un MOTIF, et ce motif est celui du dépôt — donc la
    phrase que l'utilisateur lira ailleurs dans l'application est la même.
    """

    texte: str = ""
    motif_absence: str = ""
    limite: str = ""


@dataclass(frozen=True)
class FichierCible:
    """Un `doc_N` résolu, et la représentation d'ingestion de son fichier.

    Les deux sont lus UNE FOIS par appel, jamais une fois par paire question-fichier :
    trois questions sur le même fichier portent sur le MÊME texte. C'est l'inverse d'une
    relecture par question, et c'est ce que la Bible §2.6 l. 189 demande.
    """

    art_id: str
    artifact: Any
    kind: str
    representation: Representation


def _bornes_du_payload(session, observation_id: str | None) -> tuple[int, int | None]:
    """(caractères CONSERVÉS, caractères EXTRACTS) de l'observation qui porte le texte.

    Les deux viennent de la MÊME ligne : `payload_ref` est ce qui est stocké,
    `result_json.chars_total` ce que l'extraction avait produit. C'est le seul endroit
    du dépôt où l'écart entre les deux est mesurable — et il faut le MESURER sur ces
    deux champs, jamais sur la longueur du texte que nous rendons : `texte_ingeste`
    nous le rend `strip()`é, donc comparer sa longueur à `chars_total` ferait
    annoncer une coupure d'un caractère à chaque payload terminé par un saut de ligne.
    Un rapport faux discreditant le rapport, mieux vaut lire les deux bornes à leur
    source.

    `(0, None)` quand l'observation est illisible : `None` dit « on ne sait pas »,
    et non « rien n'a été extrait ».
    """
    if not observation_id:
        return 0, None
    from backend.db.models import Observation as _Obs

    try:
        ligne = session.get(_Obs, observation_id)
        if ligne is None:
            return 0, None
        mesuree = _json.loads(ligne.result_json or "{}")
        total = int(mesuree.get("chars_total") or 0) if isinstance(mesuree, dict) else 0
    except Exception:
        return 0, None
    return len(ligne.payload_ref or ""), (total if total > 0 else None)


def _limite_de_representation(session, observation_id: str | None) -> str:
    """Ce que la représentation CONSERVÉE ne couvre pas. Vide quand elle est entière.

    Deux bornes, et elles ne disent pas la même chose — confondre les deux ferait
    annoncer une perte qui n'a pas eu lieu, ou taire une perte mesurée :

      - l'extraction a produit PLUS que ce qui est stocké : la coupure est MESURÉE,
        donc on annonce les deux nombres. Un extrait qui s'arrête sans dire où est un
        extrait qui ment, et « absent du document » devient une affirmation
        invérifiable.
      - le stock est AU PLAFOND de conservation : la coupure est IMPOSSIBLE à
        mesurer. L'extraction a pu être amputée en amont, au même plafond, et rien ne
        dit de combien. On annonce donc le plafond atteint — un fait — plutôt qu'une
        perte qu'on ne sait pas chiffrer.

    Le plafond est IMPORTÉ de `observations`, jamais réécrit ici : une seconde valeur
    divergerait sans qu'on le voie, et c'est elle qui borne la donnée.
    """
    from backend.search.observations import PAYLOAD_CHARS as plafond

    conserve, total = _bornes_du_payload(session, observation_id)
    if total is not None and total > conserve:
        return (
            f"représentation d'ingestion tronquée : {conserve} caractères conservés "
            f"sur {total} extraits — la suite n'est pas conservée et ne peut pas être "
            f"interrogée par cet outil"
        )
    if conserve >= plafond:
        return (
            f"représentation d'ingestion arrêtée à sa limite de {plafond} caractères : "
            f"la suite du document n'est pas conservée ici, sans que sa longueur soit connue"
        )
    return ""


def _representation_ingeste(session, art) -> Representation:
    """La représentation d'ingestion d'un artefact, et les phrases qu'elle doit.

    AUCUN MODÈLE, aucune relance, aucune relecture du fichier : le texte vient de ce que
    l'ingestion a déjà écrit, et les phrases de l'état d'ingestion écrit au dépôt.

    LA REPRÉSENTATION COURANTE D'ABORD. C'est elle qui porte le Markdown COMPLET ;
    l'observation n'est qu'un audit borné à `PAYLOAD_CHARS` (16 k) pour le prompt.
    Servir l'observation quand une représentation existe rendrait la fin du
    document invisible à `ask_files` — exactement le défaut que l'étape A répare.

    L'OBSERVATION EST LE REPLI LEGACY. Sans représentation (artefact antérieur à
    l'étape A), on garde l'ancien contrat : payload borné, et la limite annoncée
    avec les deux nombres quand l'écart est mesurable.
    """
    from backend.ingestion import etat as _etat
    from backend.ingestion import representations as _reps
    from backend.search import chunks as _chunks

    etat_art = _etat.lire(art)
    # TOUS les états SANS TEXTE, pas seulement `obsolète`. Une représentation
    # courante peut survivre à une réingestion qui a conclu `binaire_refuse` ou
    # `vide` : la servir donnerait à l'outil un texte que l'état déclare absent.
    # `echec` et `partiel` restent volontairement hors de cet ensemble : un échec
    # peut suivre un succès, et un partiel porte du texte utilisable.
    if etat_art.etat in _etat.ETATS_SANS_TEXTE:
        return Representation(motif_absence=_etat.phrase_sans_texte(art))

    representation = (
        _reps.representation_pour(session, art.id) if art is not None else None
    )
    if representation is not None and (representation.markdown or "").strip():
        limites = []
        if etat_art.etat == _etat.ETAT_TRONQUE:
            # `tronque` parle de la couverture (ex. borne de pages PDF) ; c'est
            # un fait d'ingestion qui reste vrai même quand le Markdown complet
            # est servi. On le dit, sans y ajouter la borne du payload.
            limites.append(etat_art.phrase)
        return Representation(
            texte=representation.markdown.strip(),
            limite=" ; ".join(t for t in limites if t),
        )

    payload, observation_id = _etat.texte_ingeste(session, art, limite=3)
    texte = (payload or "").strip()
    if not texte:
        texte = (_chunks.best_source_text(session, art) or "").strip()
        if not texte:
            # Pas de « réponse vide » : le MOTIF de l'état, mot pour mot. Il couvre
            # aussi le cas inverse — un état qui AFFIRME avoir extrait du texte alors
            # qu'aucune observation ne le corrobore — qu'en donnant une absence nommée.
            return Representation(motif_absence=_etat.phrase_sans_texte(art))
        return Representation(texte=texte)

    etat = _etat.lire(art)
    limites = [_limite_de_representation(session, observation_id)]
    if etat.etat == _etat.ETAT_TRONQUE:
        # Le relevé du dépôt, repris tel quel. `tronque` parle de l'INDEXATION (aperçu
        # borné pour le classement) et pas de ce que nous tenons : c'est une autre
        # borne, et elle se dit avec les mots qui la décrivent, pas avec les nôtres.
        limites.append(etat.phrase)
    return Representation(texte=texte, limite=" ; ".join(t for t in limites if t))


def _answer_one(
    question: str,
    doc_label: str,
    artifact,
    text: str,
    kind: str,
    evidence_ordinal: int | None = None,
    absence: str = "",
    limite: str = "",
) -> dict:
    """Extraction ciblée déterministe : mots de la question dans le texte → extrait + preuve.
    Retourne {artifact_id, name, answer, evidence_id, limits}. Pur (hors IO).

    SES LIMITES, dites ici parce qu'elles sont la conséquence du flip : c'est un
    extracteur LEXICAL, il ne comprend ni les synonymes ni les questions qui ne
    rejouent pas les mots du document (« Qui a signé ? » ne retrouve pas
    « Signataire »). Il rend la phrase qui partage le plus de mots avec la question,
    ou il dit qu'il n'a rien trouvé — et il ne devine jamais. La qualité de la réponse
    est donc celle de la REPRÉSENTATION D'INGESTION : c'est elle qu'il faut enrichir,
    pas ce classement naïf.

    `evidence_ordinal` est l'ordre du fragment PERSISTÉ qui contient l'extrait : c'est
    l'appelant qui le résout en base. Sans fragment, pas de preuve — jamais un
    ``doc_N:0`` ni la position de la phrase dans le texte.

    `absence` est le MOTIF pour lequel il n'y a rien à lire, résolu par l'appelant dans
    l'état d'ingestion. Sans lui, un fichier jamais extrait, un fichier dont
    l'extraction a échoué et un fichier dont le contenu n'est pas du texte
    renvoyaient tous « information absente du document » — donc tous inexistants aux
    yeux de l'orchestrateur, qui ne pouvait donc rien demander d'utile. Le « vide »
    ici, c'est un fichier qui n'a pas été traité, et le dire porte le MOTIF de l'état
    plutôt qu'une phrase inventée sur place.

    `limite` est ce que la représentation TENUE ne couvre pas (§2.6, plafond de
    conservation). Elle n'est pas une décence : sans elle, une réponse servie sur un
    texte coupé se lit comme une réponse complète, et une absence se lit comme une
    absence DU DOCUMENT alors qu'elle n'est peut-être que celle de la partie conservée.
    """
    name = artifact.original_filename or artifact.display_name or doc_label
    # Question hors capacités du type (spec 02) : ex. couleurs sur tableur
    ql = (question or "").lower()
    if kind == "sheet" and any(
        w in ql for w in ("couleur", "color", "décris l'image", "décris la photo")
    ):
        return {
            "artifact_id": doc_label,
            "name": name,
            "answer": "la question ne s'applique pas à ce type de fichier",
            "evidence_id": None,
            "limits": limite,
        }
    body = (text or "").strip()
    if not body:
        return {
            "artifact_id": doc_label,
            "name": name,
            "answer": absence or "information absente du document",
            "evidence_id": None,
            "limits": limite,
        }
    # Recherche naïve : la phrase qui partage le plus de MOTS avec la question.
    import unicodedata as _u

    def _mots(s: str) -> set[str]:
        """Les MOTS d'une chaîne, sans casse ni accent. Pur.

        Des MOTS, et non des sous-chaînes : « est » est contenu dans « prestation », et
        la recherche par sous-chaîne faisait donc remonter en premier une phrase qui ne
        parle pas du tout de la question. Une réponse fausse servie sur un document bien
        ingéré est le même défaut que l'absence de réponse, en plus coûteux : elle a
        l'air d'en être une.
        """
        q = _u.normalize("NFC", s or "").lower()
        q = "".join(c for c in _u.normalize("NFD", q) if not _u.combining(c))
        return {
            t
            for t in "".join(c if c.isalnum() else " " for c in q).split()
            if len(t) > 2
        }

    mots_demandes = _mots(question)
    # Découpe en lignes/phrases, score par recouvrement
    import re as _re

    pieces = [_p.strip() for _p in _re.split(r"\n+|(?<=[.!?])\s+", body) if _p.strip()]
    best, best_hit = "", 0
    for p in pieces:
        hit = len(mots_demandes & _mots(p))
        if hit > best_hit:
            best_hit, best = hit, p
    if best_hit == 0:
        # « Absente du document » serait FAUX dès que la représentation est bornée :
        # l'information peut être précisément dans la partie non conservée. La négative
        # doit donc porter sa portée, pour que l'orchestrateur puisse la vérifier au
        # lieu d'envoyer l'utilisateur chercher une réponse jamais lue.
        answer = (
            f"information absente de la partie lisible de ce fichier — {limite}"
            if limite
            else "information absente du document"
        )
        return {
            "artifact_id": doc_label,
            "name": name,
            "answer": answer,
            "evidence_id": None,
            "limits": limite,
        }
    limites = [limite] if limite else []
    if len(best) > REPONSE_CHARS:
        limites.append(f"phrase tronquée à {REPONSE_CHARS} caractères")
    evidence_id = (
        f"{doc_label}:{evidence_ordinal}" if evidence_ordinal is not None else None
    )
    return {
        "artifact_id": doc_label,
        "name": name,
        "answer": best[:REPONSE_CHARS],
        "evidence_id": evidence_id,
        "limits": " ; ".join(limites),
    }


def _ligne_reponse(bf: dict) -> str:
    """« nom (doc_N) : réponse », puis la preuve, puis la limite. Pur.

    L'ordre est celui de la lecture, et la limite vient EN DERNIER, jamais à l'intérieur
    d'une parenthèse de la preuve : une limite qu'on ne voit qu'à la présence d'une
    preuve disparaît précisément sur les réponses sans fragment — donc sur les fichiers
    que l'orchestrateur n'a pas encore réussis à faire indexer. Le plafonnement doit
    se lire TOUJOURS.
    """
    ligne = f"{bf.get('name')} ({bf.get('artifact_id')}) : {bf.get('answer')}"
    if bf.get("evidence_id"):
        ligne += f" — preuve {bf['evidence_id']}"
    if bf.get("limits"):
        ligne += f" (limite : {bf['limits']})"
    return ligne


def render_ask_files(mode: str, answers: list[dict]) -> str:
    """Rendu texte spec 02 (matrice + transversal). Pur.

    LE TRANSVERSAL REND DÉSORMAIS UNE LIGNE PAR FICHIER, comme la matrice. Il en
    concaténait toutes les réponses sur une seule ligne, ce qui rendait illisible le
    moment où une limite devait apparaître : la limite d'un fichier se retrouvait
    collée à la réponse du suivant, donc attribuable au mauvais document. Une question
    portant sur trois fichiers est par construction une comparaison : elle se lit
    ligne par ligne ou elle ne se lit pas.
    """
    if mode == "transversal":
        q = answers[0]["question"] if answers else ""
        by_file = list(answers[0].get("by_file", [])) if answers else []
        lines = [
            f"[ask_files] mode transversal — 1 question × {len(by_file)} fichiers",
            f"— « {q} »",
        ]
        for bf in by_file:
            lines.append("  " + _ligne_reponse(bf))
        preuves = [str(bf["evidence_id"]) for bf in by_file if bf.get("evidence_id")]
        if preuves:
            lines.append("  Preuves : " + ", ".join(dict.fromkeys(preuves)))
        lines.append("[fin]")
        return "\n".join(lines)
    nq = len(answers)
    nf = len(answers[0].get("by_file", [])) if answers else 0
    lines = [f"[ask_files] mode matrice — {nq} questions × {nf} fichiers"]
    for i, grp in enumerate(answers, start=1):
        lines.append(f"— Question {i} : « {grp.get('question')} »")
        for j, bf in enumerate(grp.get("by_file", []), start=1):
            lines.append(f"  {j}. {_ligne_reponse(bf)}")
    lines.append("[fin]")
    return "\n".join(lines)


def _representation_ciblee(
    rep: Representation, nature: str, cible: AskFilesScope | None
) -> Representation:
    """La représentation RÉDUITE par le ciblage §3.1, avec sa limite énoncée.

    Rien n'est filtré sans le dire : la phrase de ciblage voyage dans
    `limite`, donc le rendu porte « sur quoi la lecture a été restreinte » —
    la condition §5.2 : une comparaison se lit explicitement sur la partie
    effectivement lue, jamais sur tous les éléments.
    """
    if cible is None:
        return rep
    texte_reduit, phrase_ciblage = _applique_ciblage(rep.texte, nature, cible)
    phrases = [t for t in (rep.limite, phrase_ciblage) if t]
    return Representation(
        texte=texte_reduit, motif_absence=rep.motif_absence, limite=" ; ".join(phrases)
    )


def _exige_cible_conforme_a_la_nature(cle: str, nature: str) -> None:
    """Refuse un ciblage que la nature de la ressource ne peut pas porter. Lève [INVALID_SCOPE]."""
    if cle not in _CIBLES_PAR_NATURE.get(nature, frozenset()):
        raise ValueError(
            f"[INVALID_SCOPE] Le ciblage « {cle} » ne s'applique pas à une ressource "
            f"« {nature} » (fiche ask_files §3.3). Retire-le du scope, ou cible la "
            "ressource qui porte cette dimension."
        )


_REPERE_LIGNE = re.compile(r"\[([^\[\]]+)!([A-Z]+[0-9]+(?::[A-Z]+[0-9]+)?)\]$")


def _lignes_reperées(texte: str) -> list[tuple[str, str, str]]:
    """Les lignes portant leur repère de cellule, à la représentation structurée du tableur.

    La représentation d'ingestion (tâche 4.5) marque CHAQUE ligne de données de
    son repère natif `[Feuille!A3:C3]` — docs/intentions/mecanismes/ingestion/
    tableaux.md §2.5 : « Conserver la localisation : feuille/cellule/plage pour
    un tableur ». C'est CE repère, hérité du lecteur réel, qui porte la feuille
    (l'ancien format `# nom` + TSV a disparu avec l'aplatissement) : le ciblage
    `sheets`/`ranges` suit ces repères-là, pas des marques rapprochées.
    Retourne (feuille, plage, ligne d'origine), dans l'ordre du texte. Pur.
    """
    marquées: list[tuple[str, str, str]] = []
    for ligne in (texte or "").split("\n"):
        marquée = _REPERE_LIGNE.search(ligne.strip())
        if marquée:
            marquées.append((marquée.group(1).strip(), marquée.group(2), ligne))
    return marquées


def _plage_de(texte_plage: str) -> tuple[int, int, int, int]:
    """« A2:D50 » → (ligne de départ, ligne d'arrivée, colonne de départ, colonne d'arrivée). Pur.

    La forme est déjà garantie par le schéma (`_REPLAGE_RE`), donc ce parse ne
    gère que la lecture : lettres de colonne (1 à 3) et numéro de ligne.
    """
    gauche, droite = texte_plage.split(":")
    colonne_1, ligne_1 = _cellule_de(gauche)
    colonne_2, ligne_2 = _cellule_de(droite)
    return (
        min(ligne_1, ligne_2),
        max(ligne_1, ligne_2),
        min(colonne_1, colonne_2),
        max(colonne_1, colonne_2),
    )


def _cellule_de(brut: str) -> tuple[int, int]:
    """« B12 » → (colonne 2, ligne 12). Pur — lettre de colonne en base 26."""
    lettres = "".join(c for c in brut if c.isalpha())
    chiffres = "".join(c for c in brut if c.isdigit())
    colonne = 0
    for c in lettres.lower():
        colonne = colonne * 26 + (ord(c) - ord("a") + 1)
    return colonne, int(chiffres)


def _blocs_de_page(texte: str) -> list[str]:
    """Découpe la représentation PAR PAGE physique, pour un ciblage honnête.

    Un PDF ingéré (tâche 4.7) porte une représentation structurée : pdf.md
    §3.6 — « la représentation suit l'ordre de lecture et CONSERVE LES LIMITES
    DE PAGES », matérialisées par des en-têtes « ## Page N ». Le ciblage
    « pages » suit CES repères-là — pas un nombre de paragraphes qui dépend du
    contenu de chaque page (une page à deux blocs décalerait l'adresse de la
    suivante, et l'orchestrateur lirait une autre page que celle demandée).
    Sans en-tête (représentation paginée non structurée) : un bloc par
    paragraphe, l'ancien motif conservé. Pur.
    """
    if not re.search(r"(?m)^## Page [0-9]+$\n?", texte or ""):
        return [b for b in (texte or "").split("\n\n") if b.strip()]
    sections: list[str] = []
    courant: list[str] | None = None
    for ligne in (texte or "").split("\n"):
        if re.fullmatch(r"## Page [0-9]+", ligne.strip()):
            if courant is not None:
                sections.append("\n".join(courant).strip())
            courant = [ligne]
        elif courant is not None:
            courant.append(ligne)
    if courant is not None:
        sections.append("\n".join(courant).strip())
    return sections


def _applique_ciblage(texte: str, nature: str, cible: AskFilesScope) -> tuple[str, str]:
    """Le ciblage §3.1 appliqué au contenu CONSERVÉ : réduire ou refuser, jamais ignorer.

    Chaque branche suit la même loi (§3.3) : une cible qui se résout réduit le
    texte et l'annonce dans la limite ; une cible que le contenu ne peut pas
    porter — page hors de la conservation, feuille absente, ciblage demandé à
    la mauvaise nature — lève [INVALID_SCOPE] en citant ce qui existe vraiment.
    L'application ne suit jamais d'approximation : « presque le ciblage demandé »
    est une demande ignorée en silence. `regions` est la seule dimension
    déclarée au schéma et non résoluble localement (limites de la fiche, §9) :
    la réponse le dit explicitement, la lecture n'étant PAS restreinte par elle.
    """
    actifs = {
        nom: getattr(cible, nom)
        for nom in ("pages", "sheets", "ranges", "regions")
        if getattr(cible, nom)
    }
    if not actifs:
        return texte or "", ""
    phrases: list[str] = []
    courant = texte or ""
    for cle in actifs:
        _exige_cible_conforme_a_la_nature(cle, nature)

    if not courant.strip():
        # Rien à filtrer : la représentation ne porte pas de contenu. Le ciblage
        # n'est pas « oublié » : la phrase le dit, et le motif du dépôt (servi à
        # la place) explique pourquoi il n'y avait rien à réduire.
        return courant, (
            "ciblage « "
            + ", ".join(sorted(actifs))
            + " » sans effet : la représentation "
            "conservée ne porte aucun texte consultable"
        )

    if "pages" in actifs:
        blocs = _blocs_de_page(courant)
        hors_conserve = [p for p in actifs["pages"] if p > len(blocs)]
        if hors_conserve:
            raise ValueError(
                f"[INVALID_SCOPE] La page {hors_conserve[0]} est au-delà de la représentation "
                f"conservée : {len(blocs)} bloc(s) de page existent (fiche ask_files §3.3 : "
                "un ciblage non résoluble est demandé en précision, jamais ignoré)."
            )
        courant = "\n\n".join(blocs[p - 1] for p in actifs["pages"])
        phrases.append(
            f"ciblage pages {actifs['pages']} appliqué : {len(actifs['pages'])} bloc(s) "
            f"conservé(s) sur {len(blocs)}"
        )

    marquées: list[tuple[str, str, str]] = []
    feuilles_marquées: list[str] = []
    if "sheets" in actifs or "ranges" in actifs:
        trouvées = _lignes_reperées(courant)
        if not trouvées:
            raise ValueError(
                "[INVALID_SCOPE] Le contenu conservé ne porte pas de découpage par feuille : "
                "aucune ligne de données n'y porte son repère `[Feuille!…]` (représentation "
                "structurée, tableaux.md §2.5) ; le ciblage ne peut pas s'y appliquer, et "
                "« lire sans ciblage » serait ignorer la demande (fiche ask_files §3.3)."
            )
        marquées = trouvées
        feuilles_marquées = sorted({nom for nom, _, _ in trouvées})

    if "sheets" in actifs:
        disponibles = feuilles_marquées or []
        absentes = sorted(set(actifs["sheets"]) - set(disponibles))
        if absentes:
            raise ValueError(
                f"[INVALID_SCOPE] Feuille « {absentes[0]} » absente du contenu conservé. "
                f"Feuilles disponibles : {', '.join(disponibles) or '(aucune)'} (fiche ask_files §3.3)."
            )
        garder = set(actifs["sheets"])
        courant = "\n".join(ligne for nom, _, ligne in marquées if nom in garder)
        phrases.append(
            f"ciblage sheets {actifs['sheets']} appliqué : seules ces feuilles ont été lues"
        )

    if "ranges" in actifs:
        plages = [_plage_de(r) for r in actifs["ranges"]]
        feuilles_gardées = set(actifs.get("sheets", feuilles_marquées))
        retenues: list[str] = []
        for nom, plage_rep, ligne in marquées:
            if nom not in feuilles_gardées:
                # Un ciblage combiné (sheets + ranges) NE relit pas les feuilles
                # exclues : la réduction sheets a déjà tranché, la vervienne.
                continue
            ligne_de_rep, ligne_a_rep, col_de_rep, col_a_rep = _plage_de(plage_rep)
            for ligne_de, ligne_a, col_de, col_a in plages:
                if max(ligne_de, ligne_de_rep) <= min(ligne_a, ligne_a_rep) and max(
                    col_de, col_de_rep
                ) <= min(col_a, col_a_rep):
                    # La réduction choisit les LIGNES : la naturalisation porte
                    # les en-têtes et les valeurs d'une ligne entière, et les
                    # trancher colonne par colonne laisserait un texte mentant.
                    # La phrase énonce la limite en lignes, comme avant.
                    retenues.append(ligne)
                    break
        total_lignes = len(retenues)
        courant = "\n".join(retenues)
        phrases.append(
            f"ciblage ranges {actifs['ranges']} appliqué : {total_lignes} ligne(s) conservée(s)"
        )

    if "regions" in actifs:
        zones = " », « ".join(actifs["regions"])
        phrases.append(
            f"ciblage « regions » non appliqué : la représentation conservée ne se "
            f"découpe pas par zone, la lecture n'a PAS été restreinte par elle "
            f"(zones demandées : « {zones} »)"
        )

    return courant, " ; ".join(phrases)


def _pairs_par_question(answers: list[dict]) -> list[dict]:
    """Toutes les réponses par fichier, à plat — l'ordre de rendu est l'ordre de citation. Pur."""
    return [bf for g in (answers or []) for bf in (g.get("by_file") or [])]


def _preuves_citees(answers: list[dict]) -> list[str]:
    """`doc_N:k` réellement cités par cette réponse, dans l'ordre, sans doublon.

    Une preuve absente n'est pas une preuve : elle est omise, pas rendue vide.
    Pur."""
    vues: list[str] = []
    for bf in _pairs_par_question(answers):
        evid = str(bf.get("evidence_id") or "").strip()
        if evid and evid not in vues:
            vues.append(evid)
    return vues


def _publies_cites(resolved: dict[str, str]) -> list[str]:
    """`doc_N` réellement résolus — donc réellement lus, jamais seulement nommés.

    La liste vient de `resolved`, pas des réponses : un fichier dont la lecture a
    échoué reste un fichier que la discussion avait le droit de nommer, et c'est ce
    droit — établi par `resolve_allowed_artifacts` — qui alimente le tour. Pur.
    """
    return list(resolved)


class AskFilesTool(BaseTool[AskFilesArgs]):
    name = "ask_files"
    description = (
        "Poser une question ciblée à des fichiers précis (ids doc_N du registre ou d'un résultat). "
        "Lit la REPRÉSENTATION D'INGESTION du fichier — il ne le relit pas et n'appelle aucun modèle, "
        "donc un fichier dont l'ingestion a échoué est annoncé comme tel au lieu d'être réessayé. "
        "1 question × n fichiers = transversal ; plusieurs questions = matrice, avec au maximum 6 paires question-fichier. "
        "Ciblage facultatif scope {pages, sheets, ranges, regions} : réduit la lecture aux parties demandées ; "
        "une cible non résoluble est refusée en INVALID_SCOPE, jamais ignorée. "
        "Rend réponse par question et par fichier avec evidence_id, et signale tout ce que la représentation ne couvre pas."
    )
    input_model = AskFilesArgs
    timeout_s = 15.0
    max_output_chars = 8000

    async def execute(self, args: AskFilesArgs, ctx: ToolContext) -> ToolResult:
        conv_id = ctx.conv_id or ctx.request_id or ""
        # AUCUN `try/except` autour du corps, et c'est délibéré : il rendait toute
        # panne un `[TIMEOUT] délai dépassé`, donc une `KeyError` se lisait comme un
        # délai dépassé — un motif FAUX, que la boucle relance et que l'utilisateur lit.
        # `BaseTool.call` attrape, nomme l'exception et la remonte : c'est lui, le portier.
        from backend.db.database import SessionLocal as _SL
        from backend.db.models import Artifact as _Art
        from backend.search import docrefs as _dr, scope as _scope

        db = _SL()
        try:
            allowed = _scope.resolve_allowed_artifacts(conv_id)
            if allowed == set():
                return ToolResult(
                    ok=False,
                    display_summary="ask_files: aucun document accessible",
                    error="[RESOURCE_UNAVAILABLE] Aucun document n'est accessible pour cette conversation ou cet espace. N'utilise pas ask_files avec un doc_N ; reformule sans source documentaire, ou utilise find_files.",
                )
            # Distingue ID_UNKNOWN (jamais vu) vs ID_OUT_OF_SCOPE (existe hors périmètre) — spec 01/02
            from backend.db.models import DocRef as _DocRef

            resolved: dict[str, str] = {}
            for doc_label in args.resource_ids:
                # 1. Vérifier le DocRef local filtré par conv_id + doc_label.
                local_docref = (
                    db.query(_DocRef)
                    .filter(_DocRef.conv_id == conv_id, _DocRef.doc_label == doc_label)
                    .first()
                )
                # 2. Résoudre le label via la résolution locale (jamais de lookup mondial).
                art_id = _dr.resolve_doc_label(db, conv_id, doc_label)
                # 3. Si la résolution échoue, distinguer « connu puis perdu » vs « jamais connu ».
                if art_id is None:
                    if local_docref is not None:
                        return ToolResult(
                            ok=False,
                            display_summary="ask_files: hors périmètre",
                            error=f"[ID_OUT_OF_SCOPE] L'id {doc_label} a été connu de cette conversation, mais il n'y est plus accessible (retiré, supprimé ou hors espace actif). Retrouve un document accessible avec find_files ; ne retente pas cet id.",
                        )
                    return ToolResult(
                        ok=False,
                        display_summary="ask_files: id inconnu",
                        error=f"[ID_UNKNOWN] L'id {doc_label} n'appartient pas à cette conversation. Ne le réessaie pas tel quel ; retrouve un document accessible avec find_files.",
                    )
                # 4. Même résolu, le fichier doit rester dans le périmètre autorisé.
                if art_id not in allowed:
                    return ToolResult(
                        ok=False,
                        display_summary="ask_files: hors périmètre",
                        error=f"[ID_OUT_OF_SCOPE] L'id {doc_label} appartient à cette conversation, mais le fichier n'y est pas accessible (hors espace actif ou lien retiré). Retrouve un fichier accessible avec find_files ; ne retente pas cet id.",
                    )
                resolved[doc_label] = art_id
            # LA REPRÉSENTATION EST LUE UNE FOIS PAR FICHIER. Trois garde-fous qui
            # accompagnaient la relecture par modèle sont morts avec elle, et les
            # supprimer est un ALLÉGEAGE, pas une perte : la retente par paire (plus
            # rien ici n'appelle un moteur), le `TOO_LARGE` au-delà de 200 000
            # caractères (`best_source_text` rend au plus `PAYLOAD_CHARS` caractères,
            # ou un fichier texte de moins de 200 ko), et la relecture `read_payload`
            # qui n'était atteinte qu'avec un localisateur de page — jamais fourni.
            # Un garde-fou qu'on ne peut pas atteindre n'est pas un garde-fou : c'est
            # une seconde histoire racontée au lecteur, et une qui décrit un
            # comportement qui n'existe plus.
            cibles: dict[str, FichierCible] = {}
            for doc_label, art_id in resolved.items():
                art = db.query(_Art).filter(_Art.id == art_id).first()
                if art is None:
                    raise RuntimeError(
                        f"artefact introuvable pour {doc_label} : {art_id} — le registre "
                        f"le désigne et la table des artefacts ne l'a plus"
                    )
                kind = str(art.kind or "text").lower()
                nom = str(art.original_filename or "").lower()
                if nom.endswith((".xlsx", ".ods", ".csv")):
                    kind = "sheet"
                try:
                    representation = _representation_ciblee(
                        _representation_ingeste(db, art), kind, args.scope
                    )
                except ValueError as exc:
                    # La pluralité d'un ciblage non résoluble est une faute de
                    # l'APPEL, pas une panne : elle se rend comme un refus
                    # nommé, avec ce qui existe réellement, sans jamais se
                    # déguiser en « aucune réponse ».
                    if "[INVALID_SCOPE]" not in str(exc):
                        raise
                    return ToolResult(
                        ok=False,
                        display_summary="ask_files: ciblage invalide",
                        error=str(exc),
                    )
                cibles[doc_label] = FichierCible(
                    art_id=art_id,
                    artifact=art,
                    kind=kind,
                    representation=representation,
                )
            mode = "transversal" if len(args.questions) == 1 else "matrix"
            answers: list[dict] = []
            for q in args.questions:
                by_file: list[dict] = []
                for doc_label, cible in cibles.items():
                    representation = cible.representation
                    # La preuve est le fragment PERSISTÉ qui contient l'extrait, pas sa
                    # position dans le texte : on extrait d'abord, on rattache ensuite
                    # (un seul aller, une seule lecture). Sans fragment, aucune preuve.
                    reponse = _answer_one(
                        q,
                        doc_label,
                        cible.artifact,
                        representation.texte,
                        cible.kind,
                        absence=representation.motif_absence,
                        limite=representation.limite,
                    )
                    by_file.append(
                        {
                            **reponse,
                            "evidence_id": _chunk_evidence_id(
                                db,
                                doc_label,
                                cible.art_id,
                                str(reponse.get("answer") or ""),
                            ),
                        }
                    )
                answers.append({"question": q, "by_file": by_file})
        finally:
            try:
                db.close()
            except Exception:
                pass
        payload = {"tool": "ask_files", "ok": True, "mode": mode, "answers": answers}
        return ToolResult(
            ok=True,
            content=_json.dumps(payload, ensure_ascii=False),
            display_summary=render_ask_files(mode, answers),
            evidence_ids=_preuves_citees(answers),
            public_ids=_publies_cites(resolved),
        )
