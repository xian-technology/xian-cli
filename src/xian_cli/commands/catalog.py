from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.commands.common import _write_validator_material_files
from xian_cli.config_repo import (
    list_module_paths,
    list_network_template_paths,
    list_solution_paths,
    resolve_module_path,
    resolve_network_template_path,
    resolve_solution_path,
)
from xian_cli.contract_bundles import validate_contract_bundle
from xian_cli.models import (
    read_json,
    read_module,
    read_network_template,
    read_solution,
    write_json,
)
from xian_cli.runtime import resolve_stack_dir


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


def _catalog_root_for_manifest(
    manifest_path: Path,
    collection_name: str,
) -> Path:
    resolved = manifest_path.resolve()
    for parent in resolved.parents:
        if parent.name == collection_name:
            return parent.parent
    return resolved.parent


def _resolve_catalog_ref(
    manifest_path: Path,
    ref: str,
    *,
    collection_name: str,
) -> Path:
    raw_ref = Path(ref)
    if raw_ref.is_absolute():
        return raw_ref.expanduser().resolve()

    catalog_root = _catalog_root_for_manifest(manifest_path, collection_name)
    rooted = (catalog_root / raw_ref).resolve()
    if rooted.exists():
        return rooted
    return (manifest_path.parent / raw_ref).resolve()


def _validate_module_assets(module_path: Path, module: dict) -> dict:
    contract_paths: list[str] = module["contract_paths"]
    contract_bundle_paths: list[str] = module["contract_bundle_paths"]
    missing_contracts = [
        ref
        for ref in contract_paths
        if not _resolve_catalog_ref(
            module_path,
            ref,
            collection_name="modules",
        ).exists()
    ]
    if missing_contracts:
        raise FileNotFoundError(
            f"module {module['name']} references missing contracts: "
            f"{missing_contracts}"
        )

    validated_bundles = []
    for bundle_ref in contract_bundle_paths:
        bundle_path = _resolve_catalog_ref(
            module_path,
            bundle_ref,
            collection_name="modules",
        )
        if not bundle_path.exists():
            raise FileNotFoundError(f"module bundle not found: {bundle_ref}")
        validated_bundles.append(validate_contract_bundle(bundle_path))

    return {
        "ok": True,
        "path": str(module_path.resolve()),
        "name": module["name"],
        "contract_count": len(contract_paths),
        "contract_bundle_count": len(contract_bundle_paths),
        "bundles": validated_bundles,
    }


def _load_module(
    *,
    base_dir: Path,
    module_name: str,
    configs_dir: Path | None,
) -> tuple[Path, dict]:
    module_path = resolve_module_path(
        base_dir=base_dir,
        module_name=module_name,
        configs_dir=configs_dir,
    )
    return module_path, read_module(module_path)


def _module_recipe(module: dict, recipe_name: str | None) -> dict:
    selected_recipe = recipe_name or module["default_recipe"]
    for recipe in module["recipes"]:
        if recipe["name"] == selected_recipe:
            return recipe
    available = sorted(recipe["name"] for recipe in module["recipes"])
    raise ValueError(
        f"module recipe '{selected_recipe}' not found; available: {available}"
    )


def _bool_backend_arg(name: str, value: bool) -> str:
    return f"--{name}" if value else f"--no-{name}"


def _env_var_for_repo(repo_name: str) -> str:
    return repo_name.upper().replace("-", "_") + "_DIR"


def _resolve_external_repo_dir(
    *,
    base_dir: Path,
    repo_name: str,
    explicit: Path | None = None,
) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)

    env_value = os.environ.get(_env_var_for_repo(repo_name))
    if env_value:
        candidates.append(Path(env_value))

    candidates.extend(
        [
            base_dir / repo_name,
            base_dir.parent / repo_name,
            Path(__file__).resolve().parents[3] / repo_name,
        ]
    )
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.exists():
            return resolved

    raise FileNotFoundError(
        f"unable to resolve {repo_name}; pass --repo-dir or set "
        f"{_env_var_for_repo(repo_name)}"
    )


def _handle_external_module_install(
    *,
    args: argparse.Namespace,
    base_dir: Path,
    module: dict,
    recipe: dict,
    install: dict,
) -> int:
    repo_name = install.get("repo")
    command_text = install.get("command")
    if not isinstance(repo_name, str) or not repo_name:
        raise ValueError("external module installer must define repo")
    if not isinstance(command_text, str) or not command_text:
        raise ValueError("external module installer must define command")

    repo_dir = _resolve_external_repo_dir(
        base_dir=base_dir,
        repo_name=repo_name,
        explicit=args.repo_dir,
    )
    command = shlex.split(command_text)
    payload = {
        "module": module["name"],
        "recipe": recipe["name"],
        "repo": repo_name,
        "cwd": str(repo_dir),
        "command": command,
    }
    if args.dry_run:
        payload["dry_run"] = True
        print(json.dumps(payload, indent=2))
        return 0

    result = subprocess.run(
        command,
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        command_payload = json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError:
        command_payload = {"stdout": result.stdout}
    if result.stderr:
        command_payload["stderr"] = result.stderr
    command_payload.update(payload)
    print(json.dumps(command_payload, indent=2))
    return 0


def _handle_module_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    modules = [
        read_module(path)
        for path in list_module_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    summaries = [
        {
            "name": module["name"],
            "display_name": module["display_name"],
            "category": module["category"],
            "maturity": module["maturity"],
            "description": module["description"],
            "default_recipe": module["default_recipe"],
            "docs_path": module["docs_path"],
        }
        for module in modules
    ]
    print(json.dumps(summaries, indent=2))
    return 0


def _handle_module_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    _, module = _load_module(
        base_dir=base_dir,
        module_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(module, indent=2))
    return 0


def _handle_module_validate(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    module_path, module = _load_module(
        base_dir=base_dir,
        module_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(_validate_module_assets(module_path, module), indent=2))
    return 0


def _handle_module_install(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    module_path, module = _load_module(
        base_dir=base_dir,
        module_name=args.name,
        configs_dir=args.configs_dir,
    )
    _validate_module_assets(module_path, module)
    recipe = _module_recipe(module, args.recipe)
    install = recipe["install"]
    if install["kind"] == "external":
        return _handle_external_module_install(
            args=args,
            base_dir=base_dir,
            module=module,
            recipe=recipe,
            install=install,
        )
    if install["kind"] != "xian-stack.localnet-dex-bootstrap":
        raise ValueError(
            f"module {module['name']} recipe {recipe['name']} uses install "
            f"kind {install['kind']!r}; run the owning repo bootstrap command"
        )
    if module["name"] != "dex":
        raise ValueError("only the dex module has a stack installer today")

    if not module["contract_bundle_paths"]:
        raise ValueError("dex module must define a contract bundle")
    dex_bundle = _resolve_catalog_ref(
        module_path,
        module["contract_bundle_paths"][0],
        collection_name="modules",
    )
    stack_dir = resolve_stack_dir(base_dir, explicit=args.stack_dir)
    command = [
        sys.executable,
        str(stack_dir / "scripts" / "backend.py"),
        "localnet-dex-bootstrap",
        "--dex-bundle",
        str(dex_bundle),
        _bool_backend_arg("deploy-helper", bool(install["deploy_helper"])),
        _bool_backend_arg(
            "seed-demo-pool",
            bool(install["seed_demo_pool"]),
        ),
        _bool_backend_arg(
            "top-up-liquidity",
            bool(install["top_up_liquidity"]) or args.top_up_liquidity,
        ),
        _bool_backend_arg(
            "emit-test-swap",
            bool(install["emit_test_swap"]) or args.emit_test_swap,
        ),
    ]
    if args.rpc_url is not None:
        command.extend(["--rpc-url", args.rpc_url])
    if args.chain_id is not None:
        command.extend(["--chain-id", args.chain_id])
    if args.deployer_private_key is not None:
        command.extend(["--deployer-private-key", args.deployer_private_key])

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "module": module["name"],
                    "recipe": recipe["name"],
                    "bundle": str(dex_bundle),
                    "command": command,
                },
                indent=2,
            )
        )
        return 0

    result = subprocess.run(
        command,
        cwd=stack_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    payload["module"] = module["name"]
    payload["recipe"] = recipe["name"]
    print(json.dumps(payload, indent=2))
    return 0


def _load_solution(
    *,
    base_dir: Path,
    solution_name: str,
    configs_dir: Path | None,
) -> dict:
    solution_path = resolve_solution_path(
        base_dir=base_dir,
        solution_name=solution_name,
        configs_dir=configs_dir,
    )
    return read_solution(solution_path)


def _handle_solution_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    solutions = [
        read_solution(path)
        for path in list_solution_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    summaries = [
        {
            "name": solution["name"],
            "display_name": solution["display_name"],
            "description": solution["description"],
            "recommended_local_template": solution[
                "recommended_local_template"
            ],
            "recommended_remote_template": solution[
                "recommended_remote_template"
            ],
            "docs_path": solution["docs_path"],
            "example_dir": solution["example_dir"],
            "modules": solution["modules"],
            "services": solution["services"],
        }
        for solution in solutions
    ]
    print(json.dumps(summaries, indent=2))
    return 0


def _handle_solution_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    solution = _load_solution(
        base_dir=base_dir,
        solution_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(solution, indent=2))
    return 0


def _handle_solution_starter(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    solution = _load_solution(
        base_dir=base_dir,
        solution_name=args.name,
        configs_dir=args.configs_dir,
    )
    flow = next(
        (
            item
            for item in solution["starter_flows"]
            if item["name"] == args.flow
        ),
        None,
    )
    if flow is None:
        available = sorted(item["name"] for item in solution["starter_flows"])
        raise ValueError(
            f"solution flow '{args.flow}' not found; available: {available}"
        )

    starter = {
        "name": solution["name"],
        "display_name": solution["display_name"],
        "description": solution["description"],
        "use_case": solution["use_case"],
        "docs_path": solution["docs_path"],
        "example_dir": solution["example_dir"],
        "modules": solution["modules"],
        "services": solution["services"],
        "contract_bundle_paths": solution["contract_bundle_paths"],
        "contract_paths": solution["contract_paths"],
        "flow": flow,
    }
    print(json.dumps(starter, indent=2))
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
            raise ValueError(
                "--name is required when reading source from stdin"
            )
        module_name = args.name
    else:
        source_path = source_path.resolve()
        source = source_path.read_text(encoding="utf-8")
        module_name = args.name or _infer_contract_module_name(source_path)

    try:
        from contracting.artifacts import build_contract_artifacts
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "xian contract build-artifacts requires xian-tech-contracting"
        ) from exc

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
        payload = get_node_setup_module().generate_validator_material(
            args.private_key
        )

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
