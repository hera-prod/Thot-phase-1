"""Résolution du mode de référence source pour l'outpainting (code pur, sans GPU)."""


def _get(env, key):
    if env is None:
        return None
    if isinstance(env, dict):
        return env.get(key)
    if hasattr(env, "get"):
        try:
            return env.get(key)
        except Exception:
            return None
    if isinstance(env, str) and key == "THOT_IMAGE_SOURCE_REFERENCE":
        return env
    return None


def resolve_source_reference_mode(mode, mask_active, env=None):
    """Renvoie ``none|original|work`` selon le mode, le masque et l'environnement."""
    explicit = _get(env, "THOT_IMAGE_SOURCE_REFERENCE")
    if isinstance(explicit, str):
        explicit = explicit.strip().lower()
    else:
        explicit = ""

    if explicit in {"none", "original", "work"}:
        raw = explicit
    else:
        mode_norm = (str(mode) if mode is not None else "").strip().lower()
        if mode_norm == "outpaint" and bool(mask_active):
            raw = "original"
        else:
            raw = "none"

    if str(_get(env, "THOT_IMAGE_KEEP_SOURCE_REFERENCE") or "").strip() == "1":
        raw = "work"
    if str(_get(env, "THOT_IMAGE_NO_SOURCE_REFERENCE") or "").strip() == "1":
        raw = "none"

    if not bool(mask_active):
        raw = "none"
    if raw not in {"none", "original", "work"}:
        raw = "none"
    return raw
