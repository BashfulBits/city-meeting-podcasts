"""Freeze actual accepted-pair disagreements offline; never read gold or call providers."""

import hashlib
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
BASE = OUT.parent
sys.path.insert(0, str(BASE / "body-selection-2026-10-13"))
from body_selection import load_manifest, validate_answer  # noqa: E402

from citypods.remedy_evaluation import canonical_hash  # noqa: E402


def main():
    index = json.loads((BASE / "glm-full-2026-10-24/all-pairs-final.json").read_text())
    manifest, cases = load_manifest()
    for models in [("glm_flash", "deepseek"), ("glm_flash", "glm_full")]:
        directory = OUT / ("-".join(models))
        directory.mkdir(exist_ok=True)
        entries, provenance = [], []
        for case in sorted(cases, key=lambda c: c.case_id):
            selected = []
            for model in models:
                source = index["selected_sources"].get(f"{model}/{case.case_id}")
                if source is None:
                    break
                path = Path(source["path"])
                assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
                row = json.loads(path.read_text())
                answer = validate_answer(row["answer"], case).model_dump(mode="json")
                selected.append((model, answer, source))
            if len(selected) != 2:
                continue
            signatures = [
                (a["publication_action"], sorted(a["feed_slugs"])) for _, a, _ in selected
            ]
            if signatures[0] == signatures[1]:
                continue
            if int(canonical_hash([models, case.case_id])[0], 16) % 2:
                selected.reverse()
            entries.append(
                {
                    "case": case.model_dump(mode="json"),
                    "proposals": dict(zip("AB", [a for _, a, _ in selected], strict=True)),
                }
            )
            provenance.append(
                {
                    "case_id": case.case_id,
                    "proposals": {
                        letter: {"model": model, **source}
                        for letter, (model, _, source) in zip("AB", selected, strict=True)
                    },
                }
            )
        packet = {
            "version": 1,
            "source_manifest_hash": canonical_hash(manifest),
            "stage": "frozen-original-evidence-awaiting-expansion-and-adjudicator-approval",
            "cases": entries,
        }
        files = {
            "packet-original-evidence.json": packet,
            "provenance.json": {"cases": provenance, "packet_hash": canonical_hash(packet)},
        }
        for filename, data in files.items():
            path = directory / filename
            encoded = json.dumps(data, indent=2) + "\n"
            if path.exists():
                assert path.read_text() == encoded, "Frozen artifact differs; never overwrite"
            else:
                path.write_text(encoded)
        print(directory.name, len(entries), "packets", canonical_hash(packet))


if __name__ == "__main__":
    main()
