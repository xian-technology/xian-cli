"""A positive global stop proof for hermetic operator-flow tests."""

from pathlib import Path


def stopped_home_proof(_stack, command, *, snapshot_options, **_options):
    assert command == "node-store-snapshot-check-stopped"
    return {"runtime_stopped": True, "home": str(Path(snapshot_options["home"]).resolve())}
