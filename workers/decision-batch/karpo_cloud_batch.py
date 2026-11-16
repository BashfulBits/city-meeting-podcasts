"""Run a pinned Karpo checkpoint through the fenced decision batch transport."""

import json
import os
from pathlib import Path

from cloud_batch import run_cloud
from karpo_batch import KarpoAdapter

from citypods.judging.decision_batch_cloud import GCSObjectStore, metadata_access_token
from scripts.download_glide_weights import MODEL_PREFIXES, REVISION, download

KARPO_REVISIONS = set(MODEL_PREFIXES) - {REVISION}


def main() -> None:
    manifest_path = Path("/app/model-manifest.json")
    model_manifest = json.loads(manifest_path.read_text())
    revision = model_manifest.get("revision")
    if revision not in KARPO_REVISIONS:
        raise ValueError("expected an approved Karpo checkpoint")
    model_dir = Path(os.environ["CITYPODS_MODEL_DIR"])
    if not model_dir.parent.is_dir():
        raise ValueError("model cache volume must exist locally")

    def load_model(settings):
        import torch
        from fla.ops.gated_delta_rule import chunk_gated_delta_rule
        from karpo_decision.decider import Decider
        from karpo_decision.encoding import LengthError
        from karpo_decision.protocol import RequestError

        if not (
            torch.cuda.is_available()
            and torch.cuda.is_bf16_supported()
            and torch.version.cuda == "13.0"
            and "L4" in torch.cuda.get_device_name()
            and callable(chunk_gated_delta_rule)
        ):
            raise RuntimeError("expected an L4 BF16 CUDA 13.0 runtime with flash-linear-attention")
        print("decision batch model download started", flush=True)
        timings = download(model_manifest, model_dir, metadata_access_token())
        print(json.dumps({"model_download": timings}), flush=True)
        model = Decider.from_pretrained(
            str(model_dir),
            device="cuda",
            max_length=settings["max_len"],
            token_budget=settings["token_budget"],
        )
        return KarpoAdapter(model, RequestError, LengthError), RequestError

    store = GCSObjectStore(os.environ["CITYPODS_BATCH_BUCKET"], metadata_access_token)
    run_cloud(
        store,
        os.environ["DECISION_BATCH_MANIFEST_ID"],
        os.environ["DECISION_BATCH_ATTEMPT_ID"],
        os.environ["CLOUD_RUN_EXECUTION"],
        os.environ["CITYPODS_IMAGE_DIGEST"],
        load_model,
        model_revision=revision,
    )


if __name__ == "__main__":
    main()
