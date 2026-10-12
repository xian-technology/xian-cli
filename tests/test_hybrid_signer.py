import pytest
from xian_py.hybrid import AccountTransitionDraft
from xian_py.wallet import Wallet


@pytest.fixture
def custody(tmp_path):
    legacy = Wallet()
    draft = AccountTransitionDraft("local-test", legacy.public_key, 0)
    path = tmp_path / "active.json"
    path.write_text(draft.export_backup("active", "backup test password"))
    return legacy, draft, path


def test_cli_active_backup_and_no_legacy_fallback(custody, monkeypatch):
    from argparse import Namespace

    from xian_cli.client.handlers import _build_wallet

    legacy, draft, path = custody
    monkeypatch.setenv("TEST_BACKUP_PASSWORD", "backup test password")
    args = Namespace(
        active_backup_file=path, backup_password_env="TEST_BACKUP_PASSWORD", chain_id="local-test"
    )
    assert _build_wallet(args).public_key == legacy.public_key
    args.private_key_env = "LEGACY_KEY"
    with pytest.raises(ValueError, match="cannot be combined"):
        _build_wallet(args)
    args.private_key_env = None
    args.chain_id = "different-chain"
    with pytest.raises(ValueError, match="chain"):
        _build_wallet(args)
