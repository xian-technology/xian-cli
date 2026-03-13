# xian-cli

`xian-cli` is the operator-facing command-line interface for Xian network and node lifecycle tasks.

This repository exists to keep network orchestration concerns out of `xian-abci`. The ABCI repo should expose reusable primitives for genesis generation, config rendering, and state tools; this CLI should compose those primitives into a coherent operator workflow.

## Initial scope

- Generate validator keys and CometBFT `priv_validator_key.json`
- Create network manifests for new networks
- Create node profiles for joining existing networks
- Provide a stable place to add `node start`, `node stop`, `doctor`, snapshot, and bootstrap flows

## Lifecycle Contract

The staged node and network lifecycle contract lives in
[`docs/LIFECYCLE_CONTRACT.md`](docs/LIFECYCLE_CONTRACT.md). That document
defines:

- the intended `xian` command surface
- the internal lifecycle stages owned by `xian-cli`
- the source-of-truth artifacts for networks and nodes
- the backend boundary between `xian-cli`, `xian-stack`, and `xian-abci`

## Quickstart

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
xian --help
```

Preferred development setup:

```bash
uv sync --group dev
uv run xian --help
```

Generate a validator key:

```bash
xian keys validator generate --out-dir ./keys
```

Create a network manifest:

```bash
xian network create local-dev --chain-id xian-local-1
```

`--genesis-source` accepts either a local file path or an `http`/`https` URL.

Create a node profile to join an existing network:

```bash
xian network join mainnet-node \
  --network mainnet \
  --validator-key-ref ./keys/mainnet-node/validator_key_info.json \
  --home ./.cometbft
```

Materialize the node home from the manifests and key bundle:

```bash
xian node init mainnet-node
```

For the `xian-stack` backend, `xian node init` writes the CometBFT home to
`xian-stack/.cometbft` by default unless the profile or command overrides it.

Start and stop the node through the `xian-stack` backend:

```bash
xian node start mainnet-node
xian node stop mainnet-node
```

## Workspace recommendation

For cross-repo work, keep `xian-cli`, `xian-stack`, `xian-abci`, and `xian-contracting` under a shared parent directory such as `~/xian/`. Start future sessions from that parent workspace when changes will span repository boundaries.

`xian node init` currently assumes either:

- `xian-abci` is installed in the same Python environment, or
- you are running from the shared workspace layout (`~/xian/xian-cli`, `~/xian/xian-abci`, ...)

## Validation

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
