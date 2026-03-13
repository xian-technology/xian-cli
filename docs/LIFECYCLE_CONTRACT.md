# Xian CLI Lifecycle Contract

## Goal

`xian-cli` is the operator-facing control plane for Xian nodes and networks. It should own the workflow, persist the operator's source-of-truth files, and call lower-level code from `xian-abci` and `xian-stack`. Operators should not need to shell into containers or replay Make targets manually.

## User-Facing Command Surface

The external UX should stay smaller than the internal lifecycle.

Planned primary commands:

- `xian keys validator generate`
- `xian network create`
- `xian network join`
- `xian node init`
- `xian node start`
- `xian node stop`
- `xian node status`
- `xian snapshot restore`
- `xian doctor`

Command intent:

- `network create` defines a new network manifest.
- `network join` defines a local node profile for an existing network.
- `node init` performs all preparation stages needed before startup.
- `node start` launches the runtime and verifies health.

`node init` should hide most of the current manual steps. It is the boundary between persisted intent and generated runtime state.

## Internal Lifecycle Stages

### 1. Key Material

- Owner: `xian-cli`
- Helpers: `xian-abci` for CometBFT validator key formatting, `xian-py` for wallet utilities when needed
- Inputs: existing private key or request to generate one
- Outputs: validator key bundle and metadata

This stage produces the validator material required for `priv_validator_key.json`. Private keys must not be embedded in network manifests.

### 2. Network Manifest

- Owner: `xian-cli`
- Source of truth: network manifest JSON, either local or canonical from `xian-configs`
- Inputs: chain ID, bootstrap mode, genesis source, seeds, optional snapshot source
- Outputs: immutable network-level description

This stage defines facts about a network, not about a specific node.

### 3. Node Profile

- Owner: `xian-cli`
- Source of truth: node profile JSON
- Inputs: network reference, moniker, key references, role flags, home path, runtime backend options
- Outputs: local node intent

This stage defines machine-local choices. It should reference keys and networks, not duplicate them.

Resolution policy:

- `network join` should resolve the referenced network manifest immediately
- effective defaults such as `runtime_backend` come from the network manifest
  unless the operator passes a node-local override
- node-local overrides such as extra seeds, snapshot URL overrides, and genesis
  URL overrides belong in the node profile

### 4. Runtime Preparation

- Owner: `xian-cli`
- Backend: `xian-stack` first
- Inputs: node profile and selected runtime backend
- Outputs: prepared execution environment

For the current stack, this means building or starting the Docker runtime and ensuring the mounted CometBFT home is available.

### 5. Genesis Acquisition or Generation

- Owner: `xian-cli`
- Helpers: `xian-abci`
- Inputs:
  - join flow: genesis URL/file or bundled network source, optional snapshot URL
  - create flow: contract preset, founder key, network preset, validator set inputs
- Outputs: materialized CometBFT genesis and optional snapshot payload

Joining an existing network and creating a new network are different paths, but they converge on the same artifact: a valid `genesis.json` in the node home.

### 6. CometBFT Home Initialization

- Owner: `xian-cli`
- Backend: runtime target, with CometBFT as the tool
- Inputs: node profile and target home path
- Outputs: initialized CometBFT home tree

This stage creates the default CometBFT files such as `config.toml`, `genesis.json`, `node_key.json`, and `priv_validator_state.json` when absent.

### 7. Config Rendering

- Owner: `xian-cli`
- Helpers: `xian-abci`
- Inputs: network manifest, node profile, generated keys, initialized home
- Outputs:
  - rendered `config.toml`
  - copied or generated `genesis.json`
  - `priv_validator_key.json`
  - pruning, BDS, RPC, and seed configuration

This replaces the current manual `configure.py`-driven step with a manifest-driven render phase.

### 8. Process Start and Health Checks

- Owner: `xian-cli`
- Backend: `xian-stack`
- Inputs: prepared runtime plus rendered node home
- Outputs: running node and health report

This stage starts CometBFT, the ABCI process, and optional BDS components, then verifies health through process status and RPC checks.

## Artifact Contract

### Network Manifest

The network manifest is the network-level source of truth. Target fields:

```json
{
  "name": "mainnet",
  "chain_id": "xian-mainnet-1",
  "mode": "join",
  "runtime_backend": "xian-stack",
  "genesis_source": "https://example/genesis.json",
  "snapshot_url": "https://example/snapshot.tar.gz",
  "seed_nodes": ["node_id@host:26656"]
}
```

Resolution policy:

- prefer a local `./networks/<name>.json` manifest when present
- otherwise resolve the canonical manifest from the sibling
  `xian-configs/networks/<name>/manifest.json`

### Node Profile

The node profile is the machine-local source of truth. Target fields:

```json
{
  "name": "validator-1",
  "network": "mainnet",
  "moniker": "validator-1",
  "validator_key_ref": "./keys/validator-1/validator_key_info.json",
  "home": "~/.cometbft",
  "service_node": false,
  "runtime_backend": "xian-stack",
  "pruning": {
    "enabled": false,
    "blocks_to_keep": 100000
  }
}
```

Rules:

- network manifests do not contain private keys
- node profiles reference keys; they do not inline them
- `network join` may generate validator key material, but it should still write
  files under the workspace and store only a reference in the profile
- node profiles should not duplicate network-owned seeds, snapshot URLs, or
  genesis sources unless the operator is intentionally applying a local override
- generated runtime files are derived artifacts, not source-of-truth documents

## Cross-Repo Interface Contract

### `xian-cli`

- owns manifests, profiles, orchestration decisions, health checks, and user UX

### `xian-abci`

- should expose importable helpers for:
  - validator key formatting
  - genesis generation
  - genesis merge/update
  - CometBFT config rendering
  - snapshot import/export support

### `xian-stack`

- should expose stable runtime operations for:
  - prepare
  - start
  - stop
  - status

Short term, these can still be backed by `docker compose` and Make targets. Long term, they should be treated as backend operations, not operator UX.

### `xian-contracting`

- no direct operator lifecycle ownership
- supports `xian-abci` through runtime behavior and genesis/state semantics

### `xian-py`

- no direct node startup ownership
- acceptable dependency for wallet/key utilities where appropriate

## Immediate Refactor Targets

The current blocking gaps are:

1. `xian-abci` tooling is script-shaped instead of importable.
2. `configure.py` combines too many responsibilities.
3. the genesis-generation path is inconsistent today.
4. `xian-stack` is acting as both runtime backend and operator UX.

The first implementation pass should therefore focus on:

1. extracting importable helpers from `xian-abci`
2. teaching `xian-cli node init` to execute stages 4 through 7
3. keeping `xian-stack` as the first runtime backend without exposing its Makefile as the long-term public interface
