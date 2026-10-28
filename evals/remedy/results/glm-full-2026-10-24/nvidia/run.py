"""NVIDIA full GLM half-cohort under explicitly approved provider pause."""

import importlib.util
from pathlib import Path

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "nvidia_glm_probe", OUT.parents[1] / "gemini-replacement-2026-10-24/run.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe.OUT = OUT
probe.MODEL = "z-ai/glm-5.3"
probe.CANDIDATE_ROUTE_ID = "nvidia_glm_5_3"
probe.BASE_ROUTE_ID = next(
    key for key, route in probe.ROUTE_REGISTRY.items() if route.provider == "nvidia" and route.free
)
probe.PROVIDER_OVERRIDE = "nvidia"
probe.REASON = "review51-glm-full-nvidia-2026-10-24"
probe.SPACING_SECONDS = 65
probe.STREAMING = True
probe.OUTPUT_CAP = 32768
if __name__ == "__main__":
    probe.main()
