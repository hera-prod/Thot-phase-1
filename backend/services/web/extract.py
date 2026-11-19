"""Recherche web — couche 3 : HTML → texte, sans JavaScript et sans dépendance.

Une page web n'est pas un document : c'est un programme qui n'a pas encore
tourne. Nous n'exécutons rien. Ce module rend le texte **déjà présent** dans la
réponse et renonce à tout le reste, et il le fait avec la seule bibliothèque
standard (`html.parser`) parce qu'une dépendance de plus ici est une
dépendance de plus dans le paquet PyInstaller.

Quatre partis pris, tous vérifiables par test :

1. **Ce qui n'est pas du texte destiné au lecteur disparaît.** `script`,
   `style`, `noscript`, `head`, `svg`, `math`, `template`, `iframe`, `object`,
   et la navigation (`nav`, `footer`, `aside`, `form`). Une phrase cachée dans
   un `<script>` n'est pas une affirmation de la page : c'est une chaîne de
   caractères dans un programme, et la lire lui donnerait le statut de preuve.
2. **Le contenu principal est préféré, sans scorer.** Si `<article>` ou
   `<main>` existe *et produit du texte*, on ne garde que lui. C'est une
   heuristique légère et assumée : noter chaque bloc comme le fait Unsloth
   (150 tests pour en choisir un seul) coûterait une dépendance, alors que le
   pire cas d'un `<main>` vide — retomber sur tout le document — est inoffensif,
   et que le pire cas d'une notation fausse serait de perdre le texte utile.
3. **Un corps qui n'est pas du HTML revient tel quel.** JSON, XML, texte brut :
   un document est renvoyé intact, à la troncature près, jamais réécrit. Une
   réponse mal déguidée reste donc lisible au lieu d'être déformée en charabia
   — la différence entre « je n'ai pas su le lire » et « j'ai produit un faux
   contenu ».
4. **La version de l'extracteur voyage avec le texte** (`EXTRACTOR_VERSION`) :
   sans elle, deux pages produites par deux versions de ce fichier seraient
   indiscernables dans le cache (C16).

Un seul risque assumé, et il est du Parser lui-même : une balise de la liste
d'exclusion laissée ouverte (`<nav>` sans `</nav>`) jette la fin du document. Le
compteur de profondeur est fait pour cela, et le pire cas — une page tronquée
après sa navigation — reste une page tronquée, pas une page inventée.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

EXTRACTOR_VERSION = "web-extract-1"

# Ce qui ne porte pas de texte destiné au lecteur. Liste courte et fermée :
# c'est un refus de traiter, pas une liste d'exceptions à inventorier.
_DROP_TAGS = frozenset({
    "script", "style", "noscript", "head", "svg", "math", "template",
    "iframe", "object", "nav", "footer", "aside", "form", "select", "button",
})

# Contenu principal déclaré par la page : s'il parle, il parle seul.
_MAIN_TAGS = frozenset({"article", "main"})
# Identifiants de conteneur de contenu que les gestionnaires de contenu
# utilisent réellement. Volontairement courts : un inventaire exhaustif serait
# une liste deTests qui ne prouve rien, alors que le volume de texte tranche.
# Classes qui annoncent « ceci est de l'interface, pas du contenu ». Le conteneur
# de contenu d'un wiki contient lui-même son portail de langues, ses boutons
# d'édition et sa barre d'outils : sans ce filtre, le début d'un article
# Wikipédia est une liste de 140 noms de langue, et la citation d'une page
# commence par du bruit. Liste courte et lisible, et non un inventaire : ce que
# la classe AFFIRME suffit, on ne devine pas le reste.
_CHROME_CLASSES = frozenset({
    "noprint", "printfooter", "mw-portlet", "portlet", "mw-editsection",
    "mw-jump-link", "mw-indicators", "catlinks", "vevent", "navigation-not-searchable",
    "mw-references-wrap", "sistersitebox", "shortdescription",
})
_CONTENT_IDS = frozenset({
    "content", "main-content", "maincontent", "mw-content-text",
    "bodycontent", "article-body", "content-body",
})

# Balises qui séparent deux blocs de texte. Un saut de ligne et non deux : le
# blanc supplémentaire se fabrique à la normalisation finale, une fois le
# texte assemblé, pour qu'une coupure de balise n'amplifie pas les lignes.
_BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "br", "caption", "dd", "div", "dl",
    "dt", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "td", "th", "tr", "ul",
})

# Balises sans fin : elles ouvrent un bloc et ne le ferment jamais.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# Un paragraphe légitime peut se répéter (une citation, un sommaire) ; un lien
# de navigation, lui, se répète des dizaines de fois. La déduplication ne
# s'applique donc qu'aux lignes courtes : au-delà, c'est un paragraphe, et un
# paragraphe répété est une information, pas du bruit.
_DEDUP_MAX_CHARS = 120
_DEDUP_KEEP = 2

_ZERO_WIDTH_RE = re.compile("[‌‍⁠﻿]")
_SPACE_RE = re.compile(r"\s+")
# Espaces HORIZONAUX seulement : un saut de ligne est une frontière de paragraphe,
# pas un blanc à écraser. Confondre les deux aplatit toute la page sur une ligne.
_INLINE_SPACE_RE = re.compile(r"[^\S\n]+")
_BLANK_RUNS_RE = re.compile(r"\n{3,}")
_MARKUP_RE = re.compile(r"<\s*(!|/?)[a-zA-Z]")
_XML_PROLOG_RE = re.compile(r"^\s*<\?xml\b", re.IGNORECASE)


@dataclass(frozen=True)
class Extraction:
    """Le texte rendu, et de quoi dire exactement ce qui a été lu.

    `chars_total` est la taille AVANT troncature : c'est lui qui permet de dire
    « page lue en entier » plutôt que « page tronquée ». `truncated` est le
    seul signal qui autorise cette différence.
    """
    text: str
    title: str
    truncated: bool
    chars_total: int
    extractor_version: str = EXTRACTOR_VERSION


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def extract(html: str, *, url: str, max_chars: int) -> Extraction:
    """Rend le texte lisible de `url`, ou son corps tel quel s'il n'est pas du HTML.

    `url` est le contexte du document, et il reste dans la signature parce
    qu'un extracteur qui ignore son origine ne peut rien dire d'un titre
    manquant. La troncature est toujours signalée, jamais silencieuse.
    """
    if max_chars <= 0:
        return Extraction("", "", truncated=True, chars_total=0)

    opaque = _opaque_body(html)
    if opaque is not None:
        body, structured = opaque
        if structured:
            # Un JSON ne se « normalise » pas : un espace insécable à
            # l'intérieur d'une chaîne est un caractère, pas un bruit de mise
            # en page. Le corps part donc intact, la coupe est le seul geste.
            truncated = len(body) > max_chars
            return Extraction(
                body[:max_chars].rstrip() if truncated else body,
                _json_title(body), truncated, len(body),
            )
        text, truncated, chars_total = _bounded(body, max_chars)
        return Extraction(text, "", truncated, chars_total)

    page = _ReadableText()
    page.feed(html)
    page.close()
    readable = page.readable()
    text, truncated, chars_total = _bounded(readable, max_chars)
    return Extraction(text, page.title(), truncated, chars_total)


def _bounded(text: str, max_chars: int) -> tuple[str, bool, int]:
    """Normalise puis coupe — toujours dans cet ordre, jamais l'inverse.

    Renvoie aussi la taille d'AVANT coupe : sans elle, `chars_total` ne
    distinguerait pas une page courte d'une page coupée à la même longueur.
    """
    clean = _readable_text(text)
    if len(clean) <= max_chars:
        return clean, False, len(clean)
    return clean[:max_chars].rstrip(), True, len(clean)


def _opaque_body(html: str) -> tuple[str, bool] | None:
    """`(corps, est_structuré)` si ce n'est pas du HTML, `None` si c'en est.

    Reconnaître la forme avant de l'interpréter : c'est ce qui empêche un JSON
    d'être « normalisé » en texte. Le XML est renvoyé brut pour la même
    raison — un flux RSS n'est pas un document à lire, c'est une donnée.
    `est_structuré` distingue ce qui a une syntaxe à ne pas toucher (JSON) de
    ce qui est seulement du texte à remettre en forme.
    """
    probe = html.lstrip()
    if not probe:
        return html, False
    if probe[:1] in "{[" and _is_json(probe):
        return html, True
    if _XML_PROLOG_RE.match(probe) or not _MARKUP_RE.search(probe):
        return html, False
    return None


def _is_json(probe: str) -> bool:
    try:
        json.loads(probe)
    except ValueError:
        return False
    return True


def _json_title(body: str) -> str:
    """Le champ `title` d'un JSON, quand il y en a un. Jamais une exception."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    titre = parsed.get("title")
    return titre.strip() if isinstance(titre, str) else ""


# --------------------------------------------------------------------------
# Le parseur
# --------------------------------------------------------------------------
class _ReadableText(HTMLParser):
    """Assemble le texte d'un document en gardant deux régions séparées.

    Deux régions et non une seule : le contenu principal déclaré (`<main>`,
    `<article>`) est écrit à part, parce que la décision « on ne garde que ça »
    ne peut être prise qu'à la fin — un `<article>` peut apparaître après mille
    caractères de navigation.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._body: list[str] = []
        self._main: list[str] = []
        # Régions DÉCLARÉES par le document (conteneur sémantique), empilées.
        # On ne garde pas la première venue : on garde celle qui parle le plus.
        # `<main>` contient souvent le chrome AVANT l'article — Wikipédia y loge
        # son portail de langues — donc « la première région » n'est pas « le
        # contenu », c'est seulement la première.
        self._stack: list[tuple[str, str, list[str]]] = []
        self._chrome_open: list[str] = []
        self._regions: list[tuple[str, str]] = []
        self._title_parts: list[str] = []
        self._og_title: str = ""
        self._title_depth = 0
        self._skip_depth = 0
        self._main_depth = 0
        self._open: list[str] = []

    # -- API ----------------------------------------------------------------
    def title(self) -> str:
        """`<title>` d'abord, `og:title` en repli — l'ordre du web réel."""
        joined = " ".join(part for part in self._title_parts if part.strip())
        title = _SPACE_RE.sub(" ", _ZERO_WIDTH_RE.sub("", joined)).strip()
        return title or self._og_title

    def readable(self) -> str:
        """Le texte retenu : la région la plus dense, sinon le document.

        « La plus dense » se mesure sur le texte lisible produit, jamais sur
        la taille du balisage : c'est le volume de MOTS qui dit qu'un conteneur
        porte l'article plutôt qu'un menu.
        """
        regions = [(nom, contenu) for nom, contenu in self._regions if contenu.strip()]
        if regions:
            contenu = max(regions, key=lambda item: len(_readable_text(item[1])))[1]
            return _dedupe_lines(_readable_text(contenu))
        chosen = self._main if any(part.strip() for part in self._main) else self._body
        return _dedupe_lines(_readable_text("".join(chosen)))

    # -- Entrées ------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "title":
            self._title_depth += 1
        elif name in _MAIN_TAGS:
            self._main_depth += 1
        if self._est_chrome(attrs):
            self._chrome_open.append(name)
            self._skip_depth += 1
        region = self._region_named(attrs)
        if region is not None and not self._stack:
            # Un seul niveau : un conteneur de contenu n'en contient pas un autre
            # qui le ferait mieux, et empiler rendrait la sortie ambiguë.
            self._stack.append((region, name, []))
        if name in _DROP_TAGS:
            self._skip_depth += 1
        if name not in _VOID_TAGS:
            self._open.append(name)
        if name == "meta":
            self._read_meta(attrs)
        if self._skip_depth or name not in _BLOCK_TAGS:
            return
        self._emit("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # `<br/>`, `<meta …/>` : une ouverture sans fermeture. On n'appelle donc
        # pas la fermeture, qui ferait disparaître un bloc ouvert par erreur.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _VOID_TAGS:
            return
        if name in self._open:
            while self._open and self._open.pop() != name:
                continue
        if name == "title" and self._title_depth:
            self._title_depth -= 1
        if name in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1
        if self._stack and self._stack[-1][1] == name:
            region, _tag, morceaux = self._stack.pop()
            self._regions.append((region, "".join(morceaux)))
        if self._chrome_open and self._chrome_open[-1] == name:
            self._chrome_open.pop()
            self._skip_depth -= 1
        if name in _DROP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if self._skip_depth or name not in _BLOCK_TAGS:
            return
        self._emit("\n")

    def handle_data(self, data: str) -> None:
        # Le titre passe avant tout : il vit dans `<head>`, qui est jeté.
        if self._title_depth:
            self._title_parts.append(data)
            return
        if self._skip_depth:
            return
        self._emit(data)

    # -- Interne ------------------------------------------------------------
    def _emit(self, fragment: str) -> None:
        if fragment == "\n":
            self._break()
            return
        for index, part in enumerate(_ZERO_WIDTH_RE.sub("", fragment).split("\n")):
            # Un saut de ligne dans les données est une frontière réelle — c'est
            # ainsi qu'un `<pre>` garde sa mise en forme — donc on le respecte au
            # lieu de le réduire à un espace comme n'importe quel blanc.
            if index:
                self._break()
            text = _INLINE_SPACE_RE.sub(" ", part)
            if not text.strip():
                # Un fragment fait uniquement de blancs ne sépare rien : on garde
                # au plus un espace, pour ne pas coller deux mots.
                if not self._already_separated():
                    self._current().append(" ")
                continue
            self._current().append(text)

    def _break(self) -> None:
        """Sépare deux blocs, jamais plus de deux fois de suite.

        Une fermeture suivie d'une ouverture est le cas ordinaire d'un
        paragraphe, et donne donc une vraie ligne vide ; deux ouvertures
        imbriquées (`<div><div>`) n'en donnent pas, sans quoi une page faite de
        conteneurs deviendrait un damier de blancs.
        """
        target = self._current()
        trailing = 0
        for part in reversed(target):
            if part != "\n":
                break
            trailing += 1
        if trailing < 2:
            target.append("\n")

    def _current(self) -> list[str]:
        if self._stack:
            return self._stack[-1][2]
        return self._main if self._main_depth else self._body

    @staticmethod
    def _est_chrome(attrs: list[tuple[str, str | None]]) -> bool:
        """La balise annonce-t-elle elle-même qu'elle est de l'interface ?

        Une classe suffit. On ne déduit rien du nom de la balise, et on ne
        cherche pas à deviner : un contenu qui se déclare comme interface ne
        fait pas partie de l'article, même placé au milieu de lui.
        """
        for key, value in attrs:
            if (key or "").lower() != "class":
                continue
            classes = (value or "").lower().split()
            if any(classe in _CHROME_CLASSES for classe in classes):
                return True
        return False

    @staticmethod
    def _region_named(attrs: list[tuple[str, str | None]]) -> str | None:
        """Le nom d'un conteneur de contenu, si la page en déclare un.

        On reconnaît les conventions que les gestionnaires de contenu
        adoptent réellement : `<main>`, `<article>`, `role="main"`, et les
        identifiants `content`, `main-content`, `mw-content-text` (MediaWiki),
        `bodyContent` (MediaWiki Vector). C'est une liste courte et lisible,
        pas un exhaustive : ce qu'on cherche est un indice, et le volume de
        texte tranche ensuite.
        """
        for key, value in attrs:
            low = (key or "").lower()
            val = (value or "").strip()
            if low == "role" and val.lower() == "main":
                return "role=main"
            if low == "id" and val.lower() in _CONTENT_IDS:
                return f"id={val.lower()}"
        return None

    def _already_separated(self) -> bool:
        current = self._current()
        return not current or current[-1].endswith((" ", "\n"))

    def _read_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        if self._og_title:
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        if values.get("property", "").lower() == "og:title":
            self._og_title = _SPACE_RE.sub(" ", values.get("content", "")).strip()


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
def _readable_text(assembled: str) -> str:
    """Texte brut lisible : blancs horizontaux unifiés, blocs séparés, bords coupés."""
    text = _ZERO_WIDTH_RE.sub("", assembled.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.strip() for line in _INLINE_SPACE_RE.sub(" ", text).split("\n")]
    return _BLANK_RUNS_RE.sub("\n\n", "\n".join(lines)).strip()


def _dedupe_lines(text: str) -> str:
    """Retire la ligne de navigation répétée, jamais le paragraphe légitime.

    Une ligne est jetée à sa troisième occurrence de forme normalisée, et
    seulement si elle est courte : au-delà de `_DEDUP_MAX_CHARS` c'est un
    paragraphe, et un paragraphe qui se répète est une information.
    """
    seen: dict[str, int] = {}
    kept: list[str] = []
    for line in text.split("\n"):
        if len(line) <= _DEDUP_MAX_CHARS:
            key = line.casefold()
            occurrences = seen.get(key, 0)
            seen[key] = occurrences + 1
            if occurrences >= _DEDUP_KEEP:
                continue
        kept.append(line)
    return _BLANK_RUNS_RE.sub("\n\n", "\n".join(kept)).strip()
