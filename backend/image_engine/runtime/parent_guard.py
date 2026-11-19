from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
import time

try:
    import psutil
except Exception:  # pragma: no cover - psutil est optionnel hors Linux
    psutil = None

PR_SET_PDEATHSIG = 1


def arm_parent_guard() -> int:
    if sys.platform != "linux":
        if psutil is None:
            raise RuntimeError(
                "[PARENT_GUARD_UNAVAILABLE] Le garde parent est indisponible : "
                "psutil est absent sur cette plateforme."
            )
        parent_before = os.getppid()
        if parent_before <= 1:
            raise RuntimeError(
                "[PARENT_GUARD_FAILED] Le processus parent n'est pas identifiable."
            )

        def surveiller_parent(parent_pid: int) -> None:
            while True:
                try:
                    if hasattr(psutil, "pid_exists"):
                        parent_vivant = bool(psutil.pid_exists(parent_pid))
                    else:
                        try:
                            psutil.Process(parent_pid)
                            parent_vivant = True
                        except Exception:
                            parent_vivant = False
                except Exception:
                    parent_vivant = False
                if not parent_vivant:
                    os._exit(1)
                time.sleep(0.05)

        fil = threading.Thread(
            target=surveiller_parent, args=(parent_before,), daemon=True
        )
        fil.start()
        return parent_before

    parent_before = os.getppid()
    if parent_before <= 1:
        raise RuntimeError(
            "[PARENT_GUARD_FAILED] Le processus parent n'est pas identifiable."
        )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        result = libc.prctl(
            ctypes.c_int(PR_SET_PDEATHSIG),
            ctypes.c_ulong(signal.SIGTERM),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    except Exception as exc:
        raise RuntimeError(
            f"[PARENT_GUARD_FAILED] Armement impossible : {exc}"
        ) from exc
    parent_after = os.getppid()
    if parent_after != parent_before or parent_after <= 1:
        raise RuntimeError(
            "[PARENT_GUARD_FAILED] Le parent a changé pendant l'armement."
        )
    return parent_after


arm_parent_death_signal = arm_parent_guard

__all__ = ["PR_SET_PDEATHSIG", "arm_parent_death_signal", "arm_parent_guard"]
