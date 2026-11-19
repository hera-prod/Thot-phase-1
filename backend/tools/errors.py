# tools/errors.py — Enum fermé unique des codes d'erreur outils (brouillon n°4)
# Table maîtresse : code → sens générique → action générique.
# Chaque section d'outil du brouillon référence ces codes, jamais de variantes locales.
from __future__ import annotations

from enum import StrEnum


class ToolErrorCode(StrEnum):
    """Codes fermés — spec 01 table maîtresse. Même sémantique partout."""

    EMPTY_CALL = "EMPTY_CALL"            # appel sans argument exploitable → reformuler avec args autorisés
    EMPTY_QUERY = "EMPTY_QUERY"          # champ contenu principal vide/absent → fournir contenu, pas retenter identique
    EMPTY_BRIEF = "EMPTY_BRIEF"          # submit_brief : brief vide sans clarification → redécrire brief
    MISSING_REQUIRED = "MISSING_REQUIRED"  # champ obligatoire absent sans alternative → renseigner
    UNKNOWN_ARG = "UNKNOWN_ARG"          # clé hors liste autorisée → retirer, n'utiliser que le documenté
    ENUM_INVALID = "ENUM_INVALID"        # valeur hors enum fermé → remplacer par valeur documentée
    TOO_LONG = "TOO_LONG"                # chaîne > max documenté → raccourcir puis retenter
    TOO_MANY = "TOO_MANY"                # nb éléments > limite → réduire, diviser en plusieurs appels
    ID_UNKNOWN = "ID_UNKNOWN"            # référence inconnue du registre → revérifier via find_files
    ID_OUT_OF_SCOPE = "ID_OUT_OF_SCOPE"  # référence existe mais hors périmètre conversation → ne pas retenter, retrouver via find_files
    EVIDENCE_UNKNOWN = "EVIDENCE_UNKNOWN"  # evidence_id non obtenu dans le tour → ne citer que preuves du tour
    TIMEOUT = "TIMEOUT"                  # dépassement budget temps → retenter portée réduite ou conclure
    TOO_LARGE = "TOO_LARGE"              # volume > capacité même ciblé → réduire portée (page/plage/zone)
    BAD_ARGS = "BAD_ARGS"                # résiduel structure/type pure non couverte par un code précis
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"  # ressource supprimée, non prête ou indisponible pour la lecture demandée
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"  # mode demandé non pris en charge
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"  # service ou moteur nécessaire indisponible
    MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"  # composant de modèle impossible à charger
    OUT_OF_MEMORY = "OUT_OF_MEMORY"      # incident de ressources mémoire identifié
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # budget applicatif nécessaire épuisé
    CANCELLED = "CANCELLED"              # annulation de l'opération
    INTERNAL_ERROR = "INTERNAL_ERROR"    # incident interne non attribué plus précisément


# Action générique rendue au modèle par code (le détail par outil vit dans chaque outil).
ERROR_ACTIONS: dict[ToolErrorCode, str] = {
    ToolErrorCode.EMPTY_CALL: "Reformule l'appel avec les arguments autorisés.",
    ToolErrorCode.EMPTY_QUERY: "Fournis le contenu attendu, ne retente pas à l'identique.",
    ToolErrorCode.EMPTY_BRIEF: "Remplis brief.",
    ToolErrorCode.MISSING_REQUIRED: "Renseigne le champ manquant.",
    ToolErrorCode.UNKNOWN_ARG: "Retire la clé, n'utilise que les arguments documentés.",
    ToolErrorCode.ENUM_INVALID: "Remplace par une valeur de l'enum documenté.",
    ToolErrorCode.TOO_LONG: "Raccourcis la chaîne, puis retente.",
    ToolErrorCode.TOO_MANY: "Réduis le nombre d'éléments, divise en plusieurs appels si besoin.",
    ToolErrorCode.ID_UNKNOWN: "Revérifie la référence, retrouve-la via find_files.",
    ToolErrorCode.ID_OUT_OF_SCOPE: "Ne retente pas cet id ; retrouve un fichier accessible via find_files.",
    ToolErrorCode.EVIDENCE_UNKNOWN: "Ne cite que des preuves obtenues dans cette conversation.",
    ToolErrorCode.TIMEOUT: "Retente avec une portée réduite, ou conclus avec les résultats déjà obtenus.",
    ToolErrorCode.TOO_LARGE: "Réduis la portée de la demande (page, plage, zone).",
    ToolErrorCode.BAD_ARGS: "Corrige la structure de l'appel.",
    ToolErrorCode.RESOURCE_UNAVAILABLE: "Vérifie l'existence de la ressource, puis sa disponibilité.",
    ToolErrorCode.UNSUPPORTED_CAPABILITY: "N'utilise pas ce mode ; choisis un mode documenté.",
    ToolErrorCode.SERVICE_UNAVAILABLE: "Le service n'est pas disponible : transmets la limite, ne retente pas à l'identique.",
    ToolErrorCode.MODEL_LOAD_FAILED: "Le modèle ne peut pas être chargé : transmets la limite.",
    ToolErrorCode.OUT_OF_MEMORY: "Ressources mémoire insuffisantes : réduis ou libère, retente si une ressource s'est libérée.",
    ToolErrorCode.BUDGET_EXHAUSTED: "Le budget applicatif est épuisé : conclus avec ce qui est disponible.",
    ToolErrorCode.CANCELLED: "L'opération a été annulée : n'insiste pas.",
    ToolErrorCode.INTERNAL_ERROR: "Incident interne : reformule la demande autrement ou transmets la limite.",
}


def error_text(code: ToolErrorCode, detail: str = "") -> str:
    """Texte rendu au modèle : [CODE] action + détail. Pur."""
    base = ERROR_ACTIONS.get(code, "Erreur.")
    suffix = f" {detail.strip()}" if (detail or "").strip() else ""
    return f"[{code}] {base}{suffix}"
