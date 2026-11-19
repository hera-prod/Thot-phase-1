"""Mise en situation compaction C06 — on fait écrire jusqu'à la limite, puis on vérifie.

Sans modèle/GPU : FakeLLM déterministe (extraction naïve des faits). Scénario :
1. Conversation qui grossit (faits anciens + blabla + résultats d'outils + récent).
2. Sous le seuil 85% de 128K → pas de compaction ; au-dessus → compaction.
3. Après compaction : résumé seul témoin de l'ancien (qualité : faits clés présents),
   15K récents intacts, vieux tool results prunés, tokens réduits.
4. Panne LLM → historique inchangé (jamais de perte).
5. Persistance ContextSnapshot quand conversation_id + revision fournis.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base
from backend.inference import compaction as C


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr("backend.db.database.SessionLocal", TestSession)
    return TestSession


class FakeLLM:
    """Simule le résumé : reprend les faits marqués FACT: (qualité mesurable sans modèle)."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def create_completion(self, prompt: str, max_tokens: int = 2048, temperature: float = 0.3):
        self.calls += 1
        if self.fail:
            raise RuntimeError("LLM down")
        facts = [ln.strip() for ln in prompt.splitlines() if "FACT:" in ln]
        body = " ; ".join(facts[:20]) if facts else "conversation générale"
        return {"choices": [{"text": f"Résumé : {body}"}]}


def _msg(role: str, chars: int, tag: str = "") -> dict:
    return {"role": role, "content": f"{tag} " + "x" * max(0, chars - len(tag) - 1)}


def _situation() -> list[dict]:
    """Écrit jusqu'à dépasser le seuil : vieux faits + outils volumineux + récent à garder."""
    msgs: list[dict] = [
        {"role": "user", "content": "FACT: le client Dubois SARL, CA T2 45 000 €."},
        {"role": "assistant", "content": "FACT: décision — relancer Dubois avant le 30/09."},
        {"role": "user", "content": "FACT: facture f.xlsx, échéance 2026-03-31."},
    ]
    # Blabla ancien pour gonfler (dépasse 85% de 128K ≈ 108 800 tokens)
    for i in range(16):
        msgs.append(_msg("user", 20_000, tag=f"ancien {i}"))
    # Vieux résultats d'outils (dépassent PRUNE_PROTECT 40K tokens)
    for i in range(8):
        msgs.append({"role": "tool", "content": f"résultat outil ancien {i} " + "y" * 30_000})
    # Récent à préserver intact (sous PRESERVE_RECENT 15K tokens)
    msgs += [
        {"role": "user", "content": "Et pour Martin, c'est combien ?"},
        {"role": "assistant", "content": "Je vérifie la ligne Martin sur la facture."},
    ]
    return msgs


def test_seuil_85p_de_128k():
    assert C.should_compact(10_000) is False
    assert C.should_compact(int(128_000 * 0.85)) is True
    assert C.should_compact(int(128_000 * 0.85) - 1) is False


def test_situation_complete_compaction_qualite():
    msgs = _situation()
    total = C.count_tokens_messages(msgs)
    assert C.should_compact(total) is True  # on a bien écrit jusqu'à la limite

    llm = FakeLLM()
    out = C.compact_history(msgs, llm, conversation_id=None, revision=None)
    assert llm.calls == 1
    # 1. Un seul système checkpoint en tête, récent intact en fin (q-r.md §Prompt 3)
    assert out[0]["role"] == "system" and out[0]["content"].startswith(C.CHECKPOINT_HEADER)
    assert out[-2]["content"] == "Et pour Martin, c'est combien ?"
    assert out[-1]["content"] == "Je vérifie la ligne Martin sur la facture."
    # 2. Qualité : les 3 faits anciens survivent dans le résumé (seul contexte restant)
    for fact in ("Dubois SARL", "45 000", "30/09", "f.xlsx"):
        assert fact in out[0]["content"]
    # 3. Réduction massive de tokens, vieux blabla effacé du verbatim
    assert C.count_tokens_messages(out) < total // 4
    assert not any("ancien 3" in m.get("content", "") for m in out)
    # 4. Prune + split : les plus vieux outils sont effacés ; seuls d'éventuels
    # outils tout récents (fenêtre 15K) peuvent subsister en clair
    assert "résultat outil ancien 0" not in "\n".join(m.get("content", "") for m in out)


def test_prune_seul_protege_recent():
    msgs = [{"role": "tool", "content": "vieux " + "a" * 200_000},
            {"role": "tool", "content": "récent " + "b" * 1_000}]
    pruned = C.prune_old_tool_results(msgs[:])
    assert pruned[0]["content"] == "[Ancien résultat outil effacé]"
    assert "récent" in pruned[1]["content"]


def test_panne_llm_facts_conserves_prune_seule():
    """Panne LLM : pas de résumé, mais le prune défensif a déjà effacé les vieux outils.
    Garanti : aucun fait user/assistant ni le récent n'est perdu."""
    msgs = _situation()
    out = C.compact_history(msgs, FakeLLM(fail=True))
    flat = "\n".join(m.get("content", "") for m in out if m.get("role") in ("user", "assistant"))
    for fact in ("Dubois SARL", "f.xlsx", "Et pour Martin"):
        assert fact in flat
    assert out[-2]["content"] == "Et pour Martin, c'est combien ?"
    assert out[-1]["content"] == "Je vérifie la ligne Martin sur la facture."


def test_petite_conversation_inchangee():
    msgs = [{"role": "user", "content": "Coucou !"}, {"role": "assistant", "content": "Bonjour !"}]
    llm = FakeLLM()
    assert C.compact_history(msgs, llm) == msgs
    assert llm.calls == 0  # rien à résumer → aucun appel


# ---------- Injection du checkpoint dans le contexte (q-r.md §matrice) ----------

def _hist(n: int, size: int = 4000) -> list[dict]:
    return [{"id": f"m{i}", "role": "user" if i % 2 else "assistant",
             "content": f"message {i} " + "x" * size} for i in range(n)]


def test_apply_sans_snapshot_inchange():
    h = _hist(3)
    out, ckpt = C.apply_snapshot_to_history(h, None)
    assert out == h and ckpt is None


def test_apply_coupe_apres_covered():
    h = _hist(6)
    snap = {"summary": "# CHECKPOINT_CONVERSATION\nancien résumé", "covered_through_message_id": "m3"}
    out, ckpt = C.apply_snapshot_to_history(h, snap)
    assert [m["id"] for m in out] == ["m4", "m5"]
    assert ckpt is not None
    assert ckpt.startswith(C.CHECKPOINT_HEADER) and "ancien résumé" in ckpt


def test_apply_fenetre_recente_sans_covered():
    h = _hist(10)  # 10 × ~1000 tokens > fenêtre 15K ? non → agrandit
    big = _hist(30, size=4000)  # ~30K tokens > 15K
    snap = {"summary": "résumé ancien", "covered_through_message_id": None}
    out, ckpt = C.apply_snapshot_to_history(big, snap)
    assert ckpt is not None and len(out) < len(big)
    assert out[-1]["id"] == "m29"  # récent intact en fin
    # Petit historique sans covered → pas de duplication
    out2, ckpt2 = C.apply_snapshot_to_history(h, snap)
    assert out2 == h and ckpt2 is None


def test_apply_deja_injecte():
    h = [{"role": "system", "content": C.CHECKPOINT_HEADER + "\nrésumé"},
         {"role": "user", "content": "suite"}]
    out, ckpt = C.apply_snapshot_to_history(h, {"summary": "résumé"})
    assert out == h and ckpt is None


def test_generateur_voit_checkpoint_dans_historique():
    from backend.inference import loop as L
    msgs = L._build_generator_messages(
        [{"role": "system", "content": C.CHECKPOINT_HEADER + "\nfait ancien : Dubois"},
         {"role": "user", "content": "Et pour Martin ?"}],
        '{"language":"fr","complete":true,"facts":[]}', "Et pour Martin ?")
    body = msgs[1]["content"]
    assert "[contexte antérieur]" in body and "Dubois" in body
    assert "<DEMANDE_UTILISATEUR>\nEt pour Martin ?\n</DEMANDE_UTILISATEUR>" in body


def test_contexte_orchestrateur_avec_snapshot(tmp_repo):
    from backend.db.models import ContextSnapshot, Conversation, Message
    from backend.db.repositories.conversations import get_orchestrator_context
    TestSession = tmp_repo
    db = TestSession()
    try:
        db.add(Conversation(id="conv_s", title="t", mode="chat"))
        for i in range(5):
            db.add(Message(id=f"m{i}", conv_id="conv_s",
                           role="user" if i % 2 == 0 else "assistant",
                           content=f"contenu {i}", status="completed"))
        db.add(ContextSnapshot(id="s1", conversation_id="conv_s", revision=3,
                               covered_through_message_id="m2",
                               summary="# CHECKPOINT_CONVERSATION\nfait ancien"))
        db.commit()
    finally:
        db.close()
    hist = get_orchestrator_context(TestSession(), "conv_s")
    assert hist[0]["role"] == "system" and "fait ancien" in hist[0]["content"]
    assert [m["content"] for m in hist[1:]] == ["contenu 3", "contenu 4"]
    assert all(set(m.keys()) == {"role", "content"} for m in hist)


def test_persistance_snapshot(tmp_repo):
    TestSession = tmp_repo
    msgs = _situation()
    out = C.compact_history(msgs, FakeLLM(), conversation_id="conv_c", revision=7)
    assert out[0]["role"] == "system"
    db = TestSession()
    try:
        from backend.db.models import ContextSnapshot
        snap = db.query(ContextSnapshot).filter(ContextSnapshot.conversation_id == "conv_c").first()
        assert snap is not None and snap.revision == 7
        assert "Dubois" in (snap.summary or "")
        # get_active_snapshot retrouve le dernier
        active = C.get_active_snapshot("conv_c")
        assert active is not None and active["revision"] == 7
    finally:
        db.close()
