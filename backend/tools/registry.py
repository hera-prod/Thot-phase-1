# tools/registry.py — Registre Phase 1 (2 outils seulement, pas de toolset complexe)
# Notes FR : pas de policy/permissions (Phase 2), séquentiel 1 tool/iter
# Supprimé v0.8 : 8 outils, ToolsetProfile, cache, approval — garde vision+doc_ocr
from __future__ import annotations

from typing import Iterable

from backend.tools.base import BaseTool
from backend.tools.vision_general import VisionGeneralTool
from backend.tools.vision_ocr import VisionOCRTool
from backend.tools.get_source import GetSourceTool
from backend.tools.read_text import ReadTextTool
from backend.tools.search_documents import SearchDocumentsTool


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
    """Registre Phase 1 + annexe 11 Lot A : vision + doc_ocr + get_source + read_text + search_documents."""
    registry = ToolRegistry()
    registry.register_many([VisionGeneralTool(), VisionOCRTool(), GetSourceTool(), ReadTextTool(), SearchDocumentsTool()])
    return registry


# Singleton paresseux
_DEFAULT_REGISTRY: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY
