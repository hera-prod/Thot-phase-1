"""Recherche web — couche 5 : le cache indexé sur la conversation.

C'est ici que se tient la fonctionnalité distinctive du projet : **dans une
conversation de vingt échanges, on ne paie pas vingt fois la même question**. Une
requête déjà posée dans cette conversation et encore fraîche rend son contenu,
sans requête moteur et sans téléchargement.

Deux mots portent tout le contrat, et ils ne sont pas synonymes :

* **Rétention** = la durée de vie de la conversation. Un snapshot survit à tous
  les tours ; la conversation finie, `purge_convocation` le retire.
* **Fraîcheur** = une durée **réévaluée à chaque accès**, par `freshness_s`, dans
  l'ordre de RFC 9111 §4.2.2 : `Cache-Control: max-age`, sinon 10 % de l'écart
  `Date`−`Last-Modified`, sinon 1 h (`web_page_ttl_s`).

Une rétention illimitée n'autorise donc jamais à présenter un fait volatil ancien
comme actuel : c'est `fetched_at` qui date, jamais la seule présence d'une ligne.

**Identité de contenu, portée de réutilisation.** `public_id` dérive de l'URL ET
du hash du contenu (C16 : « un hash d'URL ne remplace pas le hash du contenu »).
La même adresse relue et inchangée retombe donc sur la MÊME ligne — c'est une
revalidation, pas une duplication — et un contenu nouveau en crée une nouvelle,
sans réécrire la précédente. `conv_id` et `scope_*` disent pour QUI cette ligne
est réutilisable : le hash n'autorise rien, la portée décide (C10).

**Silence — mais seulement ici.** Aucune fonction de ce module ne lève, et c'est
un choix LOCAL, pas une convention du dépôt. Un cache est une économie, jamais
une condition d'accès : une base fermée ou en erreur rend `None`/`0`/`""`/`{}` et
journalise le type de l'erreur en une ligne. Une recherche qui échouerait parce
que la base est indisponible serait un échec bien pire que l'absence d'économie.

Ce contrat n'est PAS celui de `observations.record_tool_observation`, qu'on cite
souvent à côté et dont le « jamais bloquant » ne vaut plus : sa PERSISTANCE lève
`ObservationNonÉcrite`, et seules ses ENRICHISSEMENTS — fiche, réindexation,
fragments — restent au mieux. Source de vérité : l'en-tête et la docstring de
`backend/search/observations.py`. La différence n'est pas une contradiction, elle
tient à ce que chaque module protège : un cache perdu se re-télécharge, une
observation perdue laisse un dépôt « réussi » sans fiche, sans fragment et sans
ligne d'index. Un contrat se déclare donc là où il s'applique, jamais par
héritage d'un voisin.
"""
from __future__ import annotations

import email.utils
import hashlib
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.config.defaults import TOOL_LIMITS
from backend.db.models import Message, ToolCall, WebSnapshot
from backend.services.web.types import WebPage, WebQuery

log = logging.getLogger(__name__)

# La fraîcheur par défaut, lue au point unique des seuils : `web_page_ttl_s`.
DEFAULT_FRESHNESS_S = float(TOOL_LIMITS["web_page_ttl_s"])
# RFC 9111 §4.2.2 : en l'absence d'expiration explicite, 10 % de l'intervalle
# entre `Last-Modified` et `Date`.
HEURISTIC_RATIO = 0.10

SCOPE_CONVERSATION = "conversation"

# Journal des requêtes moteur : sans classe ORM (voir migration 2), une seule
# table et un `INSERT OR IGNORE` suffisent à rendre un doublon sans effet.
_QUEUES_TABLE = "web_queries"
# Une citation rendue porte le `public_id` ; c'est ce repère qui décide si une
# purge a le droit de partir (T-W12 : « sources référencées conservées »).
_CITATION_RE = re.compile(r"web_[0-9a-f]{12}")

T = TypeVar("T")


# --------------------------------------------------------------------------
# Identité publique
# --------------------------------------------------------------------------
def public_id_for(url: str, content_sha256: str) -> str:
    """`web_` + sha256(url \\n hash du contenu)[:12] — déterministe, jamais un hash d'URL.

    La même URL avec le MÊME contenu rend le MÊME identifiant ; la même URL avec
    un contenu différent en rend un nouveau. C'est la règle de C16 : « si réponse
    inchangée, conserver le contenu et dater la vérification ; si contenu changé,
    créer une nouvelle version sans réécrire les preuves anciennes ».

    Le hash du contenu n'est pas un détail : une adresse qui change deux fois
    donnerait deux versions de la même page, et un identifiant dérivé de la seule
    adresse les confondrait. « Un hash d'URL ne remplace pas le hash du contenu. »
    """
    return "web_" + hashlib.sha256(f"{url}\n{content_sha256}".encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Fraîcheur — RFC 9111 §4.2.2, trois branches, dans cet ordre
# --------------------------------------------------------------------------
def freshness_s(page: Mapping[str, object]) -> float:
    """Durée de vie de la fraîcheur, en secondes, pour l'instant présent.

    L'ordre est celui de la norme : `Cache-Control: max-age` si elle est
    exploitable, sinon 10 % de (`Date` − `Last-Modified`), sinon 1 h. Un `max-age`
    illisible n'est pas une fraîcheur de 0 : c'est l'absence d'indication
    explicite, donc la branche suivante.

    Les clés lues sont `cache_control`, `last_modified`, et `date` (avec
    `fetched_at` en repli). Elles sont converties en instants absolus, donc un
    en-tête GMT et un horodatage naïf ne font jamais levier l'un sur l'autre.
    """
    explicite = _max_age_s(page.get("cache_control"))
    if explicite is not None:
        return explicite
    heuristique = _heuristique_s(page.get("last_modified"), page.get("date") or page.get("fetched_at"))
    if heuristique is not None:
        return heuristique
    return DEFAULT_FRESHNESS_S


def _max_age_s(cache_control: object) -> float | None:
    """`max-age` en secondes, ou `None` si l'en-tête ne le dit pas.

    Une valeur négative ou nulle rend le snapshot périmé à l'instant même, ce qui
    est la lecture exacte de la norme — pas une erreur à corriger.
    """
    if not isinstance(cache_control, str):
        return None
    for directive in cache_control.split(","):
        nom, _, valeur = directive.partition("=")
        if nom.strip().lower() != "max-age":
            continue
        try:
            return max(0.0, float(int(valeur.strip().strip('"'))))
        except ValueError:
            return None
    return None


def _heuristique_s(last_modified: object, date: object) -> float | None:
    """10 % de l'écart entre la dernière modification et la date de la réponse."""
    modifie = _instant(last_modified)
    reponse = _instant(date)
    if modifie is None or reponse is None:
        return None
    ecart = reponse - modifie
    if ecart <= 0:
        # Horloges incohérentes (ou Last-Modified postérieur à Date) : aucune
        # estimation ne vaut mieux que la valeur par défaut.
        return None
    return ecart * HEURISTIC_RATIO


def _instant(value: object) -> float | None:
    """Un instant en secondes POSIX, que la valeur soit un `datetime` ou un en-tête HTTP.

    Passer par le nombre de secondes est ce qui rend la soustraction toujours
    possible : un `Last-Modified` GMT (conscient) et un `fetched_at` local
    (naïf) ne sont pas soustractibles en `datetime`, alors que leurs horodatages
    le sont. L'erreur de fuseau qui subsiste n'entre dans l'estimation qu'à 10 %.
    """
    if isinstance(value, datetime):
        return value.timestamp()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return email.utils.parsedate_to_datetime(value.strip()).timestamp()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Session : courte, et jamais bloquante
# --------------------------------------------------------------------------
def _avec_session(action: Callable[[Session], T], repli: T) -> T:
    """Exécute `action` sur une session courte et rend `repli` si quoi que ce soit échoue.

    Le retour n'est garanti qu'`commit` passé : écrire une preuve et l'annoncer
    avant l'écriture effective serait le mensonge que ce cache refuse par
    ailleurs. `SessionLocal` est importé à l'appel, comme dans
    `observations`, pour rester branchable sur une base temporaire.
    """
    db: Session | None = None
    try:
        from backend.db.database import SessionLocal  # import tardif : la base se branche tard

        db = SessionLocal()
        resultat = action(db)
        db.commit()
        return resultat
    except Exception as exc:  # noqa: BLE001 — une panne de cache ne remonte jamais
        log.warning("cache web indisponible (%s) : repli silencieux", type(exc).__name__)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return repli
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------
def find_reusable(conv_id: str, query: WebQuery, *, now: datetime) -> WebPage | None:
    """Le contenu déjà obtenu pour CE couple (conversation, requête), s'il est encore frais.

    C'est la fonctionnalité : rendre cette page, c'est ne rien demander au monde.
    Le filtre porte sur les deux membres du couple — une autre conversation, ou
    une autre question, ne profitent pas de ce snapshot (C10 : la portée décide).
    Le rendu est le plus récent des états encore frais ; les autres pages d'une
    même requête se retrouvent une par une par `find_by_url`.
    """
    if not conv_id:
        return None
    return _avec_session(lambda db: _recherche_fraiche(db, conv_id, query.cache_key(), now), None)


def find_by_url(conv_id: str, url: str, *, now: datetime) -> WebPage | None:
    """L'état connu de CETTE page dans la conversation — le point d'entrée d'une revalidation.

    « La source que tu as citée dit le contraire » : pour répondre, il faut d'abord
    ce que la source disait. Cette fonction ne filtre donc **pas** sur la fraîcheur :
    un état ancien est précisément ce qu'on vient réparer. `fetched_at` le date et
    l'appelant décide s'il faut le revalider ; les états postérieurs à `now` sont
    ignorés, parce qu'un snapshot ne peut pas venir du futur.
    """
    if not conv_id or not url:
        return None
    return _avec_session(lambda db: _etat_connu(db, conv_id, url, now), None)


def find_by_id(conv_id: str, public_id: str, *, now: datetime | None = None) -> WebPage | None:
    """L'état le plus récent d'une preuve web, par identifiant PUBLIC.

    C'est le chemin qu'emprunte la résolution d'un `evidence_id` : le
    validateur de brief doit pouvoir répondre « cet extrait existe-t-il, et
    où ? », donc il lui faut la page, pas seulement une ligne. Le filtre de
    portée passe par `conv_id` comme partout ailleurs : un `web_…` connu d'une
    autre conversation n'est pas une preuve de celle-ci.
    """
    if not conv_id or not public_id:
        return None
    instant = now or datetime.now()

    def _action(db: Session) -> WebPage | None:
        ligne = (
            _snapshots(db, conv_id)
            .filter(WebSnapshot.public_id == public_id, WebSnapshot.fetched_at <= instant)
            .order_by(WebSnapshot.fetched_at.desc())
            .first()
        )
        return None if ligne is None else _to_page(ligne)

    return _avec_session(_action, None)



def _recherche_fraiche(db: Session, conv_id: str, query_key: str, now: datetime) -> WebPage | None:
    lignes = _snapshots(db, conv_id).filter(WebSnapshot.query_key == query_key).all()
    for ligne in sorted(lignes, key=lambda row: row.fetched_at, reverse=True):
        if (now - ligne.fetched_at).total_seconds() <= freshness_s(_indices_de_fraicheur(ligne)):
            return _to_page(ligne)
    return None


def _etat_connu(db: Session, conv_id: str, url: str, now: datetime) -> WebPage | None:
    ligne = (
        _snapshots(db, conv_id)
        .filter(WebSnapshot.requested_url == url, WebSnapshot.fetched_at <= now)
        .order_by(WebSnapshot.fetched_at.desc())
        .first()
    )
    return None if ligne is None else _to_page(ligne)


def _snapshots(db: Session, conv_id: str):
    """Le périmètre de conversation : la portée décide, jamais le hash (C10)."""
    return db.query(WebSnapshot).filter(WebSnapshot.conv_id == conv_id)


def _indices_de_fraicheur(ligne: WebSnapshot) -> dict[str, object]:
    """Ce que `freshness_s` peut lire sur une ligne : validateurs et horloge de lecture."""
    return {
        "cache_control": ligne.cache_control,
        "last_modified": ligne.last_modified,
        "fetched_at": ligne.fetched_at,
    }


def _to_page(ligne: WebSnapshot) -> WebPage:
    """La ligne rendue en `WebPage` — la reconstruction sort de la couche SQL.

    Aucune ligne SQLAlchemy ne franchit cette frontière : l'appelant reçoit le
    contrat de `types.py`, donc rien du schéma ne peut se glisser dans le rendu.
    """
    return WebPage(
        url=ligne.requested_url or "",
        final_url=ligne.final_url or ligne.requested_url or "",
        status=int(ligne.status or 0),
        content_type=ligne.content_type or "",
        content_sha256=ligne.content_sha256 or "",
        text=ligne.text or "",
        extractor_version=ligne.extractor_version or "",
        fetched_at=ligne.fetched_at,
        published_at=None if ligne.published_at is None else ligne.published_at.isoformat(),
        etag=ligne.etag,
        last_modified=ligne.last_modified,
        title=ligne.title or "",
        source_type=ligne.source_type or "general",
        proposed_by=ligne.proposed_by or "",
    )


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------
def remember(
    conv_id: str,
    page: WebPage,
    *,
    source_type: str,
    query_key: str,
    cache_control: str | None = None,
) -> str:
    """Persiste l'état de cette page et rend son `public_id` — `""` si rien n'a été écrit.

    Contenu inchangé : c'est une **revalidation**, donc la ligne est datée à
    nouveau et `reused_count` monte d'un cran — jamais une seconde copie. Contenu
    changé : une nouvelle ligne apparaît, et l'ancienne reste intacte avec sa date
    (C16 : « une URL identique peut correspondre à plusieurs états temporels »).

    `cache_control` est transmis à part parce que `WebPage` — le contrat de la
    couche 0, qui ne se modifie pas — ne porte pas l'en-tête brut ; c'est
    l'appelant qui a la réponse HTTP sous la main. Sans lui, la fraîcheur retombe
    sur la branche heuristique puis sur 1 h.
    """
    if not conv_id or not page.url or not page.content_sha256 or not query_key:
        return ""
    return _avec_session(
        lambda db: _souviens(db, conv_id, page, source_type, query_key, cache_control),
        "",
    )


def _souviens(
    db: Session,
    conv_id: str,
    page: WebPage,
    source_type: str,
    query_key: str,
    cache_control: str | None,
) -> str:
    public_id = public_id_for(page.url, page.content_sha256)
    ligne = db.query(WebSnapshot).filter(WebSnapshot.public_id == public_id).first()
    if ligne is None:
        db.add(WebSnapshot(
            id=f"web_snap_{uuid.uuid4().hex}",
            public_id=public_id,
            conv_id=conv_id,
            requested_url=page.url,
            final_url=page.final_url or page.url,
            # Écrit explicitement, jamais laissé au défaut du serveur : c'est la
            # seule façon que fraîcheur et écriture parlent la même horloge.
            fetched_at=page.fetched_at,
            published_at=_date_de_publication(page.published_at),
            status=page.status,
            etag=page.etag,
            last_modified=page.last_modified,
            cache_control=cache_control,
            content_type=page.content_type,
            title=page.title,
            content_sha256=page.content_sha256,
            text=page.text,
            extractor_version=page.extractor_version,
            scope_kind=SCOPE_CONVERSATION,
            scope_id=conv_id,
            # La requête qui a produit CE contenu est conservée telle quelle : la
            # changer effacerait la provenance d'un snapshot déjà cité.
            query_key=query_key,
            source_type=source_type or page.source_type,
            proposed_by=page.proposed_by,
            reused_count=0,
        ))
        return public_id
    # Même contenu : on date la vérification, on garde le texte, on compte.
    ligne.fetched_at = page.fetched_at
    ligne.reused_count = int(ligne.reused_count or 0) + 1
    ligne.last_modified = page.last_modified or ligne.last_modified
    ligne.etag = page.etag or ligne.etag
    return public_id


def _date_de_publication(valeur: str | None) -> datetime | None:
    """La date de publication ISO, normalisée en UTC naïf, ou `None` — jamais une date inventée.

    « Publication si connue » : une en-tête illisible rend l'absence, que le
    rendu affichera comme telle au lieu d'avancer une date de visite.
    """
    if not valeur:
        return None
    try:
        moment = datetime.fromisoformat(valeur)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


# --------------------------------------------------------------------------
# Journal des requêtes moteur (plafond `web_conv_queries_max`)
# --------------------------------------------------------------------------
def note_query(conv_id: str, query_key: str) -> None:
    """Journalise une requête moteur — UN ENREGISTREMENT PAR (conv_id, query_key).

    Le doublon ne double donc pas le compte : répéter la même question dans la
    même conversation ne coûte pas deux fois le budget (C15 : « réduire et
    journaliser sobrement les requêtes »). Rien n'est journalisé hors de la clé :
    le texte de la requête ne voyage pas dans ce journal.
    """
    if not conv_id or not query_key:
        return
    _avec_session(lambda db: _inscrit(db, conv_id, query_key), None)


def _inscrit(db: Session, conv_id: str, query_key: str) -> None:
    db.execute(
        text(
            f"INSERT OR IGNORE INTO {_QUEUES_TABLE} (id, conv_id, query_key) "
            "VALUES (:id, :conv_id, :query_key)"
        ),
        {"id": f"wq_{uuid.uuid4().hex}", "conv_id": conv_id, "query_key": query_key},
    )


def count_queries(conv_id: str) -> int:
    """Combien de requêtes MOTEUR distinctes cette conversation a déjà lancées."""
    if not conv_id:
        return 0
    return _avec_session(lambda db: _requetes(db, conv_id), 0)


def _requetes(db: Session, conv_id: str) -> int:
    return int(db.execute(
        text(f"SELECT COUNT(*) FROM {_QUEUES_TABLE} WHERE conv_id = :conv_id"),
        {"conv_id": conv_id},
    ).scalar_one())


# --------------------------------------------------------------------------
# Compteurs lisibles
# --------------------------------------------------------------------------
def stat(conv_id: str) -> dict:
    """Trois compteurs lisibles par l'utilisateur comme par le modèle.

    `queries` (requêtes moteur), `snapshots` (états conservés), `reused`
    (ré-exploitations). Ce sont les compteurs de politesse de C15 : ce qui a
    été demandé, ce qui a été gardé, ce qui a été évité.
    """
    if not conv_id:
        return {}
    return _avec_session(lambda db: _compteurs(db, conv_id), {})


def _compteurs(db: Session, conv_id: str) -> dict:
    lignes = _snapshots(db, conv_id).all()
    return {
        "queries": _requetes(db, conv_id),
        "snapshots": len(lignes),
        "reused": sum(int(ligne.reused_count or 0) for ligne in lignes),
    }


# --------------------------------------------------------------------------
# Purge (T-W12)
# --------------------------------------------------------------------------
def purge_convocation(conv_id: str) -> int:
    """Retire le cache de cette conversation et rend le nombre de snapshots supprimés.

    Deux bornes, et deux seulement : une autre conversation n'est jamais touchée,
    et un snapshot **encore cité** par une réponse déjà rendue est conservé — le
    résultat du rendu s'appuie dessus, et T-W12 demande « sources référencées
    conservées selon politique ou absence signalée explicitement ». La purge dit
    donc ce qu'elle a supprimé en rendant un compte, jamais un « tout effacé ».
    Le journal des requêtes part avec les snapshots : il ne vaut que par eux.
    """
    if not conv_id:
        return 0
    return _avec_session(lambda db: _purge(db, conv_id), 0)


def _purge(db: Session, conv_id: str) -> int:
    cites = _public_ids_cites(db, conv_id)
    supprimes = 0
    for ligne in _snapshots(db, conv_id).all():
        if ligne.public_id in cites:
            continue
        db.delete(ligne)
        supprimes += 1
    db.execute(text(f"DELETE FROM {_QUEUES_TABLE} WHERE conv_id = :conv_id"), {"conv_id": conv_id})
    return supprimes


def _public_ids_cites(db: Session, conv_id: str) -> frozenset[str]:
    """Les `web_…` qu'une réponse déjà rendue de cette conversation mentionne.

    On regarde ce qui a été **montré** : le plan et le brief du message, et le
    résultat d'appel d'outil. Un snapshot que rien ne cite n'est plus la preuve
    de rien et part ; un snapshot cité reste, daté, jusqu'à ce que son contenu
    soit remplacé ou que la conversation soit supprimée.
    """
    cites: set[str] = set()
    for message in db.query(Message).filter(Message.conv_id == conv_id).all():
        for champ in (message.plan_json, message.brief_json):
            cites.update(_CITATION_RE.findall(champ or ""))
    appels = (
        db.query(ToolCall)
        .join(Message, ToolCall.msg_id == Message.id)
        .filter(Message.conv_id == conv_id)
        .all()
    )
    for appel in appels:
        cites.update(_CITATION_RE.findall(appel.result or ""))
    return frozenset(cites)
