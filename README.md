# xian-cli

`xian-cli` is the operator control plane for Xian nodes and networks. It exists
to keep lifecycle UX out of `xian-abci` and to keep `xian-stack` focused on
runtime backend operations instead of end-user workflows.

## Ownership

This repo owns:

- command-line flows such as `keys`, `network create`, `network join`,
  `node init`, `node status`, `snapshot restore`, and `doctor`
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
uv run xian network create local-dev --chain-id xian-local-1 \
  --generate-validator-key --bootstrap-node validator-1 \
  --validator validator-2 --init-node
uv run xian network join mainnet-node --network mainnet \
  --generate-validator-key --init-node --restore-snapshot
uv run xian node init mainnet-node --restore-snapshot
uv run xian node status mainnet-node
uv run xian snapshot restore mainnet-node
uv run xian doctor mainnet-node
uv run xian node start mainnet-node
uv run xian node stop mainnet-node
```

`--genesis-source` accepts either a local file path or an `http`/`https` URL.
Local manifests now use the same network-first layout as `xian-configs`:
`./networks/<name>/manifest.json` with a colocated `genesis.json` when the CLI
generates one. `network join` resolves the named network manifest immediately.
It prefers that local path and otherwise falls back to the sibling
`xian-configs/networks/<name>/manifest.json` manifest. Canonical manifest data
such as `runtime_backend` is used as the default, while node-local overrides
such as `--seed`, `--snapshot-url`, and `--genesis-url` stay in the node
profile.

Both network manifests and node profiles must carry `schema_version: 1`. The
artifact contract is explicit now; the CLI validates that shape on read and
writes the explicit version on new output.

`network create` can stay lightweight and only write a manifest, but it can now
also bootstrap a fresh local network. When validator key material is available,
it can generate a colocated `genesis.json`; when `--bootstrap-node` is set, it
can also create the first node profile and optionally initialize the CometBFT
home immediately with `--init-node`. Repeating `--validator <name>` adds more
initial validator profiles and includes them in the generated local genesis.
Only the bootstrap node is initialized immediately; additional profiles remain
declared intent until they are initialized on their own machines.

If the operator does not already have validator key material, `network join`
can generate it directly with `--generate-validator-key`. By default it writes
to `./keys/<name>/validator_key_info.json` and stores that relative reference in
the node profile.

`network join --init-node` now runs the same initialization flow as `node init`
immediately after writing the node profile. When combined with
`--restore-snapshot`, it restores the effective snapshot URL after the CometBFT
home is materialized.

`node init`, `node start`, `node stop`, and `snapshot restore` use the same
local-then-canonical manifest resolution. A profile-level `--genesis-url`
override takes precedence over the manifest `genesis_source`. Snapshot URL
precedence is explicit: command-line override first, then node profile, then
network manifest.

`node status` reports bootstrap artifacts, the `xian-stack` backend state when
available, and an optional live RPC status probe. `doctor` checks workspace
resolution and, when given a node name, the profile/manifest/home prerequisites
for that node.

## Workspace Model

The preferred layout is a shared parent directory such as `~/xian/` containing
sibling checkouts of `xian-cli`, `xian-abci`, `xian-configs`, and `xian-stack`.
`uv sync --group dev` installs `xian-abci` and its transitive workspace
dependencies into the same environment as `xian-cli`. The sibling-workspace
layout remains the supported authoring model for local path dependencies.

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
