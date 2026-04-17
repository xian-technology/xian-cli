from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import xian_py.transaction as tx_api
from xian_py.wallet import Wallet
from xian_py.xian import Xian

from xian_cli.output import emit_json


def _resolve_node_url(args: argparse.Namespace) -> str:
    node_url = getattr(args, "node_url", None) or os.environ.get(
        "XIAN_NODE_URL"
    )
    if not isinstance(node_url, str) or not node_url.strip():
        raise ValueError(
            "node URL is required; pass --node-url or set XIAN_NODE_URL"
        )
    return node_url.rstrip("/")


def _resolve_chain_id(args: argparse.Namespace) -> str | None:
    chain_id = getattr(args, "chain_id", None) or os.environ.get(
        "XIAN_CHAIN_ID"
    )
    return (
        chain_id.strip()
        if isinstance(chain_id, str) and chain_id.strip()
        else None
    )


def _load_private_key_from_args(args: argparse.Namespace) -> str:
    direct = getattr(args, "private_key", None)
    env_name = getattr(args, "private_key_env", None)
    file_path = getattr(args, "private_key_file", None)

    values = [
        value
        for value in (
            direct,
            os.environ.get(env_name) if isinstance(env_name, str) else None,
            Path(file_path).read_text(encoding="utf-8").strip()
            if file_path is not None
            else None,
        )
        if isinstance(value, str) and value.strip()
    ]
    if not values:
        raise ValueError(
            "private key is required; pass --private-key, "
            "--private-key-env, or --private-key-file"
        )
    if len(values) > 1:
        raise ValueError(
            "provide only one private key source: --private-key, "
            "--private-key-env, or --private-key-file"
        )
    return values[0].strip()


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
    if args.private_key_out is not None:
        out_path = Path(args.private_key_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(wallet.private_key + "\n", encoding="utf-8")

    payload: dict[str, Any] = {
        "address": wallet.public_key,
        "public_key": wallet.public_key,
    }
    if args.include_private_key:
        payload["private_key"] = wallet.private_key
    if args.private_key_out is not None:
        payload["private_key_path"] = str(Path(args.private_key_out).resolve())
    emit_json(payload)
    return 0


def handle_query_nonce(args: argparse.Namespace) -> int:
    payload = {
        "address": args.address,
        "next_nonce": tx_api.get_nonce(_resolve_node_url(args), args.address),
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
