# xian-cli

`xian-cli` is the operator-facing control plane for Xian networks and nodes. It
handles manifests, node profiles, lifecycle commands, health checks, and local
bootstrap flows without turning `xian-abci` or `xian-stack` into user-facing
tools.

## Common Workflows

Create a local network from a template:

```bash
uv run xian network template list
uv run xian network create local-dev --chain-id xian-local-1 \
  --template single-node-dev --generate-validator-key --init-node
uv run xian node start local-dev
uv run xian node status local-dev
```

Join an existing network with a local profile:

```bash
uv run xian network join mainnet-node --network mainnet \
  --template embedded-backend --generate-validator-key \
  --init-node --restore-snapshot
uv run xian node health mainnet-node
uv run xian node endpoints mainnet-node
```

Inspect or recover a configured node:

```bash
uv run xian doctor mainnet-node
uv run xian doctor mainnet-node --skip-live-checks
uv run xian snapshot restore mainnet-node
```

## Principles

- `xian-cli` owns operator UX. Deterministic node logic stays in `xian-abci`,
  and local runtime orchestration stays in `xian-stack`.
- Manifests and node profiles are explicit artifacts, not hidden state.
- Templates and solution packs should accelerate common setups, but they should
  remain optional. An operator who knows what they are doing should still be
  able to work directly with manifests, profiles, and node homes.
- Health, endpoint discovery, and diagnostics are first-class operator
  features, not afterthoughts.

## Key Directories

- `src/xian_cli/`: commands, models, manifest handling, and backend integration
- `tests/`: CLI behavior and manifest/profile validation coverage
- `docs/`: lifecycle contracts, architecture notes, and backlog items

## What It Covers

- key generation and validator material
- network template and solution-pack discovery
- network creation and network join flows
- node initialization, start, stop, and status
- endpoint and health discovery
- snapshot restore and doctor diagnostics
- solution-pack starter flows built on `xian-configs`

## Command Groups

- `xian keys ...`: generate validator and account material
- `xian network template ...`: inspect reusable network templates
- `xian network create ...`: create a local/operator-managed network profile
- `xian network join ...`: join an existing canonical or remote network
- `xian node ...`: initialize, start, stop, inspect, and recover a node profile
- `xian doctor ...`: run broader local diagnostics
- `xian solution-pack ...`: discover starter flows built on top of the golden
  path

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
