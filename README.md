# xian-cli

`xian-cli` is the operator-facing and automation-facing control plane for
Xian. It owns manifests, node profiles, lifecycle commands, health checks,
local bootstrap flows, and JSON-first client commands without turning
`xian-abci`, `xian-py`, or `xian-stack` into user-facing tools.

The published PyPI package is `xian-tech-cli`. The installed console command
remains `xian`. Runtime-heavy commands expect access to `xian-stack` and
canonical manifests from `xian-configs`, either through the default sibling
workspace layout or explicit `--stack-dir` and `--configs-dir` flags.

## Quick Start

Local development in a sibling-repo workspace:

```bash
uv sync --group dev
uv run xian --help
```

Isolated operator install from a published release:

```bash
uv tool install xian-tech-cli       # or: pipx install xian-tech-cli
xian --help
```

Bootstrap installer (prefers `uv`, falls back to `pipx`, then
`python3 -m pip --user`):

```bash
curl -fsSL https://raw.githubusercontent.com/xian-technology/xian-cli/main/scripts/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/xian-technology/xian-cli/main/scripts/install.ps1 | iex
```

Set `XIAN_CLI_VERSION` before either installer to pin a specific release.

### Common Workflows

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

Wallet, query, and transaction automation against a running node:

```bash
uv run xian client wallet generate --include-private-key
uv run xian client query nonce --node-url http://127.0.0.1:26657 <address>
uv run xian client tx transfer \
  --node-url http://127.0.0.1:26657 \
  --private-key-env XIAN_PRIVATE_KEY \
  <recipient> 1.25
```

## Principles

- **Operator UX lives here.** Deterministic node logic stays in `xian-abci`,
  and local runtime orchestration stays in `xian-stack`. This repo is the
  control plane that ties them together.
- **Explicit artifacts, not hidden state.** Manifests and node profiles are
  human-readable files. The CLI inspects, generates, and updates them; it
  does not invent state outside them.
- **Templates accelerate, never lock in.** Templates, modules, and solutions
  shorten common setups, but an operator who knows what they are doing should
  still be able to work directly with manifests, profiles, and node homes.
- **Diagnostics are first-class.** Health, endpoint discovery, and `doctor`
  paths are core features, not afterthoughts.
- **JSON-first for automation.** Client commands and inspection commands emit
  machine-readable output suitable for scripts and CI.

## Key Directories

- `src/xian_cli/` — commands, models, manifest handling, and backend
  integration.
  - `cli.py`, `parser.py` — argument parsing and command dispatch.
  - `client/` — wallet, query, call, simulate, and transaction commands.
  - `config_repo.py`, `models.py` — manifest and profile schemas.
  - `abci_bridge.py`, `runtime.py` — node-runtime integration.
  - `contract_bundles.py` — hash-pinned contract-bundle validation.
- `scripts/` — install / packaging helpers (e.g. `install.sh`, `install.ps1`).
- `tests/` — CLI behavior and manifest / profile validation coverage.
- `docs/` — architecture, lifecycle contract, distribution notes, backlog.

## Capabilities

- key generation and validator material
- network template, module, and solution discovery
- network creation and network join flows
- node initialization, start, stop, and status
- endpoint and health discovery, including optional dashboard, monitoring,
  and stack-managed `xian-intentkit` / `xian-dex-automation`
- snapshot restore and doctor diagnostics
- module install / validation flows backed by `xian-configs`
- solution starter flows backed by `xian-configs`
- hash-pinned contract-bundle validation
- wallet, query, call / simulate, and transaction automation via `xian-py`

## Command Groups

- `xian keys ...` — generate validator and account material
- `xian network template ...` — inspect reusable network templates
- `xian network create ...` — create a local / operator-managed network profile
- `xian network join ...` — join an existing preset-backed or remote network
- `xian node ...` — initialize, start, stop, inspect, and recover a node profile
- `xian client ...` — wallet, query, call / simulate, and transaction automation
- `xian module ...` — inspect, validate, and install reusable modules
- `xian solution ...` — discover full application / operator starter flows
- `xian contract bundle ...` — validate hash-pinned contract bundles
- `xian doctor ...` — run broader local diagnostics

## Validation

```bash
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Related Docs

- [AGENTS.md](AGENTS.md) — repo-specific guidance for AI agents and contributors
- [docs/README.md](docs/README.md) — index of internal docs
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — major components and dependency direction
- [docs/BACKLOG.md](docs/BACKLOG.md) — open work and follow-ups
- [docs/LIFECYCLE_CONTRACT.md](docs/LIFECYCLE_CONTRACT.md) — node-lifecycle contract that the CLI enforces
- [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md) — packaging, install paths, and release-channel rules
