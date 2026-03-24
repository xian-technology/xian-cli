# xian-cli

`xian-cli` is the operator control plane for Xian nodes and networks. It keeps
lifecycle UX out of `xian-abci` and treats `xian-stack` as a backend runtime
surface instead of a user-facing tool.

## Scope

This repo owns:

- command-line flows such as `keys`, `network create`, `network join`,
  `node init`, `node status`, `snapshot restore`, and `doctor`
- local operator artifacts such as network manifests and node profiles
- orchestration across `xian-abci` primitives and `xian-stack` backend actions

This repo does not own:

- deterministic node logic or ABCI internals
- Docker or Compose topology
- canonical network-specific genesis assets

## Key Directories

- `src/xian_cli/`: command implementations, models, and manifest/profile logic
- `docs/`: lifecycle contracts and repo-local notes
- `tests/`: CLI and manifest/profile validation coverage

## Validation

```bash
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Related Docs

- [AGENTS.md](AGENTS.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/BACKLOG.md](docs/BACKLOG.md)
- [docs/LIFECYCLE_CONTRACT.md](docs/LIFECYCLE_CONTRACT.md)

## Common Workflows

```bash
uv run xian --help
uv run xian network template list
uv run xian keys validator generate --out-dir ./keys
uv run xian network create local-dev --chain-id xian-local-1 \
  --template single-node-dev --generate-validator-key --init-node
uv run xian network join mainnet-node --network mainnet \
  --template embedded-backend --generate-validator-key \
  --init-node --restore-snapshot
uv run xian node init mainnet-node --restore-snapshot
uv run xian node status mainnet-node
uv run xian node endpoints mainnet-node
uv run xian node health mainnet-node
uv run xian snapshot restore mainnet-node
uv run xian doctor mainnet-node
uv run xian doctor mainnet-node --skip-live-checks
uv run xian node start mainnet-node
uv run xian node stop mainnet-node
```

`--genesis-source` accepts either a local file path or an `http`/`https` URL.
Local manifests use the same network-first layout as `xian-configs`:
`./networks/<name>/manifest.json` with a colocated `genesis.json` when the CLI
generates one.
generates one. `network join` resolves the named network manifest immediately.
It prefers that local path and otherwise falls back to the sibling
`xian-configs/networks/<name>/manifest.json` manifest. Canonical manifest data
such as `runtime_backend` is used as the default, while node-local overrides
such as `--seed`, `--snapshot-url`, and `--genesis-url` stay in the node
profile.

Block-time policy is explicit too:

- `on_demand`: no empty blocks while idle
- `idle_interval`: emit an empty block after an idle interval such as `10s`
- `periodic`: keep scheduled empty blocks enabled with the chosen interval

This only changes when chain time advances during idle periods. Contract `now`
always comes from the finalized block timestamp agreed by consensus.

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

Canonical or local templates now provide the easiest way to prefill that flow.
Use `xian network template list` to inspect the available starter shapes, then
pass `--template <name>` to `network create` or `network join`. Templates can
set defaults for runtime backend, block policy, tracer mode, bootstrap
validator names, service-node / indexed mode, dashboard exposure, monitoring,
and pruning.

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

`node endpoints` prints the effective local URLs for CometBFT RPC,
`abci_query`, Xian and CometBFT metrics, and optional dashboard / Prometheus /
Grafana services. This is the quickest way to discover what a template-enabled
node is expected to expose.

`node health` is the concise live-runtime view. It surfaces the `xian-stack`
health state, endpoint health, optional disk-pressure checks, state-sync
readiness from the rendered CometBFT config, and the effective snapshot
bootstrap URL.

`doctor` now defaults to live health checks when a node name is provided. That
includes backend state, RPC reachability, and optional dashboard / monitoring
services when they are enabled in the profile. Use `--skip-live-checks` when
you only want an offline workspace and node-home preflight.

Node profiles now also carry `monitoring_enabled`. When that is true,
`xian-cli` asks `xian-stack` to manage the Prometheus and Grafana sidecars
alongside the node runtime.

## Workspace Model

The preferred layout is a shared parent directory such as `~/xian/` containing
sibling checkouts of `xian-cli`, `xian-abci`, `xian-configs`, and `xian-stack`.
