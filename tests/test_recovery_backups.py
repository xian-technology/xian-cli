import os
import stat
import tarfile
from datetime import UTC, datetime

import pytest

from xian_cli.commands.recovery import _build_recovery_backup


@pytest.mark.skipif(os.name == "nt", reason="POSIX private file permissions")
@pytest.mark.parametrize("existing_directory", [False, True])
def test_recovery_backup_keeps_validator_key_private(tmp_path, existing_directory):
    home = tmp_path / "home"
    config = home / "config"
    config.mkdir(parents=True)
    (config / "priv_validator_key.json").write_text("private-validator-key", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    if existing_directory:
        backup_dir.mkdir(mode=0o755)
    previous_umask = os.umask(0o022)
    try:
        archive = _build_recovery_backup(
            home=home,
            backup_dir=backup_dir,
            plan_name="recovery",
            node_name="validator",
        )
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    with tarfile.open(archive) as backup:
        assert (
            backup.extractfile("home/config/priv_validator_key.json").read()
            == b"private-validator-key"
        )


def test_repeated_recovery_backups_preserve_both_node_states(tmp_path, monkeypatch):
    class FrozenClock:
        @staticmethod
        def now(_timezone):
            return datetime(2026, 11, 11, tzinfo=UTC)

    monkeypatch.setattr("xian_cli.commands.recovery.datetime", FrozenClock)
    home = tmp_path / "home"
    home.mkdir()
    state = home / "state"
    archives = []
    for value in ("first", "second"):
        state.write_text(value, encoding="utf-8")
        archives.append(
            _build_recovery_backup(
                home=home,
                backup_dir=tmp_path / "backups",
                plan_name="recovery",
                node_name="validator",
            )
        )

    assert archives[0] != archives[1]
    for archive_path, value in zip(archives, (b"first", b"second"), strict=True):
        with tarfile.open(archive_path) as archive:
            assert archive.extractfile("home/state").read() == value


def test_failed_recovery_backup_removes_partial_archive(tmp_path):
    backup_dir = tmp_path / "backups"
    with pytest.raises(FileNotFoundError):
        _build_recovery_backup(
            home=tmp_path / "absent-home",
            backup_dir=backup_dir,
            plan_name="recovery",
            node_name="validator",
        )

    assert list(backup_dir.iterdir()) == []
