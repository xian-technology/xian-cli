from __future__ import annotations

import argparse
import importlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from xian_accounts import Ed25519Account
from xian_py.models import IndexedBlock, TransactionReceipt
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime

import xian_cli.abci_bridge as abci_bridge
import xian_cli.client.handlers as client_handlers
import xian_cli.output as cli_output
from xian_cli.cli import _fallback_node_endpoints, main
from xian_cli.config_repo import (
    resolve_configs_dir,
    resolve_network_manifest_path,
    resolve_network_template_path,
    resolve_solution_pack_path,
)
from xian_cli.models import (
    read_network_manifest,
    read_network_template,
    read_node_profile,
    read_solution_pack,
)
from xian_cli.runtime import (
    default_home_for_backend,
    get_xian_stack_node_endpoints,
    get_xian_stack_node_health,
    get_xian_stack_node_status,
    resolve_stack_dir,
    run_backend_command,
    start_xian_stack_node,
    stop_xian_stack_node,
    wait_for_rpc_ready,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TEST_FIXTURE_GENESIS = (
    Path(__file__).resolve().parent / "fixtures" / "genesis.json"
)
CANONICAL_DEVNET_MANIFEST = (
    WORKSPACE_ROOT / "xian-configs" / "networks" / "devnet" / "manifest.json"
)
CANONICAL_NODE_RELEASE_MANIFEST = json.loads(
    (WORKSPACE_ROOT / "xian-stack" / "release-manifest.json").read_text(
        encoding="utf-8"
    )
)
CANONICAL_RELEASE_INTEGRATED_IMAGE = (
    "ghcr.io/xian-technology/xian-node@sha256:"
    "014527ec7a7e5bc0b63f512421a3d6feedc7b3999c68113d195deb6b41eae6c3"
)
CANONICAL_RELEASE_SPLIT_IMAGE = (
    "ghcr.io/xian-technology/xian-node-split@sha256:"
    "2351ca938fe147af9bed8e827ac9c86de6686dbac228f3822de7e1b4ac41a54c"
)


def _normalized_release_manifest(manifest: dict) -> dict:
    build = manifest["build"]
    return {
        "schema_version": manifest["schema_version"],
        "components": manifest["components"],
        "build": {
            "python_image": build["python_image"],
            "go_image": build["go_image"],
            "cometbft_version": build["cometbft_version"],
            "cometbft_source_url": build["cometbft_source_url"],
            "cometbft_source_sha256": build["cometbft_source_sha256"],
            "s6_overlay_version": build["s6_overlay_version"],
            "s6_overlay_noarch_sha256": build["s6_overlay_noarch_sha256"],
            "s6_overlay_x86_64_sha256": build["s6_overlay_x86_64_sha256"],
            "s6_overlay_aarch64_sha256": build["s6_overlay_aarch64_sha256"],
        },
        "images": manifest["images"],
    }


class ValidatorKeyTests(unittest.TestCase):
    def test_generate_validator_material_shape(self) -> None:
        payload = (
            abci_bridge.get_node_setup_module().generate_validator_material()
        )
        self.assertEqual(len(payload["validator_private_key_hex"]), 64)
        self.assertEqual(len(payload["validator_public_key_hex"]), 64)
        self.assertIn("address", payload["priv_validator_key"])
        self.assertIn("pub_key", payload["priv_validator_key"])
        self.assertIn("priv_key", payload["priv_validator_key"])

    def test_generate_validator_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["keys", "validator", "generate", "--out-dir", tmp_dir]
                )
            self.assertEqual(exit_code, 0)

            output_dir = Path(tmp_dir)
            priv_validator_path = output_dir / "priv_validator_key.json"
            metadata_path = output_dir / "validator_key_info.json"

            self.assertTrue(priv_validator_path.exists())
            self.assertTrue(metadata_path.exists())

            priv_validator_payload = json.loads(
                priv_validator_path.read_text(encoding="utf-8")
            )
            metadata_payload = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )

            self.assertEqual(
                priv_validator_payload["address"],
                metadata_payload["priv_validator_key"]["address"],
            )


class _FakeContextClient:
    def __init__(self, **responses):
        self.responses = responses
        self.send_tx_calls = []
        self.send_calls = []
        self.call_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_balance(self, address: str, contract: str = "currency"):
        return self.responses["balance"]

    def get_tx(self, tx_hash: str):
        return self.responses["tx"]

    def get_block(self, height: int):
        return self.responses["block"]

    def get_block_by_hash(self, block_hash: str):
        return self.responses["block"]

    def call(self, contract: str, function: str, kwargs: dict):
        self.call_calls.append((contract, function, kwargs))
        return self.responses["call"]

    def simulate(self, contract: str, function: str, kwargs: dict):
        return self.responses["simulate"]

    def send_tx(self, **kwargs):
        self.send_tx_calls.append(kwargs)
        return self.responses["send_tx"]

    def send(self, amount, to_address, token="currency", **kwargs):
        self.send_calls.append(
            {
                "amount": amount,
                "to_address": to_address,
                "token": token,
                **kwargs,
            }
        )
        return self.responses["send"]


class ClientCommandTests(unittest.TestCase):
    def test_resolve_node_url_prefers_argument(self) -> None:
        args = argparse.Namespace(node_url="http://node.example")
        self.assertEqual(
            client_handlers._resolve_node_url(args),
            "http://node.example",
        )

    def test_resolve_node_url_uses_environment(self) -> None:
        args = argparse.Namespace(node_url=None)
        with patch.dict("os.environ", {"XIAN_NODE_URL": "http://env-node"}):
            self.assertEqual(
                client_handlers._resolve_node_url(args),
                "http://env-node",
            )

    def test_resolve_node_url_requires_value(self) -> None:
        args = argparse.Namespace(node_url=None)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "node URL is required"):
                client_handlers._resolve_node_url(args)

    def test_resolve_chain_id_uses_environment(self) -> None:
        args = argparse.Namespace(chain_id=None)
        with patch.dict("os.environ", {"XIAN_CHAIN_ID": "xian-1"}):
            self.assertEqual(
                client_handlers._resolve_chain_id(args),
                "xian-1",
            )

    def test_load_private_key_from_file(self) -> None:
        account = Ed25519Account.generate()
        with tempfile.TemporaryDirectory() as tmp_dir:
            key_path = Path(tmp_dir) / "wallet.key"
            key_path.write_text(account.private_key + "\n", encoding="utf-8")
            args = argparse.Namespace(
                private_key=None,
                private_key_env=None,
                private_key_file=str(key_path),
            )
            self.assertEqual(
                client_handlers._load_private_key_from_args(args),
                account.private_key,
            )

    def test_load_private_key_from_env(self) -> None:
        account = Ed25519Account.generate()
        args = argparse.Namespace(
            private_key=None,
            private_key_env="XIAN_PRIVATE_KEY",
            private_key_file=None,
        )
        with patch.dict(
            "os.environ",
            {"XIAN_PRIVATE_KEY": account.private_key},
        ):
            self.assertEqual(
                client_handlers._load_private_key_from_args(args),
                account.private_key,
            )

    def test_load_private_key_requires_single_source(self) -> None:
        account = Ed25519Account.generate()
        args = argparse.Namespace(
            private_key=account.private_key,
            private_key_env="XIAN_PRIVATE_KEY",
            private_key_file=None,
        )
        with patch.dict(
            "os.environ",
            {"XIAN_PRIVATE_KEY": account.private_key},
        ):
            with self.assertRaisesRegex(ValueError, "provide only one"):
                client_handlers._load_private_key_from_args(args)

    def test_load_private_key_requires_value(self) -> None:
        args = argparse.Namespace(
            private_key=None,
            private_key_env=None,
            private_key_file=None,
        )
        with self.assertRaisesRegex(ValueError, "private key is required"):
            client_handlers._load_private_key_from_args(args)

    def test_make_client_constructs_xian(self) -> None:
        args = argparse.Namespace(
            node_url="http://node.example",
            chain_id="xian-1",
        )
        wallet = object()
        with patch(
            "xian_cli.client.handlers.Xian",
            return_value="client",
        ) as ctor:
            client = client_handlers._make_client(args, wallet=wallet)

        self.assertEqual(client, "client")
        ctor.assert_called_once_with(
            "http://node.example",
            chain_id="xian-1",
            wallet=wallet,
        )

    def test_parse_json_object_rejects_invalid_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be valid JSON"):
            client_handlers._parse_json_object(
                "{bad",
                flag_name="--kwargs-json",
            )

    def test_client_wallet_generate_hides_private_key_by_default(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["client", "wallet", "generate"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["address"], payload["public_key"])
        self.assertNotIn("private_key", payload)

    def test_client_wallet_generate_can_emit_and_write_private_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "wallet.key"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "wallet",
                        "generate",
                        "--include-private-key",
                        "--private-key-out",
                        str(out_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(len(payload["private_key"]), 64)
            self.assertEqual(
                out_path.read_text(encoding="utf-8").strip(),
                payload["private_key"],
            )

    def test_client_query_nonce(self) -> None:
        with patch(
            "xian_cli.client.handlers.tx_api.get_nonce", return_value=12
        ) as get_nonce:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "nonce",
                        "--node-url",
                        "http://node.example",
                        "alice",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload, {"address": "alice", "next_nonce": 12})
        get_nonce.assert_called_once_with("http://node.example", "alice")

    def test_client_query_balance_serializes_runtime_types(self) -> None:
        fake_client = _FakeContextClient(balance=ContractingDecimal("12.50"))
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "balance",
                        "--node-url",
                        "http://node.example",
                        "alice",
                        "--contract",
                        "currency",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["balance"], "12.5")
        self.assertEqual(payload["address"], "alice")
        self.assertEqual(payload["contract"], "currency")

    def test_client_query_tx_serializes_receipt_dataclass(self) -> None:
        receipt = TransactionReceipt.from_lookup(
            {
                "success": True,
                "tx_hash": "ABC123",
                "message": {"ok": True},
                "transaction": {"hash": "ABC123"},
                "execution": {"status_code": 0},
            }
        )
        fake_client = _FakeContextClient(tx=receipt)
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "tx",
                        "--node-url",
                        "http://node.example",
                        "ABC123",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["success"])
        self.assertEqual(payload["tx_hash"], "ABC123")
        self.assertEqual(payload["execution"]["status_code"], 0)

    def test_client_query_block_by_height_serializes_dataclass(self) -> None:
        block = IndexedBlock.from_dict(
            {
                "height": 7,
                "hash": "BLOCK123",
                "tx_count": 2,
                "app_hash": "APP123",
                "block_time_iso": "2026-01-02T03:04:05Z",
            }
        )
        fake_client = _FakeContextClient(block=block)
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "block",
                        "--node-url",
                        "http://node.example",
                        "--height",
                        "7",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["height"], 7)
        self.assertEqual(payload["block_hash"], "BLOCK123")

    def test_client_query_block_by_hash_uses_hash_lookup(self) -> None:
        block = IndexedBlock.from_dict({"height": 7, "hash": "BLOCK123"})
        fake_client = _FakeContextClient(block=block)
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "block",
                        "--node-url",
                        "http://node.example",
                        "--block-hash",
                        "BLOCK123",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["block_hash"], "BLOCK123")

    def test_client_call_passes_json_kwargs(self) -> None:
        fake_client = _FakeContextClient(call={"ok": True})
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "call",
                        "--node-url",
                        "http://node.example",
                        "currency",
                        "balance_of",
                        "--kwargs-json",
                        '{"address":"alice"}',
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["result"], {"ok": True})
        self.assertEqual(
            fake_client.call_calls,
            [("currency", "balance_of", {"address": "alice"})],
        )

    def test_client_call_rejects_non_object_kwargs(self) -> None:
        with self.assertRaisesRegex(ValueError, "must decode to a JSON object"):
            main(
                [
                    "client",
                    "call",
                    "--node-url",
                    "http://node.example",
                    "currency",
                    "balance_of",
                    "--kwargs-json",
                    '["alice"]',
                ]
            )

    def test_client_simulate_returns_payload(self) -> None:
        fake_client = _FakeContextClient(
            simulate={"status_code": 0, "chi_used": 17}
        )
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "simulate",
                        "--node-url",
                        "http://node.example",
                        "currency",
                        "transfer",
                        "--kwargs-json",
                        '{"amount":1,"to":"bob"}',
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status_code"], 0)
        self.assertEqual(payload["chi_used"], 17)

    def test_client_tx_send_uses_submission_args(self) -> None:
        fake_client = _FakeContextClient(
            send_tx={
                "submitted": True,
                "tx_hash": "ABC123",
                "mode": "checktx",
            }
        )
        with (
            patch(
                "xian_cli.client.handlers._make_client",
                return_value=fake_client,
            ),
            patch(
                "xian_cli.client.handlers._build_wallet",
                return_value=object(),
            ),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "tx",
                        "send",
                        "--node-url",
                        "http://node.example",
                        "currency",
                        "approve",
                        "--kwargs-json",
                        '{"to":"bob","amount":7}',
                        "--mode",
                        "checktx",
                        "--wait-for-tx",
                        "--timeout-seconds",
                        "5",
                        "--chi",
                        "123",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tx_hash"], "ABC123")
        self.assertEqual(len(fake_client.send_tx_calls), 1)
        call = fake_client.send_tx_calls[0]
        self.assertEqual(call["contract"], "currency")
        self.assertEqual(call["function"], "approve")
        self.assertEqual(call["kwargs"], {"to": "bob", "amount": 7})
        self.assertEqual(call["mode"], "checktx")
        self.assertTrue(call["wait_for_tx"])
        self.assertEqual(call["timeout_seconds"], 5.0)
        self.assertEqual(call["chi"], 123)

    def test_client_tx_transfer(self) -> None:
        fake_client = _FakeContextClient(
            send={
                "submitted": True,
                "tx_hash": "XYZ789",
                "mode": "async",
            }
        )
        with (
            patch(
                "xian_cli.client.handlers._make_client",
                return_value=fake_client,
            ),
            patch(
                "xian_cli.client.handlers._build_wallet",
                return_value=object(),
            ),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "tx",
                        "transfer",
                        "--node-url",
                        "http://node.example",
                        "bob",
                        "1.25",
                        "--token",
                        "currency",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tx_hash"], "XYZ789")
        self.assertEqual(len(fake_client.send_calls), 1)
        call = fake_client.send_calls[0]
        self.assertEqual(call["amount"], "1.25")
        self.assertEqual(call["to_address"], "bob")
        self.assertEqual(call["token"], "currency")

    def test_client_tx_transfer_uses_private_key_env(self) -> None:
        account = Ed25519Account.generate()
        fake_client = _FakeContextClient(
            send={"submitted": True, "tx_hash": "XYZ789", "mode": "async"}
        )
        with (
            patch(
                "xian_cli.client.handlers._make_client",
                return_value=fake_client,
            ),
            patch.dict(
                "os.environ",
                {
                    "XIAN_PRIVATE_KEY": account.private_key,
                    "XIAN_NODE_URL": "http://node.example",
                },
            ),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "tx",
                        "transfer",
                        "--private-key-env",
                        "XIAN_PRIVATE_KEY",
                        "bob",
                        "2",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tx_hash"], "XYZ789")

    def test_client_tx_send_requires_private_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "private key is required"):
            main(
                [
                    "client",
                    "tx",
                    "send",
                    "--node-url",
                    "http://node.example",
                    "currency",
                    "approve",
                ]
            )


class ClientOutputTests(unittest.TestCase):
    def test_to_jsonable_handles_runtime_types_and_bytes(self) -> None:
        receipt = TransactionReceipt.from_lookup(
            {
                "success": True,
                "tx_hash": "ABC123",
                "message": ContractingDecimal("1.5"),
                "transaction": {"created": Datetime(2026, 1, 2, 3, 4, 5)},
                "execution": {"payload": b"hello", "raw": b"\xff"},
            }
        )

        payload = cli_output.to_jsonable(receipt)
        self.assertEqual(payload["message"], "1.5")
        self.assertEqual(
            payload["transaction"]["created"],
            "2026-01-02 03:04:05",
        )
        self.assertEqual(payload["execution"]["payload"], "hello")
        self.assertEqual(payload["execution"]["raw"], "ff")

    def test_emit_json_writes_newline_terminated_json(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli_output.emit_json({"ok": True})

        self.assertEqual(stdout.getvalue(), '{\n  "ok": true\n}\n')

    def test_to_jsonable_handles_lists_and_tuples(self) -> None:
        payload = cli_output.to_jsonable({"items": [1, ("a", b"\xff")]})
        self.assertEqual(payload, {"items": [1, ["a", "ff"]]})


class NetworkManifestTests(unittest.TestCase):
    def test_network_template_list_reads_canonical_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            templates_dir = configs_dir / "templates"
            templates_dir.mkdir(parents=True)
            (templates_dir / "single-node-dev.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "single-node-dev",
                        "display_name": "Single-Node Dev",
                        "description": "Local dev template",
                        "runtime_backend": "xian-stack",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                        "operator_profile": "local_development",
                        "monitoring_profile": "none",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "service_node": False,
                        "dashboard_enabled": True,
                        "monitoring_enabled": False,
                        "dashboard_host": "0.0.0.0",
                        "dashboard_port": 18080,
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "template",
                        "list",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["name"], "single-node-dev")
            self.assertEqual(
                payload[0]["operator_profile"], "local_development"
            )
            self.assertTrue(payload[0]["dashboard_enabled"])

    def test_network_create_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "local.json"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            manifest = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["name"], "local-dev")
            self.assertEqual(manifest["chain_id"], "xian-local-1")
            self.assertEqual(manifest["mode"], "create")
            self.assertEqual(manifest["runtime_backend"], "xian-stack")
            self.assertEqual(manifest["block_policy_mode"], "on_demand")
            self.assertEqual(manifest["block_policy_interval"], "0s")
            self.assertEqual(manifest["tracer_mode"], "python_line_v1")
            self.assertEqual(manifest["node_image_mode"], "local_build")
            self.assertIsNone(manifest["node_integrated_image"])
            self.assertIsNone(manifest["node_split_image"])
            self.assertIsNone(manifest["node_release_manifest"])

    def test_network_create_defaults_to_network_first_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--chain-id",
                        "xian-local-1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            manifest_path = (
                base_dir / "networks" / "local-dev" / "manifest.json"
            )
            self.assertEqual(
                result["manifest_path"],
                str(manifest_path.resolve()),
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["mode"], "create")
            self.assertIsNone(manifest["genesis_source"])
            self.assertEqual(manifest["block_policy_mode"], "on_demand")
            self.assertEqual(manifest["block_policy_interval"], "0s")
            self.assertEqual(manifest["tracer_mode"], "python_line_v1")
            self.assertEqual(manifest["node_image_mode"], "local_build")

    def test_network_create_dry_run_validates_without_writing_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--chain-id",
                        "xian-local-1",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            manifest_path = (
                base_dir / "networks" / "local-dev" / "manifest.json"
            )
            self.assertTrue(payload["dry_run"])
            self.assertEqual(
                payload["manifest_path"],
                str(manifest_path.resolve()),
            )
            self.assertFalse(manifest_path.exists())

    def test_read_network_manifest_accepts_preset_built_genesis(self) -> None:
        manifest = read_network_manifest(CANONICAL_DEVNET_MANIFEST)
        self.assertIsNone(manifest["genesis_source"])
        self.assertEqual(manifest["genesis_preset"], "devnet")
        self.assertEqual(
            manifest["genesis_time"], "2026-03-30T00:00:00.000000Z"
        )

    def test_read_network_manifest_accepts_privacy_policy_surfaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "privacy-test",
                        "chain_id": "xian-privacy-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "node_image_mode": "local_build",
                        "shielded_relayer": None,
                        "shielded_relayers": [],
                        "privacy_artifact_catalog": {
                            "path": "./privacy/artifacts.json",
                            "sha256": "a" * 64,
                        },
                        "shielded_history_policy": {
                            "feed_version": 1,
                            "compatibility_commitment": "versioned",
                            "retention_class": "archive",
                            "bds_snapshot_support": True,
                            "operator_notice": (
                                "retain encrypted payload history"
                            ),
                        },
                        "privacy_submission_policy": {
                            "disclosure_policy": "user_controlled",
                            "shared_relayer_auth_required": True,
                            "hidden_sender_submission_mode": "relayer_optional",
                            "operator_notice": (
                                "shared relayers require operator auth"
                            ),
                        },
                        "genesis_preset": "devnet",
                        "genesis_time": "2026-03-30T00:00:00.000000Z",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            manifest = read_network_manifest(manifest_path)

        self.assertEqual(
            manifest["privacy_artifact_catalog"]["path"],
            "./privacy/artifacts.json",
        )
        self.assertEqual(
            manifest["shielded_history_policy"]["compatibility_commitment"],
            "versioned",
        )
        self.assertEqual(
            manifest["privacy_submission_policy"][
                "hidden_sender_submission_mode"
            ],
            "relayer_optional",
        )

    def test_read_network_manifest_rejects_incomplete_registry_image_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "node_image_mode": "registry",
                        "node_integrated_image": (
                            "ghcr.io/xian-technology/xian-node@sha256:abc"
                        ),
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "requires both node_integrated_image"
            ):
                read_network_manifest(manifest_path)

    def test_network_create_uses_template_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            templates_dir = configs_dir / "templates"
            templates_dir.mkdir(parents=True)
            (templates_dir / "single-node-indexed.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "single-node-indexed",
                        "display_name": "Single-Node Indexed",
                        "description": "Indexed single node",
                        "runtime_backend": "xian-stack",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "native_instruction_v1",
                        "transaction_trace_logging": True,
                        "app_log_level": "DEBUG",
                        "app_log_json": True,
                        "app_log_rotation_hours": 4,
                        "app_log_retention_days": 10,
                        "simulation_enabled": False,
                        "simulation_max_concurrency": 3,
                        "simulation_timeout_ms": 2500,
                        "simulation_max_chi": 500000,
                        "parallel_execution_enabled": True,
                        "parallel_execution_workers": 4,
                        "parallel_execution_min_transactions": 12,
                        "operator_profile": "indexed_development",
                        "monitoring_profile": "local_stack",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "service_node": True,
                        "dashboard_enabled": True,
                        "monitoring_enabled": True,
                        "dashboard_host": "0.0.0.0",
                        "dashboard_port": 18080,
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--chain-id",
                        "xian-local-1",
                        "--template",
                        "single-node-indexed",
                        "--generate-validator-key",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            manifest = json.loads(
                (
                    base_dir / "networks" / "local-dev" / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(result["template"], "single-node-indexed")
            self.assertEqual(manifest["tracer_mode"], "native_instruction_v1")
            self.assertTrue(profile["service_node"])
            self.assertTrue(profile["transaction_trace_logging"])
            self.assertEqual(profile["app_log_level"], "DEBUG")
            self.assertTrue(profile["app_log_json"])
            self.assertEqual(profile["app_log_rotation_hours"], 4)
            self.assertEqual(profile["app_log_retention_days"], 10)
            self.assertFalse(profile["simulation_enabled"])
            self.assertEqual(profile["simulation_max_concurrency"], 3)
            self.assertEqual(profile["simulation_timeout_ms"], 2500)
            self.assertEqual(profile["simulation_max_chi"], 500000)
            self.assertTrue(profile["parallel_execution_enabled"])
            self.assertEqual(profile["parallel_execution_workers"], 4)
            self.assertEqual(profile["parallel_execution_min_transactions"], 12)
            self.assertEqual(profile["operator_profile"], "indexed_development")
            self.assertEqual(profile["monitoring_profile"], "local_stack")
            self.assertTrue(profile["dashboard_enabled"])
            self.assertTrue(profile["monitoring_enabled"])
            self.assertEqual(profile["dashboard_host"], "0.0.0.0")
            self.assertEqual(profile["dashboard_port"], 18080)

    def test_network_join_writes_node_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "validator-1.json"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--network",
                        "devnet",
                        "--seed",
                        "abc@127.0.0.1:26656",
                        "--service-node",
                        "--enable-dashboard",
                        "--dashboard-host",
                        "0.0.0.0",
                        "--dashboard-port",
                        "18080",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["schema_version"], 1)
            self.assertEqual(profile["name"], "validator-1")
            self.assertEqual(profile["network"], "devnet")
            self.assertEqual(profile["moniker"], "validator-1")
            self.assertEqual(profile["seeds"], ["abc@127.0.0.1:26656"])
            self.assertTrue(profile["service_node"])
            self.assertTrue(profile["dashboard_enabled"])
            self.assertEqual(profile["dashboard_host"], "0.0.0.0")
            self.assertEqual(profile["dashboard_port"], 18080)
            self.assertEqual(profile["block_policy_mode"], "idle_interval")
            self.assertEqual(profile["block_policy_interval"], "5s")
            self.assertEqual(profile["tracer_mode"], "native_instruction_v1")
            self.assertEqual(profile["node_image_mode"], "local_build")
            self.assertIsNone(profile["node_integrated_image"])
            self.assertIsNone(profile["node_split_image"])

    def test_network_join_dry_run_validates_without_writing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "devnet",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            profile_path = base_dir / "nodes" / "validator-1.json"
            self.assertTrue(payload["dry_run"])
            self.assertEqual(
                payload["node_profile_path"],
                str(profile_path.resolve()),
            )
            self.assertFalse(profile_path.exists())

    def test_network_create_writes_dashboard_settings_to_bootstrap_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            key_dir = base_dir / "keys" / "local-dev"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "keys",
                            "validator",
                            "generate",
                            "--out-dir",
                            str(key_dir),
                        ]
                    ),
                    0,
                )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--chain-id",
                        "xian-local-1",
                        "--bootstrap-node",
                        "local-dev",
                        "--validator-key-ref",
                        str(key_dir / "validator_key_info.json"),
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--enable-dashboard",
                        "--dashboard-host",
                        "0.0.0.0",
                        "--dashboard-port",
                        "18080",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            profile_path = Path(result["validators"][0]["profile_path"])
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertTrue(profile["dashboard_enabled"])
            self.assertEqual(profile["dashboard_host"], "0.0.0.0")
            self.assertEqual(profile["dashboard_port"], 18080)

    def test_network_join_uses_canonical_manifest_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": "https://example.invalid/snapshot",
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
                        "block_policy_mode": "periodic",
                        "block_policy_interval": "10s",
                        "tracer_mode": "native_instruction_v1",
                        "node_image_mode": "registry",
                        "node_integrated_image": (
                            "ghcr.io/xian-technology/xian-node@sha256:abc"
                        ),
                        "node_split_image": (
                            "ghcr.io/xian-technology/xian-node-split@sha256:def"
                        ),
                        "node_release_manifest": (
                            CANONICAL_NODE_RELEASE_MANIFEST
                        ),
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["runtime_backend"], "xian-stack")
            self.assertEqual(profile["seeds"], [])
            self.assertIsNone(profile["snapshot_url"])
            self.assertEqual(profile["block_policy_mode"], "periodic")
            self.assertEqual(profile["block_policy_interval"], "10s")
            self.assertEqual(profile["tracer_mode"], "native_instruction_v1")
            self.assertEqual(profile["node_image_mode"], "registry")
            self.assertEqual(
                profile["node_integrated_image"],
                "ghcr.io/xian-technology/xian-node@sha256:abc",
            )
            self.assertEqual(
                profile["node_split_image"],
                "ghcr.io/xian-technology/xian-node-split@sha256:def",
            )
            self.assertEqual(
                profile["node_release_manifest"],
                _normalized_release_manifest(CANONICAL_NODE_RELEASE_MANIFEST),
            )

    def test_network_join_uses_template_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            templates_dir = configs_dir / "templates"
            network_dir.mkdir(parents=True)
            templates_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )
            (templates_dir / "embedded-backend.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "embedded-backend",
                        "display_name": "Embedded Backend",
                        "description": "App backend defaults",
                        "runtime_backend": "xian-stack",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "native_instruction_v1",
                        "transaction_trace_logging": True,
                        "app_log_level": "WARNING",
                        "app_log_json": True,
                        "app_log_rotation_hours": 6,
                        "app_log_retention_days": 12,
                        "simulation_enabled": True,
                        "simulation_max_concurrency": 4,
                        "simulation_timeout_ms": 4000,
                        "simulation_max_chi": 800000,
                        "parallel_execution_enabled": True,
                        "parallel_execution_workers": 3,
                        "parallel_execution_min_transactions": 9,
                        "operator_profile": "embedded_backend",
                        "monitoring_profile": "service_node",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "service_node": True,
                        "dashboard_enabled": False,
                        "monitoring_enabled": True,
                        "dashboard_host": "127.0.0.1",
                        "dashboard_port": 8080,
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--template",
                        "embedded-backend",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertTrue(profile["service_node"])
            self.assertTrue(profile["transaction_trace_logging"])
            self.assertEqual(profile["app_log_level"], "WARNING")
            self.assertTrue(profile["app_log_json"])
            self.assertEqual(profile["app_log_rotation_hours"], 6)
            self.assertEqual(profile["app_log_retention_days"], 12)
            self.assertTrue(profile["simulation_enabled"])
            self.assertEqual(profile["simulation_max_concurrency"], 4)
            self.assertEqual(profile["simulation_timeout_ms"], 4000)
            self.assertEqual(profile["simulation_max_chi"], 800000)
            self.assertTrue(profile["parallel_execution_enabled"])
            self.assertEqual(profile["parallel_execution_workers"], 3)
            self.assertEqual(profile["parallel_execution_min_transactions"], 9)
            self.assertEqual(profile["operator_profile"], "embedded_backend")
            self.assertEqual(profile["monitoring_profile"], "service_node")
            self.assertTrue(profile["monitoring_enabled"])
            self.assertFalse(profile["dashboard_enabled"])
            self.assertEqual(profile["tracer_mode"], "native_instruction_v1")

    def test_network_join_drops_release_manifest_for_local_build_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                        "node_image_mode": "registry",
                        "node_integrated_image": (
                            "ghcr.io/xian-technology/xian-node@sha256:abc"
                        ),
                        "node_split_image": (
                            "ghcr.io/xian-technology/xian-node-split@sha256:def"
                        ),
                        "node_release_manifest": (
                            CANONICAL_NODE_RELEASE_MANIFEST
                        ),
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--node-image-mode",
                            "local_build",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["node_image_mode"], "local_build")
            self.assertIsNone(profile["node_integrated_image"])
            self.assertIsNone(profile["node_split_image"])
            self.assertIsNone(profile["node_release_manifest"])

    def test_network_join_allows_block_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--block-policy-mode",
                            "idle_interval",
                            "--block-policy-interval",
                            "10s",
                            "--tracer-mode",
                            "native_instruction_v1",
                            "--transaction-trace-logging",
                            "--app-log-level",
                            "ERROR",
                            "--app-log-json",
                            "--app-log-rotation-hours",
                            "5",
                            "--app-log-retention-days",
                            "15",
                            "--simulation-enabled",
                            "--simulation-max-concurrency",
                            "5",
                            "--simulation-timeout-ms",
                            "3500",
                            "--simulation-max-chi",
                            "900000",
                            "--parallel-execution-enabled",
                            "--parallel-execution-workers",
                            "6",
                            "--parallel-execution-min-transactions",
                            "14",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["block_policy_mode"], "idle_interval")
            self.assertEqual(profile["block_policy_interval"], "10s")
            self.assertEqual(profile["tracer_mode"], "native_instruction_v1")
            self.assertTrue(profile["transaction_trace_logging"])
            self.assertEqual(profile["app_log_level"], "ERROR")
            self.assertTrue(profile["app_log_json"])
            self.assertEqual(profile["app_log_rotation_hours"], 5)
            self.assertEqual(profile["app_log_retention_days"], 15)
            self.assertTrue(profile["simulation_enabled"])
            self.assertEqual(profile["simulation_max_concurrency"], 5)
            self.assertEqual(profile["simulation_timeout_ms"], 3500)
            self.assertEqual(profile["simulation_max_chi"], 900000)
            self.assertTrue(profile["parallel_execution_enabled"])
            self.assertEqual(profile["parallel_execution_workers"], 6)
            self.assertEqual(profile["parallel_execution_min_transactions"], 14)

    def test_network_join_allows_node_local_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--seed",
                            "local-seed@127.0.0.1:26656",
                            "--snapshot-url",
                            "https://example.invalid/node-snapshot",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["runtime_backend"], "xian-stack")
            self.assertEqual(profile["seeds"], ["local-seed@127.0.0.1:26656"])
            self.assertEqual(
                profile["snapshot_url"],
                "https://example.invalid/node-snapshot",
            )
            self.assertEqual(profile["tracer_mode"], "python_line_v1")

    def test_network_join_rejects_negative_parallel_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "parallel_execution_workers must be a non-negative integer",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--parallel-execution-workers",
                        "-1",
                    ]
                )

    def test_network_join_rejects_non_positive_simulation_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "simulation_max_concurrency must be a positive integer",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--simulation-max-concurrency",
                        "0",
                    ]
                )

    def test_network_join_can_generate_validator_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--generate-validator-key",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            self.assertTrue(
                (
                    base_dir
                    / "keys"
                    / "validator-1"
                    / "priv_validator_key.json"
                ).exists()
            )
            self.assertTrue(
                (
                    base_dir
                    / "keys"
                    / "validator-1"
                    / "validator_key_info.json"
                ).exists()
            )

    def test_network_join_requires_known_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(FileNotFoundError, "network manifest"):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        tmp_dir,
                        "--network",
                        "does-not-exist",
                    ]
                )

    def test_network_join_rejects_validator_key_dir_without_generation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(
                ValueError,
                "--validator-key-dir requires --generate-validator-key",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        tmp_dir,
                        "--network",
                        "devnet",
                        "--validator-key-dir",
                        "keys/validator-1",
                    ]
                )

    def test_network_join_rejects_restore_snapshot_without_init_node(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(
                ValueError,
                "--restore-snapshot requires --init-node",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        tmp_dir,
                        "--network",
                        "devnet",
                        "--restore-snapshot",
                    ]
                )

    def test_network_join_can_initialize_node_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--generate-validator-key",
                        "--init-node",
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                        "--parallel-execution-enabled",
                        "--parallel-execution-workers",
                        "5",
                        "--parallel-execution-min-transactions",
                        "11",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["node_initialized"])
            home = Path(result["node_init"]["home"])
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "genesis.json").exists())
            config_toml = (home / "config" / "config.toml").read_text(
                encoding="utf-8"
            )
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            self.assertIn('metrics_host = "0.0.0.0"', config_toml)

    def test_network_create_can_bootstrap_local_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / ".cometbft"
            stdout = io.StringIO()

            fake_genesis = {
                "chain_id": "xian-local-1",
                "validators": [],
                "abci_genesis": {
                    "genesis": [
                        {
                            "key": "currency.balances:founder",
                            "value": ContractingDecimal("1000000.5"),
                        },
                        {
                            "key": "currency.streams:example",
                            "value": Datetime(
                                year=2026,
                                month=1,
                                day=1,
                                hour=0,
                                minute=0,
                            ),
                        },
                    ]
                },
            }
            genesis_builder = type(
                "GenesisBuilder",
                (),
                {
                    "build_local_network_genesis": staticmethod(
                        unittest.mock.Mock(return_value=fake_genesis)
                    )
                },
            )()

            with patch(
                "xian_cli.cli.get_genesis_builder_module",
                return_value=genesis_builder,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "network",
                            "create",
                            "local-dev",
                            "--base-dir",
                            str(base_dir),
                            "--chain-id",
                            "xian-local-1",
                            "--generate-validator-key",
                            "--bootstrap-node",
                            "validator-1",
                            "--init-node",
                            "--home",
                            str(home),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            manifest_path = (
                base_dir / "networks" / "local-dev" / "manifest.json"
            )
            profile_path = base_dir / "nodes" / "validator-1.json"
            genesis_path = base_dir / "networks" / "local-dev" / "genesis.json"

            self.assertEqual(
                result["manifest_path"],
                str(manifest_path.resolve()),
            )
            self.assertEqual(
                result["profile_path"],
                str(profile_path.resolve()),
            )
            self.assertTrue(result["node_initialized"])
            self.assertTrue(genesis_path.exists())
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue(
                (
                    base_dir
                    / "keys"
                    / "validator-1"
                    / "validator_key_info.json"
                ).exists()
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            generated_genesis = json.loads(
                genesis_path.read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["genesis_source"], "./genesis.json")
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            self.assertEqual(
                generated_genesis["abci_genesis"]["genesis"][0]["value"],
                {"__fixed__": "1000000.5"},
            )
            self.assertEqual(
                generated_genesis["abci_genesis"]["genesis"][1]["value"],
                {"__time__": [2026, 1, 1, 0, 0, 0, 0]},
            )
            (genesis_builder.build_local_network_genesis.assert_called_once())

    def test_network_create_can_generate_multiple_initial_validators(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            fake_genesis = {
                "chain_id": "xian-local-1",
                "validators": [
                    {"name": "validator-1"},
                    {"name": "validator-2"},
                ],
                "abci_genesis": {},
            }
            genesis_builder = type(
                "GenesisBuilder",
                (),
                {
                    "build_local_network_genesis": staticmethod(
                        unittest.mock.Mock(return_value=fake_genesis)
                    )
                },
            )()

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_genesis_builder_module",
                return_value=genesis_builder,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "network",
                            "create",
                            "local-dev",
                            "--base-dir",
                            str(base_dir),
                            "--chain-id",
                            "xian-local-1",
                            "--generate-validator-key",
                            "--bootstrap-node",
                            "validator-1",
                            "--validator",
                            "validator-2",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(len(result["validators"]), 2)
            self.assertEqual(
                [item["name"] for item in result["validators"]],
                ["validator-1", "validator-2"],
            )
            self.assertTrue(
                (
                    base_dir
                    / "keys"
                    / "validator-2"
                    / "validator_key_info.json"
                ).exists()
            )
            validator_two_profile = json.loads(
                (base_dir / "nodes" / "validator-2.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                validator_two_profile["validator_key_ref"],
                "keys/validator-2/validator_key_info.json",
            )
            genesis_builder.build_local_network_genesis.assert_called_once()
            kwargs = (
                genesis_builder.build_local_network_genesis.call_args.kwargs
            )
            self.assertEqual(len(kwargs["validators"]), 2)


class AbciBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        abci_bridge.get_node_setup_module.cache_clear()
        abci_bridge.get_node_admin_module.cache_clear()
        abci_bridge.get_genesis_builder_module.cache_clear()

    def tearDown(self) -> None:
        abci_bridge.get_node_setup_module.cache_clear()
        abci_bridge.get_node_admin_module.cache_clear()
        abci_bridge.get_genesis_builder_module.cache_clear()

    def test_bridge_imports_installed_modules(self) -> None:
        node_setup_module = abci_bridge.get_node_setup_module()
        node_admin_module = abci_bridge.get_node_admin_module()

        self.assertEqual(node_setup_module.__name__, "xian.node_setup")
        self.assertEqual(node_admin_module.__name__, "xian.node_admin")

    def test_bridge_errors_when_helpers_are_unavailable(self) -> None:
        original_import_module = importlib.import_module

        def fake_import_module(name: str):
            if name.startswith("xian."):
                raise ModuleNotFoundError(name="xian")
            return original_import_module(name)

        with patch(
            "xian_cli.abci_bridge.import_module",
            side_effect=fake_import_module,
        ):
            with self.assertRaisesRegex(RuntimeError, "xian-abci helpers"):
                abci_bridge.get_node_setup_module()
            with self.assertRaisesRegex(RuntimeError, "xian-abci helpers"):
                abci_bridge.get_node_admin_module()
            with self.assertRaisesRegex(RuntimeError, "xian-abci helpers"):
                abci_bridge.get_genesis_builder_module()


class NodeInitTests(unittest.TestCase):
    def test_node_init_materializes_home_from_canonical_preset_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(WORKSPACE_ROOT / "xian-configs"),
                        "--network",
                        "devnet",
                        "--validator-key-ref",
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--init-node",
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["node_init"]["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )

            self.assertEqual(rendered_genesis["chain_id"], "xian-devnet-1")
            self.assertEqual(
                result["node_init"]["effective_genesis_source"],
                "preset:devnet",
            )
            self.assertEqual(len(rendered_genesis["validators"]), 2)
            self.assertEqual(
                rendered_genesis["abci_genesis"]["origin"],
                {"sender": "", "signature": ""},
            )

    def test_node_init_materializes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--seed",
                        "seed1@127.0.0.1:26656",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--validator-key-ref",
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--transaction-trace-logging",
                        "--app-log-level",
                        "WARNING",
                        "--app-log-json",
                        "--app-log-rotation-hours",
                        "8",
                        "--app-log-retention-days",
                        "21",
                        "--simulation-enabled",
                        "--simulation-max-concurrency",
                        "4",
                        "--simulation-timeout-ms",
                        "3200",
                        "--simulation-max-chi",
                        "700000",
                        "--parallel-execution-enabled",
                        "--parallel-execution-workers",
                        "5",
                        "--parallel-execution-min-transactions",
                        "11",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "node",
                        "init",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "genesis.json").exists())
            self.assertTrue(
                (home / "config" / "priv_validator_key.json").exists()
            )
            self.assertTrue((home / "config" / "node_key.json").exists())
            self.assertTrue(
                (home / "data" / "priv_validator_state.json").exists()
            )
            config_toml = (home / "config" / "config.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn("transaction_trace_logging = true", config_toml)
            self.assertIn('app_log_level = "WARNING"', config_toml)
            self.assertIn("app_log_json = true", config_toml)
            self.assertIn("app_log_rotation_hours = 8", config_toml)
            self.assertIn("app_log_retention_days = 21", config_toml)
            self.assertIn("simulation_enabled = true", config_toml)
            self.assertIn("simulation_max_concurrency = 4", config_toml)
            self.assertIn("simulation_timeout_ms = 3200", config_toml)
            self.assertIn("simulation_max_chi = 700000", config_toml)
            self.assertIn("parallel_execution_enabled = true", config_toml)
            self.assertIn("parallel_execution_workers = 5", config_toml)
            self.assertIn(
                "parallel_execution_min_transactions = 11",
                config_toml,
            )

    def test_node_init_supports_remote_genesis_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)
            (base_dir / "xian-stack").mkdir()

            genesis_payload = {
                "chain_id": "xian-remote-1",
                "validators": [],
                "abci_genesis": {},
            }
            genesis_url = "https://example.invalid/genesis.json"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "remote-dev",
                        "--chain-id",
                        "xian-remote-1",
                        "--genesis-source",
                        genesis_url,
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "remote-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "remote-dev",
                        "--validator-key-ref",
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.cli.fetch_json", return_value=genesis_payload):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-remote-1")
            self.assertEqual(
                home,
                (base_dir / "xian-stack" / ".cometbft").resolve(),
            )

    def test_node_init_prefers_profile_genesis_url_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)
            (base_dir / "xian-stack").mkdir()

            local_genesis_path = base_dir / "genesis-local.json"
            local_genesis_path.write_text(
                json.dumps(
                    {
                        "chain_id": "xian-wrong-1",
                        "validators": [],
                        "abci_genesis": {},
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "override-dev",
                        "--chain-id",
                        "xian-override-1",
                        "--genesis-source",
                        str(local_genesis_path),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "override-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "override-dev",
                        "--validator-key-ref",
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--genesis-url",
                        "https://example.invalid/override-genesis.json",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.fetch_json",
                return_value={
                    "chain_id": "xian-override-1",
                    "validators": [],
                    "abci_genesis": {},
                },
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-override-1")

    def test_node_init_accepts_raw_priv_validator_key_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            key_dir = base_dir / "keys" / "validator-1"
            key_dir.mkdir(parents=True)

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(key_dir),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--validator-key-ref",
                        str(key_dir / "priv_validator_key.json"),
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "node",
                        "init",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            self.assertTrue(
                (home / "config" / "priv_validator_key.json").exists()
            )

    def test_node_init_rejects_chain_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "bad-chain",
                        "--chain-id",
                        "xian-does-not-match",
                        "--genesis-source",
                        str(genesis_source),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "bad-chain"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "bad-chain",
                        "--validator-key-ref",
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            with self.assertRaisesRegex(ValueError, "does not match manifest"):
                main(
                    [
                        "node",
                        "init",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

    def test_node_init_uses_xian_configs_when_local_manifest_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "nodes").mkdir()
            key_dir = base_dir / "keys" / "validator-1"
            key_dir.mkdir(parents=True)
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)

            genesis_payload = {
                "chain_id": "xian-canonical-1",
                "validators": [],
                "abci_genesis": {},
            }
            (network_dir / "genesis.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(key_dir),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "canonical",
                        "--validator-key-ref",
                        str(key_dir / "validator_key_info.json"),
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            rendered_config = (home / "config" / "config.toml").read_text(
                encoding="utf-8"
            )

            self.assertEqual(rendered_genesis["chain_id"], "xian-canonical-1")
            self.assertIn("seed-1@127.0.0.1:26656", rendered_config)

    def test_node_init_can_restore_snapshot_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            key_dir = base_dir / "keys" / "validator-1"
            key_dir.mkdir(parents=True)
            home = base_dir / ".cometbft"

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--snapshot-url",
                        "https://example.invalid/snapshot-manifest.json",
                        "--snapshot-signing-key",
                        "a" * 64,
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(key_dir),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--validator-key-ref",
                        str(key_dir / "validator_key_info.json"),
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            snapshot_mock = unittest.mock.Mock(return_value="snapshot.tar.gz")
            node_admin = type(
                "NodeAdmin",
                (),
                {"apply_snapshot_archive": staticmethod(snapshot_mock)},
            )()
            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_node_admin_module",
                return_value=node_admin,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--restore-snapshot",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            snapshot_mock.assert_called_once_with(
                "https://example.invalid/snapshot-manifest.json",
                home,
                trusted_manifest_public_keys=["a" * 64],
                expected_chain_id="xian-testnet-12",
            )
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["snapshot_restored"])
            self.assertEqual(
                result["effective_snapshot_url"],
                "https://example.invalid/snapshot-manifest.json",
            )
            self.assertEqual(
                result["snapshot"]["snapshot_archive_name"],
                "snapshot.tar.gz",
            )


class NodeRuntimeTests(unittest.TestCase):
    def test_node_start_uses_xian_stack_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            config_dir = stack_dir / ".cometbft" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--enable-dashboard",
                        "--enable-monitoring",
                        "--dashboard-host",
                        "0.0.0.0",
                        "--dashboard-port",
                        "18080",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.cli.start_xian_stack_node") as start_node:
                start_node.return_value = {
                    "stack_dir": str(stack_dir),
                    "container_target": "abci-up",
                    "node_target": "node-start",
                    "rpc_checked": False,
                }
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "start",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--skip-health-check",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(start_node.call_count, 1)
            kwargs = start_node.call_args.kwargs
            self.assertEqual(kwargs["stack_dir"], stack_dir.resolve())
            self.assertEqual(
                kwargs["cometbft_home"],
                (stack_dir / ".cometbft").resolve(),
            )
            self.assertFalse(kwargs["service_node"])
            self.assertTrue(kwargs["dashboard_enabled"])
            self.assertTrue(kwargs["monitoring_enabled"])
            self.assertEqual(kwargs["dashboard_host"], "0.0.0.0")
            self.assertEqual(kwargs["dashboard_port"], 18080)
            self.assertFalse(kwargs["wait_for_rpc"])
            self.assertEqual(kwargs["node_image_mode"], "local_build")
            self.assertIsNone(kwargs["node_integrated_image"])
            self.assertIsNone(kwargs["node_split_image"])

    def test_node_start_inherits_registry_images_from_network(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks" / "mainnet").mkdir(parents=True)
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            config_dir = stack_dir / ".cometbft" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            (base_dir / "networks" / "mainnet" / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "mainnet",
                        "chain_id": "xian-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "node_image_mode": "registry",
                        "node_integrated_image": (
                            CANONICAL_RELEASE_INTEGRATED_IMAGE
                        ),
                        "node_split_image": CANONICAL_RELEASE_SPLIT_IMAGE,
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )
            (base_dir / "nodes" / "validator-1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "validator-1",
                        "network": "mainnet",
                        "moniker": "validator-1",
                        "runtime_backend": "xian-stack",
                        "stack_dir": str(stack_dir),
                        "seeds": [],
                        "genesis_url": None,
                        "snapshot_url": None,
                        "service_node": False,
                        "home": None,
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                        "transaction_trace_logging": False,
                        "app_log_level": "INFO",
                        "app_log_json": False,
                        "app_log_rotation_hours": 1,
                        "app_log_retention_days": 7,
                        "simulation_enabled": True,
                        "simulation_max_concurrency": 2,
                        "simulation_timeout_ms": 3000,
                        "simulation_max_chi": 1000000,
                        "parallel_execution_enabled": False,
                        "parallel_execution_workers": 0,
                        "parallel_execution_min_transactions": 8,
                        "operator_profile": None,
                        "monitoring_profile": None,
                        "dashboard_enabled": False,
                        "monitoring_enabled": False,
                        "dashboard_host": "127.0.0.1",
                        "dashboard_port": 8080,
                        "intentkit_enabled": False,
                        "intentkit_network_id": None,
                        "intentkit_host": "127.0.0.1",
                        "intentkit_port": 38000,
                        "intentkit_api_port": 38080,
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with patch("xian_cli.cli.start_xian_stack_node") as start_node:
                start_node.return_value = {
                    "stack_dir": str(stack_dir),
                    "container_target": "abci-up",
                    "node_target": "node-start",
                    "rpc_checked": False,
                }
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "start",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--skip-health-check",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            kwargs = start_node.call_args.kwargs
            self.assertEqual(kwargs["node_image_mode"], "registry")
            self.assertEqual(
                kwargs["node_integrated_image"],
                CANONICAL_RELEASE_INTEGRATED_IMAGE,
            )
            self.assertEqual(
                kwargs["node_split_image"],
                CANONICAL_RELEASE_SPLIT_IMAGE,
            )

    def test_node_status_surfaces_release_manifest_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks" / "mainnet").mkdir(parents=True)
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            (base_dir / "networks" / "mainnet" / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "mainnet",
                        "chain_id": "xian-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "node_image_mode": "registry",
                        "node_integrated_image": (
                            CANONICAL_RELEASE_INTEGRATED_IMAGE
                        ),
                        "node_split_image": CANONICAL_RELEASE_SPLIT_IMAGE,
                        "node_release_manifest": (
                            CANONICAL_NODE_RELEASE_MANIFEST
                        ),
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )
            (base_dir / "nodes" / "validator-1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "validator-1",
                        "network": "mainnet",
                        "moniker": "validator-1",
                        "runtime_backend": "xian-stack",
                        "node_image_mode": "registry",
                        "node_integrated_image": (
                            CANONICAL_RELEASE_INTEGRATED_IMAGE
                        ),
                        "node_split_image": CANONICAL_RELEASE_SPLIT_IMAGE,
                        "node_release_manifest": (
                            CANONICAL_NODE_RELEASE_MANIFEST
                        ),
                        "stack_dir": str(stack_dir),
                        "seeds": [],
                        "genesis_url": None,
                        "snapshot_url": None,
                        "service_node": False,
                        "home": None,
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                        "transaction_trace_logging": False,
                        "app_log_level": "INFO",
                        "app_log_json": False,
                        "app_log_rotation_hours": 1,
                        "app_log_retention_days": 7,
                        "simulation_enabled": True,
                        "simulation_max_concurrency": 2,
                        "simulation_timeout_ms": 3000,
                        "simulation_max_chi": 1000000,
                        "parallel_execution_enabled": False,
                        "parallel_execution_workers": 0,
                        "parallel_execution_min_transactions": 8,
                        "operator_profile": None,
                        "monitoring_profile": None,
                        "dashboard_enabled": False,
                        "monitoring_enabled": False,
                        "dashboard_host": "127.0.0.1",
                        "dashboard_port": 8080,
                        "intentkit_enabled": False,
                        "intentkit_network_id": None,
                        "intentkit_host": "127.0.0.1",
                        "intentkit_port": 38000,
                        "intentkit_api_port": 38080,
                    }
                ),
                encoding="utf-8",
            )

            backend_status = {
                "backend_running": True,
                "compose_services": [
                    {
                        "service": "abci",
                        "state": "running",
                        "image": CANONICAL_RELEASE_INTEGRATED_IMAGE,
                    }
                ],
            }
            rpc_status = {
                "result": {
                    "sync_info": {
                        "latest_block_height": "123",
                        "catching_up": False,
                    },
                    "node_info": {
                        "network": "xian-1",
                        "other": {"n_peers": "4"},
                    },
                }
            }

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_xian_stack_node_status",
                return_value=backend_status,
            ):
                with patch(
                    "xian_cli.cli.fetch_json",
                    return_value=rpc_status,
                ):
                    with redirect_stdout(stdout):
                        exit_code = main(
                            [
                                "node",
                                "status",
                                "validator-1",
                                "--base-dir",
                                str(base_dir),
                            ]
                        )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["node_release_manifest"],
                _normalized_release_manifest(CANONICAL_NODE_RELEASE_MANIFEST),
            )
            self.assertEqual(
                payload["summary"]["release_manifest_refs"]["xian-abci"],
                CANONICAL_NODE_RELEASE_MANIFEST["components"]["xian-abci"][
                    "ref"
                ],
            )
            self.assertEqual(
                payload["summary"]["runtime_service_images"]["abci"],
                CANONICAL_RELEASE_INTEGRATED_IMAGE,
            )
            self.assertEqual(
                payload["summary"]["node_integrated_image"],
                CANONICAL_RELEASE_INTEGRATED_IMAGE,
            )
            self.assertEqual(
                payload["summary"]["node_split_image"],
                CANONICAL_RELEASE_SPLIT_IMAGE,
            )

    def test_node_stop_uses_xian_stack_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--service-node",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.cli.stop_xian_stack_node") as stop_node:
                stop_node.return_value = {
                    "stack_dir": str(stack_dir),
                    "container_target": "abci-bds-down",
                }
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "stop",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stop_node.call_count, 1)
            kwargs = stop_node.call_args.kwargs
            self.assertEqual(kwargs["stack_dir"], stack_dir.resolve())
            self.assertEqual(
                kwargs["cometbft_home"],
                default_home_for_backend(
                    base_dir=base_dir,
                    runtime_backend="xian-stack",
                    stack_dir=stack_dir.resolve(),
                ),
            )
            self.assertTrue(kwargs["service_node"])
            self.assertFalse(kwargs["monitoring_enabled"])

    def test_node_start_requires_initialized_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--template",
                        "single-node-indexed",
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            with self.assertRaisesRegex(
                FileNotFoundError,
                "run `xian node init",
            ):
                main(
                    [
                        "node",
                        "start",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

    def test_node_status_reports_initialized_home_and_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            home = stack_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--template",
                        "single-node-indexed",
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.fetch_json",
                return_value={
                    "result": {
                        "node_info": {
                            "network": "xian",
                            "other": {"n_peers": "2"},
                        },
                        "sync_info": {
                            "latest_block_height": "12",
                            "latest_block_time": "2026-04-18T22:00:00Z",
                            "catching_up": False,
                        },
                    }
                },
            ):
                with patch(
                    "xian_cli.cli.get_xian_stack_node_status",
                    return_value={
                        "backend_running": True,
                        "node_id": "node-123",
                        "dashboard_reachable": True,
                        "prometheus_reachable": True,
                        "grafana_reachable": True,
                        "endpoints": {
                            "rpc": "http://127.0.0.1:26657",
                            "dashboard": "http://127.0.0.1:8080",
                            "prometheus": "http://127.0.0.1:9090",
                        },
                    },
                ):
                    with redirect_stdout(stdout):
                        exit_code = main(
                            [
                                "node",
                                "status",
                                "validator-1",
                                "--base-dir",
                                str(base_dir),
                            ]
                        )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["initialized"])
            self.assertTrue(result["rpc_reachable"])
            self.assertEqual(result["node_id"], "node-123")
            self.assertEqual(result["stack_dir"], str(stack_dir.resolve()))
            self.assertTrue(result["backend_checked"])
            self.assertTrue(result["backend_running"])
            self.assertEqual(result["summary"]["state"], "ready")
            self.assertEqual(result["summary"]["rpc_network"], "xian")
            self.assertEqual(result["summary"]["rpc_height"], "12")
            self.assertIsInstance(
                result["summary"]["rpc_block_age_seconds"], float
            )
            self.assertGreaterEqual(
                result["summary"]["rpc_block_age_seconds"], 0.0
            )
            self.assertEqual(result["summary"]["peer_count"], "2")
            self.assertTrue(result["summary"]["dashboard_reachable"])
            self.assertTrue(result["summary"]["prometheus_reachable"])
            self.assertEqual(
                result["endpoints"]["dashboard"],
                "http://127.0.0.1:8080",
            )

    def test_node_endpoints_reports_effective_operator_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--template",
                        "single-node-indexed",
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_xian_stack_node_endpoints",
                return_value={
                    "endpoints": {
                        "rpc": "http://127.0.0.1:26657",
                        "rpc_status": "http://127.0.0.1:26657/status",
                        "xian_metrics": "http://127.0.0.1:9108/metrics",
                        "prometheus": "http://127.0.0.1:9090",
                        "grafana": "http://127.0.0.1:3000",
                    }
                },
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "endpoints",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["dashboard_enabled"])
            self.assertTrue(result["monitoring_enabled"])
            self.assertEqual(
                result["endpoints"]["xian_metrics"],
                "http://127.0.0.1:9108/metrics",
            )
            self.assertEqual(
                result["endpoints"]["prometheus"],
                "http://127.0.0.1:9090",
            )

    def test_node_health_reports_runtime_and_statesync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            home = stack_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                f"""
[statesync]
enable = true
rpc_servers = "http://rpc-1.example:26657,http://rpc-2.example:26657"
trust_height = 120
trust_hash = "{"ab" * 32}"
trust_period = "336h0m0s"
""",
                encoding="utf-8",
            )
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--snapshot-url",
                        "https://example.invalid/snapshot.tar.gz",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--home",
                        str(home),
                        "--enable-dashboard",
                        "--enable-monitoring",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_xian_stack_node_health",
                return_value={
                    "state": "healthy",
                    "checks": {"backend": {"ok": True}},
                    "endpoints": {
                        "rpc": "http://127.0.0.1:26657",
                        "xian_metrics": "http://127.0.0.1:9108/metrics",
                    },
                },
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "health",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertIsNone(result["operator_profile"])
            self.assertIsNone(result["monitoring_profile"])
            self.assertEqual(result["health"]["state"], "healthy")
            self.assertEqual(result["statesync"]["state"], "configured")
            self.assertTrue(result["statesync"]["ready"])
            self.assertEqual(
                result["effective_snapshot_url"],
                "https://example.invalid/snapshot.tar.gz",
            )
            self.assertEqual(
                result["endpoints"]["xian_metrics"],
                "http://127.0.0.1:9108/metrics",
            )


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_workspace_and_node_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            stack_dir = base_dir / "xian-stack"
            configs_dir.mkdir()
            stack_dir.mkdir()
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--home",
                        str(home),
                        "--validator-key-ref",
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_node_setup_module",
                return_value=type(
                    "NodeSetup", (), {"__name__": "node_setup"}
                )(),
            ):
                with patch(
                    "xian_cli.cli.get_node_admin_module",
                    return_value=type(
                        "NodeAdmin", (), {"__name__": "node_admin"}
                    )(),
                ):
                    with patch(
                        "xian_cli.cli.get_genesis_builder_module",
                        return_value=type(
                            "GenesisBuilder",
                            (),
                            {"__name__": "genesis_builder"},
                        )(),
                    ):
                        with redirect_stdout(stdout):
                            with patch(
                                "xian_cli.cli.get_xian_stack_node_status",
                                return_value={
                                    "backend_running": True,
                                    "node_id": "node-123",
                                },
                            ):
                                exit_code = main(
                                    [
                                        "doctor",
                                        "validator-1",
                                        "--base-dir",
                                        str(base_dir),
                                        "--configs-dir",
                                        str(configs_dir),
                                        "--stack-dir",
                                        str(stack_dir),
                                        "--skip-live-checks",
                                    ]
                                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            check_names = {check["name"] for check in result["checks"]}
            self.assertIn("configs_dir", check_names)
            self.assertIn("stack_dir", check_names)
            self.assertIn("node_status", check_names)
            self.assertIn("node_artifacts", check_names)
            self.assertIn("endpoints", check_names)

    def test_doctor_can_run_live_health_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            stack_dir = base_dir / "xian-stack"
            configs_dir.mkdir()
            stack_dir.mkdir()
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--enable-dashboard",
                        "--enable-monitoring",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_node_setup_module",
                return_value=type(
                    "NodeSetup", (), {"__name__": "node_setup"}
                )(),
            ):
                with patch(
                    "xian_cli.cli.get_node_admin_module",
                    return_value=type(
                        "NodeAdmin", (), {"__name__": "node_admin"}
                    )(),
                ):
                    with patch(
                        "xian_cli.cli.get_genesis_builder_module",
                        return_value=type(
                            "GenesisBuilder",
                            (),
                            {"__name__": "genesis_builder"},
                        )(),
                    ):
                        with patch(
                            "xian_cli.cli.fetch_json",
                            return_value={
                                "result": {
                                    "node_info": {
                                        "network": "xian",
                                        "other": {"n_peers": "3"},
                                    },
                                    "sync_info": {
                                        "latest_block_height": "42",
                                        "catching_up": False,
                                    },
                                }
                            },
                        ):
                            with patch(
                                "xian_cli.cli.get_xian_stack_node_status",
                                return_value={
                                    "backend_running": True,
                                    "node_id": "node-123",
                                    "dashboard_reachable": True,
                                    "prometheus_reachable": True,
                                    "grafana_reachable": True,
                                    "endpoints": {
                                        "rpc": "http://127.0.0.1:26657",
                                        "dashboard": "http://127.0.0.1:8080",
                                        "prometheus": "http://127.0.0.1:9090",
                                        "grafana": "http://127.0.0.1:3000",
                                    },
                                },
                            ):
                                with redirect_stdout(stdout):
                                    exit_code = main(
                                        [
                                            "doctor",
                                            "validator-1",
                                            "--base-dir",
                                            str(base_dir),
                                            "--configs-dir",
                                            str(configs_dir),
                                            "--stack-dir",
                                            str(stack_dir),
                                        ]
                                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            check_names = {check["name"] for check in result["checks"]}
            self.assertIn("backend", check_names)
            self.assertIn("rpc", check_names)
            self.assertIn("statesync", check_names)
            self.assertIn("snapshot_bootstrap", check_names)
            self.assertIn("dashboard", check_names)
            self.assertIn("prometheus", check_names)
            self.assertIn("grafana", check_names)


class SnapshotCommandTests(unittest.TestCase):
    def test_snapshot_restore_uses_effective_snapshot_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--snapshot-url",
                        "https://example.invalid/network-snapshot-manifest.json",
                        "--snapshot-signing-key",
                        "b" * 64,
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            snapshot_mock = unittest.mock.Mock(return_value="snapshot.tar.gz")
            node_admin = type(
                "NodeAdmin",
                (),
                {"apply_snapshot_archive": staticmethod(snapshot_mock)},
            )()
            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_node_admin_module",
                return_value=node_admin,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "snapshot",
                            "restore",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            snapshot_mock.assert_called_once_with(
                "https://example.invalid/network-snapshot-manifest.json",
                home,
                trusted_manifest_public_keys=["b" * 64],
                expected_chain_id="xian-local-1",
            )
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["home"], str(home))
            self.assertEqual(
                result["snapshot_url"],
                "https://example.invalid/network-snapshot-manifest.json",
            )
            self.assertEqual(
                result["snapshot_archive_name"],
                "snapshot.tar.gz",
            )

    def test_snapshot_restore_requires_initialized_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--snapshot-url",
                        "https://example.invalid/network-snapshot.tar.gz",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            with self.assertRaisesRegex(
                FileNotFoundError, "run `xian node init"
            ):
                main(
                    [
                        "snapshot",
                        "restore",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

    def test_recovery_validate_reports_plan_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            plan_path = base_dir / "recovery-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "metering-incident-20260327",
                        "chain_id": "xian-local-1",
                        "target_height": 120,
                        "trusted_block_hash": "ABCD1234",
                        "trusted_app_hash": "DCBA4321",
                        "reason": "Recover pre-divergence state",
                        "artifact": {
                            "kind": "snapshot_url",
                            "uri": "https://example.invalid/recovery.tar.gz",
                            "sha256": "ab" * 32,
                        },
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "recovery",
                        "validate",
                        str(plan_path),
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--rpc-url",
                        "http://127.0.0.1:1/status",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(
                payload["plan"]["name"], "metering-incident-20260327"
            )
            self.assertEqual(payload["node"]["home"], str(home))
            self.assertTrue(
                payload["validation"]["requires_manual_hash_confirmation"]
            )

    def test_recovery_apply_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            plan_path = base_dir / "recovery-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "metering-incident-20260327",
                        "chain_id": "xian-local-1",
                        "target_height": 120,
                        "trusted_block_hash": "ABCD1234",
                        "trusted_app_hash": "DCBA4321",
                        "reason": "Recover pre-divergence state",
                        "artifact": {
                            "kind": "snapshot_url",
                            "uri": "https://example.invalid/recovery.tar.gz",
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "pass --yes"):
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "recovery",
                            "apply",
                            str(plan_path),
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

    def test_recovery_apply_stops_backs_up_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            plan_path = base_dir / "recovery-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "metering-incident-20260327",
                        "chain_id": "xian-local-1",
                        "target_height": 120,
                        "trusted_block_hash": "ABCD1234",
                        "trusted_app_hash": "DCBA4321",
                        "reason": "Recover pre-divergence state",
                        "artifact": {
                            "kind": "snapshot_url",
                            "uri": "https://example.invalid/recovery.tar.gz",
                            "sha256": "ab" * 32,
                        },
                        "follow_up_state_patch": {
                            "patch_id": "metering-fix-20260327",
                            "bundle_hash": "ff" * 32,
                            "activation_height": 140,
                        },
                    }
                ),
                encoding="utf-8",
            )

            stop_mock = unittest.mock.Mock(return_value={"status": "stopped"})
            backup_mock = unittest.mock.Mock(
                return_value=str(
                    base_dir / "recovery-backups" / "backup.tar.gz"
                )
            )
            snapshot_mock = unittest.mock.Mock(return_value="snapshot.tar.gz")
            node_admin = type(
                "NodeAdmin",
                (),
                {"apply_snapshot_archive": staticmethod(snapshot_mock)},
            )()
            stdout = io.StringIO()
            with (
                patch(
                    "xian_cli.cli.stop_xian_stack_node",
                    stop_mock,
                ),
                patch(
                    "xian_cli.cli.shutil.make_archive",
                    backup_mock,
                ),
                patch(
                    "xian_cli.cli.get_node_admin_module",
                    return_value=node_admin,
                ),
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "recovery",
                            "apply",
                            str(plan_path),
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--rpc-url",
                            "http://127.0.0.1:1/status",
                            "--yes",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            stop_mock.assert_called_once()
            backup_mock.assert_called_once()
            snapshot_mock.assert_called_once_with(
                "https://example.invalid/recovery.tar.gz",
                home,
                expected_sha256="ab" * 32,
            )
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["dry_run"])
            self.assertTrue(payload["stopped_node"])
            self.assertEqual(
                payload["backup_archive"],
                str(base_dir / "recovery-backups" / "backup.tar.gz"),
            )
            self.assertEqual(
                payload["snapshot_restore"]["snapshot_archive_name"],
                "snapshot.tar.gz",
            )


class RuntimeHelperTests(unittest.TestCase):
    def test_resolve_stack_dir_uses_workspace_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved = resolve_stack_dir(Path(tmp_dir))

        expected = (
            Path(__file__).resolve().parents[2] / "xian-stack"
        ).resolve()
        self.assertEqual(resolved, expected)

    def test_wait_for_rpc_ready_retries_until_result_is_available(self) -> None:
        expected_payload = {"result": {"node_info": {"network": "xian"}}}

        with patch(
            "xian_cli.runtime.fetch_json",
            side_effect=[URLError("down"), expected_payload],
        ) as fetch_json_mock:
            with patch("xian_cli.runtime.time.sleep") as sleep_mock:
                payload = wait_for_rpc_ready(
                    timeout_seconds=1.0,
                    poll_interval=0.01,
                )

        self.assertEqual(payload, expected_payload)
        self.assertEqual(fetch_json_mock.call_count, 2)
        self.assertEqual(sleep_mock.call_count, 1)

    def test_start_xian_stack_node_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        rpc_status = {"result": {"sync_info": {"catching_up": False}}}

        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {"rpc_status": rpc_status}
            result = start_xian_stack_node(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                service_node=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
                wait_for_rpc=True,
                rpc_timeout_seconds=12.5,
            )

        run_backend_command.assert_called_once_with(
            stack_dir,
            "start",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            service_node=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
            wait_for_health=True,
            rpc_timeout_seconds=12.5,
            rpc_url="http://127.0.0.1:26657/status",
        )
        self.assertEqual(result["rpc_status"], rpc_status)

    def test_start_xian_stack_node_passes_registry_image_config(self) -> None:
        stack_dir = Path("/tmp/xian-stack")

        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {"backend_running": True}
            start_xian_stack_node(
                stack_dir=stack_dir,
                node_image_mode="registry",
                node_integrated_image="ghcr.io/xian-technology/xian-node@sha256:abc",
                node_split_image="ghcr.io/xian-technology/xian-node-split@sha256:def",
                service_node=False,
                wait_for_rpc=False,
            )

        run_backend_command.assert_called_once_with(
            stack_dir,
            "start",
            cometbft_home=None,
            node_image_mode="registry",
            node_integrated_image="ghcr.io/xian-technology/xian-node@sha256:abc",
            node_split_image="ghcr.io/xian-technology/xian-node-split@sha256:def",
            service_node=False,
            dashboard_enabled=False,
            monitoring_enabled=False,
            dashboard_host="127.0.0.1",
            dashboard_port=8080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
            wait_for_health=False,
            rpc_timeout_seconds=90.0,
            rpc_url="http://127.0.0.1:26657/status",
        )

    def test_stop_xian_stack_node_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")

        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {"container_target": "abci-down"}
            result = stop_xian_stack_node(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                service_node=False,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            )

        run_backend_command.assert_called_once_with(
            stack_dir,
            "stop",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            service_node=False,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
        )
        self.assertEqual(result["container_target"], "abci-down")

    def test_get_xian_stack_node_status_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {
                "backend_running": True,
                "node_id": "abc123",
            }
            result = get_xian_stack_node_status(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                service_node=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            )

        self.assertTrue(result["backend_running"])
        self.assertEqual(result["node_id"], "abc123")
        run_backend_command.assert_called_once_with(
            stack_dir,
            "status",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            service_node=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
        )

    def test_get_xian_stack_node_endpoints_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {
                "endpoints": {"rpc": "http://127.0.0.1:26657"}
            }
            result = get_xian_stack_node_endpoints(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                service_node=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            )

        self.assertEqual(
            result["endpoints"]["rpc"],
            "http://127.0.0.1:26657",
        )
        run_backend_command.assert_called_once_with(
            stack_dir,
            "endpoints",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            service_node=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
        )

    def test_get_xian_stack_node_health_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {"state": "healthy"}
            result = get_xian_stack_node_health(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                service_node=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
                rpc_url="http://127.0.0.1:26657/status",
                check_disk=False,
            )

        self.assertEqual(result["state"], "healthy")
        run_backend_command.assert_called_once_with(
            stack_dir,
            "health",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            service_node=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
            rpc_url="http://127.0.0.1:26657/status",
            check_disk=False,
        )

    def test_fallback_node_endpoints_normalizes_wildcard_dashboard_host(
        self,
    ) -> None:
        profile = {
            "dashboard_enabled": True,
            "dashboard_host": "0.0.0.0",
            "dashboard_port": 18080,
            "monitoring_enabled": False,
        }

        endpoints = _fallback_node_endpoints(
            rpc_status_url="http://127.0.0.1:26657/status",
            profile=profile,
        )

        self.assertEqual(endpoints["dashboard"], "http://127.0.0.1:18080")
        self.assertEqual(
            endpoints["dashboard_status"],
            "http://127.0.0.1:18080/api/status",
        )

    def test_run_backend_command_surfaces_backend_stderr(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        error = subprocess.CalledProcessError(
            1,
            ["python3", "backend.py", "start"],
            output='{"state":"failed"}',
            stderr="compose interpolation failed",
        )

        with patch(
            "xian_cli.runtime.subprocess.run",
            side_effect=error,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "xian-stack backend command failed \\(start\\): "
                "compose interpolation failed",
            ):
                run_backend_command(stack_dir, "start")


class ConfigRepoTests(unittest.TestCase):
    def test_read_network_manifest_requires_explicit_schema_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "mainnet",
                        "chain_id": "xian-1",
                        "runtime_backend": "xian-stack",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "unsupported schema_version"
            ):
                read_network_manifest(manifest_path)

    def test_read_node_profile_requires_explicit_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_path = Path(tmp_dir) / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "name": "validator-1",
                        "network": "mainnet",
                        "moniker": "validator-1",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "unsupported schema_version"
            ):
                read_node_profile(profile_path)

    def test_read_network_template_requires_explicit_schema_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            template_path = Path(tmp_dir) / "template.json"
            template_path.write_text(
                json.dumps(
                    {
                        "name": "single-node-dev",
                        "display_name": "Single-Node Dev",
                        "description": "Local dev template",
                        "runtime_backend": "xian-stack",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "unsupported schema_version"
            ):
                read_network_template(template_path)

    def test_solution_pack_list_reads_canonical_solution_packs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            pack_dir = configs_dir / "solution-packs" / "credits-ledger"
            pack_dir.mkdir(parents=True)
            (pack_dir / "pack.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "credits-ledger",
                        "display_name": "Credits Ledger Pack",
                        "description": "Credits ledger starter flow",
                        "use_case": (
                            "Use Xian as an application credits ledger."
                        ),
                        "recommended_local_template": "single-node-indexed",
                        "recommended_remote_template": "embedded-backend",
                        "docs_path": "/solution-packs/credits-ledger",
                        "example_dir": "xian-py/examples/credits_ledger",
                        "contract_paths": [
                            "solution-packs/credits-ledger/contracts/credits_ledger.s.py"
                        ],
                        "starter_flows": [
                            {
                                "name": "local",
                                "display_name": "Local Starter",
                                "template": "single-node-indexed",
                                "summary": "Local starter flow",
                                "network_name": "credits-ledger-local",
                                "node_name": "validator-1",
                                "steps": [
                                    {
                                        "title": "Create network",
                                        "commands": [
                                            "uv run xian network create "
                                            "credits-ledger-local --template "
                                            "single-node-indexed --chain-id "
                                            "credits-ledger-local-1 "
                                            "--generate-validator-key "
                                            "--init-node"
                                        ],
                                        "notes": ["Run from xian-cli."],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "solution-pack",
                        "list",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["name"], "credits-ledger")
            self.assertEqual(
                payload[0]["recommended_local_template"],
                "single-node-indexed",
            )
            self.assertEqual(
                payload[0]["example_dir"],
                "xian-py/examples/credits_ledger",
            )

    def test_solution_pack_starter_returns_selected_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            pack_dir = configs_dir / "solution-packs" / "workflow-backend"
            pack_dir.mkdir(parents=True)
            (pack_dir / "pack.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "workflow-backend",
                        "display_name": "Workflow Backend Pack",
                        "description": "Workflow starter flow",
                        "use_case": "Use Xian as a shared workflow backend.",
                        "recommended_local_template": "single-node-indexed",
                        "recommended_remote_template": "embedded-backend",
                        "docs_path": "/solution-packs/workflow-backend",
                        "example_dir": "xian-py/examples/workflow_backend",
                        "contract_paths": [
                            "solution-packs/workflow-backend/contracts/job_workflow.s.py"
                        ],
                        "starter_flows": [
                            {
                                "name": "local",
                                "display_name": "Local Starter",
                                "template": "single-node-indexed",
                                "summary": "Local flow",
                                "network_name": "workflow-backend-local",
                                "node_name": "validator-1",
                                "steps": [
                                    {
                                        "title": "Create network",
                                        "commands": [
                                            "uv run xian network create "
                                            "workflow-backend-local "
                                            "--template "
                                            "single-node-indexed --chain-id "
                                            "workflow-backend-local-1 "
                                            "--generate-validator-key "
                                            "--init-node"
                                        ],
                                        "notes": [],
                                    }
                                ],
                            },
                            {
                                "name": "remote",
                                "display_name": "Remote Starter",
                                "template": "embedded-backend",
                                "summary": "Remote flow",
                                "network_name": "workflow-backend-remote",
                                "node_name": "validator-1",
                                "steps": [
                                    {
                                        "title": "Deploy remote node",
                                        "commands": [
                                            "ansible-playbook "
                                            "playbooks/deploy.yml"
                                        ],
                                        "notes": ["Run from xian-deploy."],
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "solution-pack",
                        "starter",
                        "workflow-backend",
                        "--flow",
                        "remote",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["name"], "workflow-backend")
            self.assertEqual(payload["flow"]["name"], "remote")
            self.assertEqual(payload["flow"]["template"], "embedded-backend")
            self.assertEqual(
                payload["flow"]["steps"][0]["commands"][0],
                "ansible-playbook playbooks/deploy.yml",
            )

    def test_read_solution_pack_requires_explicit_schema_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pack_path = Path(tmp_dir) / "pack.json"
            pack_path.write_text(
                json.dumps(
                    {
                        "name": "credits-ledger",
                        "display_name": "Credits Ledger Pack",
                        "description": "Credits ledger starter flow",
                        "use_case": (
                            "Use Xian as an application credits ledger."
                        ),
                        "recommended_local_template": "single-node-indexed",
                        "recommended_remote_template": "embedded-backend",
                        "docs_path": "/solution-packs/credits-ledger",
                        "example_dir": "xian-py/examples/credits_ledger",
                        "contract_paths": [
                            "solution-packs/credits-ledger/contracts/credits_ledger.s.py"
                        ],
                        "starter_flows": [
                            {
                                "name": "local",
                                "display_name": "Local Starter",
                                "template": "single-node-indexed",
                                "summary": "Local starter flow",
                                "steps": [
                                    {
                                        "title": "Create network",
                                        "commands": [
                                            "uv run xian network create "
                                            "credits-ledger-local --template "
                                            "single-node-indexed --chain-id "
                                            "credits-ledger-local-1 "
                                            "--generate-validator-key "
                                            "--init-node"
                                        ],
                                        "notes": [],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "unsupported schema_version"
            ):
                read_solution_pack(pack_path)

    def test_resolve_configs_dir_uses_workspace_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved = resolve_configs_dir(Path(tmp_dir))

        expected = (WORKSPACE_ROOT / "xian-configs").resolve()
        self.assertEqual(resolved, expected)

    def test_resolve_solution_pack_path_prefers_canonical_configs_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            pack_path = (
                configs_dir / "solution-packs" / "credits-ledger" / "pack.json"
            )
            pack_path.parent.mkdir(parents=True)
            pack_path.write_text("{}", encoding="utf-8")

            resolved = resolve_solution_pack_path(
                base_dir=base_dir,
                pack_name="credits-ledger",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, pack_path.resolve())

    def test_resolve_network_manifest_path_prefers_local_network_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            manifest_path = base_dir / "networks" / "mainnet" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}", encoding="utf-8")

            resolved = resolve_network_manifest_path(
                base_dir=base_dir,
                network_name="mainnet",
            )

        self.assertEqual(resolved, manifest_path.resolve())

    def test_resolve_network_manifest_path_prefers_canonical_configs_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            manifest_path = (
                configs_dir / "networks" / "mainnet" / "manifest.json"
            )
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}", encoding="utf-8")

            resolved = resolve_network_manifest_path(
                base_dir=base_dir,
                network_name="mainnet",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, manifest_path.resolve())

    def test_resolve_network_template_path_prefers_canonical_configs_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            template_path = configs_dir / "templates" / "single-node-dev.json"
            template_path.parent.mkdir(parents=True)
            template_path.write_text("{}", encoding="utf-8")

            resolved = resolve_network_template_path(
                base_dir=base_dir,
                template_name="single-node-dev",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, template_path.resolve())


if __name__ == "__main__":
    unittest.main()
