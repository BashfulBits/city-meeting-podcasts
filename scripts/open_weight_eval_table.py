"""Markdown tables from a ``score`` summary (review/60 Phase 3): ``python -m ... SUMMARY.json``."""

import json
import sys

COLUMNS = {
    "tags": (
        "accuracy_strict",
        "accuracy_minus_reference",
        "ece",
        "brier",
        "agreement_with_reference",
    ),
    "chapters": ("accuracy_strict", "f1", "precision", "recall", "ece", "tokens_per_tag"),
    "moments": ("accuracy_strict", "wrongly_rejected", "ece"),
}


def fmt(value):
    if value is None:
        return "n/a"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def interval(entry, key):
    ci = entry.get(key + "_ci")
    return f" [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else ""


def main(path):
    summary = json.loads(open(path).read())
    for lane, columns in COLUMNS.items():
        print(f"\n**{lane}** ({summary['split']})\n")
        print("| Model | " + " | ".join(columns) + " | invalid |")
        print("|---|" + "---:|" * (len(columns) + 1))
        for name, lanes in sorted(summary["models"].items()):
            if lane not in lanes or (lane != "chapters" and name.endswith("+single")):
                continue
            entry = lanes[lane]
            cells = []
            for column in columns:
                cell = fmt(entry.get(column))
                if column == "accuracy_minus_reference" and name != "perplexity-v1.3":
                    cell += interval(entry, column)
                cells.append(cell)
            print(f"| {name} | " + " | ".join(cells) + f" | {fmt(entry['invalid_rate'])} |")


if __name__ == "__main__":
    main(sys.argv[1])
