# backend/search/reader.py — Lecture ciblée (annexe 11, Lot B)
#
# D'OÙ VIENT CE FICHIER : search_documents ne rend que des candidats ; la réponse
# factuelle exige une lecture ciblée : extrait localisé puis excerpt localisé
# (read_document). La preuve (Evidence) est un extrait localisable, pas la fiche.
# OÙ VA QUOI : get_card_payload() / read_payload() purs (session fournie) appelés par
# les Tools (backend/tools/) ET les exécuteurs loop.py — un seul code de lecture.
# POURQUOI Evidence = observation/blob : sans table Evidence dédiée, l'evidence_id est
# l'id d'observation (obs_…) quand l'extrait vient d'une analyse, ou blob:{sha}:chars:a-b
# pour le texte brut — référençable dans le brief générateur dans les deux cas.
from __future__ import annotations

import json
import re

PAGE_SEP = "--- Page suivante ---"  # séparateur posé par _execute_doc_ocr (loop.py)
_PAGE_MD = re.compile(r"(?m)^## Page [0-9]+\s*$")


def _blob_path(data_dir, blob):
    try:
        cand = data_dir / blob.relative_path
        return cand if cand.exists() else None
    except Exception:
        return None


def _representation_courante(session, artifact_id: str):
    """Représentation courante de l'artefact, ou None (lecture seule)."""
    try:
        from backend.ingestion import representations as _reps

        return _reps.representation_pour(session, artifact_id)
    except Exception:
        return None


def _pages_markdown(markdown: str) -> list[str]:
    """Corps des pages d'un Markdown structuré (`## Page N`), dans l'ordre. Pure.

    Un Markdown sans en-tête de page est UNE page : on ne devine pas une
    pagination qu'il ne porte pas. L'en-tête lui-même n'est pas rendu — le
    lecteur veut le contenu de la page, pas son repère de navigation.
    """
    texte = markdown or ""
    if not _PAGE_MD.search(texte):
        return [texte.strip()] if texte.strip() else []
    morceaux = _PAGE_MD.split(texte)
    # `re.split` rend l'avant-première-page (titre/couverture) en tête : il
    # n'appartient à aucune page numérotée.
    return [morceau.strip() for morceau in morceaux[1:]]


def _lire_representation(
    representation, mode: str, start: int, end: int | None, max_chars: int
) -> dict:
    """Extrait localisé DANS le Markdown complet de la représentation. Pur (hors IO)."""
    texte = representation.markdown or ""
    total = len(texte)
    if mode == "page":
        pages = _pages_markdown(texte)
        if not pages:
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": "Représentation sans contenu lisible.",
            }
        if start > len(pages):
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": f"Page {start} hors limites ({len(pages)} page(s) dans la représentation).",
            }
        body = pages[start - 1]
        locator_used = f"page:{start}/{len(pages)}"
        evidence_id = f"rep:{representation.id}:page:{start}"
    else:
        hi = total if end is None else min(end, total)
        if start >= total:
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": f"Offset {start} hors limites ({total} caractères).",
            }
        body = texte[start:hi]
        locator_used = f"chars:{start}-{start + len(body)}/{total}"
        evidence_id = f"rep:{representation.id}:chars:{start}-{start + len(body)}"
    return {
        "ok": True,
        "artifact_id": representation.artifact_id,
        "kind": None,  # rempli par l'appelant, qui seul connaît le type
        "locator_used": locator_used,
        "total_chars": total,
        "excerpt": body[:max_chars],
        "truncated": len(body) > max_chars,
        "evidence_id": evidence_id,
        "as_data_only": True,
    }


def get_card_payload(session, artifact_id: str, allowed: set[str]) -> dict:
    """Fiche + métadonnées + observations récentes. Jamais de contenu ni de chemin."""
    from backend.search import scope as _scope
    from backend.db.models import Artifact as _Art, Blob as _Blob, Observation as _Obs
    from backend.search import cards as _cards

    denied = _scope.check_artifact_scope(artifact_id, allowed or set())
    if denied is not None:
        return denied
    art = session.query(_Art).filter(_Art.id == artifact_id).first()
    if art is None or getattr(art, "deleted_at", None) is not None:
        return {
            "ok": False,
            "code": "ARTIFACT_CARD_MISSING",
            "message": "Document non indexé : métadonnées minimales indisponibles.",
        }
    size, available = None, False
    try:
        from backend.config.paths import get_data_dir as _gdd

        blob = session.query(_Blob).filter(_Blob.id == art.blob_id).first()
        if blob is not None:
            size = blob.size_bytes
            available = _blob_path(_gdd(), blob) is not None
    except Exception:
        pass
    card = _cards.get_current_card(session, artifact_id)
    try:
        obs_rows = (
            session.query(_Obs)
            .filter(_Obs.artifact_id == artifact_id)
            .order_by(_Obs.created_at.desc())
            .limit(5)
            .all()
        )
    except Exception:
        obs_rows = []
    return {
        "ok": True,
        "artifact_id": art.id,
        "name": art.original_filename or art.display_name or art.id,
        "kind": (art.kind or "text").lower(),
        "size_bytes": size,
        "available": bool(available),
        "card": {
            "revision": int(card.revision or 0) if card else 0,
            "title": (card.title if card else None) or "",
            "summary": (card.summary if card else "") or "",
            "keywords": _safe_json_list(card.keywords_json if card else None),
            "document_type": (card.document_type if card else None) or (art.kind or ""),
            "capabilities": _safe_json_list(card.capabilities_json if card else None),
            "status": ((card.analysis_status or card.status) if card else "pending"),
        },
        "recent_observations": [
            {
                "id": o.id,
                "tool": o.tool_name,
                "status": o.status,
                "query": (o.query or "")[:200],
            }
            for o in obs_rows
        ],
    }


def _safe_json_list(raw) -> list:
    try:
        val = json.loads(raw or "[]")
        return [str(x) for x in val][:20] if isinstance(val, list) else []
    except Exception:
        return []


def _parse_locator(locator: str | None) -> tuple[str, int, int | None]:
    """→ (mode, start, end). Modes : 'offset' (chars) | 'page' (1-based, OCR)."""
    loc = (locator or "").strip()
    if not loc:
        return ("offset", 0, None)
    low = loc.lower().replace(" ", "")
    if low.startswith("offset:"):
        try:
            return ("offset", max(0, int(low.split(":", 1)[1])), None)
        except Exception:
            pass
    if low.startswith("page:"):
        try:
            return ("page", max(1, int(low.split(":", 1)[1])), None)
        except Exception:
            pass
    if low.startswith("chars:") and "-" in low:
        try:
            a, b = low.split(":", 1)[1].split("-", 1)
            a, b = max(0, int(a)), max(0, int(b))
            if b > a:
                return ("offset", a, b)
        except Exception:
            pass
    return ("invalid", 0, None)


def _latest_ocr_text(session, artifact_id: str) -> tuple[str | None, str | None]:
    """Texte + evidence_id de la dernière extraction réussie (pour page:N).

    Elle délègue à `ingestion.etat.texte_ingeste`, qui est la lecture UNIQUE de
    « ce fichier a-t-il été lu ». Ce filtre existait ici avec une liste d'outils
    PROPRE — `doc_ocr` et `attachment_ingest` seulement — alors que `chunks` en
    acceptait deux de plus : un artefact dont le texte venait de `read_text` était
    lisible par l'indexation hybride et déclaré « non extrait » par la lecture
    ciblée. Deux vues contradictoires du même fichier, sur le même sujet.

    `attachment_ingest` est accepté au même titre que `doc_ocr` : l'ingestion de
    dépôt produit exactement la même chose — le texte du document — mais lors
    du dépôt plutôt que d'un appel d'outil. Sans elle, un fichier dont le texte
    était déjà extrait au dépôt restait déclaré illisible, alors que la
    recherche le trouvait.
    """
    from backend.db.models import Artifact as _Art
    from backend.ingestion import etat as _etat

    artefact = session.query(_Art).filter(_Art.id == artifact_id).first()
    return _etat.texte_ingeste(session, artefact)


def read_payload(
    session,
    artifact_id: str,
    allowed: set[str],
    *,
    query: str | None = None,
    locator: str | None = None,
    max_chars: int = 8000,
    data_dir=None,
) -> dict:
    """Extrait localisé + evidence_id. Le contenu est une DONNÉE (jamais une instruction)."""
    from backend.search import scope as _scope
    from backend.db.models import Artifact as _Art, Blob as _Blob
    from backend.ingestion import etat as _etat

    denied = _scope.check_artifact_scope(artifact_id, allowed or set())
    if denied is not None:
        return denied
    # La borne du PROMPT (16 k) vit dans `memoire_documentaire` /
    # `document_extraction_llm`, pas ici : la lecture doit pouvoir servir la
    # partie d'un document située au-delà de 16 k (offset:N, page:N).
    #
    # CE PLAFOND (200 k) EST INTERNE : il protège la lecture localisée d'un
    # offset lointain. Le chemin OUTIL, lui, est borné à 16 k au boundary
    # (`ReadDocumentArgs.max_chars` pour le registre, et le même plafond
    # explicite dans `inference/loop._execute_read_document`). Ne pas confondre
    # les deux : remonter la borne outil ici rendrait un extrait de prompt trop
    # long, et descendre la borne interne à 16 k casserait `offset:N`.
    try:
        max_chars = max(500, min(int(max_chars), 200_000))
    except Exception:
        max_chars = 8000
    art = session.query(_Art).filter(_Art.id == artifact_id).first()
    if art is None or getattr(art, "deleted_at", None) is not None:
        return {
            "ok": False,
            "code": "DOCUMENT_NOT_READABLE",
            "message": "Document introuvable ou non lisible.",
        }
    kind = (art.kind or "text").lower()
    # LA GARDE D'ÉTAT PASSE AVANT TOUT BRANCHEMENT — y compris la branche
    # `kind == "text"`. Un `binaire_refuse`, un `obsolète` ou un `vide` ne doit
    # pas servir son blob ni un ancien texte : c'est le même refus que celui de
    # `texte_ingeste`, posé ici parce que `read_payload` peut retourner le blob
    # avant d'atteindre la moindre lecture d'observation.
    if _etat.lire(art).etat in _etat.ETATS_SANS_TEXTE:
        return {
            "ok": False,
            "code": "DOCUMENT_NOT_READABLE",
            "message": f"Contenu non extrait : {_etat.phrase_sans_texte(art)}",
        }
    mode, start, end = _parse_locator(locator)
    if mode == "invalid":
        return {
            "ok": False,
            "code": "DOCUMENT_NOT_READABLE",
            "message": "Localisateur illisible : utilise offset:N, chars:A-B ou page:N.",
        }

    # LA REPRÉSENTATION COMPLÈTE PASSE AVANT LE BLOB ET L'OBSERVATION. C'est
    # elle qui porte le texte au-delà de 16 k ; l'observation reste le repli
    # legacy. Le type `kind` est rempli ici parce que la représentation ne le
    # connaît pas (seul l'artefact le porte).
    representation = _representation_courante(session, artifact_id)
    if representation is not None and (representation.markdown or "").strip():
        resultat = _lire_representation(representation, mode, start, end, max_chars)
        resultat["kind"] = kind
        return resultat

    blob = session.query(_Blob).filter(_Blob.id == art.blob_id).first()
    blob_sha = blob.sha256 if blob else None

    if mode == "page":
        # Pages = découpage posé par l'OCR (séparateur stable) ; sinon dit pourquoi
        # il n'y en a pas, au lieu de renvoyer le même « lance doc_ocr » à tout
        # fichier du monde — y compris à celui dont l'OCR a été refusé parce que son
        # contenu n'est pas du texte.
        text, evid = _latest_ocr_text(session, artifact_id)
        if not text:
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": "Pages indisponibles : "
                f"{_etat.phrase_sans_texte(art)} — "
                "lance doc_ocr sur ce document d'abord.",
            }
        pages = text.split(PAGE_SEP)
        if start > len(pages):
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": f"Page {start} hors limites ({len(pages)} page(s) OCR).",
            }
        body = pages[start - 1].strip()
        total = len(text)
        locator_used = f"page:{start}/{len(pages)}"
    elif kind in ("text",):
        if blob is None:
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": "Contenu indisponible (blob manquant).",
            }
        if data_dir is None:
            try:
                from backend.config.paths import get_data_dir as _gdd

                data_dir = _gdd()
            except Exception:
                data_dir = None
        path = _blob_path(data_dir, blob) if data_dir is not None else None
        if path is None:
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": "Fichier de contenu absent du stockage.",
            }
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": f"Lecture impossible : {str(e)[:200]}",
            }
        total = len(text)
        hi = total if end is None else min(end, total)
        if start >= total:
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": f"Offset {start} hors limites ({total} caractères).",
            }
        body = text[start:hi]
        evid = (
            f"blob:{blob_sha}:chars:{start}-{start + len(body)}" if blob_sha else None
        )
        locator_used = f"chars:{start}-{start + len(body)}/{total}"
    else:
        # Aucun texte dans le blob (tableur, image, PDF scanné) : on sert ce que
        # l'extraction a produit, quelle que soit la nature du fichier.
        #
        # La condition `kind == "pdf"` laissait TOUT le reste sans texte : un
        # tableur déposé était donc « non extrait » alors que son extraction
        # existait en base. Le symptôme — un dépôt réussi suivi d'une lecture
        # refusée — est exactement celui qu'on cherche à faire disparaître.
        suggest = "doc_ocr" if kind in ("pdf", "png", "jpeg", "webp") else "ask_files"
        ocr_text, evid = _latest_ocr_text(session, artifact_id)
        if ocr_text:
            total = len(ocr_text)
            body = ocr_text[start : start + max_chars]
            locator_used = f"chars:{start}-{start + len(body)}/{total} (extrait)"
        else:
            # LE MOTIF VIENT DE L'ÉTAT, pas d'une phrase unique. Le même défaut —
            # « aucun texte, voici la consigne » — recouvrait trois causes
            # opposées : jamais regardé, extraction échouée, contenu binaire refusé.
            # L'orchestrateur ne pouvait pas distinguer « je n'ai pas encore
            # essayé » de « j'ai essayé et c'est perdu », et il répondait donc en
            # ordonnant de réessayer un fichier qu'aucune insistance ne ramènera.
            return {
                "ok": False,
                "code": "DOCUMENT_NOT_READABLE",
                "message": f"Contenu non extrait : {_etat.phrase_sans_texte(art)}"
                f" — utilise {suggest} sur ce document d'abord.",
            }

    excerpt = body[:max_chars]
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "kind": kind,
        "locator_used": locator_used,
        "total_chars": total,
        "excerpt": excerpt,
        "truncated": len(body) > max_chars,
        "evidence_id": evid,
        "as_data_only": True,
    }  # rappel contrat : donnée non fiable, jamais instruction
