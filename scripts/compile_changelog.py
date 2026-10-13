#!/usr/bin/env python3
"""Compile unmerged changelog fragments into CHANGELOG.md.

Collects fragment markdown files from ``changelog.d/`` (excluding README.md),
inserts them under the ``## Unreleased`` section in ``CHANGELOG.md``, and removes
the processed fragment files.

Supports ``--check`` to validate fragments without compiling, and ``--dry-run``
to preview the compiled changelog without modifying any files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def get_fragment_files(fragments_dir: Path) -> list[Path]:
    """Return all non-README markdown files in the fragments directory, sorted by name."""
    if not fragments_dir.is_dir():
        return []

    fragments = [
        p
        for p in fragments_dir.glob("*.md")
        if p.name.lower() != "readme.md" and not p.name.startswith(".")
    ]
    return sorted(fragments, key=lambda p: p.name.lower())


def validate_fragment(path: Path) -> list[str]:
    """Validate that a fragment file is non-empty and formatted as a Markdown list item.

    Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []
    text = path.read_text(encoding="utf-8").strip()

    if not text:
        errors.append(f"{path.name}: fragment file is empty")
        return errors

    first_line = text.splitlines()[0].strip()
    if not (first_line.startswith("- ") or first_line.startswith("* ")):
        errors.append(f"{path.name}: fragment must begin with a Markdown list item ('- ' or '* ')")

    for i, line in enumerate(text.splitlines(), start=1):
        if len(line) > 100:
            errors.append(f"{path.name}:{i}: line exceeds 100 characters ({len(line)} chars)")

    return errors


def compile_fragments(
    changelog_path: Path,
    fragments_dir: Path,
    dry_run: bool = False,
) -> int:
    """Compile all fragment files into the specified changelog.

    Returns 0 on success, non-zero on error.
    """
    fragment_files = get_fragment_files(fragments_dir)
    if not fragment_files:
        print("No changelog fragments found to compile.")
        return 0

    validation_errors: list[str] = []
    for frag in fragment_files:
        validation_errors.extend(validate_fragment(frag))

    if validation_errors:
        print("Error: Invalid changelog fragment(s):", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    if not changelog_path.is_file():
        print(f"Error: Changelog file not found at {changelog_path}", file=sys.stderr)
        return 1

    content = changelog_path.read_text(encoding="utf-8")
    unreleased_marker = "## Unreleased"
    marker_idx = content.find(unreleased_marker)
    if marker_idx == -1:
        print(
            f"Error: Marker '{unreleased_marker}' not found in {changelog_path}",
            file=sys.stderr,
        )
        return 1

    # Locate where the Unreleased entries should begin.
    # We look for the first newline following "## Unreleased".
    after_header_idx = marker_idx + len(unreleased_marker)
    next_newline = content.find("\n", after_header_idx)
    if next_newline == -1:
        insert_idx = len(content)
        prefix = content
        suffix = ""
    else:
        # Move past any blank lines immediately after "## Unreleased"
        scan_idx = next_newline + 1
        while scan_idx < len(content) and content[scan_idx] == "\n":
            scan_idx += 1

        # If an HTML comment notice follows the header, preserve it above entries
        if content[scan_idx:].startswith("<!--"):
            comment_end = content.find("-->", scan_idx)
            if comment_end != -1:
                scan_idx = comment_end + 3
                while scan_idx < len(content) and content[scan_idx] == "\n":
                    scan_idx += 1

        insert_idx = scan_idx
        prefix = content[:insert_idx]
        suffix = content[insert_idx:]

    entries = [f.read_text(encoding="utf-8").strip() for f in fragment_files]
    compiled_block = "\n\n".join(entries) + "\n\n"

    new_content = prefix + compiled_block + suffix

    if dry_run:
        print(f"--- Dry run: compiled {len(fragment_files)} fragment(s) ---")
        print(compiled_block.rstrip())
        return 0

    changelog_path.write_text(new_content, encoding="utf-8")
    for f in fragment_files:
        f.unlink()

    print(f"Successfully compiled {len(fragment_files)} fragment(s) into {changelog_path}.")
    return 0


def check_fragments(fragments_dir: Path) -> int:
    """Validate all fragment files in the fragments directory without compiling."""
    fragment_files = get_fragment_files(fragments_dir)
    if not fragment_files:
        print("No changelog fragments found.")
        return 0

    validation_errors: list[str] = []
    for frag in fragment_files:
        validation_errors.extend(validate_fragment(frag))

    if validation_errors:
        print("Error: Invalid changelog fragment(s):", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"All {len(fragment_files)} changelog fragment(s) are valid.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile changelog fragment files into CHANGELOG.md."
    )
    repo_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--changelog",
        type=Path,
        default=repo_root / "CHANGELOG.md",
        help="Path to CHANGELOG.md (default: repo root CHANGELOG.md)",
    )
    parser.add_argument(
        "--fragments-dir",
        type=Path,
        default=repo_root / "changelog.d",
        help="Path to changelog.d directory (default: repo root changelog.d)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview compilation without modifying files or deleting fragments",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate fragments for formatting errors without compiling",
    )

    args = parser.parse_args(argv)

    if args.check:
        return check_fragments(args.fragments_dir)

    return compile_fragments(
        changelog_path=args.changelog,
        fragments_dir=args.fragments_dir,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
