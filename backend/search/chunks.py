# backend/search/chunks.py — Découpage + contextualisation + ingestion (annexe 11, Lot C)
#
# D'OÙ VIENT CE FICHIER : l'hybride travaille sur des FRAGMENTS situés, pas des documents
# entiers : découpage borné → contexte 50-100 tokens → FTS + vecteur par chunk.
# OÙ VA QUOI : ensure_chunks_for_artifact() appelée après lecture/OCR de texte (bounded,
# basse priorité conceptuelle) ; search lit chunk_search + vecteurs via fts.py.
# POURQUOI CONTEXTE DÉTERMINISTE PAR DÉFAUT : le LLM de contextualisation tourne à
# l'ingestion seulement (jamais sur le chemin interactif) ; le gabarit local garantit
# un index utile hors-ligne, remplaçable par contextualizer branché (Phase 5).
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

CHUNK_SIZE = 500  # tokens (spec 01/03 : marge sous max_seq_length=511 ColBERT) — jamais de littéral ailleurs
CHUNK_OVERLAP = 15  # % médiane de 10-20% (spec 01/03)
try:
    from backend.config.defaults import CHUNK_MAX_PAR_ARTEFACT as _MAX_CHUNKS_DEFAUT
except Exception:  # import sans config (tests purs)
    _MAX_CHUNKS_DEFAUT = 200
# 200 fragments couvrent ≈ 340 000 caractères à 15 % de recouvrement, soit
# 200 × 2 000 × 85 % (20 pages denses ≈ 200 000). La borne est CONFIGURABLE
# (`THOT_MAX_CHUNKS`) et son dépassement est JOURNALISÉ : une troncature
# silencieuse laisserait croire que la fin du document est indexée.
MAX_CHUNKS = int(os.environ.get("THOT_MAX_CHUNKS", str(_MAX_CHUNKS_DEFAUT)))

# Borne locale de LECTURE et de DÉCOUPAGE d'un texte. `CHUNK_SIZE` est en
# tokens et l'estimation FR vaut ~4 caractères/token. Avec `CHUNK_OVERLAP`,
# chaque nouveau fragment n'apporte que `(100 - CHUNK_OVERLAP)` % de contenu
# neuf : 200 fragments de 2 000 caractères couvrent donc
# `MAX_CHUNKS * CHUNK_SIZE * 4 * (100 - CHUNK_OVERLAP) / 100` caractères, et
# non `MAX_CHUNKS * CHUNK_SIZE * 4`. La marge d'un fragment couvre la coupe de
# frontière. Lire un `.txt` de plusieurs centaines de Mo pour n'en indexer que
# la tête n'a donc pas de sens : `attachment_ingest._texte_brut` lit au plus
# cette borne, et `ensure_chunks_for_artifact` tronque `body` avant `chunk_text`.
MAX_CARACTERES_TEXTE = (
    MAX_CHUNKS * CHUNK_SIZE * 4 * (100 - CHUNK_OVERLAP) // 100 + CHUNK_SIZE * 4
)


def estimate_tokens(text: str) -> int:
    """Estimation tokens FR : ~4 caractères/token (variance ponctuation/accents). Pur."""
    return max(1, len(text or "") // 4)


_CHUNK_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(
    chunk_id UNINDEXED,
    artifact_id UNINDEXED,
    scope_kind UNINDEXED,
    scope_id UNINDEXED,
    text,
    context,
    tokenize = 'unicode61 remove_diacritics 2'
)"""

# La table ne change pas de SCHÉMA quand le format du vecteur change : le format
# est écrit DANS les octets (en-tête `THOTC`, voir vectors.py). Une ligne de 256
# flottants et une ligne de 128 vecteurs de jetons coexistent dans la même table,
# et c'est l'en-tête qui dit laquelle est périmée. Le bénéfice est qu'aucune
# migration n'est nécessaire — et surtout qu'aucune migration ne peut être
# oubliée : un DDL modifié par le code ferait dépendre la correction du chemin
# d'upgrade, donc un raté de migration laisserait des vecteurs muets en base.
_VECTORS_DDL = """CREATE TABLE IF NOT EXISTS artifact_chunk_vectors(
    chunk_id VARCHAR PRIMARY KEY,
    dim INTEGER,
    vector BLOB
)"""


def ensure_chunk_tables(session) -> None:
    from sqlalchemy import text as _text

    session.execute(_text(_CHUNK_FTS_DDL))
    session.execute(_text(_VECTORS_DDL))


# --- Patch des fragments après description d'une figure (étape B2) ----------
# D'OÙ VIENT CE BLOC : l'étape B1 enrichit la représentation et le bloc
# `blocks_json`, mais les fragments déjà indexés gardent le placeholder
# « description en attente ». La description existait donc dans le document
# sans être cherchable. Ici on patche les fragments CONCERNÉS, leur ligne FTS
# et leur vecteur, sans re-découper ni réindexer le document.


@dataclass(frozen=True)
class FigureDescription:
    """Description appliquée à une figure, prête pour le patch des fragments.

    `description` est le texte exact qui a été appliqué à la représentation :
    le patch cherche le bloc d'attente rendu par `figures_pdf` et le remplace
    par le bloc décrit. Aucune position n'est devinée.
    """

    figure_id: str
    page: int
    region: int
    description: str


@dataclass(frozen=True)
class FigureLimiteIndex:
    """Figure dont le fragment n'a pas pu être patché, avec sa cause nommée."""

    figure_id: str
    page: int
    region: int
    cause: str


@dataclass(frozen=True)
class ResultatPatch:
    """Résultat explicite d'un patch de fragments — aucun échec silencieux.

    `index_absent` distingue « aucun fragment n'existe encore » (l'indexation
    complète n'a pas eu lieu, il n'y a rien à patcher) de « des fragments
    existent mais le placeholder est absent/ambigu » (une vraie limite : le
    fragment ne pourra pas porter la description).
    """

    chunks_modifies: int = 0
    chunk_ids: tuple[str, ...] = ()
    figures_appliquees: int = 0
    figures_deja_appliquees: int = 0
    figures_limite: tuple[FigureLimiteIndex, ...] = ()
    index_absent: bool = False

    @property
    def ok(self) -> bool:
        """Vrai si aucune figure n'est restée sans fragment patché."""
        return not self.figures_limite


def _champ_figure(figure, nom: str, defaut=None):
    """Lit un champ d'une figure ORM, d'un dataclass ou d'un dictionnaire."""
    if isinstance(figure, dict):
        return figure.get(nom, defaut)
    return getattr(figure, nom, defaut)


def _decrire_figure(figure) -> FigureDescription:
    """Parse l'entrée du patch en `FigureDescription` (frontière unique).

    `page` et `region` sont des positions 1-based (`figures_pdf` numérote les
    régions à partir de 1). Une valeur absente, non entière ou hors de ce
    domaine ne peut pas désigner un bloc : elle lève `ValueError` au lieu de
    fabriquer page/région `0`, qui produirait un bloc introuvable et une cause
    « placeholder absent » trompeuse.
    """
    identifiant = _champ_figure(figure, "figure_id") or _champ_figure(figure, "id")
    brut_page = _champ_figure(figure, "page")
    brut_region = _champ_figure(figure, "region")
    try:
        page = int(str(brut_page))
        region = int(str(brut_region))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"figure {identifiant!r} : page/région invalides "
            f"({brut_page!r}, {brut_region!r}) — entiers ≥ 1 attendus"
        ) from exc
    if page < 1 or region < 1:
        raise ValueError(
            f"figure {identifiant!r} : page/région hors domaine "
            f"({page}, {region}) — positions 1-based attendues"
        )
    description = str(_champ_figure(figure, "description", "") or "").strip()
    return FigureDescription(
        figure_id=str(identifiant or ""),
        page=page,
        region=region,
        description=description,
    )


def _revision_figures(session, representation_id: str) -> int:
    """Révision de figures de la représentation, pour marquer le fragment lu."""
    from backend.db.models import ArtifactRepresentation as _Rep

    try:
        valeur = (
            session.query(_Rep.figures_revision)
            .filter(_Rep.id == representation_id)
            .scalar()
        )
        return int(valeur or 0)
    except Exception:
        return 0


def _contexte_patche(contexte: str | None, ancien: str, nouveau: str) -> str:
    """Contexte du fragment, mis à jour sans perdre sa forme d'origine.

    Le contexte déterministe ne contient normalement pas le bloc d'attente ;
    quand il le contient (amorce prise sur le placeholder), il est remplacé
    comme le texte. La borne de 500 caractères est celle de l'indexation.
    """
    texte = contexte or ""
    if ancien in texte:
        texte = texte.replace(ancien, nouveau, 1)
    return texte[:500]


def _remplacer_lignes_fts(
    session,
    artifact_id: str,
    chunk_id: str,
    texte: str,
    contexte: str,
) -> None:
    """Réécrit les lignes `chunk_search` d'UN fragment, sans toucher aux autres.

    Un fragment est indexé une fois PAR PORTÉE (`scope_kind`, `scope_id`). On
    relit les portées existantes avant de les recréer : une ligne manquante
    serait une recherche muette, deux lignes inventées seraient un doublon.
    Si aucune portée n'existe encore, les liens actifs de l'artefact la
    reconstituent — c'est le seul repli, et il ne devine pas de portée.
    """
    from sqlalchemy import text as _text

    lignes = session.execute(
        _text("SELECT scope_kind, scope_id FROM chunk_search WHERE chunk_id = :c"),
        {"c": chunk_id},
    ).fetchall()
    portees = [(str(ligne[0]), str(ligne[1])) for ligne in lignes]
    if not portees:
        from backend.db.models import ArtifactLink as _Link

        try:
            liens = (
                session.query(_Link)
                .filter(_Link.artifact_id == artifact_id, _Link.removed_at.is_(None))
                .all()
            )
            portees = [(str(lien.scope_kind), str(lien.scope_id)) for lien in liens]
        except Exception:
            portees = []
    session.execute(
        _text("DELETE FROM chunk_search WHERE chunk_id = :c"), {"c": chunk_id}
    )
    for scope_kind, scope_id in portees:
        session.execute(
            _text(
                """INSERT INTO chunk_search(
                       chunk_id, artifact_id, scope_kind, scope_id, text, context)
                   VALUES (:c, :a, :sk, :sid, :t, :x)"""
            ),
            {
                "c": chunk_id,
                "a": artifact_id,
                "sk": scope_kind,
                "sid": scope_id,
                "t": texte,
                "x": contexte,
            },
        )


def _supprimer_vecteur(session, chunk_id: str) -> None:
    """Supprime le vecteur périmé d'UN fragment.

    On ne réencode pas ici : le worker figures ne doit pas charger le moteur
    ColBERT sur le chemin d'ingestion, et un vecteur périmé serait pire qu'une
    absence. `vectors.detecter_vecteurs_obsoletes()` voit la ligne manquante
    (`LEFT JOIN`) et `reindexer_vecteurs()` la reconstruira au passage suivant.
    Le FTS, lui, est à jour immédiatement : la description reste cherchable.
    """
    from sqlalchemy import text as _text

    session.execute(
        _text("DELETE FROM artifact_chunk_vectors WHERE chunk_id = :c"),
        {"c": chunk_id},
    )


def mettre_a_jour_chunks_figures(
    session,
    artifact_id: str,
    representation_id: str,
    figures,
    *,
    figures_revision: int | None = None,
) -> ResultatPatch:
    """Patche les fragments qui portent le placeholder d'une figure décrite.

    Pour chaque figure appliquée :
      - le bloc d'attente EXACT est remplacé une seule fois dans CHAQUE fragment
        porteur ; le recouvrement de 15 % du découpage duplique légitimement un
        bloc en fin de fragment au début du suivant, et les deux copies doivent
        être mises à jour pour rester cohérentes (`figures_pdf.bloc_figure`) ;
      - le fragment garde `id`, `ordinal` et `card_revision` ; seuls `text`,
        `context`, `figures_revision` et `text_sha256` changent ;
      - la ou les lignes `chunk_search` de chaque fragment sont réécrites ;
      - le vecteur de chaque fragment patché est supprimé (périmé), sans
        réindexation globale.

    Une figure dont le placeholder est absent, répété DANS UN MÊME fragment ou
    incohérent (bloc d'attente et bloc décrit dans le même fragment) n'est
    JAMAIS devinée : elle revient dans `figures_limite` avec une cause, et aucun
    texte de fragment n'est modifié. Une description déjà appliquée (bloc décrit
    déjà présent, plus de bloc d'attente) est comptée
    `figures_deja_appliquees`, sans duplication.

    REPLI LEGACY : quand la représentation n'a aucun fragment mais que
    l'artefact en a d'anciens (`representation_id IS NULL`), ce sont eux qui
    portent l'index. Les porteurs du placeholder sont patchés et rattachés à la
    représentation courante. Si des fragments legacy existent sans le
    placeholder, la figure est une limite motivée : jamais `index_absent`, qui
    laisserait croire qu'il n'y a rien à réparer, ni `done`, qui masquerait un
    index périmé.

    Aucun commit : l'appelant tient la transaction (représentation + fragments
    + FTS + statut de figure + audit tombent ensemble).
    """
    from backend.db.models import ArtifactChunk as _Chk
    from backend.ingestion import figures_pdf as _fig

    if not artifact_id or not representation_id:
        raise ValueError(
            "patch des figures sans artifact_id/representation_id : "
            "le fragment ne peut pas être rattaché sans provenance"
        )

    decrites = [_decrire_figure(figure) for figure in (figures or [])]
    if not decrites:
        return ResultatPatch()

    chunks = (
        session.query(_Chk)
        .filter(
            _Chk.artifact_id == artifact_id,
            _Chk.representation_id == representation_id,
        )
        .order_by(_Chk.ordinal.asc(), _Chk.id.asc())
        .all()
    )
    # REPLI LEGACY : l'ancienne indexation a pu écrire des fragments avant que
    # la représentation ne soit rattachée (`representation_id IS NULL`). Ils sont
    # considérés « déjà indexés » par `observations._chunks_deja_indexes`, donc
    # aucun nouvelle indexation ne les remplacera : les ignorer ici laisserait
    # « description en attente » cherchable alors que la figure est `done`.
    legacy = False
    if not chunks:
        chunks = (
            session.query(_Chk)
            .filter(
                _Chk.artifact_id == artifact_id,
                _Chk.representation_id.is_(None),
            )
            .order_by(_Chk.ordinal.asc(), _Chk.id.asc())
            .all()
        )
        legacy = bool(chunks)
    # Aucun fragment du tout : l'indexation complète n'a pas encore eu lieu. Il
    # n'y a pas d'index périmé à réparer ; marquer la figure en limite punirait
    # une description parfaitement appliquée à la représentation.
    if not chunks:
        return ResultatPatch(index_absent=True)

    revision = (
        _revision_figures(session, representation_id)
        if figures_revision is None
        else int(figures_revision)
    )
    modifies: list[str] = []
    appliquees = 0
    deja_appliquees = 0
    limites: list[FigureLimiteIndex] = []

    for figure in decrites:
        if not figure.description:
            limites.append(
                FigureLimiteIndex(
                    figure.figure_id,
                    figure.page,
                    figure.region,
                    "description vide : aucun bloc de figure à indexer",
                )
            )
            continue
        attente = _fig.bloc_figure_en_attente(figure.page, figure.region)
        nouveau = _fig.bloc_figure(figure.page, figure.region, figure.description)
        porteurs_attente = [chunk for chunk in chunks if attente in (chunk.text or "")]
        porteurs_decrits = [chunk for chunk in chunks if nouveau in (chunk.text or "")]

        # Un index legacy existe mais aucun fragment ne porte le placeholder :
        # on ne peut ni le patcher ni affirmer que la description est indexée.
        # `index_absent` serait faux (des fragments existent) et `done` masquerait
        # un index potentiellement périmé.
        if legacy and not porteurs_attente:
            limites.append(
                FigureLimiteIndex(
                    figure.figure_id,
                    figure.page,
                    figure.region,
                    "figure hors index legacy : aucun fragment legacy de "
                    f"l'artefact {artifact_id!r} ne porte le placeholder de "
                    f"page {figure.page} région {figure.region}",
                )
            )
            continue

        if not porteurs_attente:
            if porteurs_decrits:
                # Le recouvrement peut avoir produit plusieurs copies : elles
                # portent toutes la description, aucune n'est en attente.
                deja_appliquees += 1
                continue
            limites.append(
                FigureLimiteIndex(
                    figure.figure_id,
                    figure.page,
                    figure.region,
                    "placeholder introuvable dans les fragments de la "
                    f"représentation {representation_id!r}",
                )
            )
            continue

        # AMBIGUÏTÉ RÉELLE SEULEMENT :
        #  - plus d'une occurrence dans UN MÊME fragment : on ne saurait pas
        #    quelle position décrit la figure ;
        #  - un fragment contient à la fois le bloc d'attente et le bloc décrit :
        #    son état est incohérent, le patcher devinerait.
        # Deux fragments porteurs qui contiennent CHACUN une occurrence sont le
        # cas normal du recouvrement (15 %) : les deux copies doivent être
        # patchées, sinon la fin de l'un et le début de l'autre divergent.
        repetitions = [
            chunk for chunk in porteurs_attente if (chunk.text or "").count(attente) > 1
        ]
        if repetitions:
            chunk = repetitions[0]
            limites.append(
                FigureLimiteIndex(
                    figure.figure_id,
                    figure.page,
                    figure.region,
                    "placeholder répété "
                    f"{(chunk.text or '').count(attente)} fois dans le fragment "
                    f"{chunk.id} : patch ambigu",
                )
            )
            continue
        incoherents = [
            chunk for chunk in porteurs_attente if nouveau in (chunk.text or "")
        ]
        if incoherents:
            limites.append(
                FigureLimiteIndex(
                    figure.figure_id,
                    figure.page,
                    figure.region,
                    "bloc d'attente et bloc décrit dans le même fragment "
                    f"{incoherents[0].id} : état incohérent",
                )
            )
            continue

        for chunk in porteurs_attente:
            texte_patche = (chunk.text or "").replace(attente, nouveau, 1)
            contexte_patche = _contexte_patche(chunk.context, attente, nouveau)
            chunk.text = texte_patche
            chunk.context = contexte_patche
            chunk.figures_revision = revision
            chunk.text_sha256 = _sha256_texte(texte_patche)
            # Un fragment legacy rattaché à la représentation courante devient
            # un fragment normal : sans ce rattachement, le prochain patch le
            # rechercherait hors de `representation_id` et le croirait absent.
            if legacy:
                chunk.representation_id = representation_id
            _remplacer_lignes_fts(
                session, artifact_id, chunk.id, texte_patche, contexte_patche
            )
            _supprimer_vecteur(session, chunk.id)
            modifies.append(chunk.id)
        appliquees += 1

    session.flush()
    chunk_ids = tuple(dict.fromkeys(modifies))
    return ResultatPatch(
        chunks_modifies=len(chunk_ids),
        chunk_ids=chunk_ids,
        figures_appliquees=appliquees,
        figures_deja_appliquees=deja_appliquees,
        figures_limite=tuple(limites),
        index_absent=False,
    )


def chunk_text(
    text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """Découpe déterministe spec 03 : 500 tokens, 10-20% recouvrement, frontières de phrase.

    `size` en tokens, `overlap` en % (10-20). Coupe aux frontières de phrase, jamais au milieu
    d'un mot. L'``ordinal`` écrit en base est l'unique rang citable : ``doc_N:k`` désigne le
    fragment PERSISTÉ d'ordre k, jamais la position d'une phrase ni un rang de résultat.
    """
    import re as _re

    body = (text or "").strip()
    if not body:
        return []
    # Phrases (frontières . ! ? + paragraphes), jamais de coupe au milieu d'un mot
    sents = [
        _s.strip() for _s in _re.split(r"(?<=[.!?…])\s+|\n\n+", body) if _s.strip()
    ]
    if not sents:
        return [body]
    try:
        ov_pct = float(overlap) if float(overlap) <= 100 else 15.0
    except Exception:
        ov_pct = 15.0
    ov_pct = min(20.0, max(10.0, ov_pct))
    chunks: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for s in sents:
        st = estimate_tokens(s)
        if cur and cur_tok + st > size:
            chunks.append(" ".join(cur).strip())
            # Recouvrement : garde la fin ≈ ov% en tokens
            keep_tok = int(size * ov_pct / 100.0)
            kept: list[str] = []
            acc = 0
            for prev in reversed(cur):
                pt = estimate_tokens(prev)
                if acc + pt > keep_tok:
                    break
                kept.insert(0, prev)
                acc += pt
            cur, cur_tok = kept, acc
            # Phrase seule > size : coupe dure au mot (garde-fou, ne doit pas arriver à 500)
            while st > size:
                words = s.split()
                cut = max(1, size * 4 // 5)
                acc_w, idx = 0, 0
                for idx, w in enumerate(words):
                    acc_w += len(w) + 1
                    if acc_w >= cut * 4:
                        break
                chunks.append(" ".join(words[: idx + 1]))
                s = " ".join(words[idx + 1 :]) or ""
                st = estimate_tokens(s)
                if not s:
                    break
            if not s:
                continue
        cur.append(s)
        cur_tok += st
    if cur:
        chunks.append(" ".join(cur).strip())
    # Garde-fou spec : aucun chunk ne dépasse `size` en estimation (marge sous 511).
    # L'estimation par phrase sous-évalue la jointure (espaces, troncatures //4) :
    # re-découpe au mot sur budget caractères (size*4), cohérent avec estimate_tokens.
    final: list[str] = []
    for ch in chunks:
        if estimate_tokens(ch) <= size:
            final.append(ch)
            continue
        words = ch.split()
        budget = size * 4
        buf: list[str] = []
        cur_len = 0
        for w in words:
            add = len(w) + (1 if buf else 0)
            if buf and cur_len + add > budget:
                final.append(" ".join(buf))
                # Recouvrement minimal sur coupe dure
                keep = max(1, len(buf) // 6)
                buf = buf[-keep:]
                cur_len = len(" ".join(buf))
                add = len(w) + (1 if buf else 0)
            buf.append(w)
            cur_len += add
        if buf:
            final.append(" ".join(buf))
    return [c for c in final if c]


def default_context(doc_title: str, ordinal: int, total: int, chunk: str) -> str:
    """Contexte déterministe (gabarit local) : titre + position + amorce factuelle."""
    head = chunk.strip().split("\n", 1)[0][:120]
    return (
        f"Document « {doc_title or '?'} », fragment {ordinal + 1}/{total}. "
        f"Amorce : {head}".strip()
    )


def _stable_chunk_id(
    artifact_id: str, card_revision: int, ordinal: int, piece: str
) -> str:
    """Identifiant déterministe d'un fragment, stable à RÉVISION CONSTANTE.

    CONTRAT (tranché au relevé Phase 8) : à révision de fiche constante, une
    réingestion qui reproduit le même fragment rend le MÊME id ; un texte
    différent rend un id différent. La formule inclut volontairement
    `card_revision` : une nouvelle révision de carte — chaque observation en crée
    une — change les ids même à texte identique. La preuve reste donc attachée à
    la révision qui l'a produite, et une fiche qui citait l'ancien fragment est
    périmée explicitement au lieu d'être redirigée en silence (voir
    `ensure_chunks_for_artifact`, Option A du relevé Phase 8).

    La normalisation (minuscules + espaces) évite qu'une simple remise en forme
    du même texte invente une nouvelle preuve. L'ordinal reste dans l'entrée :
    deux fragments au texte identique à des positions différentes sont deux
    preuves distinctes.
    """
    brut = f"{artifact_id}|{int(card_revision)}|{int(ordinal)}|{_normalized(piece)}"
    return f"chk_{hashlib.sha256(brut.encode('utf-8')).hexdigest()[:12]}"


def _indexer_fragment(session, chunk_id: str, contexte: str | None, piece: str) -> bool:
    """Encode UN fragment en ColBERT et l'écrit. Retourne Vrai si le vecteur est écrit.

    L'ingestion ne doit PAS échouer parce que le modèle manque : un fichier reste
    entièrement indexable par le FTS lexical, qui ne dépend d'aucun modèle. Un fragment
    dont le vecteur manque est donc une absence DÉCLARÉE — il ne participe pas au
    classement sémantique, et `vectors.reindexer_vecteurs()` le compte au passage
    suivant. Ce qui est refusé ici, c'est le contraire : écrire un vecteur de
    remplacement parce que le modèle était absent. Un vecteur « plausible mais
    faux » se classe au milieu du classement sans qu'on sache pourquoi, alors
    qu'une absence se voit et se répare.
    """
    from backend.search import vectors as _vec
    from sqlalchemy import text as _text

    encode = None
    try:
        with _vec.encodeur() as codeur:
            encode = codeur.document(_vec.texte_indexable(contexte, piece))
        if encode is None:
            return False
        session.execute(
            _text("""INSERT INTO artifact_chunk_vectors(chunk_id, dim, vector)
                      VALUES (:c, :d, :v)
                      ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim,
                          vector=excluded.vector"""),
            {"c": chunk_id, "d": _vec.DIM, "v": _vec.pack_vectors(encode)},
        )
        return True
    except Exception:
        return False


def _sha256_texte(texte: str) -> str:
    """Empreinte du texte d'un fragment, pour la réindexation ciblée. Pure."""
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def _indexer_fragments(session, fragments: list[tuple[str, str | None, str]]) -> int:
    """Encode et écrit les vecteurs ColBERT d'un LOT de fragments.

    L'encodeur était ouvert et refermé PAR fragment : `_indexer_fragment`
    payait le contexte du moteur à chaque appel, et le découpage pouvait en
    produire deux cents. Ici le moteur n'est chargé qu'une fois pour tout le
    lot, et les écritures de vecteurs suivent dans la même transaction.

    L'échec reste PAR fragment : un fragment dont le vecteur manque est une
    absence déclarée, jamais un vecteur de remplacement (voir
    `_indexer_fragment`). Une indisponibilité du moteur sort de la boucle et
    laisse le FTS lexical servir le document ; `reindexer_vecteurs()` comptera
    les fragments manquants au passage suivant.

    L'encodage reste SYNCHRONE : il est borné par `MAX_CHUNKS`, et le faire
    dans la transaction garantit que l'index et la preuve tombent ensemble.
    Le découpage est déjà borné par `MAX_CARACTERES_TEXTE`, donc le lot a une
    taille maximale connue.
    """
    from backend.search import vectors as _vec
    from sqlalchemy import text as _text

    ecrits = 0
    try:
        with _vec.encodeur() as codeur:
            for chunk_id, contexte, piece in fragments:
                try:
                    encode = codeur.document(_vec.texte_indexable(contexte, piece))
                    if encode is None:
                        continue
                    session.execute(
                        _text("""INSERT INTO artifact_chunk_vectors(chunk_id, dim, vector)
                                  VALUES (:c, :d, :v)
                                  ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim,
                                      vector=excluded.vector"""),
                        {
                            "c": chunk_id,
                            "d": _vec.DIM,
                            "v": _vec.pack_vectors(encode),
                        },
                    )
                    ecrits += 1
                except Exception:
                    # Un fragment illisible n'arrête pas le lot.
                    continue
    except Exception:
        # Moteur indisponible (ou échec d'ouverture) : aucune écriture de
        # remplacement, le FTS reste la vérité.
        return ecrits
    return ecrits


def ensure_chunks_for_artifact(
    session,
    artifact,
    text: str,
    scope_links: list[tuple[str, str]],
    contextualizer=None,
    *,
    representation_id: str | None = None,
    max_chunks: int | None = None,
) -> int:
    """Découpe + contexte + index FTS chunks + vecteurs. Retourne le nb de chunks. Borné.

    LE TEXTE PEUT DÉPASSER 16 k. L'appelant qui dispose de la représentation
    courante passe son Markdown COMPLET ; l'observation bornée n'est qu'un audit
    et ne doit plus être la seule vérité indexée.

    `max_chunks` (ou `MAX_CHUNKS`/`THOT_MAX_CHUNKS`) borne le travail. Quand le
    document produit plus de fragments, la coupure est EXPLICITE (journalisée avec
    le nombre réel) : une troncature silencieuse ferait croire que la fin du
    document est indexée.

    LIMITE CONNUE : cette troncature n'est PAS persistée dans l'état de
    l'artefact (aucune colonne dédiée, donc aucune migration). Elle n'existe que
    dans le journal et dans le nombre de fragments écrits ; l'état `tronque` du
    relevé d'ingestion couvre, lui, la couverture de PAGES, pas la borne de
    fragments. Un opérateur qui veut savoir si `MAX_CHUNKS` a mordu lit le
    journal `[chunks] ... troncature EXPLICITE`, ou compare `len(chunks)` à
    `MAX_CHUNKS`.

    OPTION A DU RELEVÉ PHASE 8 : les fragments qui disparaissent sont retirés
    DANS LA MÊME TRANSACTION que la péremption des fiches qui les citaient
    (`invalider_preuves_par_fragments`). Entre la suppression des chunks et
    l'application des faits, aucune fiche courante ne peut donc citer un `chk_`
    inexistant. Les fragments conservés (même id) ne sont ni supprimés ni
    réinsérés : leur lien de provenance reste intact, mais leurs métadonnées de
    représentation sont remises à jour.
    """
    from backend.db.models import ArtifactChunk as _Chk
    from backend.search import vectors as _vec
    from sqlalchemy import text as _text

    body = (text or "").strip()
    if not body:
        return 0
    # La borne de LECTURE/DÉCOUPAGE est appliquée AVANT `chunk_text` : découper
    # des millions de caractères pour n'en garder que `MAX_CHUNKS` fragments
    # coûterait un découpage de milliers de pièces inutiles. La troncature est
    # journalisée par l'appelant (lecture) et ici (appel direct).
    if len(body) > MAX_CARACTERES_TEXTE:
        log.warning(
            "[chunks] %s : texte de %d caractères tronqué à %d avant découpage "
            "(MAX_CARACTERES_TEXTE = MAX_CHUNKS × CHUNK_SIZE × 4 × "
            "(100 - CHUNK_OVERLAP) / 100 + marge) — au-delà, aucun fragment "
            "supplémentaire ne serait conservé",
            getattr(artifact, "id", "?"),
            len(body),
            MAX_CARACTERES_TEXTE,
        )
        body = body[:MAX_CARACTERES_TEXTE]
    limite = int(MAX_CHUNKS if max_chunks is None else max_chunks)
    if limite < 1:
        raise ValueError(
            f"[CHUNK_LIMIT] max_chunks doit être ≥ 1 (reçu {limite!r}) : "
            "une borne nulle ne peut pas indexer un document non vide."
        )
    # La révision de fiche fait partie de l'id (contrat « stable à révision
    # constante »). On calcule les nouveaux ids AVANT de supprimer.
    try:
        current_rev = 0
        from backend.search import cards as _cards

        card = _cards.get_current_card(session, artifact.id)
        current_rev = int(card.revision or 0) if card else 0
    except Exception:
        current_rev = 0
    tous_les_fragments = chunk_text(body)
    pieces = tous_les_fragments[:limite]
    if len(tous_les_fragments) > limite:
        log.warning(
            "[chunks] %s : %d fragments produits, troncature EXPLICITE à %d "
            "(THOT_MAX_CHUNKS/CHUNK_MAX_PAR_ARTEFACT) — la fin du document "
            "n'est pas indexée au-delà de cette borne",
            getattr(artifact, "id", "?"),
            len(tous_les_fragments),
            limite,
        )
    nouveaux_ids = [
        _stable_chunk_id(artifact.id, current_rev, i, piece)
        for i, piece in enumerate(pieces)
    ]
    try:
        old = session.query(_Chk).filter(_Chk.artifact_id == artifact.id).all()
    except Exception:
        old = []
    anciens = {c.id: c for c in old}
    retires = set(anciens) - set(nouveaux_ids)
    conserves = set(anciens) & set(nouveaux_ids)
    title = artifact.original_filename or artifact.display_name or artifact.id
    try:
        ensure_chunk_tables(session)
    except Exception:
        return 0
    try:
        with session.begin_nested():
            if retires:
                # Import paresseux : le service mémoire est au-dessus de la
                # couche recherche, et n'est chargé que si une preuve disparaît.
                from backend.services import memoire_documentaire as _md

                _md.invalider_preuves_par_fragments(
                    session,
                    artifact_id=artifact.id,
                    chunk_ids=retires,
                    commit=False,
                )
            for cid in retires:
                session.delete(anciens[cid])
            session.flush()
            for cid in retires:
                try:
                    session.execute(
                        _text("DELETE FROM chunk_search WHERE chunk_id = :c"),
                        {"c": cid},
                    )
                    session.execute(
                        _text("DELETE FROM artifact_chunk_vectors WHERE chunk_id = :c"),
                        {"c": cid},
                    )
                except Exception:
                    pass
            nouveaux: list[_Chk] = []
            a_encoder: list[tuple[str, str | None, str]] = []
            for i, piece in enumerate(pieces):
                cid = nouveaux_ids[i]
                # Empreinte du texte RÉELLEMENT persisté (même borne que la ligne).
                empreinte = _sha256_texte(piece[:4000])
                if cid in conserves:
                    # Même id = même révision + même texte normalisé. Le
                    # fragment reste cité, mais sa provenance et son empreinte
                    # doivent refléter la représentation COURANTE.
                    conserve = anciens[cid]
                    conserve.representation_id = representation_id
                    conserve.text_sha256 = empreinte
                    conserve.figures_revision = 0
                    continue
                try:
                    ctx = (
                        contextualizer(title, i, len(pieces), piece)
                        if contextualizer
                        else default_context(title, i, len(pieces), piece)
                    )
                except Exception:
                    ctx = default_context(title, i, len(pieces), piece)
                nouveaux.append(
                    _Chk(
                        id=cid,
                        artifact_id=artifact.id,
                        representation_id=representation_id,
                        figures_revision=0,
                        text_sha256=empreinte,
                        card_revision=current_rev,
                        ordinal=i,
                        text=piece[:4000],
                        context=(ctx or "")[:500],
                    )
                )
                a_encoder.append((cid, ctx, piece))
            if nouveaux:
                # BATCH : toutes les lignes sont ajoutées en une fois, puis un
                # SEUL flush avant l'encodage. L'ancien `flush()` par fragment
                # faisait N allers SQL pour un document de N fragments, et
                # `_indexer_fragment` ouvrait le moteur N fois. Les deux coûts
                # sont désormais bornés et groupés.
                session.add_all(nouveaux)
                session.flush()
                _indexer_fragments(session, a_encoder)
                for cid, ctx, piece in a_encoder:
                    for sk, sid in scope_links:
                        try:
                            session.execute(
                                _text("""INSERT INTO chunk_search(chunk_id, artifact_id, scope_kind, scope_id, text, context)
                                         VALUES (:c, :a, :sk, :sid, :t, :x)"""),
                                {
                                    "c": cid,
                                    "a": artifact.id,
                                    "sk": sk,
                                    "sid": sid,
                                    "t": piece[:4000],
                                    "x": (ctx or "")[:500],
                                },
                            )
                        except Exception:
                            pass
            session.flush()
    except Exception:
        # Fail-closed : ne jamais supprimer une preuve sans périmer les fiches
        # qui la citent. Le savepoint annule le remplacement ; les anciens
        # fragments restent citables et l'ingestion réessaiera.
        raise
    return len(pieces)


def remove_chunks_for_artifact(session, artifact_id: str) -> dict:
    """Retire chunks + index + vecteurs (purge GC) et rend un résultat structuré.

    Ne lève pas pour un artefact isolé : un GC doit pouvoir compter un échec
    sans interrompre la purge des autres. Mais rien n'est avalé — chaque erreur
    est nommée dans `errors`, et `ok` est faux. L'appelant transactionnel (GC)
    peut alors annuler le savepoint de CET artefact au lieu de laisser un index
    à moitié coupé.
    """
    from backend.db.models import ArtifactChunk as _Chk
    from sqlalchemy import text as _text

    resultat: dict = {
        "ok": True,
        "artifact_id": artifact_id,
        "chunks": 0,
        "vecteurs": 0,
        "errors": [],
    }
    try:
        ids = [
            c.id
            for c in session.query(_Chk).filter(_Chk.artifact_id == artifact_id).all()
        ]
        for c in session.query(_Chk).filter(_Chk.artifact_id == artifact_id).all():
            session.delete(c)
        session.flush()
        index_chunks = (
            session.execute(
                _text("DELETE FROM chunk_search WHERE artifact_id = :a"),
                {"a": artifact_id},
            ).rowcount
            or 0
        )
        vecteurs = 0
        for cid in ids:
            vecteurs += (
                session.execute(
                    _text("DELETE FROM artifact_chunk_vectors WHERE chunk_id = :c"),
                    {"c": cid},
                ).rowcount
                or 0
            )
        session.flush()
        resultat["chunks"] = len(ids)
        resultat["index_chunks"] = int(index_chunks)
        resultat["vecteurs"] = int(vecteurs)
    except Exception as exc:  # noqa: BLE001 - remonté dans `errors`, pas avalé
        resultat["ok"] = False
        resultat["errors"].append(f"{type(exc).__name__}: {exc}")
    return resultat


# --- Preuves : un `doc_N:k` ne vaut que s'il désigne un fragment PERSISTÉ ---
# D'OÙ VIENT CE BLOC : les preuves `doc_N:k` étaient des rangs calculés (`doc_N:0`,
# position d'une phrase), citables alors qu'aucun contenu ne les soutenait. Ici un
# identifiant de preuve est toujours adossé à une ligne `artifact_chunks` : soit le
# fragment existe, soit il n'y a pas de preuve.

_QUOTE_PROBE_MIN_CHARS = 12  # sous ce seuil, une citation ne prouve rien
_QUOTE_PROBE_SIZES = (
    120,
    60,
    40,
)  # sondes décroissantes (fragment tronqué, recouvrement)


def _normalized(text: str) -> str:
    """Minuscules + espaces normalisés : la comparaison ignore la mise en forme."""
    return " ".join(str(text or "").lower().split())


def _persisted_chunks(
    session, artifact_id: str, card_revision: int | None = None
) -> list:
    """Fragments PERSISTÉS de l'artefact, dans l'ordre des ``ordinal`` écrits en base.

    `card_revision`, quand il est fourni (> 0), restreint aux fragments de cette
    révision : une preuve doit citer le fragment de la révision courante, pas un
    fragment homonyme laissé par une révision antérieure.
    """
    from backend.db.models import ArtifactChunk as _Chk

    requete = session.query(_Chk).filter(_Chk.artifact_id == artifact_id)
    if card_revision is not None and int(card_revision) > 0:
        requete = requete.filter(_Chk.card_revision == int(card_revision))
    return requete.order_by(_Chk.ordinal.asc()).all()


def persisted_ordinals(session, artifact_id: str) -> set[int]:
    """Ordres des fragments persistés : les seuls `k` citable pour cet artefact.

    Une preuve ``doc_N:k`` dont le fragment a disparu (réingestion, purge) n'est
    plus une preuve : l'ensemble vide refuse tout.
    """
    artifact_id = str(artifact_id or "").strip()
    if not artifact_id:
        return set()
    try:
        return {int(chunk.ordinal) for chunk in _persisted_chunks(session, artifact_id)}
    except Exception:
        return set()


def ordinal_of_quote(
    session, artifact_id: str, quote: str, card_revision: int | None = None
) -> int | None:
    """Ordinal du fragment persisté qui porte réellement l'extrait cité.

    Sonde la citation la plus longue d'abord : le fragment peut être tronqué à
    l'ingestion (4000 car.) ou recoupé par le recouvrement. Un extrait qu'aucun
    fragment ne contient n'a pas de preuve — ``None``, jamais un ordinal deviné.

    `card_revision`, quand il est fourni (> 0), restreint la recherche à la
    révision courante : une réingestion qui a changé de révision ne doit pas
    valider une citation contre l'ancien fragment (relevé Phase 8, 🟡 3).
    """
    artifact_id = str(artifact_id or "").strip()
    needle = _normalized(quote)
    if not artifact_id or len(needle) < _QUOTE_PROBE_MIN_CHARS:
        return None
    try:
        texts = [
            (int(chunk.ordinal), _normalized(chunk.text))
            for chunk in _persisted_chunks(session, artifact_id, card_revision)
        ]
    except Exception:
        return None
    for size in [
        len(needle),
        *(size for size in _QUOTE_PROBE_SIZES if size < len(needle)),
    ]:
        probe = needle[:size]
        for ordinal, chunk_text in texts:
            if probe in chunk_text:
                return ordinal
    return None


def best_source_text(session, artifact) -> str:
    """Meilleur texte indexable : représentation COURANTE, sinon blob, sinon observation.

    La représentation est la vérité documentaire complète ; le blob texte et
    l'observation ne sont que des replis (legacy / fichier sans ingestion).

    La branche observation délègue à `ingestion.etat.texte_ingeste` — LA lecture unique de
    « ce fichier a-t-il été lu ». Elle existait ici sous une forme qui acceptait
    `read_document` et que `reader` ignorait : deux réponses à une question unique
    donnaient deux verdicts possibles sur le même fichier. C'est précisément
    l'indécision que le relevé d'ingestion supprime ; la laisser ici la réintroduirait
    par la porte de l'indexation hybride.
    """
    from backend.ingestion import etat as _etat

    # La même garde que `reader.read_payload`, et AVANT la branche blob : sans
    # elle, un `kind == "text"` continuait de rendre le contenu d'un artefact
    # `binaire_refuse` ou `obsolète` (le blob est lu avant toute observation).
    if artifact is None or _etat.lire(artifact).etat in _etat.ETATS_SANS_TEXTE:
        return ""
    # LA REPRÉSENTATION COURANTE EST LA VÉRITÉ DOCUMENTAIRE. L'observation
    # `payload_ref` reste bornée à 16 k pour le prompt ; la servir ici rendrait
    # la fin du document invisible à l'indexation et à `ask_files`.
    try:
        from backend.ingestion import representations as _reps

        representation = _reps.representation_pour(session, artifact.id)
        if representation is not None and (representation.markdown or "").strip():
            return representation.markdown
    except Exception:
        pass
    try:
        if (artifact.kind or "").lower() == "text":
            from backend.db.models import Blob as _Blob
            from backend.config.paths import get_data_dir as _gdd

            blob = session.query(_Blob).filter(_Blob.id == artifact.blob_id).first()
            if blob is not None and (blob.size_bytes or 0) <= 200_000:
                cand = _gdd() / blob.relative_path
                if cand.exists():
                    return cand.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    texte, _preuve = _etat.texte_ingeste(session, artifact, limite=3)
    return texte or ""
