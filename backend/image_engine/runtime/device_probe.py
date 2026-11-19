"""Sondage materiel — DeviceInfo + probe_device (rapport_gestion_memoire §1)."""
import torch
import psutil
import shutil
from dataclasses import dataclass


@dataclass
class DeviceInfo:
    backend: str
    name: str
    total_vram_gb: float
    free_vram_gb: float
    unified_memory: bool
    ram_total_gb: float
    ram_free_gb: float
    device_index: int | None = None

    @property
    def torch_device(self) -> str:
        if self.backend in ("cuda", "rocm"):
            return f"cuda:{self.device_index}" if self.device_index is not None else "cuda"
        if self.backend == "mps":
            return "mps"
        return "cpu"

    @property
    def free_vram_bytes(self) -> int:
        return int(self.free_vram_gb * 1_000_000_000)

    @property
    def total_vram_bytes(self) -> int:
        return int(self.total_vram_gb * 1_000_000_000)


def probe_device() -> DeviceInfo:
    ram = psutil.virtual_memory()
    ram_total_gb = ram.total / 1e9
    ram_free_gb = ram.available / 1e9

    if torch.cuda.is_available():
        torch.cuda.init()
        best = max(range(torch.cuda.device_count()),
                   key=lambda i: torch.cuda.mem_get_info(i)[0])
        torch.cuda.set_device(best)
        free, total = torch.cuda.mem_get_info(best)
        return DeviceInfo(
            backend="rocm" if torch.version.hip else "cuda",
            name=torch.cuda.get_device_name(best),
            total_vram_gb=total / 1e9,
            free_vram_gb=free / 1e9,
            unified_memory=False,
            ram_total_gb=ram_total_gb,
            ram_free_gb=ram_free_gb,
            device_index=best,
        )

    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return DeviceInfo(
            backend="mps",
            name="Apple Silicon (MPS)",
            total_vram_gb=0,
            free_vram_gb=0,
            unified_memory=True,
            ram_total_gb=ram_total_gb,
            ram_free_gb=ram_free_gb,
        )

    vk = shutil.which("vulkaninfo") is not None
    return DeviceInfo(
        backend="cpu",
        name="CPU" + (" (Vulkan present mais inutilisable via PyTorch → mode CPU)" if vk else ""),
        total_vram_gb=0,
        free_vram_gb=0,
        unified_memory=False,
        ram_total_gb=ram_total_gb,
        ram_free_gb=ram_free_gb,
    )
