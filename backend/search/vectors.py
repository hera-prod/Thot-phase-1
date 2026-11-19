# backend/search/vectors.py — ColBERT (interaction tardive) + fusion RRF (annexe 11, Lot C)
#
# D'OÙ VIENT CE FICHIER : `hash_embedding()` y faisait une frappe lexicale signée —
# UN vecteur par texte, 256 dimensions. C'était un SACCAGE, pas une embedding : deux
# phrases qui partagent un mot ne se ressemblaient que s'ils partageaient le mot.
# Ce fichier le remplace par le VRAI moteur qu'attend la Bible (2.5) : un modèle
# ColBERT, UN VECTEUR PAR JETON, comparé par MaxSim.
#
# POURQUOI COLBERT ET PAS UN ENCODEUR DE PHRASE. Un encodeur de phrase réduit un
# document à un point unique : le document entier doit tenir dans 128 nombres, donc
# le détail se noie. ColBERT garde un vecteur par jeton et ne compare qu'à la FIN :
#   score(requête, document) = Σ_sur_jetons_de_la_requête max_sur_jetons_du_document(q_i · d_j)
# Le mot « livraison » du document peut alors DÉJÀ apparier avec le mot « expédier »
# de la requête, parce que chaque jeton est comparé séparément. C'est ce que le
# hashing-256 ne pouvait pas faire, quelle que soit sa dimension.
#
# OÙ VA QUOI : `encode_document()` à l'ingestion (chunks.py) et par la réindexation ;
# `search_vectors()` au retrieval (fts.py) ; `rrf_fuse()` pour la fusion.
#
# LE MODÈLE. `models/colbert/LFM2.5-ColBERT-350M` — backbone LFM2.5-350M patché
# bidirectionnel (`modeling_lfm2_bidirectional.py`, exécuté par
# `trust_remote_code=True`) + tête dense 1024→128 (`1_Dense/`). Sortie REALLE de
# `AutoModel` : `last_hidden_state` de forme (batch, jetons, 1024) — un vecteur par
# jeton, ce qu'il faut. Il n'y a pas de `pooler_output` : la projection 128 est
# notre affaire, et c'est `1_Dense/model.safetensors` qui la porte.
#
# POURQUOI `transformers` ET NON PYLATE. PyLate est la bibliothèque de référence de
# ce checkpoint, et elle est la mauvaise réponse ICI, pour trois raisons mesurées :
#   1. elle impose un index PLAID, donc un DEUXIÈME stockage à côté de la table
#      `artifact_chunk_vectors` — deux formats à synchroniser, deux réindexations ;
#   2. elle tire faiss/sentence-transformers, des dépendances lourdes dans un sidecar
#      dont le budget est borné par la Bible (8 Go) ;
#   3. tout ce dont nous avons besoin tient ici : MaxSim sur des tenseurs
#      d'attention est `Σ max(q·dᵀ)`, quelques lignes, pas une base de données.
# Ce qui nous coûterait réellement, c'est la négociation du format de stockage — et
# nous la faisons nous-mêmes, dans le format des octets (voir plus bas).
#
# PRÉCISION, ET LE CHOIX EST IMPOSÉ PAR LE BUDGET. 353,3 M paramètres : bf16 = 674 Mio
# de VRAM RÉSIDENT (mesuré : 685 Mio alloués), fp32 = 1359 Mio. Avec les 6,66 Go du
# chemin de discussion, fp32 porterait le total à 8,02 Go — SOUS 8 Go il n'y a plus
# de marge, donc plus de robustesse. bf16 est la PRÉCISION D'ENTRAÎNEMENT du
# checkpoint (`config.json: dtype bfloat16`) : c'est la valeur correcte, pas un
# compromis. Sur CPU on garde fp32, où bf16 serait plus lent ET moins juste.
#
# AUCUN RÉSEAU. Le modèle est lu dans `models/`, jamais sur le hub. `trust_remote_code`
# exécute du code Python VÉCUEMENT FOURNI par ce dépôt : c'est un coût de sécurité
# assumé, et il ne porte que sur notre propre dossier.
from __future__ import annotations

import json
import logging
import os
import struct
import threading
from pathlib import Path

from backend.config.paths import resoudre_dossier_modele

log = logging.getLogger(__name__)

# --- Le format persisté : auto-descriptif, donc jamais deviné -------------------
#
# POURQUOI UN EN-TÊTE DANS LE BLOB. La table `artifact_chunk_vectors` porte une
# colonne `dim`, et `dim` suffirait à distinguer 256 de 128. Mais `dim` ne distingue
# pas deux formats de MÊME largeur : le jour où la tête dense sortira en 256, un
# vecteur 256 « à jour » et un vecteur 256 périmé seront indiscernables, et le moteur
# les confondra. Un en-tête qui se décrit lui-même rend cet état ILLÉGAL : on sait
# toujours, sans convention et sans Deviner, si une ligne est au format courant.
MAGIC = b"THOTC"       # 5 o — « Thot ColBERT »
FORMAT = 1            # 1 o — version du format ; on la monte si le format change
ENTETE = 10           # 5 (magic) + 1 (version) + 2 (dim) + 2 (nb de jetons)
DIM = 128             # sortie de la tête dense `1_Dense` (1024 → 128)
RRF_K = 60            # constante Reciprocal Rank Fusion (Elastic)

# Fenêtres du checkpoint. `DOC_MAX_JETONS` est la sienne : 511. `chunks.py` découpe
# à 500 tokens ESTIMÉS (4 caractères par token) et le texte réel la dépasse — un
# fragment mesuré fait 1985 caractères, soit 513 jetons. L'estimation est donc juste
# à un jeton près, pas plus : c'est l'encodeur qui applique la borne RÉELLE, parce
# que la borne que le modèle connaît est en JETONS et que seul le tokeniseur les
# compte. Tronquer change les embeddings de tous les jetons (l'attention est
# bidirectionnelle) : mesuré, un fragment de 1535 jetons tronqué à 511 voit son score
# passer de 13,700 à 13,799 — un écart qui ne se lit pas « gain » mais « autre chose ».
DOC_MAX_JETONS = 511
# La requête n'est PAS tronquée à 32 jetons, qui est la longueur d'entraînement du
# checkpoint : `search_info` et `find_files` posent des questions en phrases, et couper
# une question en deux est pire que de l'encoder hors distribution. `fts.py` borne
# déjà la requête à 300 caractères ; cette borne est là pour qu'un appelant direct ne
# puisse pas faire croître le coût de MaxSim sans limite.
REQ_MAX_JETONS = 160

# Le checkpoint est cherché dans le dossier `models/` de l'APPLICATION. `REPO_MODELS`
# reste le chemin du poste de développement, et il ne sert plus qu'à nommer le
# modèle dans un message : le chemin RÉEL est rendu par `chemin_modele()`.
MODELE_COLBERT_RELATIF = "colbert/LFM2.5-ColBERT-350M"
REPO_MODELS = Path(__file__).resolve().parents[2] / "models"
MODELE_COLBERT = REPO_MODELS / "colbert" / "LFM2.5-ColBERT-350M"
# La tête dense est RELATIVE au dossier du modèle. Avant c'était un chemin absolu
# figé ici, et `statut_moteur()` le testait avec `chemin / CHEMIN_DENSE` : pathlib
# ignore la base devant un chemin absolu, donc la vérification regardait TOUJOURS
# le modèle du dépôt, jamais celui que `THOT_COLBERT_PATH` désignait. Un chemin
# relatif rend le test incapable de mentir sur son sujet.
CHEMIN_DENSE = "1_Dense/model.safetensors"
PREFIXE_DOC = "[D] "  # le checkpoint distingue requête et document PAR LE PRÉFIXE
PREFIXE_REQ = "[Q] "  # mesuré : sans lui, acc@1 tombe de 0,88 à 0,69 sur paraphrases


class FormatVecteurInconnu(ValueError):
    """Un blob de vecteur n'est pas au format ColBERT attendu.

    Distingué des erreurs génériques parce que ce n'est pas une panne : c'est un
    fragment à RÉINDEXER, et l'appelant doit pouvoir le dire. Un ancien vecteur de
    frappe lexicale (256 flottants, aucun en-tête) atterrit ici systématiquement.
    """


# ---------------------------------------------------------------------------------
# Format : ranger et lire UN fragment entier dans UN blob
# ---------------------------------------------------------------------------------
#
# UNE LIGNE PAR FRAGMENT, et non une ligne par vecteur. Un fragment ColBERT fait
# 30 à 513 jetons, soit 287 en moyenne sur les fragments réels : une ligne par
# vecteur donnerait 73 000 lignes pour le corpus actuel, et la requête devrait
# réassembler 73 000 blocs avant de pouvoir réduire. Un blob par fragment arrive
# contigu en une seule lecture, et la clé primaire de la table reste `chunk_id` —
# donc AUCUNE migration de schéma : le format change dans les octets, pas dans le
# DDL. Volume mesuré sur les 13 fragments du corpus réel : 935 Kio en float16,
# 128 Kio au pire fragment. float16 suffit : sur 6 requêtes de contrôle, l'ordre
# complet est identique au fp32 et l'erreur relative sur le score du meilleur
# document vaut 0,0007 % — soit 25 fois moins que la plus petite marge observée
# entre deux documents (0,005). Stocker en float32 doublerait le volume pour rien.


def pack_vectors(vecteurs: list[list[float]] | "object") -> bytes:
    """Sérialise les vecteurs d'UN fragment : en-tête 10 o + n×128 float16.

    Accepte une matrice numpy/torch de forme (jetons, dim) ou une liste de listes.
    """
    lignes = _comme_lignes(vecteurs)
    if not lignes:
        raise FormatVecteurInconnu(
            "fragment sans vecteur : rien à ranger. Un fragment vide n'a pas de "
            "représentation ColBERT, et en écrire une faisait passer pour indexé.")
    dim = len(lignes[0])
    if dim != DIM:
        raise FormatVecteurInconnu(
            f"dimension {dim} inattendue : le checkpoint projette sur {DIM}. "
            "Un vecteur d'une autre largeur ne se compare pas, il FAIT semblance.")
    if any(len(v) != dim for v in lignes):
        raise FormatVecteurInconnu(
            "matrice de vecteurs non rectangulaire : chaque jeton du fragment doit "
            "avoir le même nombre de composantes.")
    plat: list[float] = [x for v in lignes for x in v]
    entete = MAGIC + bytes([FORMAT]) + struct.pack("<HH", dim, len(lignes))
    return entete + struct.pack(f"<{len(plat)}e", *plat)


def unpack_vectors(blob: bytes | None) -> list[list[float]]:
    """Lit un blob ColBERT en liste de vecteurs de jetons normalisés.

    LÈVE `FormatVecteurInconnu` — et ne rend JAMAIS un vecteur de remplacement —
    dès que l'en-tête ne correspond pas. C'est tout le mécanisme de réindexation :
    un blob sans en-tête (l'ancien hashing-256) ou d'une autre version est refusé
    ICI, donc il ne peut pas être confondu avec un index à jour, et il ne peut pas
    non plus être « interprété quand même » en zéros — ce qui ramènerait, en
    silence, le défaut qu'on répare.
    """
    brut = bytes(blob or b"")
    if len(brut) < ENTETE:
        raise FormatVecteurInconnu(
            f"blob de {len(brut)} o : trop court pour un en-tête de {ENTETE} o. "
            "Ce n'est pas un vecteur ColBERT.")
    if brut[:5] != MAGIC:
        raise FormatVecteurInconnu(
            f"magic {brut[:5]!r} au lieu de {MAGIC!r} : ce blob n'a pas été écrit "
            "par le moteur ColBERT. Vecteur de frappe lexicale antérieur, ou "
            "corruption — dans les deux cas il est À RÉINDEXER.")
    version = brut[5]
    if version != FORMAT:
        raise FormatVecteurInconnu(
            f"format de vecteur v{version}, moteur en v{FORMAT} : la réindexation "
            "doit précéder toute recherche sémantique.")
    dim, n_jetons = struct.unpack("<HH", brut[6:10])
    attendu = n_jetons * dim * 2
    corps = brut[ENTETE:]
    if dim != DIM:
        raise FormatVecteurInconnu(
            f"largeur {dim} dans le blob, {DIM} dans le moteur : index périmé.")
    if len(corps) != attendu:
        raise FormatVecteurInconnu(
            f"corps de {len(corps)} o pour {n_jetons}×{dim} float16, soit {attendu} o "
            "attendus : blob tronqué ou corrompu. À RÉINDEXER.")
    plat = struct.unpack(f"<{n_jetons * dim}e", corps)
    return [list(plat[i * dim:(i + 1) * dim]) for i in range(n_jetons)]


def _comme_lignes(vecteurs) -> list[list[float]]:
    """Normalise une matrice (numpy, torch ou listes) en `list[list[float]]`.

    Le seul endroit où l'on tolère trois représentations : l'encodeur renvoie du
    torch, le lecteur de test du numpy, et la rédaction d'un fragment renvoie des
    listes. Ailleurs, une seule forme circule.
    """
    if vecteurs is None:
        return []
    if hasattr(vecteurs, "tolist"):          # numpy
        return [list(r) for r in vecteurs.tolist()]
    if hasattr(vecteurs, "detach"):          # torch
        return [list(r) for r in vecteurs.detach().cpu().tolist()]
    return [list(r) for r in vecteurs]


def texte_indexable(contexte: str | None, texte: str | None) -> str:
    """CE QUE LE MOTEUR ENCODE POUR UN FRAGMENT : le contexte, puis le texte.

    UN SEUL endroit en décide, et l'ingestion comme la réindexation y passent.
    Deux chemins qui choisiraient différemment produiraient deux index qui ne se
    parlent pas ; le symptôme serait « la recherche trouve moins bien APRÈS
    réindexation », sans cause visible. Le contexte y est à dessein : il porte le
    titre du document et la position du fragment, et une occurrence comme « le
    document » y devient identifiable.
    """
    return f"{contexte or ''}\n{texte or ''}".strip()


# ---------------------------------------------------------------------------------
# Le moteur : chargé UNE fois, gardé résident
# ---------------------------------------------------------------------------------
#
# RÉSIDENT, SANS CYCLE DE DÉCHARGEMENT — un choix mesuré. Le chemin de discussion
# tient 6,66 Go de VRAM ; ColBERT en bf16 en ajoute 685 Mio, pour 7,35 Go au total,
# sous les 8 Go de la Bible. Un cycle de déchargement ne ferait donc RIEN gagner en
# mémoire ici, et coûterait un rechargement de 674 Mio à chaque recherche. Le motif
# séquentiel du chemin IMAGE (décharger avant de charger) est forcé là-bas parce que
# 5,32 + 4,96 Go dépassent 8 Go ; il ne s'applique pas ici, où rien ne déborde.
# On garde donc le modèle chargé, comme le PaddleOCR garde son worker.

class _MoteurCharge:
    """Le moteur chargé, une fois, en mémoire. Les cinq pièces dont il est fait.

    Déclaré comme type et non comme `dict` : un dictionnaire ferait porter au
    lecteur la question « qu'y a-t-il là-dedans ? » à chaque ligne qui le lit.
    Ici la réponse est dans la déclaration, une fois.
    """

    __slots__ = ("model", "tokenizer", "dense", "torch", "device")

    def __init__(self, model, tokenizer, dense, torch) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.dense = dense
        self.torch = torch
        self.device = model.device


_VERROU = threading.Lock()
_MOTEUR: _MoteurCharge | bool | None = None   # None = pas tenté ; False = indisponible


class MoteurIndisponible(RuntimeError):
    """Le modèle ColBERT n'a pas pu être chargé. Le message porte la cause."""


VARIABLE_MODELE = "THOT_COLBERT_PATH"


def chemin_modele() -> Path:
    """Où est le checkpoint ColBERT, selon les règles de l'APPLICATION.

    Avant : `THOT_COLBERT_PATH` ou le chemin calculé depuis `__file__`. C'est-à-dire
    `parents[2]/models`, donc le SOURCE. Dans un sidecar gelé, `__file__` est un
    fichier extrait dans `_MEIPASS` : le dossier de modèles ne s'y trouve pas, et le
    moteur se déclarait absent alors qu'il était posé à côté de l'application. La
    résolution passe donc par `backend.config.paths`, qui cherche dans les racines
    de l'application — dont le dossier de données, où un modèle se range sur une clé.

    La variable reste une SURCHARGE et non une condition : absente, ce n'est qu'un
    niveau sauté ; posée et morte, elle est écartée et le motif est journalisé. Dans
    les deux cas, c'est `resoudre_fichier_modele` qui en décide — une seule règle
    pour tous les exécutables et tous les modèles de l'application.
    """
    return resoudre_dossier_modele(
        MODELE_COLBERT_RELATIF, surcharge=(os.environ.get(VARIABLE_MODELE) or "").strip() or None
    )


def moteur_desactive() -> bool:
    """Vrai si l'environnement a interdit le moteur.

    Le test l'utilise pour ne pas charger 685 Mio dans la suite : on ne suppose
    jamais qu'un modèle est là, et on ne suppose jamais qu'il n'est pas là.
    """
    return str(os.environ.get("THOT_COLBERT_ENABLED", "1")).strip().lower() in (
        "0", "false", "non", "off")


def statut_moteur() -> dict:
    """CE QUE LE MOTEUR SAIT FAIRE, sans rien charger. Pur.

    Répond à la seule question qui compte pour l'honnêteté du produit : la
    recherche sémantique fonctionne-t-elle, et sinon pourquoi pas ? Le reste du
    code ne devine rien ; il appelle ceci, ou il appelle `encodeur()`.
    """
    if moteur_desactive():
        return {"moteur": "desactive", "raison": "THOT_COLBERT_ENABLED vaut 0",
                "chemin": str(chemin_modele()), "resident": False}
    chemin = chemin_modele()
    # Les trois pièces du checkpoint, testées RELATIVES au dossier trouvé : c'est
    # la seule façon que `THOT_COLBERT_PATH` soit réellement vérifié au lieu de
    # l'être contre le modèle du dépôt.
    manquants = [n for n in ("config.json", "model.safetensors", CHEMIN_DENSE)
                 if not (chemin / n).exists()] if chemin.is_dir() else ["dossier absent"]
    if manquants:
        return {"moteur": "absent",
                "raison": "modèle ColBERT introuvable (" + ", ".join(manquants) + ")",
                "chemin": str(chemin), "resident": False}
    return {"moteur": "colbert", "raison": "", "chemin": str(chemin),
            "resident": _MOTEUR not in (None, False), "dim": DIM,
            "document_max_jetons": DOC_MAX_JETONS}


def _charger() -> "_MoteurCharge":
    """Charge le backbone et la tête dense. Déterministe, une seule fois.

    Lève `MoteurIndisponible` avec la cause : un moteur absent doit se dire, pas
    se contourner en rendant un index vide que l'appelant prendra pour « rien à
    signaler ».
    """
    global _MOTEUR
    with _VERROU:
        # `THOT_COLBERT_ENABLED=0` est consulté AVANT le cache, et non après. Un
        # modèle déjà chargé rendrait sinon l'interrupteur inopérant : le premier
        # test qui a utilisé ColBERT déciderait du comportement de tous les
        # suivants, dans un ordre qui n'est pas celui du lecteur. L'interrupteur
        # doit vouloir dire « pas de moteur sémantique », sans condition.
        if moteur_desactive():
            raise MoteurIndisponible(statut_moteur()["raison"])
        if isinstance(_MOTEUR, _MoteurCharge):
            return _MOTEUR
        if _MOTEUR is False:
            raise MoteurIndisponible(statut_moteur()["raison"])
        etat = statut_moteur()
        if etat["moteur"] != "colbert":
            _MOTEUR = False
            raise MoteurIndisponible(etat["raison"])
        try:
            import torch
            from safetensors.torch import load_file
            from transformers import AutoModel, AutoTokenizer
        except Exception as exc:
            _MOTEUR = False
            raise MoteurIndisponible(
                f"transformers/safetensors/torch indisponibles ({exc}). Le moteur "
                "sémantique exige ces trois paquets ; sans eux, `pip install -r "
                "requirements.txt`.") from exc
        chemin = etat["chemin"]
        try:
            gpu = torch.cuda.is_available()
            dtype = torch.bfloat16 if gpu else torch.float32
            tokenizer = AutoTokenizer.from_pretrained(chemin)
            # Le README du checkpoint fait `tokenizer.pad_token = eos_token`, et le
            # pad ne sert QUE si on complète une requête par expansion — ce qu'on ne
            # fait pas (voir REQ_MAX_JETONS). On l'aligne quand même : c'est la
            # convention du modèle, et elle évite qu'un futur appelant découvre
            # une divergence de tokenisation.
            tokenizer.pad_token = tokenizer.eos_token
            # `trust_remote_code` exécute modeling_lfm2_bidirectional.py, qui
            # remplace GLOBALEMENT `create_causal_mask` et `Lfm2ShortConv.forward`
            # dans transformers.models.lfm2. Sans effet aujourd'hui (le LLM de Thot
            # passe par llama.cpp), mais c'est un effet de processus, donc il se
            # note ici. `config.json` porte `disable_flash_attention: true` : on
            # demande sdpa, jamais flash_attention_2, que le checkpoint refuse.
            model = AutoModel.from_pretrained(
                chemin, trust_remote_code=True, dtype=dtype, attn_implementation="sdpa")
            model = model.to("cuda" if gpu else "cpu").eval()
            # `chemin` est une CHAÎNE ici (elle vient de `statut_moteur()`), donc
            # on rejoue le chemin plutôt que d'y ajouter un segment.
            dense = load_file(str(Path(chemin) / "1_Dense" / "model.safetensors"))
            poids = dense.get("linear.weight")
            if poids is None or tuple(poids.shape) != (DIM, 1024):
                raise ValueError(
                    f"tête dense inattendue : {tuple(poids.shape) if poids is not None else None}, "
                    f"attendu ({DIM}, 1024). Le dossier modèle n'est pas celui de ce moteur.")
            _MOTEUR = _MoteurCharge(model, tokenizer,
                                    poids.to(model.device).float(), torch)
            return _MOTEUR
        except Exception as exc:
            _MOTEUR = False
            raise MoteurIndisponible(
                f"chargement ColBERT impossible depuis {chemin} ({type(exc).__name__}: {exc})"
            ) from exc


def decharger_moteur() -> None:
    """Oublie le modèle chargé (tests, ou libération explicite de VRAM)."""
    global _MOTEUR
    with _VERROU:
        _MOTEUR = None


class encodeur:  # noqa: N801 — un `with encodeur() as e:` se lit comme un verbe
    """Contexte : `with encodeur() as e:` puis `e.document(txt)` / `e.requete(txt)`.

    Un contexte plutôt qu'une fonction nue parce que le modèle doit être CHARGÉ AU
    MOMENT DE L'USAGE : l'ingestion d'un fichier ne doit pas payer 674 Mio de
    mémoire si aucun fragment n'est à encoder, et la recherche doit pouvoir
    constater l'absence du modèle sans l'avoir déjà payé. Il lève
    `MoteurIndisponible` — l'appelant décide quoi faire d'une panne annoncée.
    """

    def __enter__(self) -> "encodeur":
        self._m = _charger()
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def _encoder(self, texte: str, prefixe: str, borne: int):
        """Un texte → ses vecteurs de jetons. `None` si le texte n'a aucun jeton.

        Le moteur est rangé dans un attribut TYPÉ plutôt que dans un `dict` opaque :
        `dict` fait porter au lecteur le travail de savoir ce qu'il y a dedans, et
        le fait à chaque ligne. Ici, une seule un seul décompactage le rend, et chaque accès est
        vérifié par le lecteur.
        """
        m: _MoteurCharge = self._m
        torch = m.torch
        tokenizer = m.tokenizer
        ids = tokenizer(prefixe + (texte or ""), return_tensors="pt")["input_ids"][0]
        # La borne en JETONS s'applique ICI, après tokenisation : c'est la seule
        # façon de la connaître. `chunks.py` ne peut qu'estimer (4 caractères par
        # jeton) — mesuré, un fragment réel de 1985 caractères fait 513 jetons.
        # `-1` réserve la place du séparateur final que ColBERT ajoute.
        if len(ids) + 1 > borne:
            ids = ids[: borne - 1]
        if len(ids) < 2:
            return None
        tenseur = ids.unsqueeze(0).to(m.model.device)
        with torch.no_grad():
            etats = m.model(
                input_ids=tenseur,
                attention_mask=torch.ones_like(tenseur)).last_hidden_state[0]
        # Forme RÉELLE de la sortie : (jetons, 1024) — un vecteur par jeton, sans
        # `pooler_output`. La projection 128 puis la normalisation L2 PAR JETON sont
        # la suite du calcul ColBERT, pas une commodité : sans normalisation, un
        # MaxSim se lit dans la longueur des vecteurs au lieu de leur direction.
        #
        # La projection se fait dans la PRÉCISION DU MODÈLE (bf16 sur GPU, fp32 sur
        # CPU) : la tête dense est stockée en float32, et un produit bf16 × fp32 est
        # refusé par torch. La normalisation, elle, se fait en float32 — diviser par
        # une norme calculée en bf16 (~3 chiffres significatifs) introduirait dans
        # chaque vecteur une erreur de norme, et c'est la norme qui décide du score.
        projete = etats @ m.dense.to(etats.dtype).T
        return torch.nn.functional.normalize(projete.float(), dim=-1)

    def document(self, texte: str) -> "object | None":
        """Vecteurs de jetons d'un fragment : matrice (jetons, 128), normalisée.

        `None` si le texte n'a aucun jeton — un fragment vide. On rend `None` et
        non une matrice vide : les deux se compareraient sans erreur et
        participeraient au classement avec un score de zéro, c'est-à-dire comme
        s'ils avaient été trouvés et rejetés.
        """
        return self._encoder(texte, PREFIXE_DOC, DOC_MAX_JETONS)

    def requete(self, texte: str) -> "object | None":
        """Vecteurs de jetons d'une requête : matrice (jetons, 128), normalisée."""
        return self._encoder(texte, PREFIXE_REQ, REQ_MAX_JETONS)


def maxsim(requete, document) -> float:
    """Score ColBERT : Σ_sur_jetons_requête max_sur_jetons_document (q_i · d_j).

    Le `max` PAR JETON est l'interaction tardive : chaque jeton de la requête
    choisit le fragment dont UN jeton lui ressemble le plus, et c'est la somme de
    ces meilleurs appariements qui classe. Une somme de cosinus sur des vecteurs
    de phrase ne ferait pas cela : elle ne pourrait pas isoler le bon jeton.

    TORCH SI DISPONIBLE, PYTHON SINON. Mesuré sur le corpus réel (13 fragments,
    341 jetons en moyenne) : 200 ms en Python pur, contre 14 ms avec une seule
    multiplication matricielle. Ce n'est pas une optimisation de confort : le
    Python fait ici 13 × 341 × 13 × 128 ≈ 7,2 millions de multiplications
    scalaires par requête, et il les fait dans l'interpréteur. `torch` étant déjà
    une dépendance du moteur, l'employer ne coûte aucune dépendance nouvelle — et
    le repli Python reste, pour que `maxsim` soit calculable sans torch (c'est ce
    qui permet aux tests de format de tourner sans modèle).
    """
    if requete is None or document is None:
        return 0.0
    q = _comme_lignes(requete)
    d = _comme_lignes(document)
    if not q or not d:
        return 0.0
    torch = _torch_si_present()
    if torch is not None:
        qv = torch.as_tensor(q, dtype=torch.float32)
        dv = torch.as_tensor(d, dtype=torch.float32)
        return float((qv @ dv.T).max(dim=-1).values.sum())
    best = [max(sum(a * b for a, b in zip(qi, dj)) for dj in d) for qi in q]
    return float(sum(best))


def _torch_si_present():
    """torch, ou None. Import tardif : `maxsim` doit rester calculable sans torch."""
    try:
        import torch
        return torch
    except Exception:
        return None


# ---------------------------------------------------------------------------------
# La recherche : filtrée par PORTÉE, jamais un balayage du corpus
# ---------------------------------------------------------------------------------
#
# CE QUI ÉTAIT FAIT, ET CE QUE ÇA COÛTAIT. `search_vectors()` faisait
#   SELECT chunk_id, vector FROM artifact_chunk_vectors      — TOUTES les lignes
#   session.query(ArtifactChunk).all()                      — TOUS les fragments
# puis filtrait sur `allowed_ids` EN PYTHON. Le paramètre `allowed_ids` était donc
# reçu, documenté « restreint aux artefacts autorisés », et jamais utilisé pour
# réduire la lecture : le coût était linéaire sur le CORPUS, pas sur le périmètre.
# C'est le défaut qu'on ferme.
#
# LE CHOIX, JUSTIFIÉ PAR LE CORPUS MESURÉ. Corpus réel : 217 artefacts, dont 11
# fragmentés, 13 fragments, 935 Kio de vecteurs ColBERT. À cette taille, un index
# approximé (PLAID, HNSW, faiss) serait de la complication pure : il faudrait le
# construire, le stocker, et le resynchroniser à chaque ingestion et chaque purge —
# pour treize éléments. SQLite n'a pas de type vectoriel : une similarité
# cosinus par SQL exigerait 128 produits scalaires par vecteur de jeton, soit
# ~37 000 multiplications par fragment et par requête, en SQL — strictement pire
# qu'un produit matriciel unique. Un index approximé ne se justifierait qu'au-delà
# de ~10⁵ vecteurs ; nous en avons 13×287 ≈ 3 700.
#
# DONC : la correction est de faire porter le filtre à SQL. La jointure ci-dessous
# ramène la lecture de O(corpus) à O(périmètre), et supprime au passage la seconde
# requête qui chargeait tous les fragments en mémoire. La réduction de MaxSim, elle,
# reste en Python parce que `Σ max(q·dᵀ)` n'a pas d'équivalent SQL — c'est un choix
# mesuré, pas une facilité.


def _encoder_requete(query: str):
    """Les vecteurs de jetons d'une requête, ou `None` si rien à comparer.

    Séparée de `search_vectors()` pour que le moteur soit sondé AVANT toute
    lecture SQL : sans lui il n'y a rien à comparer, et inutile de lire un seul
    octet de vecteur. L'absence se rend par `None` — le FTS lexical reste servi —
    et `statut_moteur()` est l'appel qui dit POURQUOI. Elle est à part entière,
    jamais un effet de bord : un moteur muet qui « ne trouve rien » ressemblerait
    à un corpus sans réponse, ce qui est un autre défaut.
    """
    try:
        with encodeur() as codeur:
            return codeur.requete(query)
    except MoteurIndisponible:
        return None


def search_vectors(session, query: str, allowed_ids: set[str],
                   top_n: int = 50) -> list[tuple[str, float]]:
    """Top artefacts par MaxSim ColBERT, filtrés par portée. → [(artifact_id, score)].

    Le filtre de droits passe DANS LA LECTURE, pas après : un artefact hors
    périmètre n'est jamais décodé, donc jamais compté, donc jamais susceptible
    d'influer sur un rang — même transitoirement, même par un arrondi.
    """
    perimetre = sorted({str(a) for a in (allowed_ids or set()) if a})
    if not perimetre or not (query or "").strip():
        return []
    # Le moteur est sondé AVANT toute lecture SQL : sans lui, il n'y a rien à
    # comparer, et inutile de lire un seul octet de vecteur. L'absence se rend
    # par une liste vide — le FTS lexical reste servi — et `statut_moteur()` est
    # l'appel qui dit POURQUOI. Elle est à part entière, jamais un effet de bord.
    q = _encoder_requete(query)
    if q is None:
        return []
    predicat = "c.artifact_id IN (SELECT value FROM json_each(:perimetre))"
    try:
        from sqlalchemy import text as _text
        rows = session.execute(
            _text(f"""SELECT c.artifact_id, v.vector
                      FROM artifact_chunk_vectors v
                      JOIN artifact_chunks c ON c.id = v.chunk_id
                      WHERE {predicat}"""),
            {"perimetre": json.dumps(perimetre)},
        ).fetchall()
    except Exception:
        return []
    meilleur: dict[str, float] = {}
    for artifact_id, blob in rows:
        try:
            vecteurs = unpack_vectors(bytes(blob or b""))
        except FormatVecteurInconnu:
            # Un fragment au mauvais format ne fait pas échouer la recherche : il
            # n'y participe pas. Il est COMPTÉ par `reindexer_vecteurs()`, qui est
            # le seul endroit où un format périmé doit se voir.
            continue
        score = maxsim(q, vecteurs)
        if score > (meilleur.get(artifact_id) or 0.0):
            meilleur[artifact_id] = score
    return sorted(meilleur.items(), key=lambda kv: kv[1], reverse=True)[:max(1, int(top_n))]


def rrf_fuse(ranked_lists: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Fusion Reciprocal Rank Fusion : score = Σ 1/(k + rang). Ordre décroissant."""
    fused: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, aid in enumerate(lst, start=1):
            fused[aid] = fused.get(aid, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)


# ---------------------------------------------------------------------------------
# Réindexation : EXPLICITE, BORNÉE, SIGNALÉE
# ---------------------------------------------------------------------------------
#
# LA RÉINDEXATION EST OBLIGATOIRE ET DOIT ÊTRE VISIBLE. Le format a changé : 256
# flottants signés par fragment → 128 vecteurs de jetons par fragment. Un vecteur
# 256-dim déjà en base deviendrait silencieusement FAUX — pas approximatif, faux :
# il n'a pas de correspondance avec ce que le moteur compare désormais. Le pire
# écran possible serait une recherche qui continue de « marcher » en-classant sur
# des nombres qui ne veulent plus rien dire. Personne ne le verrait.
#
# ALORS : rien ne se réindexe tout seul. `detecter_vecteurs_obsoletes()` RÉPOND —
# `appliquer=False` est le DÉFAUT de `reindexer_vecteurs()`, exactement comme
# `reparer_ingestions_binaire_ou_tronquees()` en fts.py. Réécrire un index est une
# opération qui coûte du temps GPU ; elle se décide, elle ne se découvre pas.
#
# ET ELLE NE PEUT PAS ÊTRE SILENCIEUSE, parce que sa source est elle-même silencieuse :
# rien ne lève au moment où un fragment est indexé au mauvais format. Le seul moyen
# de rendre cela visible est de garder, en base, la preuve de ce qui a été écrit —
# d'où l'en-tête auto-descriptif. `diagnostic` lit cette preuve et nomme le motif.


def _etat_blob(blob: bytes | None) -> tuple[bool, str]:
    """(à jour, motif). La réponse vient de l'EN-TÊTE, jamais d'une supposition."""
    try:
        unpack_vectors(blob)
        return True, ""
    except FormatVecteurInconnu as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"blob illisible ({type(exc).__name__})"


def detecter_vecteurs_obsoletes(session, *, limite: int = 5000) -> list[dict]:
    """Les fragments dont le vecteur est à réindexer, avec LEUR MOTIF lisible.

    Ne lit que les EN-TÊTES des blobs (10 octets par ligne), jamais leur contenu :
    savoir ce qui est périmé n'a pas besoin de télécharger 935 Kio pour le dire.
    La comparaison croisée en-tête / `dim` de la colonne est redondante par
    construction — et c'est voulu : un blob dont l'en-tête ment sur sa largeur doit
    être vu deux fois, par deux canaux indépendants.
    """
    from sqlalchemy import text as _text
    try:
        lignes = session.execute(
            _text("""SELECT c.id, c.artifact_id, v.dim, v.vector
                      FROM artifact_chunks c
                      LEFT JOIN artifact_chunk_vectors v ON v.chunk_id = c.id
                      ORDER BY c.artifact_id, c.ordinal
                      LIMIT :n"""), {"n": max(1, int(limite))}).fetchall()
    except Exception:
        return []
    rapport: list[dict] = []
    for chunk_id, artifact_id, dim, blob in lignes:
        a_jour, motif = _etat_blob(bytes(blob) if blob is not None else None)
        if a_jour and dim is not None and int(dim) != DIM:
            a_jour, motif = False, (
                f"colonne dim={int(dim)} alors que le blob déclare {DIM} : "
                "les deux sources de vérité se contredisent, on ne sait pas laquelle ment")
        if a_jour:
            continue
        rapport.append({
            "chunk_id": chunk_id, "artifact_id": artifact_id,
            "dim_colonne": dim, "raison": motif or "vecteur manquant",
        })
    return rapport


def reindexer_vecteurs(session, *, appliquer: bool = False, limite: int = 5000,
                       suivi=None) -> dict:
    """Réindexe les fragments au format ColBERT. `appliquer=False` — LE DÉFAUT.

    Réponse :

        {ok, applique, moteur, detectes, reindexes, a_reindexer,
         deja_a_jour, ignores, limite_atteinte}

    `appliquer=False` ne touche à rien et se contente de répondre. Un index se
    répare en supprimant puis en réécrivant : deux opérations qui coûtent du temps
    d'encodeur, et qui ne doivent jamais être déclenchées par une simple
    inspection. Un défaut qui purge sur un doute est pire que le problème qu'il
    corrige.

    `limite` borne le nombre de fragments traités PAR PASSAGE, et `limite_atteinte`
    le DIT quand le plafond a mordu : un rapport partiel se rend partiel, jamais
    complet. Ce qui n'a pas été monté par ce passage est déjà dans `a_reindexer`,
    donc le prochain passage le prendra — et l'honnêteté est dans le rapport, pas
    dans un silence.

    `suivi` reçoit un mot par fragment réindexé : l'appelant peut afficher une barre
    ou journaliser, sans que ce module connaisse l'un ou l'autre.
    """
    etat = statut_moteur()
    rapport: dict = {
        "ok": True, "applique": bool(appliquer), "moteur": etat["moteur"],
        "raison_moteur": etat["raison"], "detectes": [], "reindexes": [],
        "a_reindexer": [], "deja_a_jour": 0, "ignores": [], "limite_atteinte": False,
    }
    detectes = detecter_vecteurs_obsoletes(session, limite=limite)
    rapport["detectes"] = detectes
    rapport["a_reindexer"] = [d["chunk_id"] for d in detectes]
    if not appliquer or not detectes:
        rapport["deja_a_jour"] = rapport["deja_a_jour"]
        return rapport
    if etat["moteur"] != "colbert":
        # Réécrire sans moteur produirait des zéros bien formatés : le pire
        # remplacement possible, parce qu'il ne distingue pas « indexé » de
        # « indexé vide ».
        rapport["ok"] = False
        rapport["ignores"].append({
            "raison": f"moteur ColBERT indisponible ({etat['raison'] or etat['moteur']}) : "
                      "réécrire sans modèle ne ferait que remplacer des vecteurs "
                      "périmés par des zéros bien formatés."})
        return rapport
    from sqlalchemy import text as _text
    try:
        session.execute(_text("""CREATE TABLE IF NOT EXISTS artifact_chunk_vectors(
            chunk_id VARCHAR PRIMARY KEY, dim INTEGER, vector BLOB)"""))
    except Exception:
        pass
    try:
        with encodeur() as codeur:
            for entree in detectes:
                chunk = session.execute(
                    _text("SELECT text, context FROM artifact_chunks WHERE id = :c"),
                    {"c": entree["chunk_id"]}).first()
                if chunk is None:
                    rapport["ignores"].append(
                        {"chunk_id": entree["chunk_id"], "raison": "fragment introuvable"})
                    continue
                vecteurs = codeur.document(texte_indexable(chunk[1], chunk[0]))
                if vecteurs is None:
                    rapport["ignores"].append({
                        "chunk_id": entree["chunk_id"],
                        "raison": "fragment vide : pas de jeton à encoder"})
                    continue
                session.execute(
                    _text("""INSERT INTO artifact_chunk_vectors(chunk_id, dim, vector)
                              VALUES (:c, :d, :v)
                              ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim,
                                  vector=excluded.vector"""),
                    {"c": entree["chunk_id"], "d": DIM, "v": pack_vectors(vecteurs)})
                rapport["reindexes"].append(entree["chunk_id"])
                if suivi is not None:
                    suivi(entree["chunk_id"])
    except MoteurIndisponible as exc:
        rapport["ok"] = False
        rapport["ignores"].append({"raison": str(exc)})
    session.flush()
    rapport["deja_a_jour"] = len(detectes) - len(rapport["reindexes"])
    rapport["limite_atteinte"] = len(detectes) >= max(1, int(limite))
    return rapport
