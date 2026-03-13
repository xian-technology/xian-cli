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
  --generate-validator-key
uv run xian node init mainnet-node
uv run xian node start mainnet-node
uv run xian node stop mainnet-node
```

`--genesis-source` accepts either a local file path or an `http`/`https` URL.
`network join` now resolves the named network manifest immediately. It prefers a
local `./networks/<name>.json` file and otherwise falls back to the sibling
`xian-configs/networks/<name>/manifest.json` manifest. Canonical manifest data
such as `runtime_backend` is used as the default, while node-local overrides
such as `--seed`, `--snapshot-url`, and `--genesis-url` stay in the node
profile.

If the operator does not already have validator key material, `network join`
can generate it directly with `--generate-validator-key`. By default it writes
to `./keys/<name>/validator_key_info.json` and stores that relative reference in
the node profile.

`node init`, `node start`, and `node stop` use the same local-then-canonical
manifest resolution. A profile-level `--genesis-url` override takes precedence
over the manifest `genesis_source`.

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
