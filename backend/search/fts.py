# backend/search/fts.py — Index lexical FTS5 + ranking déterministe (annexe 11, Lot A)
#
# D'OÙ VIENT CE FICHIER : annexe 11 §Index FTS5 + §Étapes 1-7 — la Phase 1 doit retrouver
# un document SANS embeddings : SQLite FTS5 + filtres + BM25 suffisent.
# OÙ VA QUOI : cards.reindexer_carte() appelle arbitrer_extrait() puis
# ecrire_ligne_index() à chaque fiche active ; search() appelé par l'exécuteur
# search_documents (loop.py) et le Tool (tools/).
# CE QUE CE MODULE NE FAIT PLUS : décider de l'ÉTAT d'une fiche. Il juge des
# octets et rend un verdict ; c'est cards.py, qui détient la fiche, qui en tire
# le statut et le résumé. Un index qui juge ne connaît ni l'historique du
# fichier, ni les autres extractions — il n'a donc rien à dire de son état.
# POURQUOI PAS D'EMBEDDINGS ICI : Lot C uniquement, sans changer l'API — ce module
# reste l'unique point de retrieval lexical.
from __future__ import annotations

import base64
import hashlib
import json
import logging
import unicodedata

from backend.utils.binary_guard import raison_binaire_texte
from backend.image_engine.runner.memprof import RunRecord as _MEMPROF_RUN_RECORD

log = logging.getLogger(__name__)

# Colonnes FTS indexées (ordre fixe — highlight() s'y réfère par index)
# 0 artifact_id, 1 scope_kind, 2 scope_id, 3 kind, 4 card_revision (UNINDEXED),
# 5 filename, 6 title, 7 summary, 8 keywords, 9 entities, 10 extracted_preview
_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS artifact_search USING fts5(
    artifact_id UNINDEXED,
    scope_kind UNINDEXED,
    scope_id UNINDEXED,
    kind UNINDEXED,
    card_revision UNINDEXED,
    filename,
    title,
    summary,
    keywords,
    entities,
    extracted_preview,
    tokenize = 'unicode61 remove_diacritics 2'
)"""

MAX_CANDIDATES = 50  # récupération large et rapide (rerank Lot C plus tard)
MAX_LIMIT = 20  # borne tool call (annexe 11)
MAX_QUERY = 300

# Bornes de l'extrait indexé. La troncature est un CHOIX — l'aperçu sert au
# classement, pas à la relecture du document — et `cards.py` applique déjà la
# même borne en amont, donc elle est rarement atteinte ici.
PREVIEW_CHARS = 2000

# TROIS verdicts, jamais deux. La Bible (2.6, « Qualité de l'ingestion »)
# demande qu'un fichier mal traité ne soit jamais pris pour un fichier bien
# traité : pour cela, la différence doit être VISIBLE, pas seulement réelle.
#   EXTRACT_INDEXE  : l'extrait a été indexé en entier.
#   EXTRACT_TRONQUE : une partie seulement l'a été — l'aperçu est borné, c'est
#                     normal, et ce n'est PAS une preuve de mauvais traitement.
#   EXTRACT_BINAIRE : RIEN n'a été indexé, le contenu n'étant pas du texte. Le
#                     mot de la fiche porte alors ce refus, distinct du tronqué —
#                     et c'est `cards.appliquer_verdict_indexation` qui l'y écrit,
#                     jamais ce module.
EXTRACT_INDEXE = "indexe"
EXTRACT_TRONQUE = "tronque"
EXTRACT_BINAIRE = "binaire_refuse"

# ---------------------------------------------------------------------------
# LE FILTRE DE TYPE : une demande, un discriminant, un refus s'il n'y en a pas
#
# LE DÉFAUT RÉPARÉ ICI. `search()` traduisait `types` en `kind` via une table
# `_TYPE_TO_KINDS`, puis n'ajoutait sa clause SQL que SI l'ensemble obtenu était
# non vide. Un `type` demandé qui ne se traduisait vers aucun kind laissait donc
# `kinds` vide, donc aucune clause, donc TOUS les types en réponse. Le filtre
# demandé n'était pas appliqué et le demandeur n'en apprenait rien : un filtre
# ignoré est rigoureusement indiscernable d'une recherche sans filtre. Demander
# l'impossible répondait « voici tout, trié autrement ». C'est un fail-open, et un
# fail-open sur une question de périmètre est le pire genre — la Bible (2.6)
# exige qu'un traitement défaillant ne soit jamais pris pour un traitement réussi.
#
# DEUX CHOIX COUPÉS, ET LE SECOND EST IMPOSSIBLE SANS LE PREMIER.
#
# 1. LE DISCRIMINANT EST `ArtifactCard.document_type`, JAMAIS `Artifact.kind`.
#    `kind` n'a pas un sens unique dans ce dépôt. `sniff_mime` y écrit
#    `image|pdf|sheet`, et plusieurs appelants le REDÉRIVENT de l'extension au
#    moment de l'afficher (`tools/find_files.py` : « .xlsx/.ods/.csv ⇒ sheet »).
#    Un filtre posé sur `kind` tranche donc une valeur que rien ne garantit, et le
#    jour où un appelant ajoute son extension au moment du rendu, l'index et
#    l'affichage se contredisent sur le même fichier — et ni l'un ni l'autre ne
#    peut alors montrer pourquoi.
#    `document_type` est le SEUL discriminant normalisé ET persisté : il vit dans
#    `artifact_cards`, il est écrit par la fiche à chaque révision
#    (`cards.ensure_card_on_import` / `cards.revise_card_after_observation`), et il
#    est indexé (`idx_cards_current_status`). Il est aussi le seul des deux que le
#    filtre puisse consulter sans faire confiance à une redérivation.
#    Le corollaire est assumé : le filtre porte sur la fiche, donc il s'applique
#    comme le filtre de `status` — en Python, sur la fiche déjà chargée, et non en
#    SQL sur une colonne dénormalisée. Deux filtres, une seule architecture.
#
# 2. UN TYPE INCONNU SE REFUSE ; IL NE SE TRADUIT PAS.
#    Rendre « aucun type ne correspond » à qui a demandé l'impossible, c'est lui
#    inventer une réponse. Le code est `ENUM_INVALID` — celui qu'un schéma d'outil
#    rend déjà pour une valeur hors ensemble fermé — parce que c'est la même faute :
#    une valeur qui n'appartient pas à l'enum. C'est `ValueError("[ENUM_INVALID] …")`
#    et NON un `{"ok": False}` : ici la cause est un BUG D'APPELANT, pas une panne
#    du moteur. Une panne se rend dans le protocole d'erreur de la page, qui reste
#    exploitable ; l'exception, elle, remonte vers l'appelant qui sait la traduire,
#    et ne peut surtout pas être confondue avec « rien trouvé » — ce « rien » qui
#    est précisément le mensonge qu'on supprime ici.
#
# L'ENUM EST PETIT ET EXPLICITE, parce que c'est ce que la fiche peut porter. Les
# alias (`tableur`, `csv`, formats) sont résolus ICI, une fois pour toutes : c'est
# ici, et nulle part ailleurs, qu'on sait ce que `document_type` contient.
# ---------------------------------------------------------------------------

_TYPE_TO_DOCUMENT_TYPES: dict[str, frozenset[str]] = {
    "image": frozenset({"image"}),
    "pdf": frozenset({"pdf"}),
    # « sheet », et NON « text » : `sniff_mime` classe un `.csv` en `sheet`
    # (`backend/ipc/attachments.py`). Traduire `csv` en `text` produirait un filtre
    # GARANTI VIDE pour tout dépôt réel — un « résultat vide silencieux », c'est-à-dire
    # le défaut qu'on répare, déplacé d'un cran et rebaptisé « je ne sais pas ».
    "sheet": frozenset({"sheet"}),
    "tableur": frozenset({"sheet"}),
    "csv": frozenset({"sheet"}),
    "xlsx": frozenset({"sheet"}),
    "ods": frozenset({"sheet"}),
    # `text` : aucun `sniff_mime` ne le produit aujourd'hui, mais une base
    # antérieure — ou une fiche écrite hors de `cards.py` — peut le porter. Le
    # garder évite de répondre « ENUM_INVALID » à un type que la base contient.
    "text": frozenset({"text"}),
    "markdown": frozenset({"text"}),
    "docx": frozenset({"text"}),
}

# Statuts de fiche : filtre "unanalysed" = pending (jamais passé au modèle)
_STATUS_ALIASES = {"unanalysed": "pending"}


def _document_types_demandes(types: list[str] | None) -> frozenset[str] | None:
    """Les `document_type` qu'une liste de `types` désigne — ou LEVE `[ENUM_INVALID]`.

    Pur, et la seule porte par laquelle un filtre de type entre dans ce module.

    `None` signifie « AUCUN filtre demandé », et c'est le cas de `types=None` COMME
    de `types=[]`. La liste vide est une demande légitime — « aucun critère » — et la
    confondre avec une valeur inconnue interdirait à un appelant de chercher large.
    C'est la distinction que le défaut réparé ne faisait pas : il traitait les deux en
    « pas de clause », puis rendait la même page pour les deux, sans dire à personne
    que l'une des deux n'avait pas été comprise.

    Une liste NON VIDE qui contient une valeur hors table est, elle, une faute
    d'appelant : on lève, et le message nomme la valeur refusée ET l'enum complet,
    parce que c'est cette phrase que l'appelant reformule. Appliquer la moitié
    connue en taisant l'autre serait le même défaut en plus discret : le demandeur
    croirait avoir filtré par image ET par tableur, et n'aurait filtré que par image.

    `.get(...)` sur une table dont les CLÉS sont l'enum rend l'état « valeur
    inconnue » INDÉREPRESENTABLE : aucune table de correspondance ne peut produire
    une valeur absente ici sans avoir échoué au `if`.
    """
    if not types:
        return None
    demandes: set[str] = set()
    refusees: list[str] = []
    for demande in types:
        document_types = _TYPE_TO_DOCUMENT_TYPES.get(str(demande).strip().lower())
        if document_types is None:
            refusees.append(str(demande))
            continue
        demandes |= document_types
    if refusees:
        raise ValueError(
            f"[ENUM_INVALID] Type de fichier inconnu : {', '.join(refusees)}. "
            f"Attendu, dans une liste, parmi : "
            f"{', '.join(sorted(_TYPE_TO_DOCUMENT_TYPES))}."
        )
    return frozenset(demandes)


def ensure_fts(session) -> None:
    """Crée la table FTS5 sur la connexion de la session (idempotent)."""
    from sqlalchemy import text as _text

    session.execute(_text(_FTS_DDL))


def normalize_query(query: str) -> list[str]:
    """Normalise SANS dénaturer : NFC, minuscules, diacritiques retirés, tokens non vides."""
    q = unicodedata.normalize("NFC", query or "").lower()
    q = "".join(
        c for c in unicodedata.normalize("NFD", q) if not unicodedata.combining(c)
    )
    return [t for t in q.split() if t]


def _fts_and(tokens: list[str]) -> str:
    """Requête FTS5 : tokens guillemettés en AND implicite (termes simples, sans syntaxe)."""
    return " ".join('"' + t.replace('"', "") + '"' for t in tokens)


def arbitrer_extrait(texte: str) -> dict:
    """VERDICT d'indexation d'un extrait : `{extract, preview, indexed_chars, reason}`. Pur.

    Décide ce qu'on fait de l'extrait proposé. L'ordre des décisions est celui de
    leur coût : le binaire d'abord parce que c'est le seul qui doive être tranché
    AVANT d'écrire quoi que ce soit, la troncature ensuite parce qu'elle est une
    borne de conception. Inverser cet ordre laisserait passer le cas dangereux —
    un ZIP est plus court que la borne, donc il passerait en « tronqué » et serait
    indexé quand même. C'est exactement le défaut qu'on corrige : un binaire COURT
    passait parce que la seule protection était la longueur.

    LE JUGEMENT EST SÉPARÉ DE L'ÉCRITURE, et c'est délibéré. La fiche doit pouvoir
    porter le motif d'un refus AVANT que la ligne d'index ne soit écrite, sans quoi
    cette ligne garderait l'ancien résumé — celui d'une ingestion réussie qui n'a
    jamais eu lieu — et le refus deviendrait introuvable par la recherche, alors
    que c'est précisément ce qu'elle doit savoir dire. `cards.reindexer_carte()`
    exploite cet ordre ; `index_card_row` ci-dessous le compose pour qui n'a rien à
    décider.
    """
    if not texte:
        return {
            "extract": EXTRACT_INDEXE,
            "preview": "",
            "indexed_chars": 0,
            "reason": "",
        }
    motif = raison_binaire_texte(texte)
    if motif:
        return {
            "extract": EXTRACT_BINAIRE,
            "preview": "",
            "indexed_chars": 0,
            "reason": (
                f"Contenu binaire refusé à l'indexation ({motif}) : aucun octet de "
                "ce fichier n'a été indexé, et son contenu n'est pas du texte."
            ),
        }
    if len(texte) > PREVIEW_CHARS:
        return {
            "extract": EXTRACT_TRONQUE,
            "preview": texte[:PREVIEW_CHARS],
            "indexed_chars": PREVIEW_CHARS,
            "reason": (
                f"Extrait tronqué à {PREVIEW_CHARS} caractères sur {len(texte)} : "
                "une partie seulement est indexée, l'aperçu servant au classement."
            ),
        }
    return {
        "extract": EXTRACT_INDEXE,
        "preview": texte,
        "indexed_chars": len(texte),
        "reason": "",
    }


def ecrire_ligne_index(
    session, artifact, card, scope_kind: str, scope_id: str, apercu: str
) -> None:
    """(Ré)écrit LA ligne d'index d'un lien de scope. Idempotent par suppression.

    N'Arbitre rien : elle écrit ce qu'on lui donne, y compris une ligne sans aperçu.
    CE QUI RESTE QUAND LE CONTENU EST REFUSÉ : la ligne, avec un aperçu vide. Elle
    porte le nom, le titre, les mots-clés et le RÉSUMÉ — donc le motif du refus,
    qui est lui-même du texte, et que la recherche peut donc trouver. Supprimer la
    ligne ferait disparaître le fichier des résultats sans laisser de trace de
    pourquoi, ce qui est le pire des deux défauts : le silence.
    """
    from sqlalchemy import text as _text
    import json as _json

    try:
        keywords = " ".join(_json.loads(card.keywords_json or "[]"))
    except Exception:
        keywords = ""
    try:
        entities = " ".join(_json.loads(card.entities_json or "[]"))
    except Exception:
        entities = ""
    session.execute(
        _text(
            "DELETE FROM artifact_search WHERE artifact_id = :a AND scope_kind = :sk AND scope_id = :sid"
        ),
        {"a": artifact.id, "sk": scope_kind, "sid": scope_id},
    )
    session.execute(
        _text("""INSERT INTO artifact_search
                 (artifact_id, scope_kind, scope_id, kind, card_revision,
                  filename, title, summary, keywords, entities, extracted_preview)
                 VALUES (:a, :sk, :sid, :k, :rev, :fn, :t, :s, :kw, :en, :ex)"""),
        {
            "a": artifact.id,
            "sk": scope_kind,
            "sid": scope_id,
            "k": (artifact.kind or "text"),
            "rev": int(card.revision or 0),
            "fn": artifact.original_filename or artifact.display_name or "",
            "t": card.title or "",
            "s": card.summary or "",
            "kw": keywords,
            "en": entities,
            "ex": apercu,
        },
    )


def index_card_row(
    session, artifact, card, scope_kind: str, scope_id: str, extracted_preview: str = ""
) -> dict:
    """Juge l'extrait puis écrit la ligne d'index — SANS décider de l'état de la fiche.

    Retourne le VERDICT d'indexation : `{"extract", "indexed_chars", "reason",
    "card_status"}`. Les appelants d'aujourd'hui l'ignoraient — ils n'avaient rien
    à décider et le contrat n'existait pas. Il existe maintenant parce que c'est
    ici, et NULLE PART AILLEURS, qu'on sait encore ce que l'extrait contenait :
    partout ailleurs il n'en reste qu'une chaîne, indiscernable d'une autre.

    CE CHEMIN NE CONDAMNE AUCUNE FICHE, et c'est volontaire : appliquer un verdict
    à une fiche est la responsabilité de `cards.py`, qui détient la fiche et son
    historique. Un index qui la condamnerait déciderait de l'état d'une ligne de
    `cards.py` sans voir ni ce qui a été demandé à ce fichier, ni si une autre
    extraction a réussi depuis. Les appelants de production passent donc par
    `cards.reindexer_carte()`, seul chemin qui statue.
    """
    verdict = arbitrer_extrait(extracted_preview)
    ecrire_ligne_index(
        session, artifact, card, scope_kind, scope_id, verdict["preview"]
    )
    return {
        "extract": verdict["extract"],
        "indexed_chars": verdict["indexed_chars"],
        "reason": verdict["reason"],
        "card_status": (card.analysis_status or card.status or "pending")
        if card is not None
        else None,
    }


def remove_artifact(session, artifact_id: str) -> dict:
    """Retire un artefact de l'index (purge GC) et rend un résultat structuré.

    Le GC compte les succès et les échecs : une suppression d'index qui échoue
    ne doit pas disparaître derrière un `None`. Le `DELETE` reste idempotent —
    zéro ligne détruite est un succès.
    """
    from sqlalchemy import text as _text

    resultat: dict = {"ok": True, "artifact_id": artifact_id, "lignes": 0, "errors": []}
    try:
        curseur = session.execute(
            _text("DELETE FROM artifact_search WHERE artifact_id = :a"),
            {"a": artifact_id},
        )
        resultat["lignes"] = int(curseur.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 - remonté dans `errors`, pas avalé
        resultat["ok"] = False
        resultat["errors"].append(f"{type(exc).__name__}: {exc}")
    return resultat


def purger_index_artifact(session, artifact_id: str) -> dict:
    """Purge les TROIS traces d'index d'un artefact : FTS, fragments, vecteurs.

    Un `binaire_refuse` ne doit laisser ni aperçu indexé, ni fragment, ni vecteur :
    les supprimer à un seul endroit laisserait les autres chemins de recherche
    continuer à renvoyer des octets refusés. Le résultat combine les deux
    opérations, pour que l'appelant sache laquelle a échoué.
    """
    from backend.search import chunks as _chunks

    fts = remove_artifact(session, artifact_id)
    chunks = _chunks.remove_chunks_for_artifact(session, artifact_id)
    erreurs = list(fts.get("errors", [])) + list(chunks.get("errors", []))
    return {
        "ok": bool(fts.get("ok")) and bool(chunks.get("ok")),
        "artifact_id": artifact_id,
        "fts": fts,
        "chunks": chunks,
        "errors": erreurs,
    }


def backfill_binaires_refuses(session, *, limite: int = 1000) -> dict:
    """Purge l'index des artefacts DÉJÀ `binaire_refuse` — idempotent.

    Le garde-fou de l'ingestion empêche le dommage futur ; ce backfill répare le
    passé. Il est appelable manuellement ou au démarrage, ne lève jamais pour un
    artefact isolé, et rend le COMPTEUR de lignes traitées (`traites`).

    Idempotent : un artefact déjà purgé est relu, purgé sans effet, et compté une
    seconde fois comme traité — la relecture est le prix d'un démarrage sûr.
    """
    from backend.db.models import Artifact as _Art

    artefacts = (
        session.query(_Art)
        .filter(_Art.ingest_state == "binaire_refuse")
        .order_by(_Art.id)
        .limit(max(1, int(limite)))
        .all()
    )
    traites = 0
    for artefact in artefacts:
        try:
            purger_index_artifact(session, artefact.id)
            traites += 1
        except Exception as exc:
            log.warning(
                "[fts] purge binaire_refuse impossible pour %s : %s: %s",
                artefact.id,
                type(exc).__name__,
                exc,
            )
    try:
        session.commit()
    except Exception as exc:
        log.warning(
            "[fts] commit du backfill binaire_refuse impossible : %s: %s",
            type(exc).__name__,
            exc,
        )
    return {
        "ok": True,
        "traites": traites,
        "artifact_ids": [a.id for a in artefacts],
        "limite": max(1, int(limite)),
    }


# ---------------------------------------------------------------------------
# Réparation d'un index DÉJÀ pollué
#
# Le garde-fou ci-dessus empêche le dommage d'exister. Il ne répare pas celui qui
# est déjà écrit : un index qui contient des octets continue d'en renvoyer tant
# que personne ne l'a purgé. Ces deux fonctions offrent ce chemin — à lancer
# volontairement, jamais au démarrage de l'application.
# ---------------------------------------------------------------------------

OUTIL_INGESTION = "attachment_ingest"  # l'outil qui extrait le texte d'une pièce jointe


def _est_deja_remis(card) -> bool:
    """Fiche déjà passée `stale` : la purge a été faite, il ne reste que la
    réingestion. Sans cette distinction, le rapport annoncerait une réparation
    qui n'a pas eu lieu à chaque nouvelle inspection."""
    return card is not None and (card.analysis_status or "") == "stale"


def detecter_ingestions_binaire_ou_tronquees(
    session, *, limite: int = 1000
) -> list[dict]:
    """Recense les artefacts dont l'extraction ENREGISTRÉE ne peut pas être crue.

    TROIS sources, parce que le dommage s'écrit à trois endroits : l'observation
    (le contenu de référence), les fragments (ce que la recherche hybride
    interroge) et la ligne d'index (l'aperçu qui sert au classement). Un artefact
    peut n'être pollué qu'à l'un des trois ; en purger un seul laisserait la
    recherche renvoyer encore des octets par un autre chemin.

    Pour les observations, seule la PLUS RÉCENTE compte par artefact : une
    extraction ancienne en octets, suivie d'une extraction propre, décrit un
    fichier déjà réparé. Exiger les deux ferait remonter des artefacts sains et
    ferait refaire un travail qui l'est déjà.

    Chaque entrée porte un motif LISIBLE, jamais un booléen : c'est cette phrase
    qui expliquera à l'utilisateur pourquoi son fichier n'est pas interrogeable.

    `limite` borne le nombre de lignes lues PAR SOURCE. Un corpus plus gros donne
    donc un rapport partiel — partiel et non faux : rien de ce qui est remonté
    ne l'est à tort, et un artefact absent du rapport n'est pas acquitté.
    """
    from backend.db.models import Artifact as _Art
    from backend.db.models import ArtifactChunk as _Chk
    from backend.db.models import Observation as _Obs
    from backend.search.observations import PAYLOAD_CHARS as _PAQUET

    trouves: dict[str, dict] = {}

    def _noter(artifact_id: str, source: str, raison: str) -> None:
        entree = trouves.setdefault(
            artifact_id, {"artifact_id": artifact_id, "sources": [], "raisons": []}
        )
        if source not in entree["sources"]:
            entree["sources"].append(source)
        if raison not in entree["raisons"]:
            entree["raisons"].append(raison)

    # 1. L'observation d'ingestion : le contenu de référence lui-même.
    vus: set[str] = set()
    for obs in (
        session.query(_Obs)
        .filter(_Obs.tool_name == OUTIL_INGESTION, _Obs.status == "completed")
        .order_by(_Obs.created_at.desc())
        .limit(limite)
        .all()
    ):
        artifact_id = obs.artifact_id
        if artifact_id in vus:
            continue
        vus.add(artifact_id)
        motif = raison_binaire_texte(obs.payload_ref or "")
        if motif:
            _noter(artifact_id, "observation", f"observation binaire ({motif})")
        elif len(obs.payload_ref or "") >= _PAQUET:
            # Incomplet, pas faux : le document est plus long que ce que Thot
            # retient. Ça se dit « à compléter », jamais « raté ».
            _noter(
                artifact_id,
                "observation",
                f"extraction tronquée à la borne de {_PAQUET} caractères : le "
                "document est plus long que ce que Thot retient",
            )

    # 2. Les fragments : ce que la recherche hybride va interroger.
    for chunk in (
        session.query(_Chk)
        .filter(_Chk.text.isnot(None))
        .order_by(_Chk.artifact_id.asc())
        .limit(limite)
        .all()
    ):
        motif = raison_binaire_texte(chunk.text or "")
        if motif:
            _noter(chunk.artifact_id, "chunks", f"fragment binaire ({motif})")

    # 3. L'aperçu indexé. `artifact_search` est une table virtuelle sans modèle
    #    ORM : une lecture SQL directe s'impose, et elle reste facultative — une
    #    base où l'index n'a jamais été créé n'a rien à polluer.
    try:
        from sqlalchemy import text as _text

        lignes = session.execute(
            _text("""SELECT artifact_id, extracted_preview FROM artifact_search
                     WHERE extracted_preview IS NOT NULL LIMIT :n"""),
            {"n": limite},
        ).all()
        for artifact_id, apercu in lignes:
            motif = raison_binaire_texte(apercu or "")
            if motif:
                _noter(str(artifact_id), "index", f"aperçu indexé binaire ({motif})")
    except Exception:
        pass

    if not trouves:
        return []
    ids = sorted(trouves)
    noms = {
        a.id: (a.original_filename or a.display_name or a.id, a.kind)
        for a in session.query(_Art).filter(_Art.id.in_(ids)).all()
    }
    rapport = []
    for artifact_id in ids:
        entree = trouves[artifact_id]
        nom, kind = noms.get(artifact_id, (artifact_id, "?"))
        rapport.append(
            {
                "artifact_id": artifact_id,
                "filename": nom,
                "kind": kind,
                "sources": entree["sources"],
                "raisons": entree["raisons"],
            }
        )
    return rapport


def reparer_ingestions_binaire_ou_tronquees(
    session,
    artefact_ids: list[str] | None = None,
    *,
    appliquer: bool = False,
    relancer: bool = False,
    limite: int = 1000,
) -> dict:
    """Remet à réingérer les artefacts dont l'extraction enregistrée est binaire ou tronquée.

    `appliquer=False` — le DÉFAUT — ne touche à rien et se contente de répondre.
    Réparer un index, c'est supprimer des lignes puis relancer une extraction :
    deux opérations qui coûtent du temps et ne doivent jamais être déclenchées par
    une simple inspection. Un défaut qui purge sur un doute est pire que le
    problème qu'il corrige.

    `relancer=False` — le défaut aussi — laisse à l'appelant le soin de déclencher
    l'extraction. Réingérer un PDF tronqué, c'est de l'OCR : cela demande un
    modèle que ce module n'a aucune raison de démarrer de lui-même. La fonction
    REND donc la liste ; qui veut la lancer, la lance.

    `artefact_ids` restreint la réparation à ce qu'on lui donne, ce qui permet de
    ne remettre que les artefacts-binaires et de laisser les tronqués — plus
    coûteux, car partiels sans être faux — à une décision humaine.

    Ce que `appliquer=True` fait, et pourquoi : les fragments et la ligne d'index
    partent, et la fiche passe `stale`. Pas `failed` — un artefact binaire n'est
    pas une faute de l'utilisateur, c'est un état de traitement à refaire, et
    `stale` dit cela dans le vocabulaire du modèle, sans inventer un statut.

    Le rapport distingue trois listes, parce qu'elles ne répondent pas à la même
    question : `detectes` (ce qui est faux), `remis_a_reingerer` (ce que CET
    appel remet) et `deja_remis` (ce qui a été remit avant et attend sa
    réingestion). Un artefact déjà remis reste listé dans `detectes` — son
    observation est IMMUABLE par conception et portera les octets de l'extraction
    fautive jusqu'à ce qu'une extraction propre l'ait remplacée. Le remettre deux
    fois ne ferait rien de plus, et ferait croire que la réparation n'a pas
    marché : le pire des deux messages.
    """
    from backend.search import chunks as _chunks

    detectes = detecter_ingestions_binaire_ou_tronquees(session, limite=limite)
    if artefact_ids is not None:
        choisies = set(artefact_ids)
        detectes = [d for d in detectes if d["artifact_id"] in choisies]
    fiches = _current_cards_map(session, [d["artifact_id"] for d in detectes])
    deja_remis = [
        d["artifact_id"]
        for d in detectes
        if _est_deja_remis(fiches.get(d["artifact_id"]))
    ]
    a_remettre = [d for d in detectes if d["artifact_id"] not in set(deja_remis)]
    rapport: dict = {
        "ok": True,
        "applique": bool(appliquer),
        "relance": False,
        "detectes": detectes,
        "remis_a_reingerer": [d["artifact_id"] for d in a_remettre],
        "deja_remis": deja_remis,
        "ignores": [],
    }
    if not appliquer or not a_remettre:
        return rapport

    for entree in a_remettre:
        artifact_id = entree["artifact_id"]
        # La purge ne dépend PAS de la fiche : un artefact sans fiche garde sa
        # ligne d'index comme un moignon, et c'est ce moignon que la recherche
        # interroge encore. On purge donc d'abord, on statue ensuite.
        remove_artifact(session, artifact_id)
        _chunks.remove_chunks_for_artifact(session, artifact_id)
        card = fiches.get(artifact_id)
        if card is None:
            rapport["ignores"].append(
                {
                    "artifact_id": artifact_id,
                    "raison": "aucune fiche à remettre à l'état",
                }
            )
            continue
        card.analysis_status = "stale"
        card.status = "stale"
        # Le motif va dans la fiche, pas seulement dans le rapport : le rapport
        # est lu par un opérateur, la fiche par l'utilisateur du fichier. Un
        # motif décrit uniquement dans le rapport laisserait le second sans rien.
        card.summary = "Extraction à refaire : " + "; ".join(entree["raisons"]) + "."
    session.flush()

    # On relance les DEUX listes : un artefact déjà remis est précisément celui
    # qui attend sa réingestion. `relancer` reste faux par défaut — décider de
    # coûter du temps GPU reste la décision de l'appelant.
    a_reingerer = rapport["remis_a_reingerer"] + rapport["deja_remis"]
    if relancer and a_reingerer:
        from backend.ingestion import attachment_ingest as _ing

        _ing.lancer_ingestion(sorted(set(a_reingerer)))
        rapport["relance"] = True
    return rapport


def _cursor_encode(fingerprint: str, offset: int) -> str:
    raw = json.dumps({"qh": fingerprint, "off": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _cursor_decode(cursor: str) -> dict | None:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        return None


def _request_fingerprint(
    query: str, scope_kind: str, scope_id: str, types: list[str] | None, status: str
) -> str:
    raw = "|".join(
        [query, scope_kind, scope_id, ",".join(sorted(types or [])), status or "any"]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _current_cards_map(session, artifact_ids: list[str]) -> dict:
    """artifact_id → fiche active (is_current=1, repli legacy : révision max)."""
    from backend.db.models import ArtifactCard as _Card

    out: dict = {}
    if not artifact_ids:
        return out
    rows = session.query(_Card).filter(_Card.artifact_id.in_(artifact_ids)).all()
    best: dict = {}
    for c in rows:
        cur = best.get(c.artifact_id)
        key = (1 if int(c.is_current or 0) == 1 else 0, int(c.revision or 0))
        if cur is None or key > cur[0]:
            best[c.artifact_id] = (key, c)
    return {aid: c for aid, (_, c) in best.items()}


def _score_row(
    tokens: list[str], full: str, row: dict, age_days: float | None
) -> tuple[float, list[str]]:
    """Score transparent annexe 11 : 4 filename, 3 title, 2 kw/entities, 1.5 summary, 1 extrait."""

    def _hit(weight: float, text: str) -> tuple[float, bool, bool]:
        t = (text or "").lower()
        if not t:
            return 0.0, False, False
        if full and full in t:
            return weight, True, True
        hits = sum(1 for tok in tokens if tok in t)
        if hits >= len(tokens):
            return round(weight * 0.75, 3), True, False
        if hits:
            return round(weight * 0.4, 3), True, False
        return 0.0, False, False

    score, reasons = 0.0, []
    s, any_h, _ = _hit(4.0, row.get("filename") or "")
    if any_h:
        score += s
        reasons.append("filename")
    s, any_h, _ = _hit(3.0, row.get("title") or "")
    if any_h:
        score += s
        reasons.append("title")
    s1, h1, _ = _hit(2.0, row.get("keywords") or "")
    s2, h2, _ = _hit(2.0, row.get("entities") or "")
    if h1 or h2:
        score += max(s1, s2)
        reasons.append("keyword: " + (", ".join(tokens)))
    s, any_h, _ = _hit(1.5, row.get("summary") or "")
    if any_h:
        score += s
        reasons.append("summary")
    s, any_h, _ = _hit(1.0, row.get("extracted_preview") or "")
    if any_h:
        score += s
        reasons.append("extrait")
    if age_days is not None and age_days >= 0:
        score += round(
            0.2 * max(0.0, 1.0 - age_days / 90.0), 3
        )  # récence = départage, jamais vérité
    score += -float(row.get("bm25") or 0.0) * 1e-6  # BM25 FTS5 : départage résiduel
    return round(score, 4), reasons


def _hybrid_rerank(
    session,
    query_str: str,
    tokens: list[str],
    full_norm: str,
    ranked: list[dict],
    allowed_ids: set[str],
    scope_kind: str,
    scope_id: str,
) -> list[dict]:
    """RRF(doc-FTS, chunk-FTS, vecteurs) + blend rerank local. Sans chunks indexés : no-op."""
    from sqlalchemy import text as _text

    try:
        n = session.execute(
            _text(
                "SELECT COUNT(*) FROM chunk_search WHERE scope_kind = :sk AND scope_id = :sid"
            ),
            {"sk": scope_kind, "sid": scope_id},
        ).scalar()
    except Exception:
        return ranked
    if not n:
        return ranked
    match_q = _fts_and(tokens)
    chunk_order: list[str] = []
    try:
        rows = (
            session.execute(
                _text("""SELECT artifact_id, bm25(chunk_search) AS r FROM chunk_search
                     WHERE scope_kind = :sk AND scope_id = :sid AND chunk_search MATCH :q
                     ORDER BY bm25(chunk_search) LIMIT 50"""),
                {"sk": scope_kind, "sid": scope_id, "q": match_q},
            )
            .mappings()
            .all()
        )
        seen: set[str] = set()
        for r in rows:
            aid = r["artifact_id"]
            if aid in (allowed_ids or set()) and aid not in seen:
                seen.add(aid)
                chunk_order.append(aid)
    except Exception:
        pass
    # LA TROISIÈME LISTE EST LE MOTEUR SÉMANTIQUE (ColBERT, MaxSim). Elle ne
    # remplace ni le FTS document ni le FTS par fragment : elle s'y AJOUTE, et la
    # fusion RRF décide de leur poids. Les deux signaux lexiques répondent à des
    # questions différentes — « quel nom contient ce fichier ? » et « quels mots
    # contient ce passage ? » — que la Bible (2.5) demande de pouvoir poser
    # séparément. Le ColBERT répond à une troisième : « de quoi parle ce fragment,
    # quels que soient les mots ? ». Le retirer laisserait un hybrid lexical, pas
    # une recherche sémantique.
    vec_order: list[str] = []
    try:
        from backend.search import vectors as _vec

        vec_order = [
            aid
            for aid, _ in _vec.search_vectors(
                session, query_str, allowed_ids or set(), 50
            )
        ]
    except Exception:
        # Une panne du moteur sémantique ne doit pas supprimer les deux listes
        # lexicales : on rend la page qu'on sait construire. `vectors.statut_moteur()`
        # est l'appel qui dit pourquoi la liste sémantique manque.
        pass
    signals = [[d["aid"] for d in ranked]]
    if chunk_order:
        signals.append(chunk_order)
    if vec_order:
        signals.append(vec_order)
    if len(signals) < 2:
        return ranked
    try:
        from backend.search import vectors as _vec2

        fused = _vec2.rrf_fuse(signals)
    except Exception:
        return ranked
    by_aid = {d["aid"]: d for d in ranked}
    # Artefacts apportés par chunks/vecteurs seuls : entrée calculée depuis la fiche
    try:
        from backend.db.models import Artifact as _ArtH

        missing = [aid for aid, _ in fused if aid not in by_aid]
        if missing:
            arts = {
                a.id: a
                for a in session.query(_ArtH).filter(_ArtH.id.in_(missing)).all()
            }
            cards = _current_cards_map(session, missing)
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            for aid in missing:
                art, card = arts.get(aid), cards.get(aid)
                if art is None or card is None:
                    continue
                try:
                    import json as _jh

                    kw = " ".join(_jh.loads(card.keywords_json or "[]"))
                except Exception:
                    kw = ""
                row = {
                    "filename": art.original_filename or "",
                    "title": card.title or "",
                    "summary": card.summary or "",
                    "keywords": kw,
                    "entities": "",
                    "extracted_preview": "",
                    "bm25": 0.0,
                }
                score, reasons = _score_row(tokens, full_norm, row, None)
                by_aid[aid] = {
                    "aid": aid,
                    "art": art,
                    "card": card,
                    "score": score,
                    "reasons": reasons or ["hybride"],
                }
    except Exception:
        pass
    fused_ids = [aid for aid, _ in fused if aid in by_aid]
    fused_ids += [d["aid"] for d in ranked if d["aid"] not in set(fused_ids)]
    # Blend : 0.6 RRF normalisé + 0.4 score lexical normalisé (déterministe)
    rrf = {aid: s for aid, s in fused}
    rmax = max(rrf.values()) if rrf else 1.0
    smax = max((by_aid[a]["score"] for a in fused_ids), default=0.0) or 1.0
    blended = sorted(
        fused_ids,
        key=lambda a: (
            0.6 * (rrf.get(a, 0.0) / rmax) + 0.4 * (by_aid[a]["score"] / smax)
        ),
        reverse=True,
    )
    return [by_aid[a] for a in blended]


def search(
    session,
    *,
    query: str,
    scope_kind: str,
    scope_id: str,
    allowed_ids: set[str],
    types: list[str] | None = None,
    status: str = "any",
    limit: int = 10,
    cursor: str | None = None,
) -> dict:
    """Recherche bornée et paginée. Ne voit QUE les ids autorisés (scope.py en amont).

    Retourne {"ok": True, page…} ou {"ok": False, "code": …, "message": …}.

    LÈVE `ValueError("[ENUM_INVALID] …")` — et ne rend donc PAS de page — si `types`
    porte une valeur hors enum. Voir `_document_types_demandes` : c'est la seule
    sortie de cette fonction qui ne soit pas une page, et c'est volontaire.
    """
    from sqlalchemy import text as _text
    from datetime import datetime, timezone

    # Premier geste, avant toute interrogation : un filtre qu'on ne comprend pas se
    # refuse IMMÉDIATEMENT. Chercher d'abord pour ne refuser qu'ensuite laisserait
    # une trace de recherche faite pour un filtre qui n'a jamais existé.
    document_types = _document_types_demandes(types)
    original = (query or "").strip()
    if not original or len(original) > MAX_QUERY:
        return {
            "ok": False,
            "code": "SEARCH_QUERY_INVALID",
            "message": "Requête absente ou trop longue (300 caractères max).",
        }
    if not scope_kind or not scope_id:
        return {
            "ok": False,
            "code": "SEARCH_SCOPE_INVALID",
            "message": "Périmètre de recherche absent ou incompatible.",
        }
    tokens = normalize_query(original)
    if not tokens:
        return {
            "ok": False,
            "code": "SEARCH_QUERY_INVALID",
            "message": "Requête illisible après normalisation.",
        }
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except Exception:
        limit = 10

    fp = _request_fingerprint(original, scope_kind, scope_id, types, status or "any")
    offset = 0
    if cursor:
        dec = _cursor_decode(cursor)
        if not dec or dec.get("qh") != fp:
            return {
                "ok": False,
                "code": "SEARCH_CURSOR_MISMATCH",
                "message": "Curseur d'une autre recherche : relance sans curseur.",
            }
        try:
            offset = max(0, int(dec.get("off", 0)))
        except Exception:
            offset = 0

    status_norm = _STATUS_ALIASES.get(
        (status or "any").lower(), (status or "any").lower()
    )

    # AUCUNE clause de type en SQL, et c'est un choix : le type est porté par la
    # FICHE (`ArtifactCard.document_type`), pas par la colonne `kind` dénormalisée
    # de l'index — voir `_TYPE_TO_DOCUMENT_TYPES`. La colonne `kind` reste donc
    # décorative ici, comme elle l'est déjà pour le `kind` rendu au client.
    match_q = _fts_and(tokens)
    params = {"sk": scope_kind, "sid": scope_id, "q": match_q}
    try:
        rows = (
            session.execute(
                _text(f"""SELECT artifact_id, scope_kind, scope_id, kind, card_revision,
                             filename, title, summary, keywords, entities, extracted_preview,
                             bm25(artifact_search) AS bm25
                      FROM artifact_search
                      WHERE scope_kind = :sk AND scope_id = :sid
                        AND artifact_search MATCH :q
                      ORDER BY bm25(artifact_search)
                      LIMIT {MAX_CANDIDATES}"""),
                params,
            )
            .mappings()
            .all()
        )
    except Exception as e:
        return {
            "ok": False,
            "code": "SEARCH_QUERY_INVALID",
            "message": f"Requête FTS rejetée : {str(e)[:200]}",
        }
    # Repli OR si le AND strict ne donne rien (requête multi-termes)
    if not rows and len(tokens) > 1:
        or_q = " OR ".join('"' + t.replace('"', "") + '"' for t in tokens)
        try:
            rows = (
                session.execute(
                    _text(f"""SELECT artifact_id, scope_kind, scope_id, kind, card_revision,
                                 filename, title, summary, keywords, entities, extracted_preview,
                                 bm25(artifact_search) AS bm25
                          FROM artifact_search
                          WHERE scope_kind = :sk AND scope_id = :sid
                            AND artifact_search MATCH :q
                          ORDER BY bm25(artifact_search)
                          LIMIT {MAX_CANDIDATES}"""),
                    {**params, "q": or_q},
                )
                .mappings()
                .all()
            )
        except Exception:
            rows = []

    # Sécurité + fraîcheur : filtre allowed, révision active, statut, type, artefact vivant
    cand_ids = sorted({r["artifact_id"] for r in rows} & set(allowed_ids or set()))
    cards = _current_cards_map(session, cand_ids)
    from backend.db.models import Artifact as _Art

    arts = (
        {a.id: a for a in session.query(_Art).filter(_Art.id.in_(cand_ids)).all()}
        if cand_ids
        else {}
    )
    now = datetime.now(timezone.utc)
    full_norm = " ".join(tokens)

    ranked: list[dict] = []
    for r in rows:
        aid = r["artifact_id"]
        if aid not in allowed_ids or aid not in cards or aid not in arts:
            continue
        card = cards[aid]
        if int(r["card_revision"] or 0) != int(card.revision or 0):
            continue  # ligne FTS d'une révision dépassée
        if (
            status_norm != "any"
            and (card.analysis_status or card.status or "") != status_norm
        ):
            continue
        art = arts[aid]
        if getattr(art, "deleted_at", None) is not None:
            continue
        age = None
        try:
            created = art.created_at
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = (now - created).total_seconds() / 86400.0
        except Exception:
            pass
        score, reasons = _score_row(tokens, full_norm, dict(r), age)
        ranked.append(
            {"aid": aid, "art": art, "card": card, "score": score, "reasons": reasons}
        )
    ranked.sort(key=lambda d: d["score"], reverse=True)
    # Lot C : fusion hybride SANS changer l'API — FTS doc + FTS chunks + vecteurs → RRF → rerank.
    # Filtres de droits AVANT tout : seuls les allowed participent aux 3 listes (T-SRCH-16).
    try:
        ranked = _hybrid_rerank(
            session,
            original,
            tokens,
            full_norm,
            ranked,
            set(allowed_ids or set()),
            scope_kind,
            scope_id,
        )
    except Exception:
        pass
    # LE FILTRE DE TYPE PASSE ICI, ET PAS DANS LA BOUCLE DE CANDIDATS.
    # Deux raisons, dont une seule se voit. Visible : la boucle ne filtre que les
    # lignes FTS, or `_hybrid_rerank` REMET ensuite dans `ranked` des artefacts
    # venus des fragments ou des vecteurs — jamais passés par la boucle. Un filtre
    # posé dans la boucle laisserait donc passer ces artefacts-là, et une recherche
    # « uniquement des images » rendrait un tableur : le fail-open qu'on répare,
    # ressorti par la porte de derrière. Invisible : après le rerank, la liste est
    # celle qui va être PAGINÉE — filtrer avant la pagination donnerait des pages
    # creuses et un `next_cursor` qui ne correspond à rien.
    # On lit `document_type` et JAMAIS `artifact.kind` : c'est le seul discriminant
    # normalisé et persisté (voir `_TYPE_TO_DOCUMENT_TYPES`). Une fiche sans
    # `document_type` n'a pas de type établi, donc elle ne peut pas satisfaire un
    # filtre qui en demande un.
    # CE QU'ON PERDE, ÉNONCÉ : le pool de candidats est borné à `MAX_CANDIDATES`
    # lignes AVANT ce filtre, donc un filtre de type peut sous-rendre dans un corpus
    # dominé par d'autres types. On l'accepte plutôt que de réintroduire un `kind IN
    # (...)` en SQL : ce pool est déjà borné de la même façon pour le filtre de
    # statut, et un préfiltre SQL sur `kind` rouvrirait exactement la faille qu'on
    # ferme — une colonne que les appelants redérivent au moment du rendu.
    if document_types:
        ranked = [
            d
            for d in ranked
            if (getattr(d.get("card"), "document_type", None) or "").strip().lower()
            in document_types
        ]

    page = ranked[offset : offset + limit]
    results = []
    for d in page:
        art, card = d["art"], d["card"]
        kind = (art.kind or "text").lower()
        # On n'annonce QUE des actions que l'orchestrateur peut réellement
        # exécuter. Lui proposer `vision` ou `doc_ocr` alors qu'ils ne sont pas
        # exposés le laisse lire une consigne inapplicable — c'est-à-dire
        # annoncer une capacité inexistante, exactement le défaut qu'on corrige
        # ailleurs. La lecture passe par `ask_files`, qui sait router vers
        # l'OCR, la vision et la lecture selon le type de fichier.
        if kind == "image":
            actions = ["ask_files"]
        elif kind == "pdf":
            actions = ["ask_files"]
        else:
            actions = ["ask_files", "read_text"]
        try:
            size = None
            from backend.db.models import Blob as _Blob

            blob = session.query(_Blob).filter(_Blob.id == art.blob_id).first()
            size = blob.size_bytes if blob else None
        except Exception:
            size = None
        results.append(
            {
                "artifact_id": art.id,
                "name": art.original_filename or art.display_name or art.id,
                "kind": kind,
                "size_bytes": size,
                "score": d["score"],
                "match_reasons": d["reasons"],
                "card": {
                    "revision": int(card.revision or 0),
                    "summary": card.summary or "",
                    "status": card.analysis_status or card.status or "pending",
                },
                "available_actions": actions,
            }
        )

    next_cursor = None
    if offset + limit < len(ranked):
        next_cursor = _cursor_encode(fp, offset + limit)
    out: dict = {
        "ok": True,
        "query": original,
        "scope": scope_kind,
        "filters": {"types": types or None, "status": status or "any"},
        "results": results,
        "next_cursor": next_cursor,
        "searched_count": len(allowed_ids or set()),
        "returned_count": len(results),
    }
    if not results:
        # Absence bornée : périmètre + pistes, jamais "le fichier n'existe pas" (T-SRCH-09)
        out["suggestions"] = [
            "retirer le filtre de type",
            "chercher avec un seul mot-clé",
            "demander la liste des fichiers (ask_files)",
        ]
    return out


# ---------------------------------------------------------------------------
# Recherche sur l'UNION des portées d'un chat (matrice de permissions, ligne 2)
# ---------------------------------------------------------------------------


def _workspace_scopes_of(session, allowed_ids: set[str]) -> list[str]:
    """Portées `workspace` présentes dans le périmètre DÉJÀ AUTORISÉ, triées.

    On ne demande pas « de quel espace s'agit-il ? » mais « quelles portées ce
    set contient-il ? ». La décision de droit est donc prise ailleurs —
    `scope.resolve_allowed_artifacts`, qui filtre aussi les espaces en corbeille —
    et jamais ici. Un chat simple n'a aucun lien `workspace` dans son périmètre :
    rien à lui ajouter, et l'appel coûte ce qu'il coûtait.
    """
    ids = sorted(str(a) for a in (allowed_ids or set()) if a)
    if not ids:
        return []
    from backend.db.models import ArtifactLink as _Link

    try:
        rows = (
            session.query(_Link.scope_id)
            .filter(
                _Link.scope_kind == "workspace",
                _Link.removed_at.is_(None),
                _Link.artifact_id.in_(ids),
            )
            .distinct()
            .all()
        )
    except Exception:
        return []
    return sorted(
        {str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()}
    )


def search_scopes(
    session,
    *,
    query: str,
    conversation_id: str,
    allowed_ids: set[str],
    types: list[str] | None = None,
    status: str = "any",
    limit: int = 10,
) -> dict:
    """Recherche sur l'union conversation + dépôt, au lieu d'une portée unique.

    `search` filtre sur UN couple (`scope_kind`, `scope_id`) et `artifact_search`
    porte UNE ligne par lien actif. Le périmètre d'un chat d'espace a deux portées
    : `conversation` et `workspace`. Un fichier versé au dépôt n'a de ligne que
    sous la seconde — interroger la seule première ne le rend pas CANDIDAT, si
    grand que soit le dépôt. C'est la raison d'être de cette fonction.

    Chaque portée est donc interrogée séparément avec le MÊME `allowed_ids`, puis
    les pages sont fusionnées par `artifact_id`. Le filtre de droits passe ainsi
    avant tout classement, dans les deux cas, et il est identique — la fusion ne
    peut rien réélargir.

    La conversation passe en premier : un fichier à la fois joint à CETTE
    discussion et versé au dépôt est retenu comme joint, ce que
    `docrefs.public_id_for_artifact` décide de son côté pour son `doc_N`. Les
    deux ne peuvent pas se contredire.

    Même contrat d'honnêteté que la recherche d'espace
    (`search_space_information`) : si une portée se tait, `partial` la nomme et
    la page reste exploitable ; si AUCUNE ne répond, on LÈVE. Un « rien trouvé »
    fabriqué par une panne serait pire qu'une erreur franche.

    L'enum de `types` se statue ICI, une fois, AVANT la boucle — et c'est ce même
    refus qui doit ressortir, pas une panne. Une valeur hors enum est indépendante
    de la portée : la laisser remonter par le `except Exception` ci-dessous la
    maquillerait en `RuntimeError("recherche multi-portée indisponible")`, une panne
    INVENTÉE pour un argument mal fourni. L'appelant relancerait indéfiniment au
    lieu de reformuler son filtre — le même silence, déplacé d'un cran.
    """
    _document_types_demandes(types)
    scopes: list[tuple[str, str]] = []
    if conversation_id:
        scopes.append(("conversation", str(conversation_id)))
    scopes.extend(
        ("workspace", ws) for ws in _workspace_scopes_of(session, allowed_ids)
    )
    if not scopes:
        return {
            "ok": False,
            "code": "SEARCH_SCOPE_INVALID",
            "message": "Aucune portée de recherche pour cette conversation.",
            "results": [],
            "scopes": [],
        }
    try:
        par_porte = max(int(limit) * 2, 20)
        borne = max(1, int(limit))
    except Exception:
        par_porte, borne = 20, 10

    merged: dict[str, dict] = {}
    echecs: list[str] = []
    for scope_kind, scope_id in scopes:
        try:
            page = search(
                session,
                query=query,
                scope_kind=scope_kind,
                scope_id=scope_id,
                allowed_ids=allowed_ids,
                types=types,
                status=status,
                limit=par_porte,
            )
        except Exception as exc:
            echecs.append(f"{scope_kind}:{type(exc).__name__}")
            continue
        if not page.get("ok"):
            echecs.append(f"{scope_kind}:{page.get('code', 'UNKNOWN')}")
            continue
        for result in page.get("results") or []:
            artifact_id = str(result.get("artifact_id") or "").strip()
            if not artifact_id or artifact_id in merged:
                continue  # première porte victorieuse : ordre des portées, donc déterministe
            merged[artifact_id] = dict(result)

    if echecs and not merged:
        raise RuntimeError("recherche multi-portée indisponible : " + ", ".join(echecs))
    results = sorted(merged.values(), key=lambda r: -float(r.get("score") or 0.0))
    fenetre = results[:borne]
    out: dict = {
        "ok": True,
        "query": query,
        "scope": "scopes",
        "scopes": [{"scope_kind": kind, "scope_id": sid} for kind, sid in scopes],
        "filters": {"types": types or None, "status": status or "any"},
        "results": fenetre,
        "searched_count": len(allowed_ids or set()),
        "returned_count": len(fenetre),
    }
    if echecs:
        out["partial"] = echecs
    return out
