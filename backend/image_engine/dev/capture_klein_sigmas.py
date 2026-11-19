# scripts/capture_klein_sigmas.py — ORACLE diffusers, pas le runtime
# Tente Flux2KleinPipeline, fallback sur scheduler seul si pas de reseau/poids
import torch
from collections.abc import Sequence


def _schedule_values(values: Sequence[float] | torch.Tensor | None, name: str) -> list[float]:
    if values is None:
        raise RuntimeError(f"Scheduler did not populate {name}.")
    return torch.as_tensor(values, dtype=torch.float32).tolist()


try:
    from diffusers.pipelines.flux2.pipeline_flux2_klein import Flux2KleinPipeline
    print("[capture] Tentative Flux2KleinPipeline.from_pretrained...")
    pipe = Flux2KleinPipeline.from_pretrained(
        "black-forest-labs/FLUX.2-klein-9b-kv", torch_dtype=torch.bfloat16, local_files_only=False
    )
    pipe.scheduler.set_timesteps(4, device="cuda" if torch.cuda.is_available() else "cpu")
    print("timesteps:", _schedule_values(pipe.scheduler.timesteps, "timesteps"))
    print("sigmas   :", _schedule_values(pipe.scheduler.sigmas, "sigmas"))
except Exception as e:
    print(f"[capture] Pipeline echoue ({e}), fallback scheduler seul")
    from diffusers.schedulers.scheduling_flow_match_euler_discrete import FlowMatchEulerDiscreteScheduler
    # Config d'apres flux2: try default
    sched = FlowMatchEulerDiscreteScheduler(num_train_timesteps=1000, shift=3.0)
    sched.set_timesteps(4, device="cuda" if torch.cuda.is_available() else "cpu")
    print("timesteps:", _schedule_values(sched.timesteps, "timesteps"))
    print("sigmas   :", _schedule_values(sched.sigmas, "sigmas"))
    # Aussi tester avec shift different (Flux Klein 4 steps distille)
    for shift in [1.0, 3.0, 5.0]:
        s = FlowMatchEulerDiscreteScheduler(num_train_timesteps=1000, shift=shift)
        s.set_timesteps(4, device="cpu")
        print(
            f"shift={shift} sigmas={_schedule_values(s.sigmas, 'sigmas')} "
            f"timesteps={_schedule_values(s.timesteps, 'timesteps')}"
        )
