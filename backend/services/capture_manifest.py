"""Validation d'un manifeste de capture image (P-12).

Ne lève jamais : un manifeste invalide renvoie (False, [raisons]).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_RUN_TYPES = {"model_inference", "synthetic_sentinel"}
_CAPTURE_STATUSES = {"complete", "incomplete", "invalid"}
_ALLOWED_ORIGINS = {
    "studio",
    "chat-tool",
    "tool-direct",
    "model_inference",
    "synthetic_sentinel",
}


def manifest_valid(manifest: Any, directory: Any) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    try:
        if not isinstance(manifest, dict):
            return False, ["invalid_manifest"]
        base = Path(directory)

        run_type = manifest.get("run_type")
        if run_type not in _RUN_TYPES:
            reasons.append("unknown_run_type")

        capture_status = manifest.get("capture_status")
        if capture_status not in _CAPTURE_STATUSES:
            reasons.append("unknown_capture_status")

        files = manifest.get("files")
        if not isinstance(files, dict):
            reasons.append("missing_files")
            files = {}

        for name, entry in (files or {}).items():
            key = str(name)
            path = base / key
            if not isinstance(entry, dict):
                reasons.append(f"invalid_file_entry:{key}")
                continue
            expected_size = entry.get("bytes", entry.get("size"))
            supplied_sha = entry.get("sha256")

            if not path.is_file():
                reasons.append(f"missing_file:{key}")
                # On continue les contrôles de hash/size sur le papier impossible.
                continue

            actual_bytes = path.read_bytes()
            if expected_size is not None and int(expected_size) != len(actual_bytes):
                reasons.append(f"size_mismatch:{key}")

            if not isinstance(supplied_sha, str) or _HEX64.match(supplied_sha) is None:
                reasons.append(f"non_hex_sha256:{key}")
            else:
                actual_sha = hashlib.sha256(actual_bytes).hexdigest()
                if supplied_sha.lower() != actual_sha:
                    reasons.append(f"sha256_mismatch:{key}")

        if capture_status == "complete":
            origin = manifest.get("origin")
            if origin not in _ALLOWED_ORIGINS:
                reasons.append("unknown_origin")
            mask_kind = manifest.get("mask_kind")
            if mask_kind is not None:
                source = manifest.get("source")
                if not isinstance(source, dict):
                    source = {}
                transform = source.get("transform")
                source_size = source.get("source_size")
                if not transform or not source_size:
                    reasons.append("missing_transform")

        return (len(reasons) == 0, reasons)
    except Exception as exc:  # pragma: no cover - defensive
        return False, [f"invalid_manifest:{exc}"]
