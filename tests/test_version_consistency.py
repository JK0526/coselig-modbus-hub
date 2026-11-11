"""Keep the integration version consistent across packaged metadata."""

import json
import tomllib
from pathlib import Path

from packaging.version import Version

from custom_components.coselig_modbus_hub.const import VERSION


def test_manifest_project_and_runtime_versions_match():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "custom_components/coselig_modbus_hub/manifest.json").read_text()
    )
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]

    assert manifest["version"] == VERSION
    assert Version(project["version"]) == Version(VERSION)
    locked = tomllib.loads((root / "uv.lock").read_text())["package"]
    package = next(package for package in locked if package["name"] == project["name"])
    assert package["version"] == project["version"]
