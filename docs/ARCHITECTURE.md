# Architecture

`xian-cli` is the operator-facing command surface for Xian nodes and networks.

Main areas:

- `src/xian_cli/cli.py`: thin parser/dispatcher entrypoint
- `src/xian_cli/commands/`: focused command implementation modules for keys,
  network templates, contract artifact/bundle helpers, network workflows, node
  operations, recovery, diagnostics, and shared node context
- `src/xian_cli/models.py`: manifest and profile models
- `src/xian_cli/runtime.py`: backend/runtime integration
- `src/xian_cli/config_repo.py`: canonical manifest resolution from `xian-configs`
- `tests/`: CLI and lifecycle contract coverage

Node commands keep orchestration in `commands/node.py`: resolve operator paths
and inputs, call the bootstrap or runtime helpers, then present their results.
`commands/node_options.py` translates profile settings into explicit ABCI
configuration options. It receives the lazily loaded bootstrap module so CLI
help and status do not import the node runtime. `commands/node_status.py` projects
collected observations into the status summary without performing I/O.

Dependency direction:

- consumes `xian-abci` bootstrap/config helpers
- consumes `xian-stack` backend operations
- consumes `xian-configs` manifests and templates
