from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import xian_py.transaction as tx_api
from xian_py.wallet import Wallet
from xian_py.xian import Xian

from xian_cli.output import emit_json
from xian_cli.secret_files import load_secret_from_args, secure_write_text


def _resolve_node_url(args: argparse.Namespace) -> str:
    node_url = getattr(args, "node_url", None) or os.environ.get("XIAN_NODE_URL")
    if not isinstance(node_url, str) or not node_url.strip():
        raise ValueError("node URL is required; pass --node-url or set XIAN_NODE_URL")
    return node_url.rstrip("/")


def _resolve_chain_id(args: argparse.Namespace) -> str | None:
    chain_id = getattr(args, "chain_id", None) or os.environ.get("XIAN_CHAIN_ID")
    return chain_id.strip() if isinstance(chain_id, str) and chain_id.strip() else None


def _load_private_key_from_args(args: argparse.Namespace) -> str:
    private_key = load_secret_from_args(
        args,
        direct_attr="private_key",
        env_attr="private_key_env",
        file_attr="private_key_file",
        stdin_attr="private_key_stdin",
        secret_name="private key",
        direct_flag="--private-key",
        env_flag="--private-key-env",
        file_flag="--private-key-file",
        stdin_flag="--private-key-stdin",
        required=True,
    )
    assert private_key is not None
    return private_key


def _build_wallet(args: argparse.Namespace) -> Wallet:
    return Wallet(private_key=_load_private_key_from_args(args))


def _make_client(
    args: argparse.Namespace,
    *,
    wallet: Wallet | None = None,
) -> Xian:
    return Xian(
        _resolve_node_url(args),
        chain_id=_resolve_chain_id(args),
        wallet=wallet,
    )


def _parse_json_object(raw: str, *, flag_name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{flag_name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{flag_name} must decode to a JSON object")
    return value


def _parse_json_object_from_path(path: str, *, label: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return _parse_json_object(raw, flag_name=label)


def _read_text_from_path(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _infer_contract_module_name(source_path: str) -> str:
    filename = Path(source_path).name
    if filename.endswith(".s.py"):
        return filename[: -len(".s.py")]
    return Path(filename).stem


def _submission_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "chi": getattr(args, "chi", None),
        "mode": getattr(args, "mode", None),
        "wait_for_tx": getattr(args, "wait_for_tx", None),
        "timeout_seconds": getattr(args, "timeout_seconds", None),
        "poll_interval_seconds": getattr(args, "poll_interval_seconds", None),
        "chi_margin": getattr(args, "chi_margin", None),
        "min_chi_headroom": getattr(args, "min_chi_headroom", None),
    }


def handle_wallet_generate(args: argparse.Namespace) -> int:
    wallet = Wallet()
    if args.include_private_key:
        raise ValueError("refusing to print private key to stdout; use --private-key-out")
    if args.private_key_out is not None:
        out_path = Path(args.private_key_out)
        secure_write_text(out_path, wallet.private_key + "\n")

    payload: dict[str, Any] = {
        "address": wallet.public_key,
        "public_key": wallet.public_key,
    }
    if args.private_key_out is not None:
        payload["private_key_path"] = str(Path(args.private_key_out).resolve())
    emit_json(payload)
    return 0


def handle_query_nonce(args: argparse.Namespace) -> int:
    payload = {
        "address": args.address,
        "next_nonce": asyncio.run(tx_api.get_nonce_async(_resolve_node_url(args), args.address)),
    }
    emit_json(payload)
    return 0


def handle_query_balance(args: argparse.Namespace) -> int:
    with _make_client(args) as client:
        balance = client.get_balance(
            address=args.address,
            contract=args.contract,
        )
    emit_json(
        {
            "address": args.address,
            "contract": args.contract,
            "balance": balance,
        }
    )
    return 0


def handle_query_tx(args: argparse.Namespace) -> int:
    with _make_client(args) as client:
        receipt = client.get_tx(args.tx_hash)
    emit_json(receipt)
    return 0


def handle_query_indexed_tx(args: argparse.Namespace) -> int:
    with _make_client(args) as client:
        tx = client.get_indexed_tx(args.tx_hash)
    emit_json(tx)
    return 0


def handle_query_txs_by_sender(args: argparse.Namespace) -> int:
    with _make_client(args) as client:
        txs = client.list_txs_by_sender(
            args.sender,
            limit=args.limit,
            offset=args.offset,
        )
    emit_json(txs)
    return 0


def handle_query_txs_by_contract(args: argparse.Namespace) -> int:
    with _make_client(args) as client:
        txs = client.list_txs_by_contract(
            args.contract,
            limit=args.limit,
            offset=args.offset,
        )
    emit_json(txs)
    return 0


def handle_query_block(args: argparse.Namespace) -> int:
    with _make_client(args) as client:
        if args.height is not None:
            block = client.get_block(args.height)
        else:
            block = client.get_block_by_hash(args.block_hash)
    emit_json(block)
    return 0


def handle_call(args: argparse.Namespace) -> int:
    kwargs = _parse_json_object(args.kwargs_json, flag_name="--kwargs-json")
    with _make_client(args) as client:
        result = client.call(args.contract, args.function, kwargs)
    emit_json(
        {
            "contract": args.contract,
            "function": args.function,
            "kwargs": kwargs,
            "result": result,
        }
    )
    return 0


def handle_simulate(args: argparse.Namespace) -> int:
    kwargs = _parse_json_object(args.kwargs_json, flag_name="--kwargs-json")
    with _make_client(args) as client:
        result = client.simulate(args.contract, args.function, kwargs)
    emit_json(result)
    return 0


def handle_tx_send(args: argparse.Namespace) -> int:
    kwargs = _parse_json_object(args.kwargs_json, flag_name="--kwargs-json")
    wallet = _build_wallet(args)
    with _make_client(args, wallet=wallet) as client:
        result = client.send_tx(
            contract=args.contract,
            function=args.function,
            kwargs=kwargs,
            nonce=args.nonce,
            **_submission_kwargs(args),
        )
    emit_json(result)
    return 0


def handle_tx_submit_source(args: argparse.Namespace) -> int:
    code = _read_text_from_path(args.source)
    if not isinstance(code, str) or not code.strip():
        raise ValueError("contract source must be a non-empty string")
    constructor_args = _parse_json_object(
        args.args_json,
        flag_name="--args-json",
    )
    name = args.name or (None if args.source == "-" else _infer_contract_module_name(args.source))
    if not isinstance(name, str) or not name.strip():
        raise ValueError("--name is required when reading source from stdin")
    name = name.strip()

    wallet = _build_wallet(args)
    with _make_client(args, wallet=wallet) as client:
        result = client.submit_contract(
            name,
            code,
            args=constructor_args or None,
            nonce=args.nonce,
            **_submission_kwargs(args),
        )
    emit_json(result)
    return 0


def handle_tx_transfer(args: argparse.Namespace) -> int:
    wallet = _build_wallet(args)
    with _make_client(args, wallet=wallet) as client:
        result = client.send(
            amount=args.amount,
            to_address=args.to,
            token=args.token,
            **_submission_kwargs(args),
        )
    emit_json(result)
    return 0
