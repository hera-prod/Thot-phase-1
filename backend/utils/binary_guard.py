"""backend/utils/binary_guard.py — un seul juge pour « ceci est-il du texte ? ».

D'où vient ce fichier. La question existait déjà, mais en un seul endroit et
sous une forme qui ne servait qu'au dépôt : `sniff_mime`
(`backend/ipc/attachments.py`) refuse un `.csv` dont les octets sont du binaire.
Le dépôt est la PORTE — or le même arbitrage se pose à deux endroits où
`sniff_mime` n'a plus son mot à dire :

  - l'ingestion des tableurs (`backend/ingestion/sheets.py`), qui décode un
    fichier pour en tirer du texte alors que le dépôt l'a DÉJÀ accepté ;
  - l'indexation (`backend/search/fts.py`), qui reçoit un extrait DÉJÀ décodé et
    n'a plus que des caractères pour se prononcer.

Trois règles que ce juge respecte, et qui expliquent sa forme :

  1. Il ne devine pas. Une signature franche tranche ; à défaut, c'est une
     PROPORTION qui tranche — et le motif rendu dit laquelle des deux a
     tranché. Un garde-fou qui refuse sur un doute ferait ignorer la moitié des
     fichiers, et l'utilisateur perdrait plus qu'il ne gagnerait.
  2. Aucun refus sans motif lisible. Refuser sans dire pourquoi reproduit
     exactement le défaut que ce module existe pour supprimer : un fichier classé
     « ingéré » sans que rien n'explique son cas.
  3. Le même contenu ne doit jamais être jugé différemment selon le point
     d'entrée. D'où les DEUX entrées : `raison_binaire` pour des octets (le
     fichier est encore intact) et `raison_binaire_texte` pour une chaîne déjà
     décodée (le dommage est fait, mais il reste à constater). Elles partagent
     le même jugement ; seul le support change.

LE SEUIL, et pourquoi 0.10. Il est mesuré, pas choisi :

  - du texte réel (CSV, JSON, code, prose accentuée) plafonne à 0 % d'octets de
    contrôle — les séparateurs tabulation/LF/CR sont exclus du décompte, donc un
    texte bien formé ne « paie » jamais ;
  - du binaire réel jamais signé (un `.ods` du corpus : 13,6 % ; des PNG : de
    8,2 % à 12,4 %) commence à 8 %.

10 % est donc dans le vide au milieu d'un intervalle de plus d'un facteur huit
de chaque côté : assez bas pour attraper du binaire sans signature, assez haut
pour ne jamais attraper du texte. Une signature couvre le cas le plus sûr
(`.ods`, `.xlsx`, `.png`…), la proportion n'est que le filet de sécurité.

`attachments.py` conserve son propre seuil, plus tolérant (0.30) : il juge un
`.csv` DÉCLARÉ, à la porte, où un refus à tort coûte à l'utilisateur son dépôt
entier. Ici on juge du texte qui a déjà traversé un décodeur, où un faux négatif
n'est pas un refus mais une corruption silencieuse indexée. Les deux seuils ne
sont pas en contradiction : le dépôt est la porte ouverte, ce juge la porte
intérieure.

Ce module ne dépend de rien : ni SQLAlchemy, ni configuration, ni disque. Il
peut donc être appelé depuis l'entrée du dépôt, depuis une extraction, ou depuis
un script de réparation, sans que l'ordre d'import change quoi que ce soit.
"""

from __future__ import annotations

# Signatures franches : un fichier qui commence par l'une d'elles EST un
# binaire, quel que soit le reste de son contenu, et aucun doute n'est permis.
#   ZIP  : le conteneur d'un .xlsx, d'un .ods et d'un .docx — mais aussi de
#          n'importe quelle archive, ce qui est justement ce qu'il faut voir.
#   OLE2 : le conteneur d'un .xls (Excel 97-2003) et d'un .doc.
#   PDF / PNG / JPEG : un document ou une image, jamais du texte à indexer.
SIGNATURES_BINAIRES: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "d'archive ZIP"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "de conteneur OLE2 (.xls / .doc)"),
    (b"%PDF", "de document PDF"),
    (b"\x89PNG\r\n\x1a\n", "d'image PNG"),
    (b"\xff\xd8\xff", "d'image JPEG"),
)

ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

RATIO_BINAIRE = 0.10
# On ne juge que le début : la signature est au début, et lire un tableur
# entier pour décider qu'il est du binaire coûterait plus que la décision ne
# rapporte. Le reste du fichier ne ferait qu'affiner un doute déjà tranché.
TAILLE_ECHANTILLON = 8192


def _est_octet_de_controle(octet: int) -> bool:
    """Vrai pour un octet qui n'apparaît pas dans du texte.

    9 (tabulation), 10 (LF) et 13 (CR) sont EXCLUS : ce sont les séparateurs du
    texte, et les compter ferait crier un CSV bien formé. Les octets >= 0x80 ne
    sont pas comptés non plus, et c'est délibéré : c'est ainsi que l'UTF-8 d'un
    texte accentué — et le latin-1 d'un tableau de chiffres — traversent le
    jugement au lieu d'être rejetés.
    """
    return octet < 9 or 13 < octet < 32


def _pourcent_binaire(tete: bytes) -> float:
    controles = sum(1 for octet in tete if _est_octet_de_controle(octet))
    return controles / len(tete)


def raison_binaire(echantillon: bytes) -> str | None:
    """`None` si ces octets sont indexables comme texte, sinon POURQUOI ils ne le sont pas.

    Le motif est destiné à être lu par un humain : il est recopié tel quel dans
    l'observation, le statut de fiche et le rapport de réparation.
    """
    if not echantillon:
        return None
    for magic, nom in SIGNATURES_BINAIRES:
        if echantillon.startswith(magic):
            return f"signature {nom}"
    tete = echantillon[:TAILLE_ECHANTILLON]
    ratio = _pourcent_binaire(tete)
    if ratio > RATIO_BINAIRE:
        return f"{round(ratio * 100)} % d'octets de contrôle non textuels"
    return None


def _octets_de(texte: str) -> bytes:
    """Les octets qu'un texte représente, sans les déformer.

    `latin-1` d'abord, et ce n'est pas un détail : c'est le décodage que les
    lecteurs de tableurs employaient pour « rattraper » un fichier illisible, et
    il est RÉVERSIBLE sur 0x00-0xFF. Un ZIP passé à travers en ressort octet pour
    octet, signature comprise — ce qui rend le refus possible APRÈS le décodage,
    alors qu'il est déjà trop tard pour l'empêcher.
    """
    try:
        return texte.encode("latin-1")
    except UnicodeEncodeError:
        # Un caractère au-delà de U+00FF : l'encodage d'origine n'était pas
        # latin-1. UTF-8 est alors le seul ré-encodage réversible plausible.
        return texte.encode("utf-8", errors="replace")


def raison_binaire_texte(texte: str) -> str | None:
    """Le même jugement, sur une chaîne déjà décodée.

    Deux indices s'ajoutent au jugement des octets, et tous deux sont propres au
    texte — donc invisibles au lecteur qui ne manipule que des octets.

    Les caractères NON IMPRIMABLES : `U+00AD` (coupure de ligne d'un PDF), `U+200B`
    (espace fine collée par un collage), les `C1` du latin-1 (`U+0080`..`U+009F`)
    qu'un contenu compressé produit en série. Sur le `.ods` réellement concerné,
    ces caractères font 29 % de l'extrait ; sur du texte réel, ils font 0 %.

    Le caractère de remplacement `U+FFFD` : il n'apparaît dans un contenu lisible
    que si un octet a été REMPLACÉ, c'est-à-dire si le décodage a échoué sans
    bruit. Le seuil est bas (5 %) parce qu'un seul caractère de remplacement dans
    un texte correct — une adresse mail, un symbole — est parfaitement normal ;
    ce qui ne l'est pas, c'est la pluie.
    """
    if not texte:
        return None
    motif = raison_binaire(_octets_de(texte))
    if motif:
        return motif
    if len(texte) < 64:
        return None  # trop court pour être un échantillon honnête : on ne juge pas
    non_imprimables = sum(1 for c in texte if not (c.isspace() or c.isprintable()))
    ratio = non_imprimables / len(texte)
    if ratio > RATIO_BINAIRE:
        return f"{round(ratio * 100)} % de caractères non imprimables"
    remplacements = texte.count("�")
    if remplacements / len(texte) > 0.05:
        return (f"{round(100 * remplacements / len(texte))} % de caractères de remplacement "
                "U+FFFD : texte issu d'un décodage forcé")
    return None


__all__ = [
    "OLE2_MAGIC",
    "RATIO_BINAIRE",
    "SIGNATURES_BINAIRES",
    "TAILLE_ECHANTILLON",
    "ZIP_MAGIC",
    "raison_binaire",
    "raison_binaire_texte",
]
