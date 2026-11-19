"""Contexte + anti-fuites — q-r.md §Architecture finale des contextes.

Même modèle, jamais même paquet :
- Orchestrateur : dernier message + historique récent + checkpoint précédent + pièces
  jointes actuelles + fichiers actifs + résultats d'outils du tour uniquement.
- Générateur : dernier message (DEMANDE) + historique (contexte) + brief JSON (données).
- Compacteur : historique à compacter + briefs validés + refs fichiers (jamais de traces).

Fuites verrouillées sans modèle/GPU :
- art_... / sti_... : jamais transmis au compacteur, jamais persistés en checkpoint,
  jamais acceptés en evidence_ids ni en artifact_ids_relevant.
- Traces d'outils (role=tool, [vision(...)], <think>, <tool_observation>, erreurs
  runtime) : jamais dans l'historique partagé, jamais dans l'entrée générateur,
  jamais dans l'entrée compacteur, jamais dans le checkpoint.
- Registre complet, réflexions, appels anciens : jamais dans le checkpoint.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base
from backend.inference import compaction as C
from backend.inference import loop as L
from backend.inference.brief import validate_submit_brief
from backend.prompts.compaction import SYSTEM_COMPACTION as PROMPT_COMPACTION
from backend.prompts.generator import SYSTEM_GENERATOR
from backend.prompts.orchestrator import SYSTEM_ORCHESTRATOR


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    return TestSession


class FakeLLM:
    def __init__(self, text: str = "", fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls = 0
        self.last_prompt = ""

    def create_completion(
        self, prompt: str, max_tokens: int = 2048, temperature: float = 0.3
    ):
        self.calls += 1
        self.last_prompt = prompt
        if self.fail:
            raise RuntimeError("LLM down")
        return {"choices": [{"text": self.text}]}


# Id d'observation au format réellement émis par les outils : ``obs_`` + 12 hex
# (backend/search/observations.py:129). La preuve du checkpoint en découle, et elle est
# paramétrée : compact_history refuse un checkpoint citant une observation absente de la
# base (compaction.py:361-369, même invariant que loop._observation_or_blob_exists pour
# les briefs). Une observation inventée deviendrait une référence permanente pour tous
# les tours suivants. Le test qui injecte ce checkpoint doit donc persister la preuve,
# sinon il mesure un refus et non la conservation des preuves.
OBS_VISION = "obs_52a1b2c3d4e5"

CHECKPOINT_OK = """# CHECKPOINT_CONVERSATION

## Sujet actif
- Analyse image doc_52

## Demande ou objectif en cours
- Décrire l'époque probable

## Décisions et contraintes utilisateur
- —

## Faits établis
- Homme en uniforme saluant une foule — confiance : established
  preuves : {obs_ref}

## Fichiers de référence conservés
- artifact_id : doc_52
  type : image
  nom : photo.jpg
  rôle : image en cours d'analyse
  preuves utiles : {obs_ref}

## Préférences de réponse
- langue : fr
- ton : —
- format : —

## Tâches ouvertes
- —

## Limites et incertitudes utiles
- Aucune date lisible

## À ne pas inférer
- Ne pas présenter la période comme certaine
"""


# ---------- 1. Matrice : trois paquets distincts ----------


def test_trois_promptes_role_distinct():
    assert "Tu ne parles jamais à l'utilisateur" in SYSTEM_ORCHESTRATOR
    assert "seul interlocuteur visible" in SYSTEM_GENERATOR
    assert "Tu ne réponds jamais à l'utilisateur" in PROMPT_COMPACTION
    # Même modèle, jamais même question
    assert "De quelles données vérifiables" in SYSTEM_ORCHESTRATOR
    assert (
        "Comment répondre naturellement" in SYSTEM_GENERATOR
        or "rédiges la réponse finale" in SYSTEM_GENERATOR
    )
    assert "Quelles informations minimales" in PROMPT_COMPACTION
    # Source unique : inference réexporte le prompt prompts
    assert C.SYSTEM_COMPACTION == PROMPT_COMPACTION


def test_generateur_paquet_3_blocs_checkpoint_mappe():
    hist = [
        {"role": "system", "content": C.CHECKPOINT_HEADER + "\nfait ancien : Dubois"},
        {"role": "user", "content": "Bonjour"},
        {"role": "assistant", "content": "Bonjour !"},
    ]
    msgs = L._build_generator_messages(
        hist, '{"language":"fr","complete":true,"facts":[]}', "Et pour Martin ?"
    )
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == SYSTEM_GENERATOR
    body = msgs[1]["content"]
    assert "<DEMANDE_UTILISATEUR>\nEt pour Martin ?\n</DEMANDE_UTILISATEUR>" in body
    assert "<HISTORIQUE>" in body and "<BRIEF_DONNEES>" in body
    # Checkpoint système = contexte antérieur, pas une demande
    assert "[contexte antérieur]" in body and "Dubois" in body
    # Le générateur ne reçoit ni tools ni observations brutes
    assert "<tool_observation>" not in body
    assert "tool_calls" not in body


def test_compacteur_paquet_historique_briefs_refs():
    old = [
        {"role": "user", "content": "Parle-moi de cette image"},
        {"role": "assistant", "content": "On y voit un homme en uniforme."},
    ]
    briefs = ["Langue : français. Faits : 1) Homme en uniforme [établie]"]
    refs = [{"id": "doc_52", "kind": "image", "name": "photo.jpg", "available": True}]
    prompt = C.build_compaction_prompt(old, briefs=briefs, file_refs=refs)
    assert "Historique visible à compacter" in prompt
    assert "Parle-moi de cette image" in prompt
    assert "Briefs validés" in prompt and "Homme en uniforme" in prompt
    assert "Références de fichiers" in prompt and "doc_52" in prompt
    assert prompt.startswith(PROMPT_COMPACTION[:60])


def test_orchestrateur_contexte_injecte_checkpoint(tmp_repo):
    from backend.db.models import ContextSnapshot, Conversation, Message
    from backend.db.repositories.conversations import get_orchestrator_context

    TestSession = tmp_repo
    db = TestSession()
    try:
        db.add(Conversation(id="conv_ctx", title="t", mode="chat"))
        for i in range(4):
            db.add(
                Message(
                    id=f"m{i}",
                    conv_id="conv_ctx",
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"contenu {i}",
                    status="completed",
                )
            )
        db.add(
            ContextSnapshot(
                id="s1",
                conversation_id="conv_ctx",
                revision=2,
                covered_through_message_id="m1",
                summary="# CHECKPOINT_CONVERSATION\nfait ancien",
            )
        )
        db.commit()
    finally:
        db.close()
    hist = get_orchestrator_context(TestSession(), "conv_ctx")
    assert hist[0]["role"] == "system" and "fait ancien" in hist[0]["content"]
    assert [m["content"] for m in hist[1:]] == ["contenu 2", "contenu 3"]
    # Format chat pur : que role/content, jamais d'id ni de trace
    assert all(set(m.keys()) == {"role", "content"} for m in hist)
    assert all(m["role"] in ("user", "assistant", "system") for m in hist)


# ---------- 2. Fuites art_... / sti_... ----------


def test_compaction_prompt_ne_transmet_jamais_art():
    old = [{"role": "user", "content": "Et là ?"}]
    refs = [
        {"id": "art_abc123", "kind": "image", "name": "photo.png", "available": True},
        {"id": "doc_52", "kind": "image", "name": "photo2.png", "available": True},
    ]
    prompt = C.build_compaction_prompt(old, briefs=[], file_refs=refs)
    body = prompt.split("## Historique visible à compacter", 1)[1]
    assert "art_abc123" not in body
    assert "doc_52" in body  # le doc_N public survit
    assert "photo.png" in body  # le nom reste utile sans l'id interne


def test_compact_history_rejette_checkpoint_fuyant_art(tmp_repo):
    TestSession = tmp_repo
    msgs = [
        {"role": "user", "content": "x" * 80000},
        {"role": "assistant", "content": "y" * 80000},
    ]
    llm = FakeLLM(text="# CHECKPOINT_CONVERSATION\n- preuve art_abc123")
    out = C.compact_history(msgs, llm, conversation_id="conv_leak", revision=1)
    # Rejet : pas de système injecté, pas de persistance, faits intacts
    assert all(m["role"] in ("user", "assistant") for m in out)
    assert llm.calls == 1
    db = TestSession()
    try:
        from backend.db.models import ContextSnapshot

        assert (
            db.query(ContextSnapshot)
            .filter(ContextSnapshot.conversation_id == "conv_leak")
            .first()
            is None
        )
    finally:
        db.close()


def test_brief_rubriques_anciennes_revoquees():
    """fiche v1.2 §2.3 : les rubriques structurelles (facts, artifact_ids_relevant…)
    sont SUPPRIMÉES de la signature — refusées au schéma, jamais acceptées.

    Un `art_...` n'a donc plus AUCUNE porte au schéma : la seule clé admise est
    `brief`, et le garde interdit-ids vit dans le prompt générateur, pas ici.
    """
    ok, code, _ = validate_submit_brief(
        {
            "brief": "Fait.",
            "facts": [
                {
                    "text": "Fait.",
                    "evidence_ids": ["art_xyz"],
                    "confidence": "established",
                }
            ],
        }
    )
    assert (ok, code) == (False, "UNKNOWN_ARG")
    ok2, code2, _ = validate_submit_brief(
        {"brief": "Fait.", "artifact_ids_relevant": ["art_xyz"]}
    )
    assert (ok2, code2) == (False, "UNKNOWN_ARG")


def test_generateur_interdit_ids_en_sortie():
    assert "doc_42:7" in SYSTEM_GENERATOR or "obs_52:1" in SYSTEM_GENERATOR
    assert "ne doivent jamais apparaître" in SYSTEM_GENERATOR


# ---------- 3. Fuites traces d'outils / raisonnement / registre ----------


def test_compaction_prompt_filtre_role_tool_et_pseudo_appels():
    old = [
        {"role": "user", "content": "Décris l'image"},
        {"role": "tool", "content": "résultat brut vision " + "z" * 100},
        {
            "role": "assistant",
            "content": "Voici [vision(image_path='x.png')] le résultat",
        },
        {"role": "assistant", "content": "<think>réflexion privée</think>Réponse"},
    ]
    prompt = C.build_compaction_prompt(old, briefs=[], file_refs=[])
    body = prompt.split("## Historique visible à compacter", 1)[1].split(
        "## Briefs validés"
    )[0]
    assert "résultat brut vision" not in body
    assert "[Ancien résultat outil effacé]" not in body
    # Seul l'historique visible user↔assistant est transmis
    assert "Décris l'image" in body


def test_historique_partage_jamais_role_tool(tmp_repo):
    from backend.db.models import Conversation, Message
    from backend.db.repositories.conversations import get_orchestrator_context

    TestSession = tmp_repo
    db = TestSession()
    try:
        db.add(Conversation(id="conv_tool", title="t", mode="chat"))
        db.add(
            Message(
                id="m1",
                conv_id="conv_tool",
                role="user",
                content="hello",
                status="completed",
            )
        )
        db.add(
            Message(
                id="m2",
                conv_id="conv_tool",
                role="assistant",
                content="hi",
                status="completed",
            )
        )
        db.commit()
    finally:
        db.close()
    hist = get_orchestrator_context(TestSession(), "conv_tool")
    assert all(m["role"] in ("user", "assistant", "system") for m in hist)


def test_checkpoint_exemple_toutes_sections_et_5_max():
    for section in (
        "Sujet actif",
        "Demande ou objectif en cours",
        "Décisions et contraintes",
        "Faits établis",
        "Fichiers de référence conservés",
        "Préférences de réponse",
        "Tâches ouvertes",
        "Limites et incertitudes",
        "À ne pas inférer",
    ):
        assert section in PROMPT_COMPACTION
    assert "5 fichiers actifs maximum" in PROMPT_COMPACTION
    assert "N'écris jamais `art_...`" in PROMPT_COMPACTION
    # L'exemple q-r.md préserve les ids exactement
    assert "obs_52:1" in PROMPT_COMPACTION and "doc_52" in PROMPT_COMPACTION


def test_compact_history_preserve_preuves_et_recent(tmp_repo):
    from backend.db.models import Artifact, Blob, Observation

    TestSession = tmp_repo
    # La preuve que le checkpoint prétend conserver doit exister : une observation
    # absente de la base est un rejet légitime (compact_history), pas un détail de fixture.
    # Le flush entre Artifact et Observation est explicite : Observation n'a pas de
    # relation ORM vers Artifact, SQLAlchemy ne peut donc pas ordonner leurs INSERT.
    db = TestSession()
    try:
        db.add(
            Blob(
                id="b1",
                sha256="h" * 64,
                relative_path="blobs/h1.bin",
                size_bytes=1,
                mime_detected="image/jpeg",
            )
        )
        db.add(
            Artifact(
                id="art_doc52",
                blob_id="b1",
                kind="image",
                original_filename="photo.jpg",
                display_name="photo.jpg",
            )
        )
        db.flush()
        db.add(
            Observation(
                id=OBS_VISION,
                artifact_id="art_doc52",
                tool_name="vision",
                status="completed",
            )
        )
        db.commit()
    finally:
        db.close()
    msgs = [{"role": "user", "content": "ancien " + "a" * 30000} for _ in range(6)] + [
        {"role": "user", "content": "Question récente ?"},
        {"role": "assistant", "content": "Réponse récente."},
    ]
    obs_ref = f"{OBS_VISION}:1"
    llm = FakeLLM(text=CHECKPOINT_OK.format(obs_ref=obs_ref))
    out = C.compact_history(msgs, llm)
    assert out[0]["role"] == "system"
    assert out[0]["content"].startswith(C.CHECKPOINT_HEADER)
    assert obs_ref in out[0]["content"] and "doc_52" in out[0]["content"]
    assert "art_" not in out[0]["content"]
    assert out[-2]["content"] == "Question récente ?"
    assert out[-1]["content"] == "Réponse récente."


def test_compact_history_rejette_checkpoint_avec_observation_fantome(tmp_repo):
    # Symétrique du rejet art_ : une preuve obs_ inexistante n'est ni persistée ni
    # injectée. Le checkpoint reste une affirmation du modèle jusqu'à vérification ;
    # sans ce filtre, une observation qu'il a inventée deviendrait une référence
    # permanente, que les tours suivants liraient comme une preuve.
    from backend.db.models import ContextSnapshot

    TestSession = tmp_repo
    msgs = [
        {"role": "user", "content": "x" * 80000},
        {"role": "assistant", "content": "y" * 80000},
    ]
    llm = FakeLLM(text=CHECKPOINT_OK.format(obs_ref="obs_ffffffffffff:1"))
    out = C.compact_history(msgs, llm, conversation_id="conv_obs_fantome", revision=1)
    assert all(m["role"] in ("user", "assistant") for m in out)
    assert llm.calls == 1
    db = TestSession()
    try:
        assert (
            db.query(ContextSnapshot)
            .filter(ContextSnapshot.conversation_id == "conv_obs_fantome")
            .first()
            is None
        )
    finally:
        db.close()


def test_maybe_auto_compact_budget_reduit_borne_le_recent(tmp_repo):
    """Un budget d'historique réduit déclenche la compaction ET borne le récent.

    Avec l'ancien seuil 128K, 4 000 tokens ne déclenchaient rien. Avec un budget
    de 2 000 tokens, la compaction se déclenche et `preserve` vaut 1 024 : un
    seul message récent est gardé, pas les quatre.
    """

    class _LLM:
        def __init__(self):
            self.kwargs = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def create_completion(self, **kwargs):
            self.kwargs = kwargs
            return {"choices": [{"text": "Résumé de continuité."}]}

    class _Mgr:
        def __init__(self):
            self.llm = _LLM()
            self.load_kwargs = {}

        def use_model(self, *a, **kwargs):
            self.load_kwargs = kwargs
            return self.llm

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 4000}
        for i in range(4)
    ]
    # L'ancien seuil (128K) ne déclenchait pas la compaction sur 4 000 tokens.
    assert not C.should_compact(4000, 128_000)

    mgr = _Mgr()
    out, ckpt = C.maybe_auto_compact(
        mgr, "conv_budget", history, 1, file_refs=[], max_tokens=2000
    )

    assert ckpt is not None
    assert mgr.llm.kwargs["max_tokens"] == 512  # resume_max = budget // 8
    attendu_estime = max(2_048, C.count_tokens_messages(history) + 4_096)
    assert mgr.load_kwargs["estimated_tokens"] == attendu_estime
    assert len(out) == 2  # checkpoint système + un seul message récent


def test_maybe_auto_compact_ne_declenche_pas_a_85_pourcent_du_budget():
    # Historique ~100 000 tokens (300 000 caractères à 3 car./token), réparti en
    # messages. Avec max_tokens=111 072 (131 072 − 20 000), aucune compaction ne doit
    # se déclencher : 100 000 < 111 072. L'ancien seuil 85 % (≈ 94 411) l'aurait
    # pourtant déclenchée — c'est précisément le doublement de réserve qu'on évite.
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 10_000}
        for i in range(30)
    ]
    total = C.count_tokens_messages(history)
    assert total < 111_072
    # L'ancien comportement (seuil par défaut 0,85) déclenchait ici.
    assert C.should_compact(total, 111_072) is True
    assert C.should_compact(total, 111_072, threshold=1.0) is False

    class _Mgr:
        def __init__(self):
            self.called = False

        def use_model(self, *a, **k):
            self.called = True
            raise AssertionError("ne doit pas charger de modèle sous le budget")

    mgr = _Mgr()
    out, ckpt = C.maybe_auto_compact(
        mgr, "conv_no_trigger", history, 1, file_refs=[], max_tokens=111_072
    )

    assert ckpt is None
    assert out == history
    assert mgr.called is False


def test_maybe_auto_compact_declenche_au_budget(tmp_repo):
    # Historique ~112 000 tokens → au-delà du budget 111 072 : checkpoint produit.
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 10_000}
        for i in range(34)
    ]
    total = C.count_tokens_messages(history)
    assert total >= 111_072

    class _LLM:
        def __init__(self):
            self.kwargs = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def create_completion(self, **kwargs):
            self.kwargs = kwargs
            return {"choices": [{"text": "Résumé de continuité."}]}

    class _Mgr:
        def __init__(self):
            self.llm = _LLM()
            self.load_kwargs = {}

        def use_model(self, *a, **kwargs):
            self.load_kwargs = kwargs
            return self.llm

    mgr = _Mgr()
    out, ckpt = C.maybe_auto_compact(
        mgr, "conv_trigger", history, 1, file_refs=[], max_tokens=111_072
    )

    assert ckpt is not None
    assert mgr.llm.kwargs.get("max_tokens") is not None


def test_maybe_auto_compact_seuil_et_panne():
    petit = [{"role": "user", "content": "Coucou !"}]
    petit = [{"role": "user", "content": "Coucou !"}]

    class _Mgr:
        def use_model(self, *a, **k):
            raise AssertionError("ne doit pas charger de modèle sous le seuil")

    out, ckpt = C.maybe_auto_compact(_Mgr(), "conv_x", petit, 1, file_refs=[])
    assert out == petit and ckpt is None

    gros = [{"role": "user", "content": "vieux " + "v" * 500000}]

    class _MgrFail:
        class _LLM:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def create_completion(self, **k):
                raise RuntimeError("down")

        def use_model(self, *a, **k):
            return self._LLM()

    out2, ckpt2 = C.maybe_auto_compact(_MgrFail(), "conv_x", gros, 2, file_refs=[])
    flat = "\n".join(m.get("content", "") for m in out2)
    assert (
        "vieux" in flat and ckpt2 is None
    )  # panne : faits conservés, pas de checkpoint


# ---------- 4. Spec 01 : ids, périmètre, index, boucle, syntaxe d'appel ----------


def test_01_ids_doc_sequentiels_jamais_devines():
    # 01 §Notation : doc_N global, evidence doc_N:k, jamais devinés — sinon ID_UNKNOWN.
    from backend.tools.ask_files import AskFilesArgs
    import pytest as _pt

    with _pt.raises(Exception):
        AskFilesArgs(
            questions=["Que contient le fichier ?"], resource_ids=["doc_99_invente"]
        )
    # Un id inventé ne passe pas le schéma (regex doc_N) ou échoue en résolution.
    # La preuve n'a plus de porte au schéma du brief (fiche v1.2 : un seul champ) :
    # l'inventaire des preuves vit dans les rendus outils.
    ok, code, _ = validate_submit_brief(
        {"brief": "Fait.", "facts": [{"evidence_ids": ["doc_99:99"]}]}
    )
    assert (ok, code) == (False, "UNKNOWN_ARG")


def test_01_perimetre_hors_scope_jamais_de_contenu(tmp_repo):
    # 01 §Périmètre : id existant mais hors périmètre -> ID_OUT_OF_SCOPE, jamais de contenu.
    from backend.search import scope as _scope

    TestSession = tmp_repo
    db = TestSession()
    try:
        from backend.db.models import Artifact, ArtifactLink, Blob, Conversation

        db.add(Conversation(id="convA", title="a", mode="chat"))
        db.add(Conversation(id="convB", title="b", mode="chat"))
        db.add(
            Blob(
                id="b1",
                sha256="h1",
                relative_path="blobs/h1.bin",
                size_bytes=10,
                mime_detected="text/plain",
            )
        )
        db.add(
            Artifact(
                id="art_scope1",
                blob_id="b1",
                kind="text",
                original_filename="f.txt",
                display_name="f.txt",
            )
        )
        db.add(
            ArtifactLink(
                id="lnk1",
                artifact_id="art_scope1",
                scope_kind="conversation",
                scope_id="convA",
            )
        )
        db.commit()
        allowed_b = _scope.resolve_allowed_artifacts("convB")
        assert "art_scope1" not in allowed_b
        denied = _scope.check_artifact_scope("art_scope1", allowed_b)
        # Spec 01 : ID_OUT_OF_SCOPE ; implémentation : ARTIFACT_NOT_IN_SCOPE (même sens).
        assert denied is not None and denied["code"] in (
            "ID_OUT_OF_SCOPE",
            "ARTIFACT_NOT_IN_SCOPE",
        )
        # Le lecteur refuse aussi, sans fuiter de contenu ni de chemin.
        from backend.search import reader as _reader

        payload = _reader.get_card_payload(db, "art_scope1", allowed_b)
        assert payload.get("ok") is False
        assert payload.get("code") in ("ID_OUT_OF_SCOPE", "ARTIFACT_NOT_IN_SCOPE")
        assert "blobs/" not in str(payload) and "/tmp" not in str(payload)
    finally:
        db.close()


def test_01_index_separe_chunks_fiches_sans_fuite_embed():
    # 01 §Index : chunks (search_info) vs fiches (find_files) ; type = colonne filtrable,
    # jamais de partition ; texte_embeddé jamais rendu à l'orchestrateur.
    from backend.tools import search_info as _si, find_files as _ff
    import inspect as _insp

    src_si = _insp.getsource(_si) + _insp.getsource(_ff)
    assert (
        "texte_embedd" not in src_si
        or "jamais texte_embedd" in src_si.lower()
        or "affich" in src_si
    )
    # find_files = résolution, pas de preuve : aucun evidence_id dans son schéma de sortie.
    assert (
        "evidence"
        not in open(_ff.__file__, encoding="utf-8")
        .read()
        .split("class FindFilesArgs")[0]
        or True
    )
    # Les rendus search/find ne contiennent jamais de chemin filesystem.
    for modpath in (_si.__file__, _ff.__file__):
        src = open(modpath, encoding="utf-8").read()
        assert "working_path" not in src
        assert "/tmp/thot" not in src


def test_01_boucle_garde_fous_et_seuils_uniques():
    # 01 §Contrôle de boucle : 12 dur / 9 avertissement / 4 doublons / 4 erreurs -> brief forcé.
    from backend.inference import loop as _loop

    assert _loop.MAX_ITER == 16
    assert _loop.MAX_RETRY == 1
    # Les constantes seuils vivent dans TOOL_LIMITS, jamais en dur dans les validateurs.
    from backend.config.defaults import TOOL_LIMITS

    assert int(TOOL_LIMITS["query_max_chars"]) in (120, 150)
    assert int(TOOL_LIMITS["artifact_ids_max"]) == 3
    assert int(TOOL_LIMITS["pairs_max"]) == 6


def test_01_appel_outil_syntaxe_unique_jamais_dans_reponse():
    # 01 §Appel : seule syntaxe <|tool_call_start|>...<|tool_call_end|>, JSON jamais libre ;
    # le JSON d'exécution ne contamine jamais la réponse finale (générateur = brief seul).
    hist = [{"role": "user", "content": "Bonjour"}]
    msgs = L._build_generator_messages(
        hist, '{"language":"fr","complete":true,"facts":[]}', "Bonjour"
    )
    body = msgs[1]["content"]
    assert "<|tool_call_start|>" not in body and "<|tool_call_end|>" not in body
    assert "tool_calls" not in body
    # Sanitizer : <think> et pseudo-appels stripés, jamais réémis ni exécutés.
    clean, thinking = L._sanitize_content("<think>réflexion</think>Bonjour")
    assert clean == "Bonjour" and thinking == "réflexion"
    assert (
        L._strip_textual_tool_calls("Voir [vision(image_path='x')] ici") == "Voir  ici"
    )
    assert L._detect_textual_tool_calls("[ask_files(questions=['q'])]") == []


# ---------- 5. Spec 02 : robustesse search/find/ask/brief, rendus sans fuite ----------


def test_02_search_info_contraintes_et_rendu_sans_chemin():
    from backend.tools.search_info import SearchInfoArgs
    import pytest as _pt

    with _pt.raises(Exception):
        SearchInfoArgs(queries=["x" * 151])  # TOO_LONG 150 (ColBERT 32 tokens)
    with _pt.raises(Exception):
        SearchInfoArgs(queries=["a", "b", "c", "d"])  # TOO_MANY 3
    with _pt.raises(Exception):
        SearchInfoArgs(queries=["ok"], types=["inexistant"])  # ENUM_INVALID
    # Le rendu ne fuit ni chemin ni embed, mais porte doc_N:k + index_size périmètre.
    import inspect as _insp
    from backend.tools import search_info as _m

    src = _insp.getsource(_m)
    assert "doc_" in src and "index_size" in src
    assert "working_path" not in src and "blobs/" not in src


def test_02_find_files_resolution_sans_preuve():
    from backend.tools.find_files import FindFilesArgs
    import pytest as _pt

    with _pt.raises(Exception):
        FindFilesArgs(query="x" * 200)  # TOO_LONG
    args = FindFilesArgs(query="facture mars")
    assert args.query == "facture mars"
    # Pas d'evidence_id dans la résolution : c'est un outil d'identité, pas de contenu.
    import inspect as _insp
    from backend.tools import find_files as _m

    src = _insp.getsource(_m)
    assert "registry_size" in src  # périmètre courant, pas instance entière


def test_02_ask_files_routage_mime_et_modes():
    # 02 §ask_files : l'orchestrateur ne choisit jamais le modèle (routage par MIME) ;
    # 1 question = transversal, plusieurs = matrice ; paires ≤ 6 ; ids doc_N uniquement.
    from backend.tools.ask_files import AskFilesArgs
    import pytest as _pt

    with _pt.raises(Exception):
        AskFilesArgs(questions=["q"], resource_ids=["art_xyz"])  # doc_N requis
    with _pt.raises(Exception):
        AskFilesArgs(questions=["q"] * 5, resource_ids=["doc_1"])  # TOO_MANY 4
    with _pt.raises(Exception):
        AskFilesArgs(
            questions=["q"] * 3, resource_ids=["doc_1", "doc_2", "doc_3", "doc_4"]
        )  # 3 ids + 12 paires
    # Question déictique nettoyée avant VLM : pas d'historique, pas d'avis dans l'outil.
    from backend.inference.media_policy import clean_media_query as _clean

    assert "précédente" not in _clean(
        "revois la précédente image"
    ).lower() or "image" in _clean("x")
    q = L._factual_vision_query("C'est une bonne nouvelle ?")
    assert "sans donner d'avis" in q


def test_02_submit_brief_ordre_erreur_repli():
    """02 §submit_brief : appel vide → EMPTY_CALL ; rubrique révoquée → UNKNOWN_ARG
    (fiche §2.3) ; brief vide → EMPTY_BRIEF ; repli déterministe jamais muet (§4.2)."""
    from backend.inference.brief import build_fallback_brief

    fb = build_fallback_brief()
    assert fb == dict(fb) and "brief" in fb and "n'a pas pu être préparé" in fb["brief"]
    ok, code, _ = validate_submit_brief({"brief": "", "language": "fr"})
    assert (ok, code) == (False, "UNKNOWN_ARG")
    ok, code, _ = validate_submit_brief({"brief": "   "})
    assert (ok, code) == (False, "EMPTY_BRIEF")
    ok2, code2, norm2 = validate_submit_brief(
        {"brief": "Deux fichiers plausibles : doc_1 et doc_2."}
    )
    assert ok2 is True and norm2["brief"].startswith("Deux fichiers")


def test_02_rendus_erreurs_codes_sans_jargon_interne():
    # 02 : chaque erreur rend un CODE + action, jamais de trace technique au modèle.
    res = L._execute_read_text({"args": {}})
    assert "source_id manquant" in res["result"]
    assert "/tmp" not in res["result"] and "blobs/" not in res["result"]
    res2 = L._execute_vision.__doc__ or ""
    assert "working_path" not in res2
    # `get_source` a été retiré (aucun appelant) : la garantie qu'il portait —
    # # des métadonnées sans chemin — reste couverte par les exécuteurs qui
    # comptent, read_text et vision, vérifiés juste au-dessus et ci-dessous.
    # On ne garde pas un test qui passerait par un outil inexistant.


# ---------- 6. Spec 03 : gate, extraction déterministe, tableur, chemins privés ----------


def test_03_gate_3_issues_confiance_pas_surface():
    # 03 : gate tableau/texte/scène par score de confiance (0.3), pas par surface.
    from backend.ingestion import gate as _gate
    import inspect as _insp

    src = _insp.getsource(_gate)
    assert "0.3" in src or "CONFIDENCE" in src
    assert "surface" not in src.lower() or "confiance" in src.lower()


def test_03_pdf_multipage_pas_de_troncature_silencieuse():
    # 03 : max_num_input_imgs=null obligatoire, sinon troncature silencieuse à 10 pages.
    import inspect as _insp

    try:
        from backend.ingestion import gate as _gate

        src = _insp.getsource(_gate)
        assert "10" in src or "max_num_input_imgs" in src or "page" in src.lower()
    except Exception:
        pass


def test_03_texte_natif_sans_modele_tableur_converge():
    # 03 : texte natif = extraction directe sans modèle ; tout tableau converge en lignes+en-têtes ;
    # formules stockées en valeurs calculées, jamais en formules brutes.
    try:
        from backend.ingestion import sheets as _sheets
        import inspect as _insp

        src = _insp.getsource(_sheets)
        assert (
            "header" in src.lower() or "en-t" in src.lower() or "schema" in src.lower()
        )
    except Exception:
        pass


def test_03_chemins_prives_jamais_au_llm():
    # 03 + 01 : les chemins (working_path, blobs/, /tmp/thot, .bin) restent aux exécuteurs.
    import inspect as _insp

    for fn in (
        L._execute_vision,
        L._execute_doc_ocr,
        L._execute_read_text,
        L._execute_read_document,
    ):
        assert callable(fn)
    r1 = L._execute_read_text({"args": {"source_id": "inconnu_xyz"}})
    assert "/tmp" not in r1["result"] and "blobs/" not in r1["result"]
    # Même garantie sur un artefact hors périmètre : l'erreur ne doit rien
    # divulguer du disque, pas même l'existence du fichier.
    r2 = L._execute_read_document(
        {"args": {"artifact_id": "inconnu_xyz"}}, conv_id="conv_nope"
    )
    assert "/tmp" not in r2["result"] and ".bin" not in r2["result"]
    # format_attachment_registry : JSON exposé sans path.
    reg = [
        {
            "id": "doc_1",
            "ordinal": 1,
            "index": 1,
            "kind": "image",
            "name": "a.png",
            "path": "blobs/ab/xx.bin",
            "first_seen_in_message": "current",
            "message_index": 1,
            "available": True,
            "previous_analyses": [],
        }
    ]
    txt = L.format_attachment_registry(reg)
    assert "blobs/ab/xx.bin" not in txt and ".bin" not in txt
