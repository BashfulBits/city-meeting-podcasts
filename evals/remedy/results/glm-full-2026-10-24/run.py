"""Full GLM replacement comparison, using the approved frozen-packet probe runner."""

import importlib.util
from pathlib import Path

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "cohort_probe", OUT.parent / "gemini-replacement-2026-10-24/run.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
probe.OUT = OUT
probe.MODEL = "glm-5.3-free"
probe.CANDIDATE_ROUTE_ID = "beatapi_glm_5_3_free"
probe.REASON = "review51-glm-full-2026-10-24"
probe.PARTITION = 0
probe.API_BASE_OVERRIDE = "https://direct.beatapi.io/v1"
probe.STREAMING = True
probe.OUTPUT_CAP = 32768
if __name__ == "__main__":
    probe.main()
