from __future__ import annotations

from pathlib import Path

from scripts.compile_changelog import (
    check_fragments,
    compile_fragments,
    get_fragment_files,
    validate_fragment,
)


def test_get_fragment_files_ignores_readme_and_hidden(tmp_path: Path) -> None:
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    (frag_dir / "README.md").write_text("# Readme", encoding="utf-8")
    (frag_dir / ".gitkeep").write_text("", encoding="utf-8")
    (frag_dir / "2284-entry-b.md").write_text("- **Entry B.**", encoding="utf-8")
    (frag_dir / "2281-entry-a.md").write_text("- **Entry A.**", encoding="utf-8")

    files = get_fragment_files(frag_dir)
    assert [f.name for f in files] == ["2281-entry-a.md", "2284-entry-b.md"]


def test_validate_fragment(tmp_path: Path) -> None:
    # Valid
    p_valid = tmp_path / "valid.md"
    p_valid.write_text("- **Title.** Description.\n  Second line.", encoding="utf-8")
    assert validate_fragment(p_valid) == []

    # Empty
    p_empty = tmp_path / "empty.md"
    p_empty.write_text("   \n", encoding="utf-8")
    errs = validate_fragment(p_empty)
    assert any("fragment file is empty" in e for e in errs)

    # Not a markdown list item
    p_nolist = tmp_path / "nolist.md"
    p_nolist.write_text("Some text without list bullet", encoding="utf-8")
    errs = validate_fragment(p_nolist)
    assert any("must begin with a Markdown list item" in e for e in errs)

    # Line too long
    p_long = tmp_path / "long.md"
    p_long.write_text("- **Title.** " + "x" * 100, encoding="utf-8")
    errs = validate_fragment(p_long)
    assert any("exceeds 100 characters" in e for e in errs)


def test_compile_fragments_success(tmp_path: Path) -> None:
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    (frag_dir / "README.md").write_text("# Readme", encoding="utf-8")
    (frag_dir / "01-first.md").write_text("- **First item.** Description one.", encoding="utf-8")
    (frag_dir / "02-second.md").write_text("- **Second item.** Description two.", encoding="utf-8")

    changelog = tmp_path / "CHANGELOG.md"
    initial_text = (
        "# Changelog\n\n"
        "Introduction text.\n\n"
        "## Unreleased\n\n"
        "- **Existing item.** Old description.\n\n"
        "## 0.9.0\n\n"
        "- **Released item.**\n"
    )
    changelog.write_text(initial_text, encoding="utf-8")

    rc = compile_fragments(changelog_path=changelog, fragments_dir=frag_dir, dry_run=False)
    assert rc == 0

    # Verify fragment files were deleted (except README.md)
    remaining = [p.name for p in frag_dir.iterdir()]
    assert remaining == ["README.md"]

    # Verify changelog content
    updated = changelog.read_text(encoding="utf-8")
    expected = (
        "# Changelog\n\n"
        "Introduction text.\n\n"
        "## Unreleased\n\n"
        "- **First item.** Description one.\n\n"
        "- **Second item.** Description two.\n\n"
        "- **Existing item.** Old description.\n\n"
        "## 0.9.0\n\n"
        "- **Released item.**\n"
    )
    assert updated == expected


def test_compile_fragments_preserves_notice_comment(tmp_path: Path) -> None:
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    (frag_dir / "01-first.md").write_text("- **First item.**", encoding="utf-8")

    changelog = tmp_path / "CHANGELOG.md"
    initial_text = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "<!-- NOTICE: Do not edit directly! -->\n\n"
        "- **Old item.**\n"
    )
    changelog.write_text(initial_text, encoding="utf-8")

    rc = compile_fragments(changelog_path=changelog, fragments_dir=frag_dir)
    assert rc == 0

    updated = changelog.read_text(encoding="utf-8")
    expected = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "<!-- NOTICE: Do not edit directly! -->\n\n"
        "- **First item.**\n\n"
        "- **Old item.**\n"
    )
    assert updated == expected


def test_compile_fragments_dry_run(tmp_path: Path) -> None:
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    (frag_dir / "01-first.md").write_text("- **First item.**", encoding="utf-8")

    changelog = tmp_path / "CHANGELOG.md"
    initial_text = "# Changelog\n\n## Unreleased\n\n- **Old.**\n"
    changelog.write_text(initial_text, encoding="utf-8")

    rc = compile_fragments(changelog_path=changelog, fragments_dir=frag_dir, dry_run=True)
    assert rc == 0

    # Fragment file should NOT be deleted
    assert (frag_dir / "01-first.md").is_file()
    # Changelog should NOT be modified
    assert changelog.read_text(encoding="utf-8") == initial_text


def test_compile_fragments_missing_unreleased_header(tmp_path: Path) -> None:
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    (frag_dir / "01-first.md").write_text("- **First item.**", encoding="utf-8")

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog with no unreleased section\n", encoding="utf-8")

    rc = compile_fragments(changelog_path=changelog, fragments_dir=frag_dir)
    assert rc == 1


def test_check_fragments(tmp_path: Path) -> None:
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    assert check_fragments(frag_dir) == 0

    (frag_dir / "01-valid.md").write_text("- **Valid.**", encoding="utf-8")
    assert check_fragments(frag_dir) == 0

    (frag_dir / "02-invalid.md").write_text("invalid plain text", encoding="utf-8")
    assert check_fragments(frag_dir) == 1
