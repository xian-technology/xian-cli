# Xian CLI Lifecycle Contract

## Goal

`xian-cli` is the operator-facing control plane for Xian nodes and networks. It should own the workflow, persist the operator's source-of-truth files, and call lower-level code from `xian-abci` and `xian-stack`. Operators should not need to shell into containers or replay Make targets manually.

## User-Facing Command Surface

The external UX should stay smaller than the internal lifecycle.

Primary commands:

- `xian keys validator generate`
- `xian network template list`
- `xian network template show`
- `xian network create`
- `xian network join`
- `xian node init`
- `xian node start`
- `xian node stop`
- `xian node status`
- `xian node endpoints`
- `xian node health`
- `xian recovery validate`
- `xian recovery apply`
- `xian snapshot restore`
- `xian doctor`

Command intent:

- `network template list/show` surfaces canonical or local starter templates
  that prefill network and node-profile defaults.
- `network create` defines a new network manifest and may bootstrap the first
  local node for that network, including a multi-validator local genesis when
  the operator declares additional initial validators.
- `network join` defines a local node profile for an existing network and may
  initialize the node immediately.
- `node init` performs all preparation stages needed before startup.
- `node status` reports local bootstrap state, backend runtime state, and
  optional live RPC status.
- `snapshot restore` applies the effective snapshot source to an initialized
  node home.
- `doctor` checks workspace prerequisites and optional node prerequisites.
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
- Inputs: chain ID, bootstrap mode, genesis source, seeds, optional snapshot
  source, and block-time policy
- Outputs: immutable network-level description

This stage defines facts about a network, not about a specific node.

### 3. Node Profile

- Owner: `xian-cli`
- Source of truth: node profile JSON
- Inputs: network reference, moniker, key references, role flags, home path,
  and optional local block-policy override
- Outputs: local node intent

This stage defines machine-local choices. It should reference keys and networks, not duplicate them.

Resolution policy:

- local manifests should be written at `./networks/<name>/manifest.json`
- local templates may be written at `./templates/<name>.json`
- `network join` should resolve the referenced network manifest immediately
- template resolution should prefer a local `./templates/<name>.json` file and
  otherwise fall back to `xian-configs/templates/<name>.json`
- `runtime_backend` is explicit and currently must be `xian-stack`
- node-local overrides such as extra seeds, snapshot URL overrides, and genesis
  URL overrides belong in the node profile
- `network create` may write a colocated `genesis.json` beside the manifest
- `network join --init-node` should immediately hand the new profile into the
  same node initialization path used by `node init`

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
  - join flow: genesis URL/file or deterministic preset manifest, optional snapshot URL
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

This is the manifest-driven render phase that replaced the earlier manual
`configure.py` workflow.

### 8. Snapshot Restore

- Owner: `xian-cli`
- Helpers: `xian-abci`
- Inputs: initialized node home and effective snapshot source
- Outputs: restored chain state files inside the node home

Snapshot restore is a distinct stage because it depends on an initialized home
but happens before process startup. Snapshot source precedence should stay
explicit and stable:

1. explicit CLI override
2. node profile `snapshot_url`
3. network manifest `snapshot_url`

### 9. Process Start and Health Checks

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
  "schema_version": 1,
  "name": "devnet",
  "chain_id": "xian-devnet-1",
  "mode": "join",
  "runtime_backend": "xian-stack",
  "genesis_preset": "devnet",
  "genesis_time": "2026-03-30T00:00:00.000000Z",
  "snapshot_url": null,
  "seed_nodes": [],
  "block_policy_mode": "idle_interval",
  "block_policy_interval": "5s"
}
```

Resolution policy:

- prefer a local `./networks/<name>/manifest.json` manifest when present
- otherwise resolve the canonical manifest from the sibling
  `xian-configs/networks/<name>/manifest.json`

### Node Profile

The node profile is the machine-local source of truth. Target fields:

```json
{
  "schema_version": 1,
  "name": "validator-1",
  "network": "devnet",
  "moniker": "validator-1",
  "validator_key_ref": "./keys/validator-1/validator_key_info.json",
  "home": "~/.cometbft",
  "service_node": false,
  "runtime_backend": "xian-stack",
  "pruning_enabled": false,
  "blocks_to_keep": 100000,
  "monitoring_enabled": false,
  "intentkit_enabled": false,
  "intentkit_network_id": "xian-devnet",
  "intentkit_host": "127.0.0.1",
  "intentkit_port": 38000,
  "intentkit_api_port": 38080,
  "dex_automation_enabled": false,
  "dex_automation_host": "127.0.0.1",
  "dex_automation_port": 38280,
  "dex_automation_config": null,
  "block_policy_mode": "on_demand",
  "block_policy_interval": "0s"
}
```

Rules:

- manifests and profiles must declare `schema_version: 1`
- network manifests do not contain private keys
- node profiles reference keys; they do not inline them
- `block_policy_mode` is network-owned by default and may be overridden
  intentionally in the node profile
- `on_demand` means no idle empty blocks; `idle_interval` means empty blocks
  only after an idle interval; `periodic` means scheduled empty blocks are
  enabled
- contract `now` always comes from finalized block time; the policy only
  changes whether time advances while the chain is idle
- `network create` may generate a local `genesis.json`, but that file remains a
  derived network artifact rather than a place to hide node-local state
- canonical preset manifests build genesis deterministically from a contract
  bundle plus a fixed `genesis_time`; they do not carry a checked-in
  `genesis.json`
- templates may provide defaults for runtime backend, tracing, bootstrap
  validator names, service-node mode, dashboard exposure, monitoring,
  `xian-intentkit` exposure, `xian-dex-automation` exposure, and pruning, but
  explicit CLI flags should still win
- `intentkit_network_id` selects the Xian network slot inside
  `xian-intentkit`; canonical networks map to `xian-mainnet`, `xian-testnet`,
  or `xian-devnet`, while local and private stack-managed networks default to
  `xian-localnet`
- stack-managed `xian-intentkit` uses the node's resolved RPC endpoint and
  chain ID to generate `xian-intentkit/deployment/.env`
- stack-managed `xian-dex-automation` uses the node's resolved RPC endpoint,
  a generated local service-wallet key file, and a generated config unless the
  profile points `dex_automation_config` at an explicit path
- `network join` may generate validator key material, but it should still write
  files under the workspace and store only a reference in the profile
- node profiles should not duplicate network-owned seeds, snapshot URLs, or
  genesis sources unless the operator is intentionally applying a local override
- generated runtime files are derived artifacts, not source-of-truth documents
- `network join` may optionally execute node initialization immediately, but it
  must still persist the profile before derived runtime files are created

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
  - validate
  - smoke
  - smoke-cli
  - start
  - stop
  - status
  - optional stack-managed `xian-intentkit` bring-up and shutdown through the
    same start/stop/status/endpoints/health surface
  - optional stack-managed `xian-dex-automation` bring-up and shutdown through
    the same start/stop/status/endpoints/health surface
  - localnet-init
  - localnet-build
  - localnet-up
  - localnet-down
  - localnet-status

### `xian-intentkit`

- remains an independent repo with its own Compose topology and env contract
- should not be copied into `xian-stack`; the stack owns only the thin adapter
  layer, generated env handoff, and published local ports

### `xian-dex-automation`

- remains an independent repo with its own Python service, local admin UI, and
  config contract
- should not be copied into `xian-stack`; the stack owns only the thin adapter,
  generated local config/key material, process lifecycle, and published local
  port

These are now exposed through the machine-readable `scripts/backend.py`
wrapper in `xian-stack`. Internally that wrapper may still call `make` and
`docker compose`, but the wrapper is the backend contract that `xian-cli`
consumes.

### `xian-contracting`

- no direct operator lifecycle ownership
- supports `xian-abci` through runtime behavior and genesis/state semantics

### `xian-py`

- no direct node startup ownership
- acceptable dependency for wallet/key utilities where appropriate

## Current Refactor Targets

The current implementation already supports:

1. canonical manifest resolution from `xian-configs`
2. validator key generation during `network join`
3. optional node initialization during `network join`
4. explicit snapshot restore as a separate command or as part of `node init`
5. creation-side bootstrap through `network create --bootstrap-node`
6. explicit `node status` and `doctor` inspection flows
7. multi-validator local network creation through repeated `--validator`
8. stable backend status integration through `xian-stack node-status`

The next implementation passes should focus on keeping this contract small and
stable while expanding what sits behind it, especially richer canonical network
metadata in `xian-configs`.
