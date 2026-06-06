from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.commands.common import _write_validator_material_files
from xian_cli.config_repo import (
    list_network_template_paths,
    resolve_network_template_path,
)
from xian_cli.contract_bundles import validate_contract_bundle
from xian_cli.models import (
    read_json,
    read_network_template,
    write_json,
)


def _load_template(
    *,
    base_dir: Path,
    template_name: str | None,
    configs_dir: Path | None,
) -> dict | None:
    if template_name is None:
        return None
    template_path = resolve_network_template_path(
        base_dir=base_dir,
        template_name=template_name,
        configs_dir=configs_dir,
    )
    return read_network_template(template_path)


def _handle_network_template_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    templates = [
        read_network_template(path)
        for path in list_network_template_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    print(json.dumps(templates, indent=2))
    return 0


def _handle_network_template_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    template = _load_template(
        base_dir=base_dir,
        template_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(template, indent=2))
    return 0


def _handle_contract_bundle_validate(args: argparse.Namespace) -> int:
    result = validate_contract_bundle(args.path)
    print(json.dumps(result, indent=2))
    return 0


def _handle_contract_build_artifacts(args: argparse.Namespace) -> int:
    source_path = args.source
    if str(source_path) == "-":
        source = sys.stdin.read()
        if not args.name:
            raise ValueError("--name is required when reading source from stdin")
        module_name = args.name
    else:
        source_path = source_path.resolve()
        source = source_path.read_text(encoding="utf-8")
        module_name = args.name or _infer_contract_module_name(source_path)

    try:
        from contracting.artifacts import build_contract_artifacts
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("xian contract build-artifacts requires xian-tech-contracting") from exc

    artifacts = build_contract_artifacts(
        module_name=module_name,
        source=source,
        lint=not args.no_lint,
        vm_profile="xian_vm_v1",
    )
    if args.output is None:
        print(json.dumps(artifacts, indent=2, sort_keys=True))
    else:
        write_json(args.output.resolve(), artifacts, force=args.force)
    return 0


def _infer_contract_module_name(source_path: Path) -> str:
    filename = source_path.name
    if filename.endswith(".s.py"):
        return filename[: -len(".s.py")]
    return source_path.stem


def _handle_keys_validator_generate(args: argparse.Namespace) -> int:
    if args.out_dir is not None:
        metadata_path = _write_validator_material_files(
            out_dir=args.out_dir.resolve(),
            private_key=args.private_key,
            force=args.force,
        )
        payload = read_json(metadata_path)
    else:
        payload = get_node_setup_module().generate_validator_material(args.private_key)

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
