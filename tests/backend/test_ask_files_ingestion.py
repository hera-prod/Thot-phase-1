"""`ask_files` interroge la REPRÉSENTATION D'INGESTION — et le prouve.

CE QUE CE FICHIER GARDE, et que rien d'autre ne gardait :

  1. Une question sur un fichier ingéré répond DEPUIS ce que l'ingestion a écrit, et
     aucun modèle n'est appelé pour ça. La preuve n'est pas une assertion
     indirecte (« le résultat a l'air d'être venu de la base ») : c'est un PIÈGE
     qui explose bruyamment au moindre appel de modèle, armé pendant l'ingestion
     ET pendant la question — plus le compte des observations, qui doit rester
     inchangé après la question. C'est la Bible §2.6 l. 189 : « l'ingestion
     initiale doit être effectuée à l'ajout du fichier, sans recommencer
     systématiquement l'analyse complète à chaque question ».
  2. Le plafond de conservation est ANNONCÉ. `Observation.payload_ref` garde au plus
     16 000 caractères d'une extraction qui en a produit davantage ; servie telle
     quelle, la réponse est fausse sans le dire.
  3. L'état d'ingestion est CONSOMMÉ, pas recalculé : `en_cours`, `binaire_refuse` et
     `tronque` donnent chacun un motif distinct, et ce sont les mots du dépôt.
  4. La citation reste traçable : `evidence_ids` et `public_ids` sont remplis, et le
     `doc_N` cité est celui du fichier demandé.

MONTAGE : base neuve et jetable par test, racines de données isolées, dépôt par le
VRAI chemin (`stage_picker_bytes` puis `attachments_prepare`), ingestion RÉELLEMENT
exécutée (`attachment_ingest._ingérer_un`) — une observation fabriquée à la main
prouverait que l'outil lit une observation, pas qu'il lit l'ingestion. Aucun octet ni
aucune ligne du projet n'est touché ; aucun modèle, aucun GPU, aucun réseau.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db import database as database_module
from backend.db.models import (
    Artifact,
    ArtifactRepresentation,
    Base,
    Conversation,
    Observation,
)
from backend.inference import loop as L
from backend.ingestion import attachment_ingest, etat as ETAT
from backend.ingestion import paddle_client
from backend.ipc import attachments as A
from backend.search.observations import PAYLOAD_CHARS, record_tool_observation


# ── Pièges : tout appel de modèle doit ÉCHOUER BRUYAMMENT ────────────────────


class _ModeleAppele(RuntimeError):
    """Un modèle a été appelé alors que la Bible l'interdit."""


class _PoigneeModele:
    """Le `manager` que le chemin vivant reçoit. Il ne sait RIEN faire.

    Le chemin abandonné l'utilisait (`manager.use_model("LFM2.5-VL-3B")` sur les
    pixels) ; l'adaptateur ne doit plus le toucher du tout. Un `__getattr__` qui lève
    transforme « il n'a pas appelé le modèle » en fait vérifié plutôt qu'en intention :
    le moindre accès à un attribut — donc le moindre chargement — casse le test.
    """

    def __getattr__(self, nom: str):
        raise _ModeleAppele(
            f"le chemin vivant d'ask_files a touché le manager de modèle ({nom!r}) : "
            "il doit lire la représentation d'ingestion, pas interroger un modèle"
        )


def _interdit(ou: str):
    def _piege(*args, **kwargs):
        raise _ModeleAppele(
            f"modèle appelé par {ou} — ask_files doit répondre depuis l'ingestion"
        )

    return _piege


@pytest.fixture()
def aucun_modele(monkeypatch):
    """Arme les pièges. Rend un `manager` qui explose au moindre usage."""
    for nom in ("parse_png", "_ensure"):
        monkeypatch.setattr(
            paddle_client, nom, _interdit(f"paddle_client.{nom}"), raising=False
        )
    for nom in (
        "_execute_vision",
        "_execute_doc_ocr",
        "_execute_read_text",
        "_execute_read_document",
    ):
        monkeypatch.setattr(L, nom, _interdit(f"loop.{nom}"), raising=False)
    return _PoigneeModele()


@pytest.fixture()
def pieges_bien_armes(aucun_modele):
    """Vérifie que les PIÈGES FONCTIONNENT, avant de croire les autres tests.

    Un piège mal posé — mauvais nom d'attribut, `raising=False` sur un attribut
    inexistant, une exception avalée plus bas — ne lève jamais, et tous les tests du
    fichier passent alors sans rien prouver. On le Demonstrate donc d'abord.
    """
    with pytest.raises(_ModeleAppele):
        aucun_modele.use_model("LFM2.5-VL-3B")
    with pytest.raises(_ModeleAppele):
        paddle_client.parse_png(b"png")
    with pytest.raises(_ModeleAppele):
        L._execute_doc_ocr(None, {"args": {}}, "req")
    return aucun_modele


# ── Montage ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def monde(tmp_path, monkeypatch):
    """Base neuve, index FTS et tables de fragments, racines de données jetables.

    Le déclencheur d'ingestion du dépôt est neutralisé : il lance un thread, et un
    thread survit au test qui l'a créé pour venir écrire dans la base du suivant. On
    exécute donc `_ingérer_un` nous-mêmes, au moment choisi — donc dans un contexte où
    les pièges de modèle sont ARMÉS.
    """
    moteur = create_engine(f"sqlite:///{tmp_path / 'ask-files-ingestion.db'}")
    Base.metadata.create_all(moteur)
    from backend.db.database import _ensure_fts
    from backend.search.chunks import ensure_chunk_tables

    _ensure_fts(moteur)
    sessions = sessionmaker(bind=moteur, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", sessions)
    racine = tmp_path / "data"
    (racine / "blobs").mkdir(parents=True)
    (racine / "temp").mkdir(parents=True)
    # `THOT_APP_DATA_DIR` doit S'APPELER `data` : `resolve_data_dir` ne renvoie le
    # dossier tel quel que s'il est déjà un dossier `data`, sinon il y ajoute un
    # second `data`. Les blobs seraient alors écrits sous `<racine>/data/blobs` et
    # recherchés sous `<racine>` — donc introuvables, et l'extraction échouerait sur
    # un fichier parfaitement déposé. Aucune redirection de `paths_bridge` n'est
    # nécessaire : la variable d'environnement suffit, et elle redirige les DEUX
    # côtés — celui qui écrit le blob et celui qui le relit.
    monkeypatch.setenv("THOT_PORTABLE", "1")
    monkeypatch.setenv("THOT_APP_DATA_DIR", str(racine))
    monkeypatch.setattr(A, "_declencher_ingestion", lambda ids: None)
    with sessions() as db:
        ensure_chunk_tables(db)
        db.commit()
    return sessions


def pdf_avec_texte(texte: str) -> bytes:
    """PDF RÉEL, texte RÉELLEMENT extractible. `pymupdf` l'écrit, il le relit.

    Un PDF fabriqué à la main — un en-tête, un catalogue, du texte en clair dans le
    corps du fichier — n'a pas de flux de contenu : l'extraction n'y trouve rien,
    l'état d'ingestion devient `vide`, et le test vérifierait un motif d'absence au
    lieu d'une réponse.
    """
    import pymupdf

    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 780), texte, fontsize=9)
        return doc.tobytes()
    finally:
        doc.close()


CONTRAT = (
    "Contrat de prestation. Montant total : 1 200,00 euros. "
    "Signataire : Atelier Beziers. Date : 12 mars 2026. "
    "La prestataire s'engage a livrer les fournitures. "
) * 12

DEUXIEME = (
    "Annexe du devis. Reference : DV-4471. "
    "Delai de livraison : trois semaines ouvrables. "
) * 12


def _discussion(sessions) -> str:
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    with sessions() as db:
        db.add(Conversation(id=conversation, title="Ingestion", mode="chat"))
        db.commit()
    return conversation


def _deposer(
    sessions, nom: str, octets: bytes, conversation: str | None = None
) -> tuple[str, str]:
    """Dépôt par le VRAI chemin. Renvoie (conversation, artifact_id)."""
    with sessions() as db:
        cible = conversation or _discussion(sessions)
        item = A.stage_picker_bytes(
            db,
            {
                "requestId": f"ask-{uuid.uuid4().hex[:8]}",
                "filename": nom,
                "bytes": octets,
            },
        )["items"][0]
        (artefact,) = A.attachments_prepare(
            db,
            {
                "conversationId": cible,
                "stageItemIds": [item["stage_item_id"]],
                "names": [nom],
            },
        )["artifact_ids"]
        db.commit()
    return cible, artefact


def _ingérer(artefact: str) -> None:
    """L'ingestion RÉELLE, exécutée ici, dans le contexte piégé."""
    attachment_ingest._ingérer_un(artefact)


def _etat(sessions, artefact: str) -> str:
    with sessions() as db:
        return ETAT.lire(db.get(Artifact, artefact)).etat


def _observations(sessions, artefact: str) -> list[Observation]:
    with sessions() as db:
        return db.query(Observation).filter(Observation.artifact_id == artefact).all()


def _payload(sessions, artefact: str) -> str:
    return _observations(sessions, artefact)[0].payload_ref or ""


def _question(
    questions: list[str],
    doc_labels: list[str],
    conversation: str,
    manager=None,
    scope: dict | None = None,
) -> dict:
    args: dict = {"questions": questions, "resource_ids": doc_labels}
    if scope is not None:
        args["scope"] = scope
    return L._execute_ask_files(
        {"args": args},
        manager,
        conv_id=conversation,
        request_id="req_ask",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. LA REPRÉSENTATION D'INGESTION, ET AUCUN MODÈLE
# ══════════════════════════════════════════════════════════════════════════════


def test_la_question_repond_depuis_la_representation_d_ingestion(
    monde, pieges_bien_armes
):
    """Le texte rendu vient de l'observation d'ingestion, et aucun modèle n'est appelé.

    Les pièges sont ARMÉS PENDANT L'INGESTION elle-même : l'extraction du PDF passe par
    `pymupdf` — seuil natif par page franchi et aucune image, donc aucun OCR — et si
    l'adaptateur appelait un modèle, il exploserait ici.
    """
    sessions = monde
    conversation, artefact = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    _ingérer(artefact)

    assert _etat(sessions, artefact) == ETAT.ETAT_EXTRAIT, (
        "le montage doit ingérer vraiment"
    )
    avant = len(_observations(sessions, artefact))

    r = _question(["Quel est le montant ?"], ["doc_1"], conversation, pieges_bien_armes)

    assert r["ok"] is True
    assert "1 200,00" in r["result"], "le texte ingéré ne revient pas"
    # La réponse est DANS la représentation : elle en est un extrait, pas une relecture.
    assert "Montant total : 1 200,00 euros." in _payload(sessions, artefact)
    assert "Montant total : 1 200,00 euros." in r["result"]
    # La question n'a RIEN relancé : une observation de plus, et c'est l'ingestion
    # qui recommence — précisément ce que la Bible §2.6 l. 189 interdit.
    assert len(_observations(sessions, artefact)) == avant, (
        "la question a produit une observation : elle a relu le fichier"
    )


def test_le_gestionnaire_de_modele_n_est_jamais_touche(monde, pieges_bien_armes):
    """Un `manager` qui explose au moindre attribut, et qui n'a pas été touché.

    Séparé du test précédent parce que les deux preuves sont distinctes : l'une porte
    sur l'OCR et la vision, celle-ci sur le LLM. Un chemin qui n'appellerait ni l'un ni
    l'autre mais effleurerait le gestionnaire passerait le premier test.
    """
    sessions = monde
    conversation, artefact = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    _ingérer(artefact)

    r = _question(["Quel est le montant ?"], ["doc_1"], conversation, pieges_bien_armes)

    assert r["ok"] is True and "1 200,00" in r["result"]


def test_trois_questions_une_seule_relecture(monde, pieges_bien_armes):
    """Trois questions, une représentation : l'ask_files ne recharge pas par paire."""
    sessions = monde
    conversation, artefact = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    _ingérer(artefact)
    avant = len(_observations(sessions, artefact))

    r = _question(
        ["Quel est le montant ?", "Quel est le signataire ?", "Quelle est la date ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
    )

    assert r["ok"] is True and "mode matrice" in r["result"]
    assert len(_observations(sessions, artefact)) == avant


# ══════════════════════════════════════════════════════════════════════════════
# 2. LE PLAFOND : ANNONCÉ, JAMAIS DISSIMULÉ
# ══════════════════════════════════════════════════════════════════════════════


def _tableur_geant() -> bytes:
    """Un tableur dont l'extraction dépassera de loin le plafond de conservation.

    AUCUN mot de la question ne doit exister dans la partie conservée, sinon la
    question a légitimement une réponse et le test ne prouve rien. D'où des
    intitulés numériques (`Intitule-0001`) et non des mots récurrents comme
    « ligne » ou « poste », qui tomberaient sur n'importe quelle ligne.

    La représentation structurée (tâche 4.5) a changé CE QUI est conservé : le
    fichier n'est plus aplati en TSV linéaire, il est lu en schéma + échantillon
    d'index (spec 03) + suite de lecture (mecanismes/ingestion/tableaux.md §4.4 :
    « Une indexation initiale échantillonnée est possible pour de grands fichiers,
    mais elle doit être signalée ». `zeppelin` est donc placé sur une ligne que
    NI l'échantillon NI le palier de 16 000 caractères n'atteignent : valeur
    numérique moyenne (1400 — jamais min/max, donc jamais « extrême »), rang au
    milieu (450 — pas parmi les premières ni les dernières, ni dans le pas de
    complétion de l'échantillon). Son mot existe dans le fichier (ask_files le
    relit quand la demande le vise) mais pas dans le stock — c'est exactement
    l'écart que l'absence doit porter.
    """
    lignes = ["cle;intitule;valeur"]
    lignes += [f"CL-{i:04d};Intitule-{i:04d};{1000 + i}" for i in range(1, 450)]
    lignes.append("CL-0450;Intitule tardif zeppelin;1400")
    lignes += [f"CL-{i:04d};Intitule-{i:04d};{1000 + i}" for i in range(451, 900)]
    return ("\n".join(lignes) + "\n").encode("utf-8")


def test_la_representation_complete_depasse_le_payload(monde, pieges_bien_armes):
    """Le payload reste borné, mais `ask_files` lit la représentation COMPLÈTE.

    L'observation n'en conserve que 16 000 caractères pour le prompt. Ce n'est
    plus la vérité documentaire : la représentation porte le texte entier, et
    l'outil ne doit donc plus annoncer une coupure du document.
    """
    sessions = monde
    octets = _tableur_geant()
    conversation, artefact = _deposer(sessions, "budget.csv", octets)
    _ingérer(artefact)

    payload = _payload(sessions, artefact)
    assert len(payload) == PAYLOAD_CHARS, (
        f"le montage ne touche pas le plafond : {len(payload)} caractères conservés"
    )
    assert "zeppelin" not in payload, (
        "le mot de la dernière ligne doit être hors du stock borné"
    )

    r = _question(
        ["Quel est le montant de l'intitule CL-0042 ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
    )

    assert r["ok"] is True
    assert "CL-0042" in r["result"] and "1042" in r["result"]
    assert f"arrêtée à sa limite de {PAYLOAD_CHARS} caractères" not in r["result"], (
        f"la représentation complète ne doit pas annoncer la borne du payload :\n{r['result']}"
    )


def test_une_reponse_au_dela_du_payload_est_trouvee(monde, pieges_bien_armes):
    """`zeppelin` est hors du payload de 16 k, mais DANS la représentation.

    La représentation structurée du tableur porte aussi la suite de lecture :
    l'outil doit donc trouver cette ligne, au lieu de la déclarer absente de la
    partie lisible. C'est exactement le gain de l'étape A.
    """
    sessions = monde
    octets = _tableur_geant()
    conversation, artefact = _deposer(sessions, "budget.csv", octets)
    _ingérer(artefact)
    assert "zeppelin" in octets.decode("utf-8"), "montage : le mot est dans le fichier"
    assert "zeppelin" not in _payload(sessions, artefact), (
        "montage : il est hors du stock borné"
    )

    r = _question(
        ["Quel est le montant de zeppelin ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
    )

    assert r["ok"] is True
    assert "information absente" not in r["result"]
    assert "zeppelin" in r["result"] and "1400" in r["result"]


def test_une_absence_sur_representation_bornee_le_dit(monde, pieges_bien_armes):
    """Sans représentation (legacy), l'absence porte sa portée bornée.

    `zeppelin` est dans le fichier déposé et absent du payload de 16 k. Sans
    représentation courante, l'outil ne peut donc pas le trouver — et il ne doit
    pas dire « il n'est pas dans le document ». Il dit « absente de la partie
    lisible » et nomme la borne qui explique cette absence, pour que
    l'orchestrateur puisse la vérifier au lieu d'envoyer l'utilisateur chercher
    une réponse jamais lue.
    """
    sessions = monde
    octets = _tableur_geant()
    conversation, artefact = _deposer(sessions, "budget.csv", octets)
    _ingérer(artefact)
    assert "zeppelin" in octets.decode("utf-8"), "montage : le mot est dans le fichier"
    assert "zeppelin" not in _payload(sessions, artefact), (
        "montage : il est hors du stock borné"
    )
    # On retire la représentation pour éprouver le REPLI legacy : c'est la seule
    # situation où le payload borné est encore la vérité lue par `ask_files`.
    with sessions() as db:
        db.query(ArtifactRepresentation).filter(
            ArtifactRepresentation.artifact_id == artefact
        ).delete()
        db.commit()

    r = _question(
        ["Quel est le montant de zeppelin ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
    )

    assert r["ok"] is True
    assert "information absente du document" not in r["result"], (
        "une absence sur une représentation bornée ne peut pas se dire du document"
    )
    assert "partie lisible" in r["result"]
    # Le repli legacy annonce la borne soit comme un plafond atteint, soit comme
    # une coupe mesurée (les deux nombres). Dans les deux cas, le nombre conservé
    # doit être là : « borné » sans sa borne serait une formule vide.
    messages_borne = (
        f"arrêtée à sa limite de {PAYLOAD_CHARS} caractères",
        f"{PAYLOAD_CHARS} caractères conservés sur",
    )
    assert any(message in r["result"] for message in messages_borne), (
        f"l'absence ne dit pas POURQUOI elle est une absence :\n{r['result']}"
    )


def test_une_coupe_mesuree_est_annoncee_avec_les_deux_nombres(monde, pieges_bien_armes):
    """Sans représentation (legacy), l'écart mesurable reste annoncé.

    `Observation.result_json` porte `chars_total` — ce que l'extraction avait
    produit. C'est le seul endroit du dépôt où l'on compare « extrait » et
    « conservé » ; sans lui, un extrait coupé est indiscernable d'un extrait
    complet, et le mot « tronqué » deviendrait une formule vide.
    """
    sessions = monde
    conversation, artefact = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    _ingérer(artefact)
    # Une observation d'outil dont le résultat dépassait le plafond — le cas réel d'un
    # `doc_ocr` de plusieurs pages, ou d'une transcription de longue description.
    long = "Contrat de prestation. Montant total : 1 200,00 euros. " * 1500
    record_tool_observation(artefact, "doc_ocr", None, long, ok=True)
    # On retire la représentation pour éprouver le REPLI legacy : c'est la seule
    # situation où le payload borné est encore la vérité lue par `ask_files`.
    with sessions() as db:
        db.query(ArtifactRepresentation).filter(
            ArtifactRepresentation.artifact_id == artefact
        ).delete()
        db.commit()

    r = _question(["Quel est le montant ?"], ["doc_1"], conversation, pieges_bien_armes)

    assert r["ok"] is True and "1 200,00" in r["result"]
    assert "tronquée" in r["result"]
    assert f"{PAYLOAD_CHARS} caractères conservés" in r["result"]
    assert f"sur {len(long)} extraits" in r["result"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. L'ÉTAT D'INGESTION, CONSOMMÉ TEL QU'IL EST ÉCRIT
# ══════════════════════════════════════════════════════════════════════════════


def test_un_fichier_en_cours_n_est_pas_encore_interrogeable(monde, pieges_bien_armes):
    """`en_cours` : l'extraction a commencé, elle n'a pas donné de verdict.

    Le motif vient du relevé — « extraction en cours, aucun verdict pour l'instant » —
    et il est DIFFÉRENT de celui d'un fichier binaire et de celui d'un fichier tronqué.
    Un même « rien de lisible » pour trois causes opposées est exactement
    l'indécision que le relevé d'ingestion supprime.
    """
    sessions = monde
    conversation, artefact = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    with sessions() as db:
        ETAT.marquer(db, artefact, ETAT.ETAT_EN_COURS, "extraction demandée (type pdf)")

    r = _question(["Quel est le montant ?"], ["doc_1"], conversation, pieges_bien_armes)

    assert r["ok"] is True
    motif = ETAT.PHRASES[ETAT.ETAT_EN_COURS]
    assert motif in r["result"], f"le motif de l'état est absent :\n{r['result']}"
    assert "extraction demandée (type pdf)" in r["result"], (
        "la raison consignée doit suivre"
    )
    assert "1 200,00" not in r["result"], (
        "un fichier en cours n'a pas de réponse à donner"
    )


def test_un_fichier_obsolete_n_est_pas_propose_aux_outils(monde, pieges_bien_armes):
    """`obsolète` : une source remplacée ou retirée ne s'interroge plus.

    D5 (cycle de vie des informations §3.1) : le document reste consultable
    dans son dernier état connu, mais il cesse d'être proposé à la recherche
    ni aux outils. `ask_files` en tire les conséquences : on ne lit plus la
    représentation précédente, le MOTIF de l'état est servi, mot pour mot. Une
    interrogation qui reviendrait sur le même `doc_N` ne ressusciterait pas ce
    qui a été remplacé.
    """
    sessions = monde
    conversation, artefact = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    _ingérer(artefact)
    with sessions() as db:
        ETAT.marquer(db, artefact, ETAT.ETAT_OBSOLETE, "source retirée du dépôt")

    r = _question(["Quel est le montant ?"], ["doc_1"], conversation, pieges_bien_armes)

    assert r["ok"] is True
    motif = ETAT.PHRASES[ETAT.ETAT_OBSOLETE]
    assert motif in r["result"], f"le motif de l'état est absent :\\n{r['result']}"
    assert "source retirée du dépôt" in r["result"], "la raison consignée doit suivre"
    assert "1 200,00" not in r["result"], (
        "un fichier obsolète n'a pas de réponse à donner"
    )
    assert r["evidence_ids"] == [], "une absence n'a pas de preuve à citer"


def test_un_binaire_refuse_n_a_rien_a_interroger(monde, pieges_bien_armes):
    """`binaire_refuse` : RIEN n'a été indexé, donc il n'y a rien à lire.

    Le cas que l'ancien chemin perdait : il relançait un modèle sur le fichier, et
    pouvait donc produire une réponse — pour un fichier dont rien n'a jamais pu être
    lu. Ici l'outil dit ce que l'ingestion a conclu, et n'invente rien.
    """
    sessions = monde
    conversation, artefact = _deposer(sessions, "budget.csv", _tableur_geant())
    with sessions() as db:
        ETAT.marquer(
            db,
            artefact,
            ETAT.ETAT_BINAIRE,
            "Contenu binaire refusé à l'indexation : aucun octet indexé",
        )

    r = _question(
        ["Quel est le montant de l'intitule CL-0042 ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
    )

    assert r["ok"] is True
    motif = ETAT.PHRASES[ETAT.ETAT_BINAIRE]
    assert motif in r["result"], f"le motif de l'état est absent :\n{r['result']}"
    reponse = [ligne for ligne in r["result"].splitlines() if "budget.csv" in ligne]
    assert reponse and not any("CL-0042" in ligne for ligne in reponse), (
        f"une réponse a été inventée pour un fichier binaire :\n{r['result']}"
    )
    assert r["evidence_ids"] == [], "une absence n'a pas de preuve à citer"


def test_un_fichier_tronque_donne_son_propre_motif(monde, pieges_bien_armes):
    """`tronque` : le texte est là, mais l'indexation s'arrête à sa borne.

    Le motif est celui du dépôt, et il parle d'INDEXATION — pas de la représentation
    que nous tenons. Confondre les deux bornes ferait annoncer une perte là où il n'y
    en a pas, et tairait celle qu'il y a. Les trois motifs doivent rester distincts :
    c'est ce qui permet à l'orchestrateur de dire « réessaie plus tard » pour l'un et
    « n'insiste pas » pour l'autre.
    """
    sessions = monde
    conversation, artefact = _deposer(sessions, "budget.csv", _tableur_geant())
    _ingérer(artefact)
    assert _etat(sessions, artefact) == ETAT.ETAT_TRONQUE, (
        "montage : le dépôt doit être tronqué"
    )

    r = _question(
        ["Quel est le montant de l'intitule CL-0042 ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
    )

    assert r["ok"] is True
    assert "CL-0042" in r["result"], "le texte extrait reste interrogeable"
    assert ETAT.PHRASES[ETAT.ETAT_TRONQUE] in r["result"], (
        f"le motif de l'état tronqué est absent :\n{r['result']}"
    )
    motifs = {
        ETAT.PHRASES[ETAT.ETAT_TRONQUE],
        ETAT.PHRASES[ETAT.ETAT_BINAIRE],
        ETAT.PHRASES[ETAT.ETAT_EN_COURS],
    }
    assert len(motifs) == 3, "trois états, trois motifs : aucun ne doit se confondre"


# ══════════════════════════════════════════════════════════════════════════════
# 4. LA CITATION : `evidence_ids` ET `public_ids`
# ══════════════════════════════════════════════════════════════════════════════


def test_les_preuves_et_les_identifiants_designent_le_bon_fichier(
    monde, pieges_bien_armes
):
    """Deux fichiers, deux `doc_N`, deux contenus : la citation ne peut pas se confondre.

    C'est ce qui permet au brief de nommer une source sans ambiguïté. Un `doc_N` cité
    pour une observation qui appartient à l'autre fichier produirait une réponse
    VRAIE ET FAUSSE à la fois — le pire des deux verdicts, et le seul que rien ne
    signale.
    """
    sessions = monde
    conversation, premier = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    _, second = _deposer(sessions, "annexe.pdf", pdf_avec_texte(DEUXIEME), conversation)
    _ingérer(premier)
    _ingérer(second)

    r = _question(
        ["Quel est le montant ?", "Quel est le délai de livraison ?"],
        ["doc_1", "doc_2"],
        conversation,
        pieges_bien_armes,
    )

    assert r["ok"] is True
    assert r["public_ids"] == ["doc_1", "doc_2"]
    assert r["evidence_ids"], "aucune preuve : le brief ne pourra rien citer"
    assert all(e.startswith(("doc_1:", "doc_2:")) for e in r["evidence_ids"])
    # Chaque réponse est rendue SOUS le nom du fichier à qui elle appartient.
    lignes = r["result"].splitlines()
    for contenu, doc_label in (("1 200,00", "doc_1"), ("trois semaines", "doc_2")):
        porteuses = [ligne for ligne in lignes if contenu in ligne]
        assert porteuses, f"{contenu} absent de la réponse :\n{r['result']}"
        assert any(doc_label in ligne for ligne in porteuses), (
            f"{contenu} est rendu sous le mauvais fichier :\n{r['result']}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 5. HORS PÉRIMÈTRE : LE MOTIF DIT POURQUOI
# ══════════════════════════════════════════════════════════════════════════════


def test_un_id_inconnu_et_un_id_hors_perimetre_ne_se_confondent_pas(
    monde, pieges_bien_armes
):
    """« Inconnu » et « hors périmètre » sont deux verdicts, pas un « fichier introuvable ».

    Dire « inconnu » à l'orchestrateur l'enverrait chercher un `find_files` qui ne le
    trouvera jamais, pour un `doc_N` qui existe bel et bien — dans une AUTRE
    discussion. Les deux messages sont donc distincts, et chacun dit ce qu'il faut
    faire. C'est le cas réel du modèle qui cite le `doc_2` d'une discussion voisine.

    Les fichiers sont de contenus DIFFÉRENTS : le dépôt déduplique les blobs par
    empreinte, donc deux fichiers identiques partagent un artefact — et le `doc_2`
    foreign serait devenu parfaitement résolvable.
    """
    sessions = monde
    conversation, _ = _deposer(sessions, "contrat.pdf", pdf_avec_texte(CONTRAT))
    _, _deposer(sessions, "annexe.pdf", pdf_avec_texte(DEUXIEME), conversation)
    autre, _ = _deposer(sessions, "ailleurs.pdf", pdf_avec_texte(CONTRAT * 2))

    inconnu = _question(
        ["Quel est le montant ?"], ["doc_99"], conversation, pieges_bien_armes
    )
    assert inconnu["ok"] is False
    assert "[ID_UNKNOWN]" in inconnu["result"], f"{inconnu['result']}"
    assert "find_files" in inconnu["result"]

    # `doc_2` a été alloué dans l'AUTRE discussion : il n'existe aucun DocRef pour
    # ce label dans CETTE conversation, donc il y est inconnu (et non « hors
    # périmètre » d'ici — le verdict distant n'est pas divulgué).
    hors = _question(["Quel est le montant ?"], ["doc_2"], autre, pieges_bien_armes)

    assert hors["ok"] is False
    assert "[ID_UNKNOWN]" in hors["result"], (
        f"un `doc_N` alloué ailleurs n'est pas connu de cette conversation :\n{hors['result']}"
    )
    assert "[ID_OUT_OF_SCOPE]" not in hors["result"]


# ══════════════════════════════════════════════════════════════════════════════
# 4. LE CIBLAGE `scope` — FICHE ask_files §3.1/§3.3, réduite ou refusée, jamais systématiquement ignorée
# ══════════════════════════════════════════════════════════════════════════════


def pdf_deux_pages(texte_page_un: str, texte_page_deux: str) -> bytes:
    """PDF RÉEL de DEUX pages, chacune dense : les blocs « \\n\\n » du texte consolidé
    comptent une page chacune — sinon le ciblage « pages » n'a rien à réduire."""
    import pymupdf

    doc = pymupdf.open()
    try:
        for texte in (texte_page_un, texte_page_deux):
            page = doc.new_page()
            page.insert_textbox(pymupdf.Rect(50, 50, 545, 780), texte, fontsize=9)
        return doc.tobytes()
    finally:
        doc.close()


def classeur_feuilles(feuilles: dict[str, list[list[str]]]) -> bytes:
    """Classeur RÉEL multi-feuilles : chaque feuille existe vraiment dans le fichier."""
    import io

    import openpyxl

    classeur = openpyxl.Workbook()
    classeur.remove(classeur.active)
    for nom, lignes in feuilles.items():
        feuille = classeur.create_sheet(title=nom)
        for ligne in lignes:
            feuille.append(ligne)
    tampon = io.BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()


def test_avec_pages_le_resultat_ne_porte_que_la_page_demandee(monde, pieges_bien_armes):
    """Le ciblage `pages [2]` réduit la lecture à la page 2 — et l'annonce.

    La même question, sans `scope`, retrouve le montant sur le document ENTIER :
    c'est ce tableau de deux appels qui prouve la réduction, pas le résultat isolé.
    """
    sessions = monde
    conversation, artefact = _deposer(
        sessions, "annexe.pdf", pdf_deux_pages(CONTRAT, DEUXIEME)
    )
    _ingérer(artefact)
    payload = _payload(sessions, artefact)
    assert "Annexe du devis" in payload, (
        "montage : la page 2 doit être dans la conservation"
    )

    hors_de_la_page = _question(
        ["Quel est le montant ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
        scope={"pages": [2]},
    )
    assert hors_de_la_page["ok"] is True, (
        f"le ciblage valide doit passer : {hors_de_la_page['result']}"
    )
    assert "1 200,00" not in hors_de_la_page["result"], (
        f"le contenu de la page 1 a été lu malgré le ciblage pages [2] :\n{hors_de_la_page['result']}"
    )
    assert "ciblage pages [2] appliqué" in hors_de_la_page["result"], (
        f"la réduction doit s'énoncer :\n{hors_de_la_page['result']}"
    )

    par_le_ciblage = _question(
        ["Quel est le delai de livraison ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
        scope={"pages": [2]},
    )
    assert (
        par_le_ciblage["ok"] is True and "trois semaines" in par_le_ciblage["result"]
    ), f"le contenu DEMANDÉ — page 2 — doit répondre :\n{par_le_ciblage['result']}"

    comme_aujourd_hui = _question(
        ["Quel est le montant ?"], ["doc_1"], conversation, pieges_bien_armes
    )
    assert "1 200,00" in comme_aujourd_hui["result"], (
        "sans scope, la lecture entière doit répondre comme avant le ciblage"
    )
    assert "ciblage" not in comme_aujourd_hui["result"], (
        "sans scope, aucune phrase de ciblage ne doit apparaître"
    )


def test_avec_sheets_la_feuille_demandee_se_lit_sans_les_autres(
    monde, pieges_bien_armes
):
    """`sheets ['Factures']` limite la lecture à CETTE feuille du classeur conservé.

    Chaque ligne de valeur porte un mot qui n'existe que sur elle : la question
    lexicale de l'outil frappe la bonne ligne, et le test montre que la
    réduction a bien exclu l'autre feuille, pas seulement préférencé.
    """
    sessions = monde
    classeur = classeur_feuilles(
        {
            "Factures": [["Rubrique", "Montant"], ["Buvette de Service", "1200,00"]],
            "Honoraires": [["Rubrique", "Forfait"], ["Pétale du Jardin", "4500,00"]],
        }
    )
    conversation, artefact = _deposer(sessions, "comptes.xlsx", classeur)
    _ingérer(artefact)
    assert "Honoraires" in _payload(sessions, artefact), (
        "montage : les deux feuilles sont conservées"
    )

    sur_la_feuille = _question(
        ["Quel montant de la buvette de Service ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
        scope={"sheets": ["Factures"]},
    )
    assert sur_la_feuille["ok"] is True
    assert "1200,00" in sur_la_feuille["result"]
    assert "4500,00" not in sur_la_feuille["result"], (
        "ce qui a été lu doit provenir de la seule feuille demandée"
    )
    assert "ciblage sheets ['Factures'] appliqué" in sur_la_feuille["result"]


def test_avec_ranges_seules_les_lignes_demandees_repondent(monde, pieges_bien_armes):
    """`ranges ['A3:B3']` ne lit que la ligne 3 du contenu consolidé : la ligne 2 et la 4 n'existent pas pour la question."""
    sessions = monde
    classeur = classeur_feuilles(
        {
            "Inventaire": [
                ["Nom", "Prix"],
                ["Clous", "12,50"],
                ["Marteau", "32,00"],
                ["Vis", "8,00"],
            ],
        }
    )
    conversation, artefact = _deposer(sessions, "inventaire.xlsx", classeur)
    _ingérer(artefact)

    ligne_du_marteau = _question(
        ["Quel est le prix du Marteau ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
        scope={"ranges": ["A3:B3"]},
    )
    assert ligne_du_marteau["ok"] is True
    assert "32,00" in ligne_du_marteau["result"]
    assert (
        "12,50" not in ligne_du_marteau["result"]
        and "8,00" not in ligne_du_marteau["result"]
    ), f"les lignes hors plage ne doivent pas répondre :\n{ligne_du_marteau['result']}"
    assert "ciblage ranges ['A3:B3'] appliqué" in ligne_du_marteau["result"]


def test_une_zone_visuelle_est_denoncee_sans_filtrage_factice(monde, pieges_bien_armes):
    """`regions` est une cible énoncée, pas une réduction feinte.

    La représentation conservée ne se découpe pas par zone : prétendre le
    contraire fabriquerait un filtre inexistant. La réponse continue donc de
    lire, mais la limite le dit en toutes lettres — la demande porte, n'est
    pas dissoute dans « la page entière » du silence.
    """
    sessions = monde
    conversation, artefact = _deposer(
        sessions, "annexe.pdf", pdf_deux_pages(CONTRAT, DEUXIEME)
    )
    _ingérer(artefact)

    r = _question(
        ["Quel est le montant ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
        scope={"regions": ["hiéroglyphe du bas"]},
    )
    assert r["ok"] is True
    assert "1 200,00" in r["result"], (
        "sans découpage possible, la lecture reste entière — et le dit"
    )
    assert "non appliqué" in r["result"] and "n'a PAS été restreinte" in r["result"], (
        f"la non-application d'une zone doit être énoncée :\n{r['result']}"
    )
    assert "hiéroglyphe du bas" in r["result"], (
        "la zone demandée est rappelée, pas fondue"
    )


def test_une_page_hors_conservation_et_une_feuille_absente_sont_refusees_bruyamment(
    monde, pieges_bien_armes
):
    """Ciblage non résoluble = refus [INVALID_SCOPE] qui cite ce qui existe (fiche §3.3).

    Le CONTRAIRE — lire « presque le ciblage » — ignore la demande en silence :
    l'orchestrateur croirait avoir lu la page 7 alors que rien n'a existé.
    """
    sessions = monde
    conversation, artefact = _deposer(
        sessions, "annexe.pdf", pdf_deux_pages(CONTRAT, DEUXIEME)
    )
    _ingérer(artefact)

    r = _question(
        ["Quel est le montant ?"],
        ["doc_1"],
        conversation,
        pieges_bien_armes,
        scope={"pages": [7]},
    )
    assert r["ok"] is False
    assert "[INVALID_SCOPE]" in r["result"], f"le refus porte son code : {r['result']}"
    assert "page 7" in r["result"] and "2 bloc(s)" in r["result"], (
        f"le refus cite la page demandée ET ce qui existe :\n{r['result']}"
    )

    classeur = classeur_feuilles(
        {"Factures": [["Date", "Montant"], ["2026-03-15", "1200,00"]]}
    )
    conversation_classeur, artefact_classeur = _deposer(
        sessions, "comptes.xlsx", classeur
    )
    _ingérer(artefact_classeur)

    r2 = _question(
        ["Quel est le montant ?"],
        ["doc_1"],
        conversation_classeur,
        pieges_bien_armes,
        scope={"sheets": ["Primes"]},
    )
    assert r2["ok"] is False and "[INVALID_SCOPE]" in r2["result"]
    assert "Primes" in r2["result"] and "Factures" in r2["result"], (
        f"la feuille absente et la feuille réelle se nomment :\n{r2['result']}"
    )


def test_une_cible_inconnue_ou_vide_est_refusee_au_schema(monde, pieges_bien_armes):
    """Le schéma refuse la clé mal écrite et le scope sans dimension, AVANT l'exécution.

    La fiche découpe quatre clés au mot : `pages`, `sheets`, `ranges`, `regions`.
    Une clé autres — `page`, faute de frappe la plus fréquente — est un ciblage
    invalide, pas un argument inconnu générique : elle porte [INVALID_SCOPE]."""
    sessions = monde
    conversation, artefact = _deposer(
        sessions, "annexe.pdf", pdf_deux_pages(CONTRAT, DEUXIEME)
    )
    _ingérer(artefact)

    faute_de_frappe = L._execute_ask_files(
        {
            "args": {
                "questions": ["? "],
                "resource_ids": ["doc_1"],
                "scope": {"page": [4]},
            }
        },
        pieges_bien_armes,
        conv_id=conversation,
        request_id="req_ask",
    )
    assert (
        faute_de_frappe["ok"] is False
        and "[INVALID_SCOPE]" in faute_de_frappe["result"]
    )
    assert "page" in faute_de_frappe["result"], "la clé fautive est nommée"
    assert "sheets" in faute_de_frappe["result"], (
        "les quatre clés légales sont rappelées"
    )

    cible_vide = L._execute_ask_files(
        {"args": {"questions": ["?"], "resource_ids": ["doc_1"], "scope": {}}},
        pieges_bien_armes,
        conv_id=conversation,
        request_id="req_ask",
    )
    assert cible_vide["ok"] is False and "[INVALID_SCOPE]" in cible_vide["result"], (
        f"un scope qui ne cible rien est un ciblage invalide, pas « tout lire » :\n{cible_vide['result']}"
    )
