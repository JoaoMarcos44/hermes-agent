"""Contract for the WhatsApp bridge's resolved security dependency graph."""

import json
from pathlib import Path

from packaging.version import Version


REPO_ROOT = Path(__file__).parents[2]
BRIDGE = REPO_ROOT / "scripts" / "whatsapp-bridge"


def test_body_parser_and_its_qs_are_outside_advisory_ranges() -> None:
    manifest = json.loads((BRIDGE / "package.json").read_text())
    lock = json.loads((BRIDGE / "package-lock.json").read_text())

    assert "body-parser" not in manifest["overrides"]
    body_parser = lock["packages"]["node_modules/body-parser"]
    assert Version(body_parser["version"]) not in {
        Version("1.20.5"),
        Version("1.20.6"),
    }
    body_parser_qs = lock["packages"]["node_modules/body-parser/node_modules/qs"]
    assert Version(body_parser_qs["version"]) >= Version("6.16.0")
