"""A refused key-generation command must preserve existing custody material."""

import hashlib
import io
import json
import os
from contextlib import redirect_stdout

import pytest

from xian_cli.cli import main


@pytest.mark.parametrize("kind", ["file", "dangling-link"])
def test_existing_metadata_does_not_leave_an_unrelated_validator_key(tmp_path, kind):
    metadata = tmp_path / "validator_key_info.json"
    if kind == "file":
        metadata.write_text("existing recovery material")
        metadata.chmod(0o600)
    else:
        metadata.symlink_to(tmp_path / "missing")

    with pytest.raises(FileExistsError):
        main(["keys", "validator", "generate", "--out-dir", str(tmp_path)])

    if kind == "file":
        assert metadata.read_text() == "existing recovery material"
    else:
        assert metadata.is_symlink()
    assert not (tmp_path / "priv_validator_key.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX secret permissions")
def test_refused_metadata_overwrite_preserves_the_validator_key(tmp_path):
    args = ["keys", "validator", "generate", "--out-dir", str(tmp_path)]
    with redirect_stdout(io.StringIO()):
        assert main(args) == 0
    key = tmp_path / "priv_validator_key.json"
    before = hashlib.sha256(key.read_bytes()).hexdigest()
    metadata = tmp_path / "validator_key_info.json"
    metadata.chmod(0o644)

    with pytest.raises(PermissionError):
        main([*args, "--force"])

    assert hashlib.sha256(key.read_bytes()).hexdigest() == before


def test_explicit_force_replaces_both_key_files_consistently(tmp_path):
    args = ["keys", "validator", "generate", "--out-dir", str(tmp_path)]
    with redirect_stdout(io.StringIO()):
        assert main(args) == 0
        assert main([*args, "--force"]) == 0

    key = json.loads((tmp_path / "priv_validator_key.json").read_text())
    metadata = json.loads((tmp_path / "validator_key_info.json").read_text())
    assert key["address"] == metadata["priv_validator_key"]["address"]
    assert key["pub_key"] == metadata["priv_validator_key"]["pub_key"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.parametrize("existing_parent", [False, True])
def test_secret_output_creates_private_parents_without_changing_existing_ones(
    tmp_path, existing_parent
):
    parent = tmp_path / "wallets"
    if existing_parent:
        parent.mkdir(mode=0o755)
    with redirect_stdout(io.StringIO()):
        assert (
            main(["client", "wallet", "generate", "--private-key-out", str(parent / "wallet.key")])
            == 0
        )
    assert parent.stat().st_mode & 0o777 == (0o755 if existing_parent else 0o700)
    assert (parent / "wallet.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
def test_validator_outputs_remain_readable_by_owner_under_restrictive_umask(tmp_path):
    previous = os.umask(0o777)
    try:
        with redirect_stdout(io.StringIO()):
            assert main(["keys", "validator", "generate", "--out-dir", str(tmp_path)]) == 0
    finally:
        os.umask(previous)
    for name in ("priv_validator_key.json", "validator_key_info.json"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
