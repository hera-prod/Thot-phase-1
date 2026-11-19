# backend/inference/loop_guards.py — Garde-fous boucle orchestrateur (spec 01 § contrôle de boucle + 03 rappel)
# Un appel = une invocation d'outil (paramètres internes ne comptent pas : search_info×3 = 1 appel).
# Flux nominal : find_files (1) + search_info (1) + ask_files (1) + submit_brief (1) = 4 appels.
from __future__ import annotations

import hashlib
import json
import time

from backend.config.defaults import (
    DUPLICATE_WINDOW,
    MAX_CONSECUTIVE_ERRORS,
    MAX_DUPLICATE_CALLS,
    MAX_FAILING_CALLS_BEFORE_TOOL_BLOCK,
    MAX_GENERIC_CONSECUTIVE_FAILURES,
    MAX_SUCCESSFUL_TOOL_CALLS,
    MAX_TOOL_CALLS,
    MAX_TOOL_CALLS_BY_TOOL,
    WARN_MESSAGE,
    WARN_TOOL_CALLS,
)


class LoopGuard:
    """Compteur global au tour utilisateur (reset par tour). Journalise outil/hash/statut/durée."""

    def __init__(self) -> None:
        self.calls: list[dict] = []  # {tool, args_hash, ok, code, duration_ms, t}
        self.consecutive_errors = 0
        self.warned = False
        self.forced_reason: str | None = None
        # Nouveaux garde-fous (spec 04) — blocage d'outils et quotas.
        self.blocked_tools: set[str] = set()
        self._blocked_first_seen: set[str] = set()
        self._recent_failed_tools: list[str] = []
        self._tool_failure_counts: dict[str, int] = {}
        self._success_count: int = 0

    @staticmethod
    def args_hash(tool: str, args: dict) -> str:
        try:
            blob = json.dumps({"tool": tool, "args": args or {}}, sort_keys=True, ensure_ascii=False)
            return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        except Exception:
            return str(hash(str(args)))

    # ---------- blocage d'outils ----------
    def is_tool_blocked(self, tool: str) -> bool:
        return tool in self.blocked_tools

    def block_tool(self, tool: str, reason: str | None = None) -> None:
        """Bloque un outil et mémorise le premier blocage (non forçant)."""
        self.blocked_tools.add(tool)
        self._blocked_first_seen.add(tool)

    def mark_first_block(self, tool: str, reason: str | None = None) -> None:
        self.block_tool(tool, reason)

    def block_tool_now(self, tool: str, reason: str | None = None) -> None:
        self.block_tool(tool, reason)

    def mark_tool_blocked_first(self, tool: str, reason: str | None = None) -> None:
        self.block_tool(tool, reason)

    def force_brief(self, reason: str) -> str:
        self.forced_reason = reason
        return reason

    def mark_blocked_retry_attempt(self, tool: str) -> str | None:
        """Tentative sur un outil déjà bloqué.

        Retourne (et force) ``blocked_tool_retry`` si l'outil avait déjà été bloqué
        précédemment ; sinon aucune force.
        """
        if tool in self._blocked_first_seen or tool in self.blocked_tools:
            self.forced_reason = "blocked_tool_retry"
            return "blocked_tool_retry"
        return None

    def tool_call_count(self, tool: str) -> int:
        return sum(1 for c in self.calls if c.get("tool") == tool)

    def warn_message(self) -> str | None:
        """À WARN_TOOL_CALLS (9) : injection système explicite. Une seule fois par tour."""
        if len(self.calls) >= WARN_TOOL_CALLS and not self.warned:
            self.warned = True
            return WARN_MESSAGE
        return None

    def record(
        self,
        tool: str,
        args: dict,
        *,
        ok: bool,
        code: str | None = None,
        duration_ms: int = 0,
        blocked_retry: bool = False,
        block_tool_now: bool = False,
    ) -> str | None:
        """Enregistre un appel ; retourne un code de sortie forcée si applicable."""
        h = self.args_hash(tool, args or {})
        entry = {
            "tool": tool,
            "args_hash": h,
            "ok": bool(ok),
            "code": code or ("ok" if ok else "error"),
            "duration_ms": int(duration_ms),
            "t": time.time(),
        }

        # Cas special : tentative sur outil deja bloque → sortie forcee immediate.
        if blocked_retry:
            self.calls.append(entry)
            self.forced_reason = "blocked_tool_retry"
            return "blocked_tool_retry"

        self.calls.append(entry)

        # Blocage demande par l'appelant (sans force), applicable meme en succes.
        if block_tool_now:
            self.block_tool(tool, reason="blocked_now")

        # submit_brief n'est jamais compté dans les erreurs consécutives : c'est la sortie (spec 01)
        if ok:
            self.consecutive_errors = 0
            # reset par-outil failure count + recent streak
            self._tool_failure_counts[tool] = 0
            # recent streak reset on any success
            self._recent_failed_tools = []
            self._success_count += 1
            if self._success_count >= MAX_SUCCESSFUL_TOOL_CALLS:
                self.forced_reason = "max_successful_calls"
                return "max_successful_calls"
        elif tool != "submit_brief":
            self.consecutive_errors += 1
            self._recent_failed_tools.append(tool)
            if len(self._recent_failed_tools) > 3:
                self._recent_failed_tools = self._recent_failed_tools[-3:]
            self._tool_failure_counts[tool] = self._tool_failure_counts.get(tool, 0) + 1

            # Blocage par outil des que le quota d'echecs est atteint (pas de force ici).
            if self._tool_failure_counts.get(tool, 0) >= MAX_FAILING_CALLS_BEFORE_TOOL_BLOCK:
                self.block_tool(tool, reason="too_many_failures")

            # Au 3e echec consecutif, l'outil en echec est bloque (sans force yet).
            if self.consecutive_errors >= MAX_GENERIC_CONSECUTIVE_FAILURES:
                self.block_tool(tool, reason="consecutive_failures")

            # Decision de force pour les echecs generiques :
            # - outils distincts dans les 3 derniers echecs > 1 → force
            # - impossible de rattacher a un seul outil → force
            # - jamais pour un blocage pur par outil (meme outil repete) tant que
            #   le seuil global legacy n'est pas atteint.
            distinct = len(set(self._recent_failed_tools))
            force_generic = (
                self.consecutive_errors >= MAX_GENERIC_CONSECUTIVE_FAILURES
                and distinct != 1
            )
            force_legacy = self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS
            if force_generic or force_legacy:
                self.forced_reason = "errors"
                return "errors"

        # Duplication : 4 répétitions du même couple (outil, hash exact) dans fenêtre 8 → stagnation
        window = self.calls[-DUPLICATE_WINDOW:]
        same = sum(1 for c in window if c["tool"] == tool and c["args_hash"] == h)
        if same >= MAX_DUPLICATE_CALLS:
            self.forced_reason = "duplicate"
            return "duplicate"
        if len(self.calls) >= MAX_TOOL_CALLS:
            self.forced_reason = "max_calls"
            return "max_calls"
        return None

    def should_force_brief(self) -> bool:
        return self.forced_reason is not None

    def metrics(self) -> dict:
        return {"calls": len(self.calls), "consecutive_errors": self.consecutive_errors,
                "forced": self.forced_reason, "warned": self.warned}
