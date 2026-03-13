# Repository Guidelines

## Scope
- `xian-cli` owns the operator UX for Xian nodes and networks.
- Keep commands such as `network create`, `network join`, `node init`, `node start`, `node stop`, `node status`, and future `doctor` flows here.
- Reusable bootstrap logic belongs in `xian-abci`; backend runtime orchestration belongs in `xian-stack`.
- This repo should consume network metadata and manifests, not become the canonical home for chain-specific genesis assets.

## Project Layout
- `src/xian_cli/cli.py`: main command surface and argument parsing.
- `src/xian_cli/models.py`: network manifest and node profile models.
- `src/xian_cli/runtime.py`: backend-specific runtime helpers.
- `src/xian_cli/abci_bridge.py`: import bridge into `xian-abci`.
- `src/xian_cli/cometbft.py`: key generation and CometBFT-specific helpers.
- `docs/LIFECYCLE_CONTRACT.md`: canonical lifecycle and artifact contract.
- `tests/`: CLI, manifest/profile, and backend dispatch tests.

## Change Routing
- Do not copy ABCI setup logic into this repo. Import helpers from `xian-abci` instead.
- Do not grow `xian-stack` Make targets into the public UX. `xian-cli` should call stable backend operations and present the operator-facing flow.
- If you change manifest or profile formats, update `docs/LIFECYCLE_CONTRACT.md` and `README.md` in the same change.
- If a feature needs canonical network definitions, keep those outside this repo and consume them here.

## Validation
- Preferred setup: `uv sync --group dev`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Tests: `uv run pytest`

## Notes
- The reference workspace is `~/xian` with sibling repos beside this one.
- Keep this repo small and orchestration-focused. Long operator flows should compose lower-level helpers rather than re-implement them.
