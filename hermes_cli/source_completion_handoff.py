"""Finish a launch-time source completion after Windows Desktop has quit.

Electron invokes this only through the detached desktop-update handoff. The
source-completion marker remains the durable obligation until the shared tail
succeeds; failures deliberately leave it in place for explicit recovery.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.source.resolve()

    from pm.environments import activate_dependencies

    activate_dependencies(root)
    from hermes_cli.source_completion import complete_source_checkout
    from hermes_cli.venv_sync import clear_completion, completion_pending_path

    # Another owner may have completed the obligation during Electron teardown.
    if not completion_pending_path(root).is_file():
        return 0

    ok = complete_source_checkout(
        root,
        desktop=True,
        assume_yes=True,
        completion_message=None,
        announce="\n✓ Code updated!",
    )
    if not ok:
        return 1
    clear_completion(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
