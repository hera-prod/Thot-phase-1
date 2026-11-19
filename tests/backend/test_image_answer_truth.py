"""La vérité image est/backend/, jamais dans la prose du 8B — et dans le bon ordre.

Quatre défauts mesurés en E2E sur le chemin réel, verrouillés ici :

1. Un tour où l'image A ÉTÉ publiée et attachée, dont le brief portait une limite
   générique contradictoire (« Le brief n'a pas pu être validé après plusieurs
   tentatives. »), produisait : « Je ne dispose pas des capacités nécessaires
   pour créer des images, donc je ne peux pas les générer. » Le fait `established`
   était exact ; le 8B a pesé la limite plus lourd. Le backend corrige désormais
   la négation — le MIROIR de `_without_unsupported_image_claims`.
2. Le refus « Selon le brief… » est une fuite d'échafaudage, pas une réponse.
3. La sonde VRAM était évaluée AVANT la libération du modèle texte — donc elle
   mesurait l'occupant (8B ≈ 5,3 Go) : une image par process, silencieusement.
4. Un `[SERVICE_UNAVAILABLE]` laissait l'effet en `uncertain` (empreinte bloquée
   à jamais) et renvoyait `[generate_image: [OUT_OF_MEMORY] …]`.
"""
from __future__ import annotations

import pytest

from backend.inference import loop as loop_mod

# --- 1. Le MIROIR : nier une image RÉELLEMENT publiée ----------------------

_PUBLISHED = {"ok": True, "image_published": True, "artifact_id": "art_ok"}


def _published_status() -> dict:
    return loop_mod._record_image_status(None, dict(_PUBLISHED))



# Les trois formulations réellement lues en E2E, plus leurs variantes.
MEASURED_DENIALS = (
    "Je ne dispose pas des capacités nécessaires pour créer des images, donc je ne peux pas les générer.",
    "Je ne peux pas générer d'image dans cet environnement.",
    "Je suis incapable de créer des images pour vous.",
    "Je n'ai pas la capacité de générer une image ici.",
)


# Formes que le 8B produit après un ÉCHEC d'outil suivi d'une RÉUSSITE : le
# présent d'incapacité ne suffit pas, et le passif conjugue deux fois le verbe
# (« n'a pas été générée »). C'est exactement ce que q-r.md a produit : un
# `image.generate` refusé pour `reference_ids`, puis réussi, et un brief
# contenant à la fois « image générée [établie] » et « n'a pas pu être terminée ».
#
# Le groupe nominal reste exigé : « je ne peux pas éditer les images » doit
# survivre (voir test_les_limites_legitimes_ne_sont_jamais_touchees).
RETRY_SHAPED_DENIALS = (
    "Je suis désolé, mais je n'ai pas pu générer l'image demandée en raison d'une limitation technique.",
    "Je n'ai pas pu créer l'image.",
    "L'image n'a pas pu être générée.",
    "La photo n'a pas été créée.",
    "L'illustration n'a pas été produite.",
    "Le portrait n'a pas été dessiné.",
)


@pytest.mark.parametrize("denial", RETRY_SHAPED_DENIALS)
def test_un_echec_suivi_d_une_reussite_ne_produit_pas_de_refus(denial):
    """Un outil qui échoue puis réussit ne doit jamais produire un refus final.

    C'est le cas de q-r.md : `image.generate` refusé pour `reference_ids`
    ([I2I_UNAVAILABLE]), puis réussi en 123 s, et un brief contenant à la fois
    « une image a été générée [établie] » et « n'a pas pu être terminée ». Le
    miroir ne voyait que le présent d'incapacité et laissait passer le passé.
    """
    status = _published_status()
    corrigée = loop_mod._with_image_truth_guards(denial, status)
    assert not loop_mod._IMAGE_DENIAL_RE.search(corrigée), denial
    assert corrigée == loop_mod._IMAGE_STATUS_FACT, \
        "une réponse entièrement faite de ce refus devient la VÉRITÉ du backend, pas de la prose"


@pytest.mark.parametrize("denial", MEASURED_DENIALS)
def test_un_tour_publie_ne_peut_pas_nier_l_image(denial):
    status = _published_status()
    corrigée = loop_mod._with_image_truth_guards(denial, status)
    assert not loop_mod._IMAGE_DENIAL_RE.search(corrigée), denial
    assert corrigée == loop_mod._IMAGE_STATUS_FACT, \
        "une réponse entièrement faite de ce refus devient la VÉRITÉ du backend, pas de la prose"


def test_limite_generique_contradictoire_ne_produit_plus_de_denial():
    """Le cas E2E complet : fait réservé exact + limite orchestrateur fausse."""
    brief = {"brief": "Le brief n'a pas pu être validé : la préparation a échoué."}
    status = _published_status()
    merged = loop_mod._with_image_status(brief, status)
    assert merged is not None
    assert loop_mod._IMAGE_STATUS_FACT in merged["brief"], \
        "la phrase image réservée doit survivre à la limite orchestrateur"
    denial = "Je ne dispose pas des capacités nécessaires pour créer des images, donc je ne peux pas les générer."
    assert loop_mod._with_image_truth_guards(denial, status) == loop_mod._IMAGE_STATUS_FACT


def test_une_reponse_partielle_ne_perd_que_le_refus():
    status = _published_status()
    réponse = "Voici l'image que j'ai créée. Je ne peux pas générer d'image dans cet environnement."
    corrigée = loop_mod._with_image_truth_guards(réponse, status)
    assert corrigée == "Voici l'image que j'ai créée."


# Le motif doit rester étroit : ces phrases sont des VÉRITÉS sur le produit.
LEGITIMATE_STATEMENTS = (
    "Je ne peux pas éditer une image existante dans ce moteur.",
    "Je ne sais pas faire d'animation à partir d'une image.",
    "Je ne peux pas retoucher les images, seulement en créer de nouvelles.",
    "L'image est dans la pièce jointe.",
    "Je peux vous décrire ce que montre l'image.",
    # Un refus Honnête : la seconde, elle, n'a pas été faite.
    "Voici l'image que j'ai créée. Je ne peux pas en générer une seconde.",
)


@pytest.mark.parametrize("phrase", LEGITIMATE_STATEMENTS)
def test_les_limites_legitimes_ne_sont_jamais_touchees(phrase):
    assert not loop_mod._IMAGE_DENIAL_RE.search(phrase), phrase
    status = _published_status()
    assert loop_mod._with_image_truth_guards(phrase, status) is phrase


def test_le_miroir_ne_touche_rien_quand_aucune_image_n_est_publiee():
    status = loop_mod._record_image_status(None, {"ok": False, "result": "[EFFECT_FAILED]"})
    denial = "Je ne peux pas générer d'image dans cet environnement."
    assert loop_mod._with_image_truth_guards(denial, status) is denial


def test_une_reponse_honnete_publiee_passe_octet_pour_octet():
    # Law 3 : le garde ne réécrit que ce qu'il doit corriger.
    status = _published_status()
    propre = "Voici l'image que j'ai créée, au format carré.\n\nTu veux une autre version ?"
    assert loop_mod._with_image_truth_guards(propre, status) is propre


# --- 2. Le générateur ne cite pas son échafaudage -------------------------

MEASURED_META_REFERENCES = (
    "Selon le brief, une image a été générée et publiée pour cette demande.",
    "Le brief indique qu'une image a été générée et publiée pour cette demande.",
    "D'après le brief, l'image montre un papillon en macro.",
    "Les données fournies mentionnent une image publiée.",
    "Le système a trouvé une image correspondant à la demande.",
)


@pytest.mark.parametrize("phrase", MEASURED_META_REFERENCES)
def test_la_reference_au_brief_disparait_de_la_reponse(phrase):
    corrigée = loop_mod._without_brief_meta_reference(phrase)
    assert not loop_mod._BRIEF_META_LEAD_RE.search(corrigée), corrigée
    assert "brief" not in corrigée.lower()


def test_le_rewrite_garde_le_fait_et_enleve_la_source():
    corrigée = loop_mod._without_brief_meta_reference(
        "Selon le brief, une image a été générée et publiée pour cette demande.")
    assert corrigée == "Une image a été générée et publiée pour cette demande."


@pytest.mark.parametrize("phrase", (
    "J'ai besoin d'un brief pour la réunion de demain.",
    "Voici mon plan : je rédige le brief en fin de semaine.",
    "Le rapport indique que la croissance est de trois pour cent cette année.",
))
def test_une_reponse_sans_auto_reference_passe_octet_pour_octet(phrase):
    assert loop_mod._without_brief_meta_reference(phrase) is phrase


def test_le_meta_reference_ne_touche_qu_une_tete_de_phrase():
    phrase = "Tu peux me donner un brief sur cepapillon, comme demandé."
    assert loop_mod._without_brief_meta_reference(phrase) is phrase


# La queue : une clause qui EXCUSE la phrase par le mécanisme, en fin de phrase.
MEASURED_META_CLAUSES = (
    "Une image a bien été générée et publiée pour votre demande, bien que le brief n'ait pas pu être validé après plusieurs tentatives.",
    "Voici le montant, même si la validation du système n'a pas abouti.",
    "Le chiffre est confirmé, quoique l'orchestrateur n'ait pas su le vérifier.",
    "Le total est confirmé, bien que la synthèse n'ait pas été validée.",
)


@pytest.mark.parametrize("phrase", MEASURED_META_CLAUSES)
def test_la_clause_qui_excuse_par_le_brief_disparait(phrase):
    corrigée = loop_mod._without_brief_meta_reference(phrase)
    assert "brief" not in corrigée.lower(), corrigée
    assert "plusieurs tentatives" not in corrigée, corrigée
    assert corrigée.endswith("."), f"la phrase conservée garde sa ponctuation : {corrigée!r}"
    assert not loop_mod._BRIEF_META_TRAIL_RE.search(corrigée), corrigée


# La concession seule ne suffit PAS : sans sujet-mécanisme, c'est une phrase
# normale sur un document, une demande, une image.
INNOCENT_CLAUSES = (
    "Voici le document demandé, même si la version finale reste à valider.",
    "J'ai réservé la table, bien que le système de réservation n'ait pas confirmé.",
    "L'image est générée, mais le contrôle qualité reste à faire.",
    "Voici le plan : je rédige le brief en fin de semaine.",
    "Le rapport indique que la croissance est de trois pour cent cette année.",
    "Tu peux me donner un brief sur cepapillon, comme demandé.",
    "Une image a été publiée, toutefois.",
    "L'image est jointe, bien que le contrôle qualité reste incertain.",
)


@pytest.mark.parametrize("phrase", INNOCENT_CLAUSES)
def test_une_concession_innocente_passe_octet_pour_octet(phrase):
    assert loop_mod._without_brief_meta_reference(phrase) is phrase


# --- 3. Une image PUBLIÉE se dit toujours (le sous-claim est un mensonge) ----

DESCRIPTIF_SANS_IMAGE = "Un chat tigré au regard vif, assis près d'une fenêtre, lumière douce."


def test_une_reponse_muette_sur_l_image_publiee_la_denonce():
    status = _published_status()
    corrigée = loop_mod._with_image_truth_guards(DESCRIPTIF_SANS_IMAGE, status)
    assert corrigée.startswith(loop_mod._IMAGE_STATUS_FACT), corrigée
    assert "chat" in corrigée, "la description du modèle doit survivre : on ajoute, on ne remplace pas"


def test_le_fait_reserve_est_ecrit_une_seule_fois():
    """Le brief et la réponse disent la MÊME chose : une seule source, pas deux copies."""
    status = _published_status()
    # Fiche v1.2 : le brief est un seul champ — le fait réservé l'ouvre,
    # la réconciliation n'invente pas une seconde forme structurée.
    brief = loop_mod._with_image_status(None, status)
    assert brief is not None
    assert brief["brief"].startswith(loop_mod._IMAGE_STATUS_FACT)
    assert loop_mod._IMAGE_STATUS_FACT in loop_mod._with_image_truth_guards(
        DESCRIPTIF_SANS_IMAGE, status)


def test_sans_publication_la_reponse_ne_mentionne_jamais_d_image():
    status = loop_mod._record_image_status(None, {"ok": False, "result": "[SERVICE_UNAVAILABLE]"})
    réponse = "Un chat tigré au regard vif, assis près d'une fenêtre."
    corrigée = loop_mod._with_image_truth_guards(réponse, status)
    assert "image" not in corrigée.lower(), "aucune image n'existe : rien ne doit en parler"
    assert "génér" not in corrigée.lower()


def test_une_reponse_qui_nomme_deja_l_image_passe_octet_pour_octet():
    status = _published_status()
    réponse = "Voici l'image que j'ai créée, au format carré.\n\nTu veux une autre version ?"
    assert loop_mod._with_image_truth_guards(réponse, status) is réponse


# --- 4. La marge VRAM se mesure APRÈS la libération du modèle texte --------

def _recording_manager(journal: list[str]):
    class Manager:
        def acquire_admission(self, run_id, timeout=None):
            journal.append("admission")
            return True

        def unload_all(self):
            journal.append("unload")

        def release_admission(self, run_id):
            journal.append("release")

    return Manager()


def test_ordre_admission_puis_dechargement_puis_sonde_vram(monkeypatch):
    """L'ORDRE est le contrat : inverser les deux lignes réintroduit le défaut.

    La sonde VRAM doit voir la mémoire LIBÉRÉE, sinon elle mesure l'occupant
    et refuse une génération qui aurait réussi.
    """
    from backend.services import flux_spike as _fx, image_service as svc

    journal: list[str] = []
    monkeypatch.setattr(svc, "check_install", lambda: journal.append("install") or (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: journal.append("vram") or (True, "ok"))

    def diffusion(*args, **kwargs):
        journal.append("t2i")
        raise RuntimeError("[STUB] diffusion arrêtée pour l'assertion d'ordre")

    monkeypatch.setattr(_fx, "run_t2i", diffusion)

    with pytest.raises(RuntimeError, match="STUB"):
        svc.generate(_recording_manager(journal), "run_ordre", "un papillon en macro")

    assert journal.index("unload") < journal.index("vram"), \
        f"le modèle texte doit être libéré AVANT la sonde VRAM : {journal}"
    assert journal[:4] == ["install", "admission", "unload", "vram"], journal


def test_un_refus_independant_de_la_memoire_ne_decharge_rien(monkeypatch):
    """Coût : une installation mal configurée ne doit PAS coûter un unload."""
    from backend.services import image_service as svc

    journal: list[str] = []
    monkeypatch.setattr(svc, "check_install", lambda: journal.append("install") or (False, "non activé"))
    monkeypatch.setattr(svc, "check_vram", lambda: journal.append("vram") or (True, "ok"))

    with pytest.raises(RuntimeError, match="SERVICE_UNAVAILABLE"):
        svc.generate(_recording_manager(journal), "run_refus", "un papillon en macro")

    assert journal == ["install"], journal


def test_la_sonde_vram_refuse_apres_le_dechargement(monkeypatch):
    from backend.services import flux_spike as _fx, image_service as svc

    journal: list[str] = []
    monkeypatch.setattr(svc, "check_install", lambda: (True, "ok"))
    monkeypatch.setattr(svc, "check_vram", lambda: (False, "VRAM libre insuffisante : 5.79 / 9.0 Go."))
    monkeypatch.setattr(_fx, "run_t2i", lambda *a, **k: pytest.fail("aucune diffusion si la VRAM manque"))

    with pytest.raises(RuntimeError, match=r"\[OUT_OF_MEMORY\].*5\.79 / 9\.0 Go"):
        svc.generate(_recording_manager(journal), "run_vram", "un papillon en macro")

    assert journal == ["admission", "unload", "release"], journal


# --- 4. Un seul niveau de tag d'erreur ------------------------------------

def test_le_tag_imbrique_est_aplati():
    plat = loop_mod._flat_error_tag("[generate_image: [OUT_OF_MEMORY] VRAM libre insuffisante : 5.79 / 9.0 Go.]")
    assert plat == "[OUT_OF_MEMORY] VRAM libre insuffisante : 5.79 / 9.0 Go."
    assert loop_mod._flat_error_tag(plat) is not None and loop_mod._flat_error_tag(plat) == plat


@pytest.mark.parametrize("brut,attendu", (
    ("[EFFECT_UNCERTAIN]", "[EFFECT_UNCERTAIN]"),
    ("[EFFECT_RUNNING]", "[EFFECT_RUNNING]"),
    ("[generate_image: [REPLAY_UNUSABLE: [REPLAY_UNUSABLE] boom]]", "[REPLAY_UNUSABLE] boom"),
    ("[BAD_ARGS] seed hors bornes", "[BAD_ARGS] seed hors bornes"),
))
def test_le_tag_unique_est_preserve(brut, attendu):
    assert loop_mod._flat_error_tag(brut) == attendu
