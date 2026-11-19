# tools/registry.py — Registre des outils, avec leur rôle déclaré
#
# DEUX CATÉGORIES, et la différence est structurante :
#
#   OUTILS ORCHESTRATEUR — proposés au modèle dans sa liste d'actions. Il les
#     choisit lui-même. Ce sont les seuls dont le nom doit lui être familier.
#
#   SOUS-OUTILS — jamais proposés. Ce sont des compétences que les outils
#     orchestrateur appellent en interne, selon le type de fichier reçu.
#     `ask_files` ne demande pas au modèle de choisir entre OCR, vision et
#     lecture : il route. Lui exposer les trois l'obligerait à deviner, et il se
#     tromperait — un OCR sur une illustration ne rend rien, une description
#     visuelle sur un tableau perd les chiffres.
#
# Un sous-outil retiré casse son parent. Un outil orchestrateur retiré devient
# invisible. Ce ne sont pas les mêmes opérations.
#
# `get_source` et `get_document_card` ont été retirés : aucun appelant. Ils ne
# servaient qu'à annoncer des actions que le modèle ne pouvait pas exécuter.
#
# AUCUN outil n'est enregistré sans rôle : un outil que rien n'appelle n'est
# pas une capacité, c'est du code qui promet sans être tenu de livrer.
from __future__ import annotations

from typing import Iterable

from backend.tools.base import BaseTool
from backend.tools.read_document import ReadDocumentTool
from backend.tools.vision_general import VisionGeneralTool
from backend.tools.vision_ocr import VisionOCRTool
from backend.tools.read_text import ReadTextTool
from backend.tools.search_documents import SearchDocumentsTool
from backend.tools.search_info import SearchInfoTool
from backend.tools.find_files import FindFilesTool
from backend.tools.ask_files import AskFilesTool
from backend.tools.generate_image import GenerateImageTool
from backend.tools.web_research import WebResearchTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def register_many(self, tools: Iterable[BaseTool]) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"Outil inconnu: {name}")
        return self._tools[name]

    def try_get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def definitions(self) -> list[dict]:
        """Définitions OpenAI function pour LLM. Filtre chat mode côté loop."""
        return [t.definition() for t in self._tools.values()]


def build_default_registry() -> ToolRegistry:
    """Construit le registre en séparant ce que le modèle voit de ce qu'il ne voit pas.

    L'ordre d'insertion est une convention lue par les tests : les définitions
    de découverte sont proposées dans cet ordre au re-ask non contraint.
    """
    registry = ToolRegistry()
    registry.register_many([SearchInfoTool(), FindFilesTool(), AskFilesTool()])
    registry.register(GenerateImageTool())
    registry.register(WebResearchTool())
    # Sous-outils : compétences appelées par les outils ci-dessus. Ni exposés au
    # modèle, ni protégés d'un appel direct — les retirer casserait `ask_files`.
    registry.register_many([
        VisionOCRTool(), VisionGeneralTool(),
        ReadDocumentTool(), ReadTextTool(), SearchDocumentsTool(),
    ])
    return registry


# Singleton paresseux
_DEFAULT_REGISTRY: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY
