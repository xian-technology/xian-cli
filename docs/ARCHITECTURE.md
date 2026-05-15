# Architecture

`xian-cli` is the operator-facing command surface for Xian nodes and networks.

Main areas:

- `src/xian_cli/cli.py`: thin parser/dispatcher entrypoint
- `src/xian_cli/commands/`: focused command implementation modules for
  catalog/key/contract commands, network workflows, node operations, recovery,
  diagnostics, and shared node context
- `src/xian_cli/models.py`: manifest and profile models
- `src/xian_cli/runtime.py`: backend/runtime integration
- `src/xian_cli/config_repo.py`: canonical manifest resolution from `xian-configs`
- `tests/`: CLI and lifecycle contract coverage

Dependency direction:

- consumes `xian-abci` bootstrap/config helpers
- consumes `xian-stack` backend operations
- consumes `xian-configs` manifests
