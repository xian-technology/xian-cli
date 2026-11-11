"""Node initialization translates operator intent without changing precedence."""

import io
import json
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.cli import main


def initialization_profile(case):
    profile = {"moniker": "validator", "validator_key_ref": "keys/profile.json", "home": "home"}
    network = {"chain_id": "local-test"}
    if case == "inherited":
        network.update(block_policy_mode="periodic", block_policy_interval="7s")
    if case == "overrides":
        network.update(
            block_policy_mode="periodic",
            block_policy_interval="7s",
            p2p={
                "seeds": ["seed-network@127.0.0.1:26656"],
                "persistent_peers": ["peer-network@127.0.0.1:26656"],
            },
        )
        profile.update(
            p2p={
                "seeds": ["seed-local@127.0.0.1:26656"],
                "persistent_peers": ["peer-local@127.0.0.1:26656"],
            },
            pruning_enabled=True,
            blocks_to_keep=500,
            block_policy_mode="idle_interval",
            block_policy_interval="2s",
            transaction_trace_logging=True,
            app_log_level="WARNING",
            app_log_json=True,
            app_log_rotation_hours=8,
            app_log_retention_days=20,
            app_log_max_file_mb=30,
            app_log_max_total_mb=60,
            simulation_enabled=False,
            simulation_max_concurrency=4,
            simulation_timeout_ms=1010,
            simulation_max_chi=333333,
            tx_fee_mode="free_metered",
            free_tx_max_chi=123456,
            free_block_max_chi=3456789,
            parallel_execution_enabled=True,
            parallel_execution_workers=3,
            parallel_execution_min_transactions=2,
            advanced={
                "cometbft": {
                    "allow_cors": False,
                    "proxy_app": "unix:///tmp/custom.sock",
                    "prometheus": False,
                },
                "statesync": {
                    "enabled": True,
                    "rpc_servers": ["http://localhost:26657", "http://localhost:26667"],
                    "trust_height": 42,
                    "trust_hash": "ab" * 32,
                    "trust_period": "48h0m0s",
                },
                "metrics": {
                    "enabled": False,
                    "host": "127.0.0.2",
                    "port": 9234,
                    "bds_refresh_seconds": 2.5,
                },
                "pending_nonce": {"reservation_ttl_seconds": 12.5, "max_per_sender": 7},
                "parallel_execution": {
                    "max_speculative_waves": 9,
                    "min_wave_acceptance_ratio": 0.7,
                    "low_acceptance_min_wave_size": 3,
                    "warm_workers": False,
                    "access_estimates_enabled": False,
                },
            },
            services={
                "bds": {
                    "enabled": True,
                    "dsn": "postgresql://user:pw@localhost:5432/db",
                    "host": "postgres",
                    "port": 5555,
                    "database": "testdb",
                    "user": "user",
                    "password": "pw",
                    "pool_min_size": 2,
                    "pool_max_size": 3,
                    "statement_timeout_ms": 12,
                    "acquire_timeout_ms": 345,
                    "application_name": "test-bds",
                    "queue_max_size": 31,
                    "catchup_enabled": False,
                    "catchup_poll_seconds": 2.0,
                    "rpc_url": "http://localhost:26657",
                    "spool_dir": "/test/spool",
                    "spool_warn_entries": 25,
                    "spool_warn_bytes": 512,
                    "disk_free_warn_bytes": 1024,
                }
            },
        )
    return profile, network


def observe_initialization(tmp_path, case, *flags, profile_changes=None):
    profile, network = initialization_profile(case)
    profile.update(profile_changes or {})
    key = {"address": "profile", "pub_key": {}, "priv_key": {}}
    (tmp_path / "keys").mkdir(exist_ok=True)
    (tmp_path / "keys/profile.json").write_text(json.dumps(key))
    override_key = key | {"address": "override"}
    (tmp_path / "keys/override.json").write_text(json.dumps(override_key))
    real_setup = get_node_setup_module()
    setup = SimpleNamespace(**vars(real_setup))
    setup.render_node_configs = Mock(wraps=real_setup.render_node_configs)
    setup.materialize_cometbft_home = Mock(return_value={"home": "materialized-home"})
    restore = Mock(return_value={"restored": "snapshot"})
    with (
        patch("xian_cli.commands.node.get_node_setup_module", return_value=setup),
        patch(
            "xian_cli.commands.node._load_profile_and_network",
            return_value=(
                tmp_path / "nodes/node.json",
                profile,
                tmp_path / "networks/local.json",
                network,
            ),
        ),
        patch(
            "xian_cli.commands.node._resolve_effective_genesis_payload",
            return_value=(
                {"chain_id": "local-test"},
                "bundle:local",
            ),
        ),
        patch("xian_cli.commands.node._restore_snapshot", restore),
        redirect_stdout(io.StringIO()) as stdout,
    ):
        assert main(["node", "init", "node", "--base-dir", str(tmp_path), *flags]) == 0
    options = setup.render_node_configs.call_args.kwargs["options"]
    return json.loads(json.dumps(asdict(options))), setup, restore, json.loads(stdout.getvalue())


@pytest.mark.parametrize("case", ["defaults", "inherited", "overrides"])
def test_initialization_preserves_every_effective_option(tmp_path, case):
    expected = json.loads(
        (Path(__file__).parent / "fixtures/node_options_baseline.json").read_text()
    )
    options, setup, restore, result = observe_initialization(tmp_path, case)
    assert options == expected[case]
    assert setup.materialize_cometbft_home.call_args.kwargs["home"] == tmp_path / "home"
    assert (
        setup.materialize_cometbft_home.call_args.kwargs["priv_validator_key"]["address"]
        == "profile"
    )
    assert result == {
        "home": "materialized-home",
        "effective_genesis_source": "bundle:local",
        "effective_snapshot_url": None,
        "snapshot_restored": False,
    }
    restore.assert_not_called()


def test_explicit_paths_and_snapshot_follow_the_same_initialization_plan(tmp_path):
    _, setup, restore, result = observe_initialization(
        tmp_path,
        "defaults",
        "--validator-key",
        "keys/override.json",
        "--home",
        "explicit-home",
        "--stack-dir",
        "stack",
        "--snapshot-url",
        "http://localhost/snapshot.tar.gz",
        "--restore-snapshot",
        "--force",
    )
    materialized = setup.materialize_cometbft_home.call_args.kwargs
    assert materialized["home"] == tmp_path / "explicit-home"
    assert materialized["priv_validator_key"]["address"] == "override"
    assert materialized["overwrite"] is True
    assert restore.call_args.kwargs["explicit_home"] == tmp_path / "explicit-home"
    assert restore.call_args.kwargs["stack_dir"] == tmp_path / "stack"
    assert result["effective_snapshot_url"] == "http://localhost/snapshot.tar.gz"
    assert result["snapshot_restored"] is True
    assert result["snapshot"] == {"restored": "snapshot"}


@pytest.mark.parametrize("key_reference", [None, "keys/missing.json"])
def test_initialization_requires_an_existing_validator_key(tmp_path, key_reference):
    with pytest.raises(FileNotFoundError, match="validator key reference is required"):
        observe_initialization(
            tmp_path, "defaults", profile_changes={"validator_key_ref": key_reference}
        )
