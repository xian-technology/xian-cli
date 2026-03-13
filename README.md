# xian-cli

`xian-cli` is the operator control plane for Xian nodes and networks. It exists
to keep lifecycle UX out of `xian-abci` and to keep `xian-stack` focused on
runtime backend operations instead of end-user workflows.

## Ownership

This repo owns:

- command-line flows such as `keys`, `network create`, `network join`, and `node init`
- local operator artifacts such as network manifests and node profiles
- orchestration across `xian-abci` primitives and `xian-stack` backend actions

This repo does not own:

- deterministic node logic or genesis/config rendering internals
- Docker or Compose topology
- canonical network-specific genesis assets

The lifecycle contract is documented in
[`docs/LIFECYCLE_CONTRACT.md`](docs/LIFECYCLE_CONTRACT.md).

## Current Commands

```bash
uv sync --group dev
uv run xian --help
uv run xian keys validator generate --out-dir ./keys
uv run xian network create local-dev --chain-id xian-local-1
uv run xian network join mainnet-node --network mainnet \
  --validator-key-ref ./keys/mainnet-node/validator_key_info.json
uv run xian node init mainnet-node
uv run xian node start mainnet-node
uv run xian node stop mainnet-node
```

`--genesis-source` accepts either a local file path or an `http`/`https` URL.
If `./networks/<name>.json` does not exist locally, `node init`, `node start`,
and `node stop` fall back to the sibling `xian-configs/networks/<name>.json`
manifest.

## Workspace Model

The preferred layout is a shared parent directory such as `~/xian/` containing
sibling checkouts of `xian-cli`, `xian-abci`, `xian-configs`, and `xian-stack`.
`xian node init`
currently expects either:

- `xian-abci` installed in the same Python environment, or
- the sibling-workspace layout described above

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
