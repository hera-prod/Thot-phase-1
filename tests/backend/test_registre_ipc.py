"""Un canal IPC se déclare à QUATRE endroits. Ce test tient les quatre ensemble.

Le bug que ce fichier verrouille a été subi, pas anticipé. Un jour, deux canaux ont
été ajoutés côté Electron (`preload.cjs`, `commands.cjs`) et côté dispatch Python
(`main.py:_IPC_HANDLERS`) — puis le bouton a affiché « Unknown espace.depot_integrite »
à l'utilisateur. Redémarrer n'a rien changé : le quatrième endroit avait été oublié.

| # | Fichier                        | Rôle                                     |
|---|--------------------------------|-----------------------------------------|
| 1 | `electron/preload.cjs`         | verbe exposé au renderer                |
| 2 | `electron/commands.cjs`        | liste blanche + routage vers la méthode |
| 3 | `backend/inference/ipc.py`     | `PROTOCOL_METHODS`, porte d'entrée      |
| 4 | `backend/main.py`              | `_IPC_HANDLERS`, dispatch vers le handler|

Les tests existants (`tests/electron/commands.test.cjs`) vérifient 1↔2. Ils ne
peuvent pas voir 3 : Electron peut annoncer avec autant de assurance une méthode que
personne ne sait servir, et l'utilisateur reçoit un refus au lieu d'une page.

Pourquoi ce test est en Python et non en Node : les deux registres qui doivent
concorder sont des dictionnaires Python. Les lire tous les deux ici évite d'écrire un
analyseur de JavaScript fragile, et importer `backend.main` est peu coûteux (0,6 s).

Deux направions sont vérifiées, parce qu'elles échouent différemment :
  · une méthode ROUTÉE mais absente de `PROTOCOL_METHODS` → refus à l'entrée ;
  · une clé morte dans l'un des deux registres Python → un canal retiré à moitié,
    qui ne sert personne et ne se remarque pas.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.inference.ipc import PROTOCOL_METHODS
from backend.main import _IPC_HANDLERS

# Racine du dépôt : ce fichier vit dans tests/backend/, le code dans backend/.
RACINE = Path(__file__).resolve().parents[2]
COMMANDS = RACINE / "electron" / "commands.cjs"
PRELOAD = RACINE / "electron" / "preload.cjs"
MAIN = RACINE / "backend" / "main.py"

# Méthodes servies par une BRANCHE DÉDIÉE de `handle()`, pas par `_IPC_HANDLERS`.
# Elles sont dans `PROTOCOL_METHODS` sans y être : leur handler est du cas particulier
# (streaming, annulation, émission d'événement), pas une fonction à appeler. Les nommer
# ici, avec leur raison, évite que la garde les signale à tort — ou qu'on les ajoute à
# `_IPC_HANDLERS` pour faire taire le test, ce qui les ferait échouer à l'exécution.
BRANCHES_DEDIEES = {
    "chat.send": "stream de conversation, `handle()` émet au fil de l'eau",
    "cancel": "annulation d'un run en cours",
    "image.generate": "génération d'image, worker unique et file de priorité",
    "image.status": "état des images, émis sur événement et non sur réponse",
    "web.settings": "réglages web, écriture puis relecture confirmée",
    "health": "sonde de santé, répond explicitement `not_implemented_via_sidecar`",
    "resync": "resynchronisation, répond explicitement `not_implemented_via_sidecar`",
}


def _routage_electron() -> dict[str, str]:
    """Lit `SIDECAR_METHODS` dans `electron/commands.cjs`.

    Le fichier est lu comme du texte, pas exécuté : `require()` tirerait tout Electron
    dans le process de test. Le motif est donc strict, et son échec est une ÉCHEC DE
    TEST — jamais un résultat vide. Un test qui passe en n'ayant rien lu est pire
    qu'un test absent : il donne une assurance fausse.
    """
    source = COMMANDS.read_text(encoding="utf-8")
    bloc = re.search(
        r"SIDECAR_METHODS\s*=\s*Object\.freeze\(\{(?P<corps>.*?)\}\s*\)", source, re.S
    )
    assert bloc is not None, (
        f"{COMMANDS} : SIDECAR_METHODS est introuvable ou a changé de forme. "
        "Ce test ne peut plus vérifier la concordance —corrigez le motif, ou le test."
    )
    routage = dict(
        re.findall(r"([A-Za-z_]\w*):\s*'([A-Za-z_][\w.]*)'", bloc.group("corps"))
    )
    assert len(routage) >= 20, (
        f"{COMMANDS} : seulement {len(routage)} routages lus sur une liste qui en "
        "attend bien davantage. Le motif ne suit plus la forme du fichier."
    )
    return routage


def _preload_commands() -> set[str]:
    """Lit la liste `COMMANDS` du preload, sans l'exécuter (il require electron)."""
    source = PRELOAD.read_text(encoding="utf-8")
    bloc = re.search(
        r"const COMMANDS = Object\.freeze\(\[(?P<corps>.*?)\]\)", source, re.S
    )
    assert bloc is not None, (
        f"{PRELOAD} : COMMANDS est introuvable ou a changé de forme. "
        "Ce test ne peut plus vérifier la concordance."
    )
    commandes = set(re.findall(r"'([a-z_]+)'", bloc.group("corps")))
    assert commandes, (
        f"{PRELOAD} : aucune commande lue — le motif ne suit plus le fichier."
    )
    return commandes


def _branche(source: str, methode: str) -> bool:
    """`handle()` traite-t-il cette méthode par une branche qui lui soit propre ?

    Deux écritures coexistent dans `handle()` : `method == "x"`, et l'appartenance à
    un groupe `method in ("x", "y")`. Ne reconnaître que la première ferait crier à
    l'exemption de `health` et `resync`, qui ont précisément la seconde forme.
    """
    if f'method == "{methode}"' in source:
        return True
    return (
        re.search(rf'method\s+in\s+\([^)]*"{re.escape(methode)}"', source) is not None
    )


def test_le_mot_du_canal_est_dans_les_deux_registres_python():
    """Les deux dictionnaires Python ne peuvent pas diverger.

    Une clé présente dans l'un et absente de l'autre est soit un canal mort (le client
    attend une réponse qui ne viendra jamais), soit une porte qui refuse un canal
    pourtant annoncé. Les deux sont invisibles sans cette assertion.
    """
    sans_handler = sorted(PROTOCOL_METHODS - set(_IPC_HANDLERS) - set(BRANCHES_DEDIEES))
    assert sans_handler == [], (
        "Ces méthodes sont acceptées par le protocole mais aucun handler ne les sert :\n"
        + "\n".join(f"  - {m}" for m in sans_handler)
        + "\nSoit ajoutez le handler dans _IPC_HANDLERS, soit retirez la méthode de "
        "PROTOCOL_METHODS, soit — si elle a une branche dédiée — ajoutez-la à "
        "BRANCHES_DEDIEES dans ce test avec sa raison."
    )

    sans_protocole = sorted(set(_IPC_HANDLERS) - PROTOCOL_METHODS)
    assert sans_protocole == [], (
        "Ces méthodes ont un handler mais le protocole les refuse à l'entrée :\n"
        + "\n".join(f"  - {m}" for m in sans_protocole)
        + "\nC'est LA PORTE 3 (`backend/inference/ipc.py`, PROTOCOL_METHODS). "
        "Sans elle, la commande part et reçoit `unknown_method`."
    )


def test_toute_methode_routee_par_electron_est_acceptee_par_le_protocole():
    """Le refus exact du bug : une méthode routée que le protocole ne connaît pas.

    C'est l'assertion qui aurait dû exister avant. Elle dit où chercher, parce que
    « Unknown espace.depot_integrite » ne le disait pas — et faire perdre une heure à
    quelqu'un qui cherchait du bon côté est le vrai coût de l'absence de cette garde.
    """
    routage = _routage_electron()
    refusees = {
        commande: methode
        for commande, methode in routage.items()
        if methode not in PROTOCOL_METHODS
    }

    detail = "\n".join(
        f"  - {commande} -> {methode}   ABSENTE de PROTOCOL_METHODS "
        "(backend/inference/ipc.py)"
        for commande, methode in sorted(refusees.items())
    )
    assert refusees == {}, (
        "Electron route des méthodes que le protocole refuse à l'entrée. Le bouton part, "
        "puis l'utilisateur reçoit `unknown_method` — et un redémarrage ne change rien.\n"
        f"{detail}\nCorrigez PROTOCOL_METHODS dans backend/inference/ipc.py."
    )


def test_les_canaux_du_depot_sont_connus_des_quatre_côtés():
    """Le cas concret du bug, nommé explicitement.

    Un test générique qui casse sur le prochain oubli est utile ; un test qui fixationne
    les deux canaux du dépôt dit aussi ce qu'ils font. Il échouerait avec un message
    parlant de dépôt, pas de « méthode 37 ».
    """
    from backend.main import _IPC_HANDLERS as handlers

    for methode, cible in (
        ("espace.depot_integrite", "espace_depot_verifier"),
        ("espace.depot_reparer", "espace_depot_reparer"),
    ):
        assert methode in PROTOCOL_METHODS, f"{methode} absent de PROTOCOL_METHODS"
        assert methode in handlers, f"{methode} absent de _IPC_HANDLERS"
        assert handlers[methode].endswith(f":{cible}"), (
            f"{methode} pointe vers {handlers[methode]!r} au lieu de {cible!r}"
        )

    routage = _routage_electron()
    for commande in ("espace_depot_verifier", "espace_depot_reparer"):
        assert commande in routage, (
            f"{commande} absent du routage Electron (electron/commands.cjs)"
        )


def test_les_canaux_de_memoire_sont_connus_des_quatre_portes():
    """Forcer, corriger, supprimer : handler, protocole, registry et preload.

    Les trois écrivent la mémoire ; `espace.memoire` reste la seule porte de
    lecture. Une porte manquante donne soit un refus à l'entrée, soit un bouton
    annoncé que rien ne sert — les deux invisibles sans cette garde.
    """
    from backend.main import _IPC_HANDLERS as handlers

    attendus = {
        "espace.memoire.forcer": "espace_memoire_forcer",
        "espace.memoire.corriger": "espace_memoire_corriger",
        "espace.memoire.supprimer": "espace_memoire_supprimer",
    }
    routage = _routage_electron()
    preload = _preload_commands()

    for methode, cible in attendus.items():
        assert methode in PROTOCOL_METHODS, f"{methode} absent de PROTOCOL_METHODS"
        assert methode in handlers, f"{methode} absent de _IPC_HANDLERS"
        assert handlers[methode].endswith(f":{cible}"), (
            f"{methode} pointe vers {handlers[methode]!r} au lieu de {cible!r}"
        )

        commande = cible  # le verbe Electron suit le nom du handler
        assert routage.get(commande) == methode, (
            f"{commande} route vers {routage.get(commande)!r} au lieu de {methode!r}"
        )
        assert commande in preload, f"{commande} absent de la liste COMMANDS du preload"

    # Les méthodes mutantes ne doivent pas emprunter la voie de lecture.
    from backend.inference.ipc import READ_ONLY_METHODS

    assert attendus.keys().isdisjoint(READ_ONLY_METHODS)
    assert "espace.memoire" in READ_ONLY_METHODS


def test_les_branches_dediees_existent_bien_leur_branche():
    """La liste `BRANCHES_DEDIEES` ne doit pas devenir une excuse.

    Si `BRANCHES_DEDIEES` nomme une méthode qui n'a plus de branche dédiée dans
    `handle()`, quelqu'un a retiré la branche — et l'exemption devient un mensonge
    qui laisserait un canal mort. Cette assertion relie donc l'excuse à sa raison.
    """
    source = MAIN.read_text(encoding="utf-8")
    sans_branche = sorted(
        methode for methode in BRANCHES_DEDIEES if not _branche(source, methode)
    )
    assert sans_branche == [], (
        "Ces méthodes sont exemptées de _IPC_HANDLERS mais n'ont plus de branche "
        "dédiée dans handle() :\n"
        + "\n".join(f"  - {m}" for m in sans_branche)
        + "\nL'exemption n'a plus de raison. Rétablissez la branche, ou retirez "
        "l'entrée de BRANCHES_DEDIEES pour que la garde signale le canal mort."
    )
