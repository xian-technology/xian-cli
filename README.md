# xian-cli

`xian-cli` is the operator-facing and automation-facing control plane for Xian.
It handles manifests, node profiles, lifecycle commands, health checks, local
bootstrap flows, and JSON-first client commands without turning `xian-abci`,
`xian-py`, or `xian-stack` into user-facing tools.

The published PyPI package name is `xian-tech-cli`. The installed console
command remains `xian`.

## Install

For local development in a sibling-repo workspace:

```bash
uv sync --group dev
uv run xian --help
```

For an isolated operator install from a published release:

```bash
uv tool install xian-tech-cli
xian --help
```

`pipx install xian-tech-cli` is also a valid operator install path if you
prefer `pipx` over `uv`.

For a bootstrap installer that prefers `uv`, then `pipx`, then
`python3 -m pip --user`:

```bash
curl -fsSL https://raw.githubusercontent.com/xian-technology/xian-cli/main/scripts/install.sh | sh
```

On Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/xian-technology/xian-cli/main/scripts/install.ps1 | iex
```

Set `XIAN_CLI_VERSION` before invoking either installer if you want to pin a
specific release.

The CLI itself is Python-packaged today, but it is the canonical operator
surface for Xian. Runtime-heavy commands still expect access to `xian-stack`
and canonical manifests from `xian-configs`, either via the default sibling
workspace layout or explicit `--stack-dir` and `--configs-dir` flags.

## Common Workflows

Create a local network from a template:

```bash
uv run xian network template list
uv run xian network create local-dev --chain-id xian-local-1 \
  --template single-node-dev --generate-validator-key --init-node
uv run xian node start local-dev
uv run xian node status local-dev
```

Join a preset-backed shared network with a local profile:

```bash
uv run xian network join devnet-node --network devnet \
  --template embedded-backend --generate-validator-key \
  --init-node --restore-snapshot
uv run xian node health devnet-node
uv run xian node endpoints devnet-node
```

Inspect or recover a configured node:

```bash
uv run xian doctor devnet-node
uv run xian doctor devnet-node --skip-live-checks
uv run xian snapshot restore devnet-node
```

For remote snapshot bootstrap, prefer a signed snapshot manifest plus trusted
snapshot signing keys in the network manifest or node profile.

Drive a node directly for wallet, query, and transaction automation:

```bash
uv run xian client wallet generate --include-private-key
uv run xian client query nonce --node-url http://127.0.0.1:26657 <address>
uv run xian client tx transfer \
  --node-url http://127.0.0.1:26657 \
  --private-key-env XIAN_PRIVATE_KEY \
  <recipient> 1.25
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
- endpoint and health discovery, including optional dashboard, monitoring, and
  stack-managed `xian-intentkit`
- snapshot restore and doctor diagnostics
- solution-pack starter flows built on `xian-configs`
- wallet, query, call/simulate, and transaction automation through `xian-py`

## Command Groups

- `xian keys ...`: generate validator and account material
- `xian network template ...`: inspect reusable network templates
- `xian network create ...`: create a local/operator-managed network profile
- `xian network join ...`: join an existing preset-backed or remote network
- `xian node ...`: initialize, start, stop, inspect, and recover a node profile
- `xian client ...`: wallet, query, call/simulate, and transaction automation
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
- [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md)
- [docs/LIFECYCLE_CONTRACT.md](docs/LIFECYCLE_CONTRACT.md)
