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
Local manifests use the same network-first layout as `xian-configs`:
`./networks/<name>/manifest.json` with a colocated `genesis.json` when the CLI
generates one.

## Workspace Model

The preferred layout is a shared parent directory such as `~/xian/` containing
sibling checkouts of `xian-cli`, `xian-abci`, `xian-configs`, and `xian-stack`.
