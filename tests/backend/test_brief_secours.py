"""Brief de secours — tâche 5.3 : le repli porte les cinq éléments du brief.

Fiches : orchestration.md §4.3 (brief automatique) et submit_brief.md §4.1-§4.3
(brief automatique). Un tour dont le relais n'a pas abouti doit exposer, dans le
texte du brief, les cinq éléments attendus — objectif initial, éléments déjà
obtenus, sources disponibles, opérations en échec, ce qui reste indéterminé — sans
nouvelle inférence et sans appel de modèle.
"""

from backend.inference.brief import build_fallback_brief, render_brief


def _tour_partiel() -> dict:
    """Un tour avec des résultats partiels ET un outil en échec."""
    return {
        "objective": "Comparer les contrats A et B.",
        "obtained": ["Le contrat A indique un préavis de trente jours."],
        "sources": ["doc_42:0"],
        "failed_operations": [
            "[TIMEOUT] lecture de la clause du contrat B interrompue"
        ],
        "indeterminate": ["préavis du contrat B non établi"],
    }


def _tour_vide() -> dict:
    """Un tour sans résultat : objectif initial + indéterminé honnête."""
    return {
        "objective": "Quel est le délai de résiliation du contrat Alpha ?",
        "obtained": [],
        "sources": [],
        "failed_operations": [],
        "indeterminate": [],
    }


def test_brief_replie_porte_cinq_elements_avec_resultats_partiels_et_echec():
    """Brief de secours d'un tour partiel : les cinq éléments de §4.3 dans le texte."""
    fb = build_fallback_brief(_tour_partiel())

    # Sortie conforme à submit_brief v1.2 §2.2 : un seul champ, le texte porte les éléments.
    assert set(fb) == {"brief"}
    texte = render_brief(fb)
    assert texte

    # 1. Objectif initial.
    assert "Comparer les contrats A et B." in texte
    # 2. Éléments déjà obtenus (résultats partiels / preuves d'outils du tour).
    assert "préavis de trente jours" in texte
    # 3. Sources disponibles.
    assert "doc_42:0" in texte
    # 4. Opérations en échec (codes / erreurs du tour).
    assert "TIMEOUT" in texte
    # 5. Ce qui reste indéterminé.
    assert "préavis du contrat B" in texte


def test_brief_replie_tour_vide_objectif_et_indetermine_honneste():
    """Brief de secours d'un tour vierge : objectif initial + indéterminé honnête."""
    fb = build_fallback_brief(_tour_vide())
    texte = render_brief(fb)

    assert "Quel est le délai de résiliation du contrat Alpha ?" in texte
    assert "indéterminé" in texte.lower()


def test_brief_replie_sans_etat_utilise_le_replie_honneste():
    """Sans état fiable, le repli dit l'absence : le texte ne se tait pas (§4.2)."""
    fb = build_fallback_brief()
    assert set(fb) == {"brief"}
    assert "Le brief n'a pas pu être préparé" in fb["brief"]
