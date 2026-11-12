"""Files, seeds and archive of `xian network package-operator-bundle`."""

import io
import json
import tarfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from xian_cli.cli import main

CONFIGS_DIR = Path(__file__).resolve().parents[2] / "xian-configs"
SEED_PLACEHOLDER = "REPLACE_WITH_<node_id>@<public-host>:26656"
BUNDLE_FILES = [
    "manifest.json",
    "genesis.json",
    "bootstrap-seed.txt",
    "README.md",
    "SMOKE-CHECKLIST.md",
    "participant-join.sh",
    "participant-bds-node.sh",
]


def run_cli(*argv: str) -> dict:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main(list(argv)) == 0
    return json.loads(stdout.getvalue())


def create_network(base_dir: Path, *extra: str) -> None:
    run_cli(
        "network",
        "create",
        "shared",
        "--base-dir",
        str(base_dir),
        "--chain-id",
        "shared-1",
        *extra,
    )


def package(base_dir: Path, *extra: str) -> dict:
    return run_cli(
        "network",
        "package-operator-bundle",
        "shared",
        "--base-dir",
        str(base_dir),
        "--configs-dir",
        str(CONFIGS_DIR),
        *extra,
    )


def test_bundle_without_seeds_writes_a_placeholder_seed_and_metadata(tmp_path):
    create_network(tmp_path)

    result = package(tmp_path)

    bundle_dir = tmp_path / "dist" / "shared-operator-bundle"
    assert result == {
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(bundle_dir / "manifest.json"),
        "genesis_path": str(bundle_dir / "genesis.json"),
        "network": "shared",
        "chain_id": "shared-1",
        "archive_path": None,
    }
    assert (bundle_dir / "bootstrap-seed.txt").read_text() == f"{SEED_PLACEHOLDER}\n"
    metadata = json.loads((bundle_dir / "operator-bundle.json").read_text())
    assert metadata["schema"] == "xian.operator_bundle.v1"
    assert metadata["bootstrap_seed"] == SEED_PLACEHOLDER
    assert metadata["files"] == BUNDLE_FILES
    assert metadata["source_manifest"] == str(
        (tmp_path / "networks" / "shared" / "manifest.json").resolve()
    )
    bundled = json.loads((bundle_dir / "manifest.json").read_text())
    assert bundled["p2p"] == {"seeds": [], "persistent_peers": []}


def test_manifest_seed_is_the_default_bootstrap_seed(tmp_path):
    create_network(tmp_path, "--seed", "first@10.0.0.1:26656", "--seed", "second@10.0.0.2:26656")

    package(tmp_path)

    bundle_dir = tmp_path / "dist" / "shared-operator-bundle"
    assert (bundle_dir / "bootstrap-seed.txt").read_text() == "first@10.0.0.1:26656\n"
    bundled = json.loads((bundle_dir / "manifest.json").read_text())
    assert bundled["p2p"]["seeds"] == ["first@10.0.0.1:26656", "second@10.0.0.2:26656"]


def test_explicit_seed_joins_the_manifest_seeds_once(tmp_path):
    create_network(tmp_path, "--seed", "first@10.0.0.1:26656")

    package(tmp_path, "--bootstrap-seed", "first@10.0.0.1:26656")
    bundled = json.loads((tmp_path / "dist/shared-operator-bundle/manifest.json").read_text())
    assert bundled["p2p"]["seeds"] == ["first@10.0.0.1:26656"]

    package(tmp_path, "--bootstrap-seed", "new@10.0.0.9:26656", "--force")
    bundled = json.loads((tmp_path / "dist/shared-operator-bundle/manifest.json").read_text())
    assert bundled["p2p"]["seeds"] == ["first@10.0.0.1:26656", "new@10.0.0.9:26656"]
    seed_file = tmp_path / "dist/shared-operator-bundle/bootstrap-seed.txt"
    assert seed_file.read_text() == "new@10.0.0.9:26656\n"


def test_relative_output_is_under_the_base_dir_and_must_be_a_directory(tmp_path):
    create_network(tmp_path)
    (tmp_path / "taken").write_text("not a directory")

    with pytest.raises(FileExistsError, match="bundle output is not a directory"):
        package(tmp_path, "--output", "taken")

    result = package(tmp_path, "--output", "out/bundle")
    assert result["bundle_dir"] == str((tmp_path / "out" / "bundle").resolve())


def test_archive_holds_the_bundle_and_is_not_overwritten_without_force(tmp_path):
    create_network(tmp_path)

    result = package(tmp_path, "--archive")

    archive = Path(result["archive_path"])
    assert archive == tmp_path / "dist" / "shared-operator-bundle.tar.gz"
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert {f"shared-operator-bundle/{name}" for name in BUNDLE_FILES} <= names
    with pytest.raises(FileExistsError, match="pass --force to overwrite"):
        package(tmp_path, "--archive")
    assert package(tmp_path, "--archive", "--force")["archive_path"] == str(archive)
