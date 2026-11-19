"""Oracle diffusers — generation hors ligne pour validation (v2 §1, hors src/)."""
import torch
from PIL import Image

from diffusers.pipelines.flux2.pipeline_flux2_klein import Flux2KleinPipeline
from diffusers.schedulers.scheduling_flow_match_euler_discrete import FlowMatchEulerDiscreteScheduler


def generate_oracle(prompt, height=1024, width=1024, seed=42, output="oracle.png"):
    pipe = Flux2KleinPipeline.from_pretrained("black-forest-labs/FLUX.2-klein-9b-kv", torch_dtype=torch.bfloat16)
    scheduler_result = FlowMatchEulerDiscreteScheduler.from_config(
        pipe.scheduler.config,
        return_unused_kwargs=False,
        use_dynamic_shifting=False,
        shift=3.0,
    )
    if isinstance(scheduler_result, tuple):
        scheduler, _ = scheduler_result
    else:
        scheduler = scheduler_result
    pipe.scheduler = scheduler
    pipe.enable_sequential_cpu_offload()  # oracle uniquement, pas dans src/
    result = pipe(
        prompt=prompt,
        height=height,
        width=width,
        num_inference_steps=4,
        generator=torch.Generator(device="cpu").manual_seed(seed),
    )
    images = getattr(result, "images", None)
    if not isinstance(images, list) or not images:
        raise RuntimeError("Diffusers pipeline returned no images.")
    image = images[0]
    if not isinstance(image, Image.Image):
        raise TypeError("Diffusers pipeline returned a non-PIL image.")
    image.save(output)
    print(f"Oracle saved {output} {image.size}")
    return image

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", required=True)
    p.add_argument("--output", default="oracle.png")
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()
    generate_oracle(a.prompt, a.height, a.width, a.seed, a.output)
