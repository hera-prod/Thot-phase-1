"""Un espace peuplé s'ANNONCE en une ligne, et ses fichiers deviennent citables.

Deux questions distinctes, un seul tour. La première se joue dans le CONTEXTE que le
modèle reçoit : il doit savoir qu'il travaille dans un espace qui contient des fichiers
déposés, et savoir qu'ils ne sont pas des pièces jointes. La seconde se joue dans le
TEXTE du brief : depuis la signature v1.2 (`{"brief"}`, fiche §2.2), la validation ne
porte plus de contrôle de preuve — un `doc_N` de dépôt que l'orchestrateur a réellement
lu est cité dans le brief à l'endroit où le rendu outil l'a posé.

Trois abstentions verrouillées ici, chacune avec son faux positif plausible :
  * un chat simple n'entend RIEN — pas de ligne d'espace du tout ;
  * un dépôt vide n'annonce RIEN — « cet espace contient 0 fichier » est du bruit que le
    modèle lirait comme une information ;
  * la ligne ne dépend pas du registre — le cas principal est une discussion sans la
    moindre pièce jointe, où le registre est vide et le dépôt reste à découvrir.

Et un garde-fou maintenu autrement : élargir les labels citables ne franchit pas le
périmètre. Un `doc_N` d'un AUTRE espace reste hors de portée de la LECTURE
(`scope.resolve_allowed_artifacts`) — la règle vit dans le lecteur, testée au niveau
du test de périmètre, plus dans un inventaire de preuves à valider.

Le modèle est simulé : un échec doit signifier « un maillon est cassé », pas « le 8B a
mal répondu ». La base est temporaire (conftest.py isole déjà la suite) ; aucun octet du
projet n'est touché.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Artifact,
    ArtifactChunk,
    ArtifactLink,
    Base,
    Blob,
    Conversation,
)
from backend.inference import loop as L
from backend.inference.brief import validate_submit_brief
from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR
from backend.search import docrefs
from backend.services import espaces as service

LIGNE_DEPOT = "[Espace —"
DEMANDE = "Que disent les fichiers déposés dans cet espace ?"
SYNTHESE_ESPACE = "Synthèse d'espace :"
PHRASE_CADRAGE = (
    "[Contexte documentaire d'espace — il ne conditionne ni une création d'image "
    "ni une réponse générale ; le tour courant prime.]"
)


@pytest.fixture()
def base(tmp_path, monkeypatch):
    """Base temporaire, racines de données jetables, émissions neutralisées."""
    engine = create_engine(f"sqlite:///{tmp_path / 'depot-annonce.db'}")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", sessions)
    data_dir = tmp_path / "data"
    (data_dir / "blobs").mkdir(parents=True)
    monkeypatch.setattr("backend.config.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr(L, "emit", lambda event, payload, **kwargs: None)
    return sessions


def _fichier(db, artifact_id: str, nom: str) -> str:
    db.add(
        Blob(
            id=f"blob_{artifact_id}",
            sha256=f"sha_{artifact_id}",
            relative_path=f"blobs/{artifact_id}.bin",
            size_bytes=len(nom),
            mime_detected="text/plain",
        )
    )
    db.add(
        Artifact(
            id=artifact_id,
            blob_id=f"blob_{artifact_id}",
            kind="text",
            original_filename=nom,
            display_name=nom,
        )
    )
    db.commit()
    return artifact_id


def _discussion(db, conv_id: str, workspace_id: str | None = None) -> str:
    db.add(
        Conversation(
            id=conv_id, title="Discussion", mode="chat", workspace_id=workspace_id
        )
    )
    db.commit()
    return conv_id


def _depot_peuple(db, conv_id: str, nombre: int) -> tuple[str, list[str]]:
    """Un espace, une discussion qui lui appartient, `nombre` fichiers VERSÉS dedans.

    Le versement passe par `ArtifactLink(scope_kind="workspace")` : c'est le seul
    chemin qui distingue un dépôt d'un fichier resté dans une discussion.
    """
    workspace_id = str(service.create_workspace(db, "Documents").id)
    _discussion(db, conv_id, workspace_id)
    artefacts = []
    for n in range(1, nombre + 1):
        artifact_id = _fichier(db, f"art_depot_{n}", f"note-{n}.txt")
        db.add(
            ArtifactLink(
                id=f"lnk_depot_{n}",
                artifact_id=artifact_id,
                scope_kind="workspace",
                scope_id=workspace_id,
                role="shared",
            )
        )
        artefacts.append(artifact_id)
    db.commit()
    return workspace_id, artefacts


class RuntimeFaux:
    """Le strict nécessaire à `run_agent_loop` : une réponse scriptée par appel."""

    runtime_id = "faux"
    runtime_fingerprint = "faux"

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.requests: list = []

    def complete_chat(self, request):
        self.requests.append(request)
        message = self._script.pop(0) if self._script else {"content": "Réponse."}
        return SimpleNamespace(
            raw={"choices": [{"message": message, "finish_reason": "stop"}]}
        )


def _appel(name: str, call_id: str, arguments: dict) -> dict:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _brief_valide(*cites: str) -> dict:
    """Un brief v1.2 recevable, qui cite (s'il y en a) les preuves obtenues ce tour."""
    texte = "Le rapport trimestriel est déposé dans l'espace."
    if cites:
        texte = (
            "Le rapport cite le budget du trimestre. Provenance : "
            + ", ".join(cites)
            + "."
        )
    return {"brief": texte}


TOUR_SANS_OUTIL = [
    {"content": "Je regarde ce que contient cet espace."},
    _appel("submit_brief", "call_brief", _brief_valide()),
    {"content": "Cet espace ne contient aucun fichier."},
]


def _contexte_orchestrateur(runtime: RuntimeFaux) -> str:
    """Le contexte du tour tel que le modèle le reçoit.

    Le PREMIER appel est celui de la découverte : c'est là que le registre et les
    lignes de contexte sont assemblés. La phase du brief reconstruit le même contexte,
    donc une assertion qui viserait « n'importe quel appel » ne prouverait rien.
    """
    for request in runtime.requests:
        contenu = "\n".join(
            str(message.get("content") or "") for message in request.messages
        )
        if "<REGISTRE_NON_FIABLE>" in contenu:
            return contenu
    raise AssertionError("l'orchestrateur n'a jamais reçu de registre")


def _tour(conv_id: str, script: list[dict] | None = None) -> tuple[RuntimeFaux, dict]:
    runtime = RuntimeFaux(script or TOUR_SANS_OUTIL)
    _, trace = L.run_agent_loop(
        None,
        conv_id=conv_id,
        request_id=f"req_{conv_id}",
        message=DEMANDE,
        image_paths=None,
        history_shared=[],
        runtime=runtime,
    )
    return runtime, trace


# ---------- 1-4. L'ANNONCE DANS LE CONTEXTE DU TOUR ----------


def test_un_chat_simple_n_entend_aucune_ligne_d_espace(base):
    """Un chat n'appartient à aucun espace : rien à annoncer, donc rien à dire.

    Une ligne ici mentirait — le modèle irait chercher dans un dépôt qui n'existe pas.
    """
    db = base()
    try:
        _discussion(db, "conv_simple")
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_simple")[0])
    assert LIGNE_DEPOT not in contexte
    assert DEMANDE in contexte, "le test ne prouve rien si la demande n'est plus là"


def test_une_synthese_d_espace_est_cadree_comme_contexte_non_prioritaire(base):
    """Dans un espace, la synthèse arrive AVEC son cadrage.

    Sans la phrase, « aucune orientation d'espace » peut se lire « aucune direction
    artistique possible » : le modèle s'interdirait une création que le tour courant
    demande. Le cadrage est donc le compagnon indissociable de la synthèse.
    """
    db = base()
    try:
        workspace_id = str(service.create_workspace(db, "Projet").id)
        _discussion(db, "conv_synthese", workspace_id)
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_synthese")[0])
    assert SYNTHESE_ESPACE in contexte, "l'espace doit recevoir sa synthèse"
    assert PHRASE_CADRAGE in contexte, "la synthèse doit être cadrée comme contexte"


def test_un_chat_simple_ne_recoit_ni_synthese_ni_phrase_de_cadrage(base):
    """Un chat simple n'a pas d'espace : ni synthèse, ni cadrage — sinon le modèle
    croirait travailler dans un dépôt qui n'existe pas.
    """
    db = base()
    try:
        _discussion(db, "conv_chat_simple")
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_chat_simple")[0])
    assert SYNTHESE_ESPACE not in contexte
    assert PHRASE_CADRAGE not in contexte


def test_un_espace_au_depot_vide_n_annonce_ni_zero_ni_absence(base):
    """Un dépôt vide s'annonce par son SILENCE : pas de ligne, pas de « 0 fichier ».

    Un énoncé nul ferait une ligne de plus par tour sur toutes les discussions d'espace,
    et le modèle y lirait une contrainte là où il n'y a rien à découvrir.
    """
    db = base()
    try:
        workspace_id = str(service.create_workspace(db, "Vide").id)
        _discussion(db, "conv_vide", workspace_id)
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_vide")[0])
    assert LIGNE_DEPOT not in contexte
    assert "0 fichier" not in contexte


def test_un_espace_avec_des_fichiers_annonce_le_nombre_et_le_chemin(base):
    """Trois fichiers déposés : le modèle reçoit le NOMBRE et `find_files`, rien d'autre.

    Le nombre plutôt que la liste est la décision qui tient un dépôt de cinquante
    fichiers hors du contexte ; et aucun `art_` ne traverse, puisque le prompt interdit
    formellement au modèle de connaître ces identifiants.
    """
    db = base()
    try:
        _depot_peuple(db, "conv_plein", 3)
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_plein")[0])
    ligne = [ln for ln in contexte.splitlines() if LIGNE_DEPOT in ln]
    assert len(ligne) == 1, f"une seule ligne, ou aucune : {ligne}"
    assert "3 fichiers déposés" in ligne[0]
    assert "find_files" in ligne[0]
    assert "ne sont pas requis pour une création d'image" in ligne[0]
    assert "art_" not in ligne[0]
    assert "note-1.txt" not in contexte and "note-2.txt" not in contexte, (
        "la ligne annonce un NOMBRE : la liste reste à find_files"
    )


def test_la_ligne_apparait_meme_sans_la_moindre_piece_jointe(base):
    """Le cas principal : une discussion sans pièce jointe, où le registre est vide.

    Si la ligne dépendait du registre, elle ne parlerait jamais à l'orchestrateur là où
    il a justement besoin de savoir qu'il existe autre chose que la conversation. Le
    registre vide est vérifié dans la même assertion : c'est bien lui qui est vide.
    """
    db = base()
    try:
        _depot_peuple(db, "conv_sans_piece", 2)
    finally:
        db.close()
    contexte = _contexte_orchestrateur(_tour("conv_sans_piece")[0])
    assert "Aucun fichier dans cette conversation." in contexte, (
        "le registre doit être vide : c'est ce qui prouve l'indépendance"
    )
    ligne = [ln for ln in contexte.splitlines() if LIGNE_DEPOT in ln]
    assert len(ligne) == 1 and "2 fichiers déposés" in ligne[0]


def test_le_prompt_annonce_la_ligne_que_le_modele_recoit(base):
    """La rubrique « Contexte que tu reçois » doit décrire ce qui arrive réellement.

    Une ligne injectée que le prompt ne mentionne pas est un contexte que le modèle n'a
    rien appris à interpréter : il la lirait comme une phrase de l'utilisateur.
    """
    section = SYSTEM_ORCHESTRATOR.split("## Contexte que tu reçois", 1)[1].split(
        "\n## ", 1
    )[0]
    assert "espace" in section
    assert "find_files" in section
    assert "dépôt" in section


# ---------- 5-6. LA BRIEF ET UN doc_N DE DÉPÔT ----------


def _fragment(db, artifact_id: str, ordinal: int) -> None:
    db.add(
        ArtifactChunk(
            id=f"chk_{artifact_id}_{ordinal}",
            artifact_id=artifact_id,
            ordinal=ordinal,
            text=f"fragment {ordinal} du rapport trimestriel",
        )
    )
    db.commit()


def _depot_avec_label(base, conv_id: str) -> str:
    """Un dépôt dont le premier fichier PORTE un `doc_N` pour cette discussion.

    `allocate_depot_label` est le chemin réel : c'est lui qui alloue le numéro, sans
    lien de conversation — un fichier du dépôt n'appartient à aucune discussion.
    """
    db = base()
    try:
        _, artefacts = _depot_peuple(db, conv_id, 1)
        _fragment(db, artefacts[0], 1)
        label = docrefs.allocate_depot_label(db, conv_id, artefacts[0])
        db.commit()
    finally:
        db.close()
    return label


def test_un_doc_N_de_depot_lu_peut_etre_cite_dans_le_texte_du_brief(base):
    """Lu ce tour, donc citable — dans le TEXTE du brief désormais.

    La signature v1.2 `{"brief"}` ne porte plus de contrôle de preuve (fiche §2.3 :
    les obligations d'evidence_ids sont supprimées) : c'est le RENDU outil qui rend
    l'identifiant disponible, et le brief peut s'y appuyer librement.
    Le registre de la conversation ne contient que des pièces jointes : un fichier
    du dépôt n'y entrera jamais (`build_current_artifact_entries` part des pièces
    du tour) — l'invariant du vide est conservé.
    """
    label = _depot_avec_label(base, "conv_cit")
    db = base()
    try:
        registre = L.build_current_artifact_entries("conv_cit", None)
    finally:
        db.close()
    assert registre == [], "un fichier du dépôt ne doit pas entrer au registre du tour"
    evidence_id = f"{label}:1"
    ok, code, normalise = validate_submit_brief(_brief_valide(evidence_id, label))
    assert (ok, code) == (True, "ok"), (
        f"un identifiant rendu au tour reste citable : {normalise.get('error')}"
    )
    assert evidence_id in normalise["brief"] and label in normalise["brief"]


def test_une_preuve_inventee_n_a_plus_de_fenetre_au_schema(base):
    """Lu JAMAIS ⇒ jamais obtenu : le schéma v1.2 ne juge plus la preuve, la
    structure qui la portait est RÉVOQUÉE.

    Les deux refus anciens (label sans preuve, preuve d'un rang jamais obtenu)
    se réduisent à un seul verrou : aucune clé `evidence_ids` à déposer des
    identifiants — l'oubli est devenu impossible, pas intercepté. La règle
    produit qui reste en vie vit là où la preuve NAÎT : un rang sans fragment
    persisté n'est jamais émis (`ask_files._chunk_evidence_id` vaut None, testé
    dans test_public_id_contract).
    """
    label = _depot_avec_label(base, "conv_pas_lu")
    db = base()
    try:
        registre = L.build_current_artifact_entries("conv_pas_lu", None)
    finally:
        db.close()
    assert registre == []

    ok, code, _ = validate_submit_brief(_brief_valide(f"{label}:1"))
    assert (ok, code) == (True, "ok"), (
        "un texte libre peut citer : ce n'est plus le schéma qui tranche"
    )

    # L'ancienne fenêtre est FERMÉE : impossible de déposer un rang inventé.
    ok2, code2, _ = validate_submit_brief(
        {"brief": "Réponse.", "evidence_ids": [f"{label}:9"]}
    )
    assert (ok2, code2) == (False, "UNKNOWN_ARG")
    inventee = {
        "brief": "Réponse.",
        "facts": [{"evidence_ids": [f"{label}:9"]}],
        "language": "fr",
        "complete": True,
    }
    ok3, code3, _ = validate_submit_brief(inventee)
    assert (ok3, code3) == (False, "UNKNOWN_ARG")


# Le périmètre n'a pas disparu : il vit où il a toujours vécu — dans `scope`.
# Le vérificateur de brief est parti (son seul consommateur était la liste
# `facts.evidence_ids`) ; un `doc_N` étranger reste hors de portée de la
# LECTURE (`scope.resolve_allowed_artifacts`) — c'est la règle qui protège,
# et elle est testée au niveau du lecteur (test_01_perimetre_hors_scope_jamais_de_contenu).

# ---------- 3. LE REGISTRE : CE QUI Y ENTRE, ET CE QUI N'Y ENTRE PAS ----------


def test_un_fichier_de_depot_n_entre_pas_au_registre_de_la_conversation(
    base, monkeypatch
):
    """Le dépôt n'entre pas au registre : celui-ci annonce « LES SOURCES DE CETTE
    CONVERSATION », donc un fichier du dépôt y figurerait comme une pièce jointe.

    Un tour réel le prouve — c'est le texte que le modèle lit. Le même tour prouve la
    citation : l'orchestrateur lit `doc_1` du dépôt, et son brief texte SURVIT avec la
    preuve `doc_1:1`. Le résultat de l'outil ne porte NI `public_ids` NI `evidence_ids`
    en métadonnée — la preuve n'arrive que dans le TEXTE du résultat, comme le fait
    `search_info` — donc rien ne pousse le label dans le registre du tour.
    """
    label = _depot_avec_label(base, "conv_registre")
    db = base()
    try:
        artefacts = db.query(Artifact).filter(Artifact.id == "art_depot_1").one()
        nom = artefacts.display_name
    finally:
        db.close()

    def faux_ask_files(tool_call, manager, conv_id=None, **kw):
        return {
            "ok": True,
            "latency_ms": 1,
            "artifact_id": "art_depot_1",
            "result": json.dumps(
                {
                    "artifact_id": "art_depot_1",
                    "evidence_id": f"{label}:1",
                    "text": "le budget du trimestre",
                }
            ),
        }

    monkeypatch.setattr(L, "_execute_ask_files", faux_ask_files)
    script = [
        _appel(
            "ask_files",
            "call_ask",
            {"questions": ["Quel est le budget ?"], "resource_ids": [label]},
        ),
        {"content": "J'ai lu le fichier du dépôt."},
        _appel("submit_brief", "call_brief", _brief_valide(f"{label}:1")),
        {"content": "Le budget est indiqué dans le rapport."},
    ]
    runtime, trace = _tour("conv_registre", script)
    contexte = _contexte_orchestrateur(runtime)

    registre = contexte.split("<REGISTRE_NON_FIABLE>")[1].split(
        "</REGISTRE_NON_FIABLE>"
    )[0]
    assert "Aucun fichier dans cette conversation." in registre
    assert label not in registre and nom not in registre, (
        "le dépôt n'entre pas au registre : celui-ci annonce les pièces de CETTE conversation"
    )
    assert f"{label}:1" in trace["brief_json"], (
        "le brief citant le fichier du dépôt doit être VALIDÉ, pas remplacé par le repli"
    )
