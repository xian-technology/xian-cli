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
    list_contract_pack_paths,
    list_example_paths,
    list_network_template_paths,
    list_product_paths,
    resolve_contract_pack_path,
    resolve_example_path,
    resolve_network_template_path,
    resolve_product_path,
)
from xian_cli.contract_bundles import validate_contract_bundle
from xian_cli.models import (
    read_contract_pack,
    read_example,
    read_json,
    read_network_template,
    read_product,
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


def _validate_contract_pack_assets(pack_path: Path, pack: dict) -> dict:
    contract_paths: list[str] = pack["contract_paths"]
    contract_bundle_paths: list[str] = pack["contract_bundle_paths"]
    missing_contracts = [
        ref
        for ref in contract_paths
        if not _resolve_catalog_ref(
            pack_path,
            ref,
            collection_name="contract-packs",
        ).exists()
    ]
    if missing_contracts:
        raise FileNotFoundError(
            f"contract pack {pack['name']} references missing contracts: {missing_contracts}"
        )

    validated_bundles = []
    for bundle_ref in contract_bundle_paths:
        bundle_path = _resolve_catalog_ref(
            pack_path,
            bundle_ref,
            collection_name="contract-packs",
        )
        if not bundle_path.exists():
            raise FileNotFoundError(f"contract pack bundle not found: {bundle_ref}")
        validated_bundles.append(validate_contract_bundle(bundle_path))

    return {
        "ok": True,
        "path": str(pack_path.resolve()),
        "name": pack["name"],
        "contract_count": len(contract_paths),
        "contract_bundle_count": len(contract_bundle_paths),
        "bundles": validated_bundles,
    }


def _load_contract_pack(
    *,
    base_dir: Path,
    pack_name: str,
    configs_dir: Path | None,
) -> tuple[Path, dict]:
    pack_path = resolve_contract_pack_path(
        base_dir=base_dir,
        pack_name=pack_name,
        configs_dir=configs_dir,
    )
    return pack_path, read_contract_pack(pack_path)


def _contract_pack_recipe(pack: dict, recipe_name: str | None) -> dict:
    selected_recipe = recipe_name or pack["default_recipe"]
    for recipe in pack["recipes"]:
        if recipe["name"] == selected_recipe:
            return recipe
    available = sorted(recipe["name"] for recipe in pack["recipes"])
    raise ValueError(f"contract pack recipe '{selected_recipe}' not found; available: {available}")


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
        f"unable to resolve {repo_name}; pass --repo-dir or set {_env_var_for_repo(repo_name)}"
    )


def _handle_external_contract_pack_install(
    *,
    args: argparse.Namespace,
    base_dir: Path,
    pack: dict,
    recipe: dict,
    install: dict,
) -> int:
    repo_name = install.get("repo")
    command_text = install.get("command")
    if not isinstance(repo_name, str) or not repo_name:
        raise ValueError("external contract pack installer must define repo")
    if not isinstance(command_text, str) or not command_text:
        raise ValueError("external contract pack installer must define command")

    repo_dir = _resolve_external_repo_dir(
        base_dir=base_dir,
        repo_name=repo_name,
        explicit=args.repo_dir,
    )
    command = shlex.split(command_text)
    payload = {
        "contract_pack": pack["name"],
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


def _handle_contract_pack_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    packs = [
        read_contract_pack(path)
        for path in list_contract_pack_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    summaries = [
        {
            "name": pack["name"],
            "display_name": pack["display_name"],
            "category": pack["category"],
            "maturity": pack["maturity"],
            "description": pack["description"],
            "default_recipe": pack["default_recipe"],
            "docs_path": pack["docs_path"],
        }
        for pack in packs
    ]
    print(json.dumps(summaries, indent=2))
    return 0


def _handle_contract_pack_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    _, pack = _load_contract_pack(
        base_dir=base_dir,
        pack_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(pack, indent=2))
    return 0


def _handle_contract_pack_validate(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    pack_path, pack = _load_contract_pack(
        base_dir=base_dir,
        pack_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(_validate_contract_pack_assets(pack_path, pack), indent=2))
    return 0


def _handle_contract_pack_install(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    pack_path, pack = _load_contract_pack(
        base_dir=base_dir,
        pack_name=args.name,
        configs_dir=args.configs_dir,
    )
    _validate_contract_pack_assets(pack_path, pack)
    recipe = _contract_pack_recipe(pack, args.recipe)
    install = recipe["install"]
    if install["kind"] == "external":
        return _handle_external_contract_pack_install(
            args=args,
            base_dir=base_dir,
            pack=pack,
            recipe=recipe,
            install=install,
        )
    if install["kind"] != "xian-stack.localnet-dex-bootstrap":
        raise ValueError(
            f"contract pack {pack['name']} recipe {recipe['name']} uses "
            f"install kind {install['kind']!r}; run the owning repo bootstrap "
            "command"
        )
    if pack["name"] != "dex":
        raise ValueError("only the dex contract pack has a stack installer today")

    if not pack["contract_bundle_paths"]:
        raise ValueError("dex contract pack must define a contract bundle")
    dex_bundle = _resolve_catalog_ref(
        pack_path,
        pack["contract_bundle_paths"][0],
        collection_name="contract-packs",
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
                    "contract_pack": pack["name"],
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
    payload["contract_pack"] = pack["name"]
    payload["recipe"] = recipe["name"]
    print(json.dumps(payload, indent=2))
    return 0


def _load_example(
    *,
    base_dir: Path,
    example_name: str,
    configs_dir: Path | None,
) -> dict:
    example_path = resolve_example_path(
        base_dir=base_dir,
        example_name=example_name,
        configs_dir=configs_dir,
    )
    return read_example(example_path)


def _handle_example_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    examples = [
        read_example(path)
        for path in list_example_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    summaries = [
        {
            "name": example["name"],
            "display_name": example["display_name"],
            "description": example["description"],
            "recommended_local_template": example["recommended_local_template"],
            "docs_path": example["docs_path"],
            "example_dir": example["example_dir"],
            "contract_packs": example["contract_packs"],
            "services": example["services"],
        }
        for example in examples
    ]
    print(json.dumps(summaries, indent=2))
    return 0


def _handle_example_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    example = _load_example(
        base_dir=base_dir,
        example_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(example, indent=2))
    return 0


def _handle_example_starter(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    example = _load_example(
        base_dir=base_dir,
        example_name=args.name,
        configs_dir=args.configs_dir,
    )
    flow = next(
        (item for item in example["starter_flows"] if item["name"] == args.flow),
        None,
    )
    if flow is None:
        available = sorted(item["name"] for item in example["starter_flows"])
        raise ValueError(f"example flow '{args.flow}' not found; available: {available}")

    starter = {
        "name": example["name"],
        "display_name": example["display_name"],
        "description": example["description"],
        "use_case": example["use_case"],
        "docs_path": example["docs_path"],
        "example_dir": example["example_dir"],
        "contract_packs": example["contract_packs"],
        "services": example["services"],
        "contract_bundle_paths": example["contract_bundle_paths"],
        "contract_paths": example["contract_paths"],
        "flow": flow,
    }
    print(json.dumps(starter, indent=2))
    return 0


def _load_product(
    *,
    base_dir: Path,
    product_name: str,
    configs_dir: Path | None,
) -> dict:
    product_path = resolve_product_path(
        base_dir=base_dir,
        product_name=product_name,
        configs_dir=configs_dir,
    )
    return read_product(product_path)


def _handle_product_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    products = [
        read_product(path)
        for path in list_product_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    summaries = [
        {
            "name": product["name"],
            "display_name": product["display_name"],
            "category": product["category"],
            "maturity": product["maturity"],
            "description": product["description"],
            "source_owner_repo": product["source_owner_repo"],
            "docs_path": product["docs_path"],
            "contract_packs": product["contract_packs"],
            "apps": product["apps"],
            "services": product["services"],
            "lifecycle": product["lifecycle"],
        }
        for product in products
    ]
    print(json.dumps(summaries, indent=2))
    return 0


def _handle_product_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    product = _load_product(
        base_dir=base_dir,
        product_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(product, indent=2))
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
