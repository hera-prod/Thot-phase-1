# tools/base.py — socle minimal Phase 1 (fusion v0.8 tools/base.py simplifié)
# Notes FR : validation stricte à la frontière, pas de sanitization complexe (Phase 2)
# Supprimé v0.8 : permissions, sanitization, toolsets, sourceRef, attachedFiles — garde 2 tools chaleur
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ValidationError


class ToolContext(BaseModel):
    """Contexte d'exécution injecté — run_id + chemins temporaires."""

    run_id: str
    request_id: str
    image_paths: list[str] = []  # /tmp/thot/{request_id}/{hash}.ext


class ToolResult(BaseModel):
    ok: bool
    content: str = ""
    display_summary: str = ""
    error: str | None = None
    duration_ms: int = 0
    truncated: bool = False
    tool_name: str | None = None
    tool_call_id: str | None = None


class BaseTool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]
    timeout_s: float = 15.0  # Phase 1 : 15s max
    max_output_chars: int = 8_000

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }

    async def call(self, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started = time.monotonic()
        try:
            args = self.input_model.model_validate(raw_args)
        except ValidationError as exc:
            return ToolResult(
                ok=False,
                display_summary=f"{self.name}: arguments invalides",
                error=f"Arguments invalides: {exc.errors()}",
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        try:
            async with asyncio.timeout(self.timeout_s):
                result = await self.execute(args, ctx)
        except TimeoutError:
            return ToolResult(
                ok=False,
                display_summary=f"{self.name}: timeout",
                error=f"{self.name} timeout après {self.timeout_s}s.",
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                display_summary=f"{self.name}: erreur",
                error=f"{self.name} erreur: {type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                tool_name=self.name,
            )
        # Troncature Phase 1 (8K chars)
        content = result.content
        truncated = False
        if len(content) > self.max_output_chars:
            content = content[: self.max_output_chars] + f"\n\n[Tronqué : {len(content)}→{self.max_output_chars} chars]"
            truncated = True
        return result.model_copy(update={"content": content, "truncated": truncated, "duration_ms": int((time.monotonic() - started) * 1000), "tool_name": self.name})

    @abstractmethod
    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError
