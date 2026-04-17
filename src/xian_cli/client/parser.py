from __future__ import annotations

import argparse

from xian_cli.client import handlers


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--node-url",
        help="RPC base URL; defaults to XIAN_NODE_URL",
    )
    parser.add_argument(
        "--chain-id",
        help="optional explicit chain ID; defaults to XIAN_CHAIN_ID",
    )


def _add_wallet_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--private-key",
        help="64-character Ed25519 private key hex",
    )
    parser.add_argument(
        "--private-key-env",
        help="environment variable that contains the private key",
    )
    parser.add_argument(
        "--private-key-file",
        help="path to a file that contains the private key hex",
    )


def _add_submission_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--chi",
        type=int,
        help="explicit chi to supply; omit to use SDK estimation",
    )
    parser.add_argument(
        "--nonce",
        type=int,
        help="explicit nonce override",
    )
    parser.add_argument(
        "--mode",
        choices=("async", "checktx", "commit"),
        help="submission mode",
    )
    parser.add_argument(
        "--wait-for-tx",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="wait for final receipt after submission",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="receipt wait timeout in seconds",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        help="receipt poll interval in seconds",
    )
    parser.add_argument(
        "--chi-margin",
        type=float,
        help="chi estimation margin override",
    )
    parser.add_argument(
        "--min-chi-headroom",
        type=int,
        help="minimum chi headroom override",
    )


def register_client_commands(subparsers: argparse._SubParsersAction) -> None:
    client_parser = subparsers.add_parser(
        "client",
        help="wallet, query, and transaction automation backed by xian-py",
    )
    client_subparsers = client_parser.add_subparsers(
        dest="client_command",
        required=True,
    )

    wallet_parser = client_subparsers.add_parser(
        "wallet", help="wallet helpers"
    )
    wallet_subparsers = wallet_parser.add_subparsers(
        dest="client_wallet_command",
        required=True,
    )
    wallet_generate_parser = wallet_subparsers.add_parser(
        "generate",
        help="generate a new Xian wallet",
    )
    wallet_generate_parser.add_argument(
        "--include-private-key",
        action="store_true",
        help="include the private key in stdout JSON",
    )
    wallet_generate_parser.add_argument(
        "--private-key-out",
        help="write the private key hex to a file",
    )
    wallet_generate_parser.set_defaults(handler=handlers.handle_wallet_generate)

    query_parser = client_subparsers.add_parser(
        "query", help="readonly queries"
    )
    query_subparsers = query_parser.add_subparsers(
        dest="client_query_command",
        required=True,
    )

    query_nonce_parser = query_subparsers.add_parser(
        "nonce",
        help="get the next nonce for an address",
    )
    _add_connection_args(query_nonce_parser)
    query_nonce_parser.add_argument(
        "address", help="account public key/address"
    )
    query_nonce_parser.set_defaults(handler=handlers.handle_query_nonce)

    query_balance_parser = query_subparsers.add_parser(
        "balance",
        help="get a token balance for an address",
    )
    _add_connection_args(query_balance_parser)
    query_balance_parser.add_argument(
        "address", help="account public key/address"
    )
    query_balance_parser.add_argument(
        "--contract",
        default="currency",
        help="token contract name; defaults to currency",
    )
    query_balance_parser.set_defaults(handler=handlers.handle_query_balance)

    query_tx_parser = query_subparsers.add_parser(
        "tx",
        help="get a transaction receipt by hash",
    )
    _add_connection_args(query_tx_parser)
    query_tx_parser.add_argument(
        "tx_hash", help="uppercase or lowercase tx hash"
    )
    query_tx_parser.set_defaults(handler=handlers.handle_query_tx)

    query_block_parser = query_subparsers.add_parser(
        "block",
        help="get a block by height or hash",
    )
    _add_connection_args(query_block_parser)
    block_group = query_block_parser.add_mutually_exclusive_group(required=True)
    block_group.add_argument("--height", type=int, help="block height")
    block_group.add_argument("--block-hash", help="block hash")
    query_block_parser.set_defaults(handler=handlers.handle_query_block)

    call_parser = client_subparsers.add_parser(
        "call",
        help="call a contract function without submitting a transaction",
    )
    _add_connection_args(call_parser)
    call_parser.add_argument("contract", help="contract name")
    call_parser.add_argument("function", help="exported function name")
    call_parser.add_argument(
        "--kwargs-json",
        default="{}",
        help="JSON object of keyword arguments",
    )
    call_parser.set_defaults(handler=handlers.handle_call)

    simulate_parser = client_subparsers.add_parser(
        "simulate",
        help="simulate a transaction and estimate chi",
    )
    _add_connection_args(simulate_parser)
    simulate_parser.add_argument("contract", help="contract name")
    simulate_parser.add_argument("function", help="exported function name")
    simulate_parser.add_argument(
        "--kwargs-json",
        default="{}",
        help="JSON object of keyword arguments",
    )
    simulate_parser.set_defaults(handler=handlers.handle_simulate)

    tx_parser = client_subparsers.add_parser(
        "tx",
        help="signed transaction submission helpers",
    )
    tx_subparsers = tx_parser.add_subparsers(
        dest="client_tx_command",
        required=True,
    )

    tx_send_parser = tx_subparsers.add_parser(
        "send",
        help="submit a contract transaction",
    )
    _add_connection_args(tx_send_parser)
    _add_wallet_args(tx_send_parser)
    _add_submission_args(tx_send_parser)
    tx_send_parser.add_argument("contract", help="contract name")
    tx_send_parser.add_argument("function", help="exported function name")
    tx_send_parser.add_argument(
        "--kwargs-json",
        default="{}",
        help="JSON object of keyword arguments",
    )
    tx_send_parser.set_defaults(handler=handlers.handle_tx_send)

    tx_transfer_parser = tx_subparsers.add_parser(
        "transfer",
        help="transfer tokens between accounts",
    )
    _add_connection_args(tx_transfer_parser)
    _add_wallet_args(tx_transfer_parser)
    _add_submission_args(tx_transfer_parser)
    tx_transfer_parser.add_argument("to", help="recipient address")
    tx_transfer_parser.add_argument("amount", help="amount to transfer")
    tx_transfer_parser.add_argument(
        "--token",
        default="currency",
        help="token contract name; defaults to currency",
    )
    tx_transfer_parser.set_defaults(handler=handlers.handle_tx_transfer)
