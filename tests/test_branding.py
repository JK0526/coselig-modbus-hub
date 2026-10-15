"""Keep public integration labels and tracked docs on the current brand."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def test_tracked_public_files_do_not_expose_legacy_product_aliases():
    root = Path(__file__).resolve().parents[1]
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
        .decode()
        .split("\0")
    )
    public_paths = [
        root / name
        for name in tracked
        if name
        and (
            name == "README.md"
            or (
                name.startswith("custom_components/coselig_modbus_hub/")
                and Path(name).suffix.lower() in {".js", ".json", ".md", ".py"}
            )
            or (name.startswith("docs/") and name.endswith(".md"))
        )
        and not name.startswith("docs/specs/")
    ]
    forbidden = (
        "SUN" + "WAVE",
        "L" + "10",
        "R" + "4J",
        "R" + "4P",
    )
    legacy_l2_alias = "L" + "2"
    legacy_l2_product_alias = re.compile(
        r"(?:P210\s*(?:[/／(（]\s*)?"
        + re.escape(legacy_l2_alias)
        + r"\b|\b"
        + re.escape(legacy_l2_alias)
        + r"\s*(?:[/／)）]\s*)?P210)",
        re.IGNORECASE,
    )
    violations = []
    for path in public_paths:
        content = path.read_text(encoding="utf-8")
        if any(term.casefold() in content.casefold() for term in forbidden):
            violations.append(str(path.relative_to(root)))
        if legacy_l2_product_alias.search(content):
            violations.append(str(path.relative_to(root)))
    assert not violations, f"Legacy product aliases found in: {sorted(set(violations))}"
