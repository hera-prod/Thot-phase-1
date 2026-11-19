import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if "--image-worker" in sys.argv[1:]:
    sys.argv = [sys.argv[0], *[arg for arg in sys.argv[1:] if arg != "--image-worker"]]
    runpy.run_module("backend.image_engine.spike_t2i", run_name="__main__")
else:
    runpy.run_module("backend.main", run_name="__main__")
