# xian_cli

## Purpose
- This package contains the `xian-cli` command surface and supporting models.

## Contents
- `cli.py`: top-level command entrypoint
- `models.py`: network manifests and node profiles
- `runtime.py`: backend integration
- `config_repo.py`: canonical network manifest resolution

## Notes
- Keep this package orchestration-focused. Reusable lower-level bootstrap logic belongs in sibling repos.

