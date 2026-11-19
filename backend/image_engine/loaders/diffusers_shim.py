"""Shim d'import diffusers — contourne `diffusers/loaders/__init__.py` inimportable.

Cause : `diffusers/loaders/__init__` -> single_file_model -> transformers (5.14.1)
-> sklearn -> scipy compile numpy1 vs numpy 2.5.3 de l'env ("numpy.dtype size changed").
Tout le reste de diffusers (models, schedulers utilises, VAE Flux2) s'importe bien.

Le shim pre-injecte un `diffusers.loaders` leger : les mixins ne servent ici que de
classes de base (aucun chargement single-file / LoRA dans ce pipeline).
"""
import sys
import types
from pathlib import Path

_SHIM_ATTR = "_THOT_SHIMMED"


def ensure_diffusers_loaders():
    mod = sys.modules.get("diffusers.loaders")
    if mod is not None and getattr(mod, _SHIM_ATTR, False):
        return mod
    if mod is not None:
        return mod  # vrai module deja charge (env sain) : ne pas ecraser
    mod = types.ModuleType("diffusers.loaders")
    setattr(mod, _SHIM_ATTR, True)

    class PeftAdapterMixin:
        pass

    class FromOriginalModelMixin:
        @classmethod
        def from_single_file(cls, *args, **kwargs):
            raise RuntimeError("from_single_file indisponible (shim diffusers.loaders)")

    mod.__dict__["PeftAdapterMixin"] = PeftAdapterMixin
    mod.__dict__["FromOriginalModelMixin"] = FromOriginalModelMixin

    # Sous-module `diffusers.loaders.single_file_model` (import direct par certains modeles)
    sfm = types.ModuleType("diffusers.loaders.single_file_model")
    sfm.__dict__["FromOriginalModelMixin"] = FromOriginalModelMixin
    sys.modules["diffusers.loaders.single_file_model"] = sfm

    def _shim_getattr(name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        # Tout autre mixin chargeur (UNet2DConditionLoadersMixin, ...) : dummy.
        # Echec bruyant seulement si on appelle vraiment un chargement single-file/LoRA.
        cls = type(name, (), {"__module__": "diffusers.loaders",
                              "__doc__": "stub via diffusers_shim (env numpy2)"})
        mod.__dict__[name] = cls
        return cls

    mod.__dict__["__getattr__"] = _shim_getattr
    sys.modules["diffusers.loaders"] = mod

    # `diffusers/models/autoencoders/__init__.py` est EAGER (autoencoder_rae ->
    # transformers dinov2 -> scipy casse). Stub package avec le __path__ reel :
    # `vae` et `autoencoder_kl_flux2` se chargent fichier par fichier sans le __init__.
    import diffusers
    hooks_path = str(Path(diffusers.__file__).parent / "hooks")
    hooks_pkg = types.ModuleType("diffusers.hooks")
    hooks_pkg.__dict__["__path__"] = [hooks_path]
    sys.modules["diffusers.hooks"] = hooks_pkg
    ae_path = str(Path(diffusers.__file__).parent / "models" / "autoencoders")
    pkg = types.ModuleType("diffusers.models.autoencoders")
    pkg.__dict__["__path__"] = [ae_path]
    sys.modules["diffusers.models.autoencoders"] = pkg
    return mod
