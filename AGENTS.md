# Repository Guidelines

## Scope
- `xian-cli` owns the operator UX for Xian nodes and networks.
- Keep commands such as `network create`, `network join`, `node init`,
  `node start`, `node stop`, `node status`, `snapshot restore`, and `doctor`
  flows here.
- Reusable bootstrap logic belongs in `xian-abci`; backend runtime orchestration belongs in `xian-stack`.
- This repo should consume network metadata and manifests, not become the canonical home for chain-specific genesis assets.

## Project Layout
- `src/xian_cli/cli.py`: main command surface and argument parsing.
- `src/xian_cli/models.py`: network manifest and node profile models.
- `src/xian_cli/config_repo.py`: resolution of canonical network manifests from
  `xian-configs`.
- `src/xian_cli/runtime.py`: backend-specific runtime helpers.
- `src/xian_cli/abci_bridge.py`: import bridge into `xian-abci`.
- `src/xian_cli/cometbft.py`: key generation and CometBFT-specific helpers.
- `docs/LIFECYCLE_CONTRACT.md`: canonical lifecycle and artifact contract.
- `tests/`: CLI, manifest/profile, and backend dispatch tests.

## Change Routing
- Do not copy ABCI setup logic into this repo. Import helpers from `xian-abci` instead.
- Do not grow `xian-stack` Make targets into the public UX. `xian-cli` should call stable backend operations and present the operator-facing flow.
- If you change manifest or profile formats, update `docs/LIFECYCLE_CONTRACT.md` and `README.md` in the same change.
- If a feature needs canonical network definitions, keep those in
  `xian-configs` and consume them here.
- Prefer the network-first local layout `./networks/<name>/manifest.json`.
  Treat flat `./networks/<name>.json` manifests as legacy fallback only.
- `network join` must resolve the referenced network manifest up front. Keep
  network-owned metadata in the manifest and node-local overrides in the
  profile.
- `network create` may generate a colocated local `genesis.json`, but private
  key material must still live under `./keys/` and be referenced from node
  profiles rather than embedded in manifests.
- `network create` may also define multiple initial validators via repeated
  `--validator` flags. Only the bootstrap node should carry machine-local home
  and stack settings by default.
- `network join --init-node` should reuse the same initialization path as
  `node init`, not fork a second bootstrap implementation.
- Snapshot restore precedence must stay explicit: CLI override, then node
  profile, then canonical manifest.
- Canonical manifests from `xian-configs` now live at
  `networks/<name>/manifest.json` with colocated `genesis.json`.

## Validation
- Preferred setup: `uv sync --group dev`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Tests: `uv run pytest`

## Notes
- The reference workspace is `~/xian` with sibling repos beside this one.
- `xian-cli` should prefer local `./networks` manifests when present, but fall
  back cleanly to canonical manifests from the sibling `xian-configs` repo.
- `network create --bootstrap-node` should be able to carry the flow through to
  a ready-to-init node without requiring manual file assembly.
- `node status` should include backend state from `xian-stack` when the profile
  uses that runtime backend, not just local file checks.
- Keep precedence rules explicit: local node overrides win over network
  defaults, but the CLI should not duplicate canonical network metadata into
  node profiles without a clear reason.
- `network join` may generate validator key material as part of the bootstrap
  flow, but the generated files should still be referenced from the profile
  rather than inlined into it.
- If snapshot restore behavior changes, update both `README.md` and
  `docs/LIFECYCLE_CONTRACT.md` in the same change.
- Keep this repo small and orchestration-focused. Long operator flows should compose lower-level helpers rather than re-implement them.
