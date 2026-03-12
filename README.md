# xian-cli

`xian-cli` is the operator-facing command-line interface for Xian network and node lifecycle tasks.

This repository exists to keep network orchestration concerns out of `xian-abci`. The ABCI repo should expose reusable primitives for genesis generation, config rendering, and state tools; this CLI should compose those primitives into a coherent operator workflow.

## Initial scope

- Generate validator keys and CometBFT `priv_validator_key.json`
- Create network manifests for new networks
- Create node profiles for joining existing networks
- Provide a stable place to add `node start`, `node stop`, `doctor`, snapshot, and bootstrap flows

## Quickstart

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
xian --help
```

Generate a validator key:

```bash
xian keys validator generate --out-dir ./keys
```

Create a network manifest:

```bash
xian network create local-dev --chain-id xian-local-1
```

Create a node profile to join an existing network:

```bash
xian network join mainnet-node \
  --chain-id xian-mainnet-1 \
  --seed c3861ffd16cf6708aef6683d3d0471b6dedb3116@152.53.18.220:26656
```

## Workspace recommendation

For cross-repo work, keep `xian-cli`, `xian-stack`, `xian-abci`, and `xian-contracting` under a shared parent directory such as `~/xian/`. Start future sessions from that parent workspace when changes will span repository boundaries.

