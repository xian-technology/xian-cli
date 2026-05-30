from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from xian_cli.commands.catalog import _load_template
from xian_cli.commands.common import (
    _default_intentkit_network_id,
    _default_network_manifest_path,
    _effective_node_release_manifest,
    _pick_template_value,
    _resolve_node_image_settings,
    _resolve_output_path,
    _stringify_path_for_profile,
    _write_validator_material_files,
)
from xian_cli.commands.node import _initialize_node_from_args
from xian_cli.commands.node_context import (
    _build_creation_genesis,
    _extract_validator_private_key_hex,
    _resolve_effective_genesis_payload,
    _resolve_path,
)
from xian_cli.config_repo import resolve_network_manifest_path
from xian_cli.models import (
    NetworkManifest,
    NodeProfile,
    read_json,
    read_network_manifest,
    write_json,
)
from xian_cli.network_plans import build_profile_runtime_fields


def _collect_creation_validator_names(
    args: argparse.Namespace,
    *,
    template: dict | None = None,
) -> tuple[str | None, list[str]]:
    bootstrap_name = args.bootstrap_node
    if bootstrap_name is None and template is not None:
        bootstrap_name = template.get("bootstrap_node_name")
    validator_names: list[str] = []

    if bootstrap_name is not None:
        validator_names.append(bootstrap_name)

    validator_inputs = (
        args.validator
        if args.validator is not None
        else (template.get("additional_validator_names") if template else [])
    )
    for validator_name in validator_inputs or []:
        if validator_name in validator_names:
            raise ValueError(f"duplicate validator name in network creation: {validator_name}")
        validator_names.append(validator_name)

    return bootstrap_name, validator_names


def _collect_creation_validators(
    *,
    args: argparse.Namespace,
    base_dir: Path,
    bootstrap_name: str | None,
    validator_names: list[str],
) -> list[dict[str, object]]:
    validators: list[dict[str, object]] = []
    if not validator_names:
        if args.validator_key_ref is not None:
            raise ValueError("--validator-key-ref requires --bootstrap-node")
        return validators

    if not args.generate_validator_key:
        if len(validator_names) > 1:
            raise ValueError(
                "multi-validator network creation currently requires --generate-validator-key"
            )
        if args.validator_key_ref is None:
            return validators

    for index, validator_name in enumerate(validator_names):
        validator_key_ref: str | None = None
        validator_key_payload: dict | None = None

        if args.generate_validator_key:
            key_dir = args.validator_key_dir or base_dir / "keys" / validator_name
            if args.validator_key_dir is not None and len(validator_names) > 1:
                key_dir = key_dir / validator_name
            if not key_dir.is_absolute():
                key_dir = (base_dir / key_dir).resolve()
            metadata_path = _write_validator_material_files(
                out_dir=key_dir,
                force=args.force,
            )
            validator_key_ref = _stringify_path_for_profile(
                metadata_path,
                base_dir=base_dir,
            )
            validator_key_payload = read_json(metadata_path)
        elif index == 0 and args.validator_key_ref is not None:
            validator_key_path = _resolve_output_path(
                base_dir=base_dir,
                explicit_output=args.validator_key_ref,
                default_path=args.validator_key_ref,
            )
            validator_key_ref = _stringify_path_for_profile(
                validator_key_path,
                base_dir=base_dir,
            )
            validator_key_payload = read_json(validator_key_path)

        validators.append(
            {
                "name": validator_name,
                "moniker": (
                    args.moniker
                    if validator_name == bootstrap_name and args.moniker is not None
                    else validator_name
                ),
                "validator_key_ref": validator_key_ref,
                "validator_key_payload": validator_key_payload,
                "power": args.validator_power,
                "is_bootstrap": validator_name == bootstrap_name,
            }
        )

    return validators


def _handle_network_create(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    template = _load_template(
        base_dir=base_dir,
        template_name=args.template,
        configs_dir=args.configs_dir,
    )
    target = _resolve_output_path(
        base_dir=base_dir,
        explicit_output=args.output,
        default_path=_default_network_manifest_path(base_dir, args.name),
    )
    network_dir = target.parent

    if args.generate_validator_key and args.validator_key_ref is not None:
        raise ValueError("pass either --validator-key-ref or --generate-validator-key, not both")
    if args.validator_key_dir is not None and not args.generate_validator_key:
        raise ValueError("--validator-key-dir requires --generate-validator-key")
    if args.init_node and args.bootstrap_node is None:
        if template is None or template.get("bootstrap_node_name") is None:
            raise ValueError("--init-node requires --bootstrap-node")
    if getattr(args, "dry_run", False):
        # All argument validation has passed; summarize the planned layout
        # without writing files or generating keys. If the user then drops
        # the --dry-run flag, the same inputs will run the real flow.
        dry_plan: dict[str, object] = {
            "dry_run": True,
            "name": args.name,
            "template": template["name"] if template else None,
            "manifest_path": str(target),
            "network_dir": str(network_dir),
            "bootstrap_node": args.bootstrap_node,
            "generate_validator_key": bool(args.generate_validator_key),
            "init_node": bool(args.init_node),
            "genesis": (
                {"kind": "source", "source": args.genesis_source}
                if args.genesis_source
                else {"kind": "bundle", "bundle": args.genesis_bundle}
            ),
        }
        print(json.dumps(dry_plan, indent=2))
        return 0
    bootstrap_name, validator_names = _collect_creation_validator_names(
        args,
        template=template,
    )
    if args.init_node and bootstrap_name is None:
        raise ValueError("--init-node requires --bootstrap-node")
    validators = _collect_creation_validators(
        args=args,
        base_dir=base_dir,
        bootstrap_name=bootstrap_name,
        validator_names=validator_names,
    )

    genesis = (
        {"kind": "source", "source": args.genesis_source}
        if args.genesis_source is not None
        else None
    )
    genesis_build: dict[str, object] | None = None
    generated_genesis_path: Path | None = None
    if genesis is None:
        if not validators or any(
            validator["validator_key_payload"] is None for validator in validators
        ):
            if bootstrap_name is not None or validator_names:
                raise ValueError(
                    "local network creation without --genesis-source "
                    "requires validator key material; pass "
                    "--generate-validator-key or --validator-key-ref"
                )
        else:
            founder_private_key = args.founder_private_key or _extract_validator_private_key_hex(
                validators[0]["validator_key_payload"]
            )
            generated_genesis_path = network_dir / "genesis.json"
            genesis = _build_creation_genesis(
                chain_id=args.chain_id,
                founder_private_key=founder_private_key,
                validators=validators,
                genesis_bundle=args.genesis_bundle,
            )
            write_json(
                generated_genesis_path,
                genesis,
                force=args.force,
                preserve_runtime_types=True,
            )
            genesis = {"kind": "source", "source": "./genesis.json"}
            genesis_build = {
                "bundle": args.genesis_bundle,
                "generated_by": "xian network create",
            }
    if genesis is None:
        genesis = {"kind": "bundle", "bundle": args.genesis_bundle}
    if bootstrap_name is not None and genesis is None and not validators:
        raise ValueError(
            "--bootstrap-node requires either --genesis-source or local "
            "genesis generation via validator key material"
        )
    if validators and any(validator["validator_key_ref"] is None for validator in validators):
        raise ValueError(
            "initial validator profiles require validator key material; "
            "pass --generate-validator-key"
        )

    node_image_mode, node_integrated_image, node_split_image = _resolve_node_image_settings(
        node_image_mode=_pick_template_value(
            args.node_image_mode,
            None,
            "local_build",
        ),
        node_integrated_image=args.node_integrated_image,
        node_split_image=args.node_split_image,
    )

    manifest = NetworkManifest(
        name=args.name,
        chain_id=args.chain_id,
        genesis=genesis,
        genesis_build=genesis_build,
        snapshot_url=args.snapshot_url,
        snapshot_signing_keys=args.snapshot_signing_key or [],
        p2p={"seeds": args.seed or [], "persistent_peers": []},
        block_policy_mode=_pick_template_value(
            args.block_policy_mode,
            None if template is None else template.get("block_policy_mode"),
            "on_demand",
        ),
        block_policy_interval=_pick_template_value(
            args.block_policy_interval,
            None if template is None else template.get("block_policy_interval"),
            "0s",
        ),
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        node_release_manifest=None,
    )
    write_json(target, manifest.to_dict(), force=args.force)

    result: dict[str, object] = {
        "manifest_path": str(target),
        "genesis": genesis,
        "template": None if template is None else template["name"],
    }
    if generated_genesis_path is not None:
        result["generated_genesis_path"] = str(generated_genesis_path)
    result["validators"] = []

    for validator in validators:
        validator_result: dict[str, object] = {
            "name": validator["name"],
            "moniker": validator["moniker"],
            "validator_key_ref": validator["validator_key_ref"],
        }
        profile_path = _resolve_output_path(
            base_dir=base_dir,
            explicit_output=(args.node_output if validator["is_bootstrap"] else None),
            default_path=base_dir / "nodes" / f"{validator['name']}.json",
        )
        profile = NodeProfile(
            name=validator["name"],
            network=args.name,
            moniker=validator["moniker"],
            validator_key_ref=validator["validator_key_ref"],
            node_image_mode=manifest.node_image_mode,
            node_integrated_image=manifest.node_integrated_image,
            node_split_image=manifest.node_split_image,
            node_release_manifest=manifest.node_release_manifest,
            stack_dir=(
                str(args.stack_dir)
                if validator["is_bootstrap"] and args.stack_dir is not None
                else None
            ),
            p2p={"seeds": [], "persistent_peers": []},
            genesis=None,
            snapshot_url=(args.snapshot_url if validator["is_bootstrap"] else None),
            snapshot_signing_keys=(
                list(args.snapshot_signing_key or []) if validator["is_bootstrap"] else []
            ),
            home=(str(args.home) if validator["is_bootstrap"] and args.home is not None else None),
            block_policy_mode=manifest.block_policy_mode,
            block_policy_interval=manifest.block_policy_interval,
            **build_profile_runtime_fields(
                args=args,
                template=template,
                runtime_services=bool(validator["is_bootstrap"]),
                intentkit_network_id_default="xian-localnet",
            ),
        )
        write_json(profile_path, profile.to_dict(), force=args.force)
        validator_result["profile_path"] = str(profile_path)
        result["validators"].append(validator_result)

        if validator["is_bootstrap"]:
            result["profile_path"] = str(profile_path)
            if validator["validator_key_ref"] is not None:
                result["validator_key_ref"] = validator["validator_key_ref"]

    if bootstrap_name is not None:
        bootstrap_validator = next(
            (validator for validator in validators if validator["name"] == bootstrap_name),
            None,
        )
        if bootstrap_validator is None or bootstrap_validator["validator_key_ref"] is None:
            raise ValueError(
                "--bootstrap-node requires validator key material; "
                "pass --generate-validator-key or --validator-key-ref"
            )

        if args.init_node:
            bootstrap_profile_path = Path(result["profile_path"])
            init_args = argparse.Namespace(
                name=bootstrap_name,
                base_dir=base_dir,
                profile=bootstrap_profile_path,
                network=target,
                validator_key=None,
                stack_dir=args.stack_dir,
                configs_dir=args.configs_dir,
                home=args.home,
                force=args.force,
                restore_snapshot=False,
                snapshot_url=None,
            )
            result["node_initialized"] = True
            result["node_init"] = _initialize_node_from_args(init_args)

    print(json.dumps(result, indent=2))
    return 0


def _handle_network_join(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    template = _load_template(
        base_dir=base_dir,
        template_name=args.template,
        configs_dir=args.configs_dir,
    )
    network_path = resolve_network_manifest_path(
        base_dir=base_dir,
        network_name=args.network,
        explicit_manifest=args.network_manifest,
        configs_dir=args.configs_dir,
    )
    network = read_network_manifest(network_path)
    if args.generate_validator_key and args.validator_key_ref is not None:
        raise ValueError("pass either --validator-key-ref or --generate-validator-key, not both")
    if args.validator_key_dir is not None and not args.generate_validator_key:
        raise ValueError("--validator-key-dir requires --generate-validator-key")
    if args.restore_snapshot and not args.init_node:
        raise ValueError("--restore-snapshot requires --init-node")

    if getattr(args, "dry_run", False):
        target_profile = _resolve_output_path(
            base_dir=base_dir,
            explicit_output=args.output,
            default_path=base_dir / "nodes" / f"{args.name}.json",
        )
        dry_plan: dict[str, object] = {
            "dry_run": True,
            "name": args.name,
            "network": args.network,
            "network_manifest": str(network_path),
            "node_profile_path": str(target_profile),
            "generate_validator_key": bool(args.generate_validator_key),
            "init_node": bool(args.init_node),
            "restore_snapshot": bool(args.restore_snapshot),
        }
        print(json.dumps(dry_plan, indent=2))
        return 0

    requested_node_image_mode = _pick_template_value(
        args.node_image_mode,
        None,
        network.get("node_image_mode") or "local_build",
    )
    node_image_mode, node_integrated_image, node_split_image = _resolve_node_image_settings(
        node_image_mode=requested_node_image_mode,
        node_integrated_image=_pick_template_value(
            args.node_integrated_image,
            None,
            network.get("node_integrated_image")
            if requested_node_image_mode == "registry"
            else None,
        ),
        node_split_image=_pick_template_value(
            args.node_split_image,
            None,
            network.get("node_split_image") if requested_node_image_mode == "registry" else None,
        ),
    )

    validator_key_ref: str | None = None
    if args.validator_key_ref is not None:
        validator_key_ref = _stringify_path_for_profile(
            args.validator_key_ref,
            base_dir=base_dir,
        )
    elif args.generate_validator_key:
        key_dir = args.validator_key_dir or base_dir / "keys" / args.name
        if not key_dir.is_absolute():
            key_dir = (base_dir / key_dir).resolve()
        metadata_path = _write_validator_material_files(
            out_dir=key_dir,
            force=args.force,
        )
        validator_key_ref = _stringify_path_for_profile(
            metadata_path,
            base_dir=base_dir,
        )

    profile = NodeProfile(
        name=args.name,
        network=args.network,
        moniker=args.moniker or args.name,
        validator_key_ref=validator_key_ref,
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        node_release_manifest=_effective_node_release_manifest(
            {
                "node_image_mode": node_image_mode,
                "node_integrated_image": node_integrated_image,
                "node_split_image": node_split_image,
            },
            network,
        ),
        stack_dir=str(args.stack_dir) if args.stack_dir is not None else None,
        p2p={"seeds": args.seed or [], "persistent_peers": []},
        genesis=(
            {"kind": "source", "source": args.genesis_source} if args.genesis_source else None
        ),
        snapshot_url=args.snapshot_url,
        snapshot_signing_keys=args.snapshot_signing_key or [],
        home=str(args.home) if args.home is not None else None,
        block_policy_mode=_pick_template_value(
            args.block_policy_mode,
            None if template is None else template.get("block_policy_mode"),
            network.get("block_policy_mode", "on_demand"),
        ),
        block_policy_interval=_pick_template_value(
            args.block_policy_interval,
            None if template is None else template.get("block_policy_interval"),
            network.get("block_policy_interval", "0s"),
        ),
        **build_profile_runtime_fields(
            args=args,
            template=template,
            runtime_services=True,
            intentkit_network_id_default=_default_intentkit_network_id(network.get("name")),
        ),
    )
    target = args.output or base_dir / "nodes" / f"{args.name}.json"
    if not target.is_absolute():
        target = (base_dir / target).resolve()
    write_json(target, profile.to_dict(), force=args.force)
    if not args.init_node:
        print(f"wrote node profile to {target} using {network_path}")
        return 0

    init_args = argparse.Namespace(
        name=args.name,
        base_dir=base_dir,
        profile=target,
        network=network_path,
        validator_key=None,
        stack_dir=args.stack_dir,
        configs_dir=args.configs_dir,
        home=args.home,
        force=args.force,
        restore_snapshot=args.restore_snapshot,
        snapshot_url=args.snapshot_url,
    )
    init_result = _initialize_node_from_args(init_args)
    print(
        json.dumps(
            {
                "profile_path": str(target),
                "network_path": str(network_path),
                "template": None if template is None else template["name"],
                "validator_key_ref": validator_key_ref,
                "node_initialized": True,
                "node_init": init_result,
            },
            indent=2,
        )
    )
    return 0


def _write_text_file(
    path: Path,
    content: str,
    *,
    force: bool = False,
    executable: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o755)


def _safe_bundle_relative_path(ref: str) -> Path:
    path = Path(ref)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"bundle reference must stay relative: {ref}")
    return path


def _copy_optional_network_asset(
    *,
    manifest_path: Path,
    bundle_dir: Path,
    ref: str,
    force: bool,
) -> None:
    source_path = _resolve_path(
        ref,
        base_dir=manifest_path.parent,
        fallback_dir=manifest_path.parent,
    )
    if source_path is None or not source_path.exists():
        raise FileNotFoundError(f"network asset not found: {ref}")
    target_path = bundle_dir / _safe_bundle_relative_path(ref)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and not force:
        raise FileExistsError(f"{target_path} already exists; pass --force to overwrite")
    shutil.copyfile(source_path, target_path)


def _operator_join_script(*, network_name: str, bds_enabled: bool) -> str:
    if bds_enabled:
        service_args = """
  --enable-bds
  --enable-monitoring
  --enable-dashboard
  --dashboard-host "${DASHBOARD_HOST:-127.0.0.1}"
  --dashboard-port "${DASHBOARD_PORT:-8080}"
"""
    else:
        service_args = """
  --no-enable-bds
  --no-enable-monitoring
  --no-enable-dashboard
"""
    return f"""#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
XIAN_CMD="${{XIAN_CMD:-xian}}"
NODE_NAME="${{NODE_NAME:?set NODE_NAME}}"
if [[ -z "${{BOOTSTRAP_SEED:-}}" ]]; then
  BOOTSTRAP_SEED="$(tr -d '\\n' < "${{BUNDLE_DIR}}/bootstrap-seed.txt")"
fi
PARALLEL_EXECUTION_WORKERS="${{PARALLEL_EXECUTION_WORKERS:-8}}"
PARALLEL_MIN_TXS="${{PARALLEL_EXECUTION_MIN_TRANSACTIONS:-8}}"

if [[ "${{BOOTSTRAP_SEED}}" == REPLACE_WITH_* ]]; then
  echo "set BOOTSTRAP_SEED or update bootstrap-seed.txt before joining" >&2
  exit 2
fi

args=(
  network join "${{NODE_NAME}}"
  --base-dir "${{BUNDLE_DIR}}"
  --network "{network_name}"
  --network-manifest "${{BUNDLE_DIR}}/manifest.json"
  --generate-validator-key
  --seed "${{BOOTSTRAP_SEED}}"
  --simulation-enabled
  --parallel-execution-enabled
  --parallel-execution-workers "${{PARALLEL_EXECUTION_WORKERS}}"
  --parallel-execution-min-transactions "${{PARALLEL_MIN_TXS}}"
{service_args}  --init-node
)

if [[ -n "${{STACK_DIR:-}}" ]]; then
  args+=(--stack-dir "${{STACK_DIR}}")
fi
if [[ -n "${{CONFIGS_DIR:-}}" ]]; then
  args+=(--configs-dir "${{CONFIGS_DIR}}")
fi
if [[ -n "${{NODE_IMAGE_MODE:-}}" ]]; then
  args+=(--node-image-mode "${{NODE_IMAGE_MODE}}")
fi

"${{XIAN_CMD}}" "${{args[@]}}"
"${{XIAN_CMD}}" node start "${{NODE_NAME}}" --base-dir "${{BUNDLE_DIR}}"
"${{XIAN_CMD}}" node health "${{NODE_NAME}}" --base-dir "${{BUNDLE_DIR}}"
"""


def _operator_bundle_readme(
    *,
    network_name: str,
    chain_id: str,
    includes_privacy_catalog: bool,
) -> str:
    privacy_note = (
        "- `privacy/` - copied privacy artifact catalog referenced by the manifest\n"
        if includes_privacy_catalog
        else ""
    )
    return f"""# {network_name} Operator Bundle

This bundle is a shareable handoff for operators joining `{chain_id}`.

## Included Files

- `manifest.json` - modern network manifest
- `genesis.json` - materialized genesis for this network
- `bootstrap-seed.txt` - bootstrap seed placeholder or live seed
- `participant-join.sh` - join as a lean validator-capable node
- `participant-bds-node.sh` - join with BDS, dashboard, and monitoring
- `SMOKE-CHECKLIST.md` - quick post-start checks
{privacy_note}
No private validator keys, node homes, databases, or logs are included.

## Lean Validator

```bash
NODE_NAME=<node-name> ./participant-join.sh
```

If `bootstrap-seed.txt` still contains a placeholder:

```bash
BOOTSTRAP_SEED='<node_id>@<public-host>:26656' \\
  NODE_NAME=<node-name> \\
  ./participant-join.sh
```

## BDS Node

```bash
NODE_NAME=<node-name> ./participant-bds-node.sh
```

Optional environment variables:

- `XIAN_CMD` - xian CLI executable, defaults to `xian`
- `STACK_DIR` - explicit `xian-stack` checkout
- `CONFIGS_DIR` - explicit `xian-configs` checkout
- `NODE_IMAGE_MODE` - override manifest image mode, for example `local_build`
- `DASHBOARD_HOST` / `DASHBOARD_PORT` - dashboard bind settings
- `PARALLEL_EXECUTION_WORKERS` - defaults to `8`
- `PARALLEL_EXECUTION_MIN_TRANSACTIONS` - defaults to `8`

Keep RPC, GraphQL, Prometheus, and Grafana private unless you intentionally
operate them as public services.
"""


def _operator_bundle_smoke_checklist(*, chain_id: str) -> str:
    return f"""# Smoke Checklist

Run after the node starts.

```bash
xian node health <node-name> --base-dir .
xian node endpoints <node-name> --base-dir .
curl -fsS http://127.0.0.1:26657/status
```

Confirm:

- the status response reports network `{chain_id}`
- `catching_up` becomes `false` after initial sync
- BDS nodes expose dashboard status at `http://127.0.0.1:8080/api/status`
- BDS nodes expose GraphQL at `http://127.0.0.1:5000/graphql`

For validator onboarding, send the generated validator public key to the
network coordinator after startup:

```bash
python3 - <<'PY'
import json
from pathlib import Path

key_path = Path("keys/<node-name>/validator_key_info.json")
payload = json.loads(key_path.read_text())
print(payload["validator_public_key_hex"])
PY
```
"""


def _handle_network_package_operator_bundle(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    manifest_path = resolve_network_manifest_path(
        base_dir=base_dir,
        network_name=args.network,
        explicit_manifest=args.network_manifest,
        configs_dir=args.configs_dir,
    )
    network = read_network_manifest(manifest_path)
    output_dir = args.output or (base_dir / "dist" / f"{network['name']}-operator-bundle")
    if not output_dir.is_absolute():
        output_dir = (base_dir / output_dir).resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise FileExistsError(f"bundle output is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    genesis, genesis_ref = _resolve_effective_genesis_payload(
        profile={},
        network=network,
        base_dir=base_dir,
        manifest_path=manifest_path,
        configs_dir=args.configs_dir,
    )

    bundled_manifest = dict(network)
    bundled_manifest["genesis"] = {"kind": "source", "source": "./genesis.json"}
    if args.bootstrap_seed:
        p2p = dict(bundled_manifest.get("p2p") or {})
        p2p_seeds = list(p2p.get("seeds") or [])
        if args.bootstrap_seed not in p2p_seeds:
            p2p_seeds.append(args.bootstrap_seed)
        p2p["seeds"] = p2p_seeds
        p2p.setdefault("persistent_peers", [])
        bundled_manifest["p2p"] = p2p

    write_json(output_dir / "manifest.json", bundled_manifest, force=args.force)
    write_json(
        output_dir / "genesis.json",
        genesis,
        force=args.force,
        preserve_runtime_types=True,
    )

    privacy_catalog = bundled_manifest.get("privacy_artifact_catalog")
    includes_privacy_catalog = False
    if isinstance(privacy_catalog, dict) and privacy_catalog.get("path"):
        _copy_optional_network_asset(
            manifest_path=manifest_path,
            bundle_dir=output_dir,
            ref=str(privacy_catalog["path"]),
            force=args.force,
        )
        includes_privacy_catalog = True

    bootstrap_seed = args.bootstrap_seed
    if bootstrap_seed is None:
        p2p = bundled_manifest.get("p2p") or {}
        p2p_seeds = p2p.get("seeds") if isinstance(p2p, dict) else []
        bootstrap_seed = p2p_seeds[0] if p2p_seeds else "REPLACE_WITH_<node_id>@<public-host>:26656"
    _write_text_file(
        output_dir / "bootstrap-seed.txt",
        f"{bootstrap_seed}\n",
        force=args.force,
    )
    _write_text_file(
        output_dir / "README.md",
        _operator_bundle_readme(
            network_name=network["name"],
            chain_id=network["chain_id"],
            includes_privacy_catalog=includes_privacy_catalog,
        ),
        force=args.force,
    )
    _write_text_file(
        output_dir / "SMOKE-CHECKLIST.md",
        _operator_bundle_smoke_checklist(chain_id=network["chain_id"]),
        force=args.force,
    )
    _write_text_file(
        output_dir / "participant-join.sh",
        _operator_join_script(network_name=network["name"], bds_enabled=False),
        force=args.force,
        executable=True,
    )
    _write_text_file(
        output_dir / "participant-bds-node.sh",
        _operator_join_script(network_name=network["name"], bds_enabled=True),
        force=args.force,
        executable=True,
    )
    bundle_metadata = {
        "schema": "xian.operator_bundle.v1",
        "schema_version": 1,
        "network": network["name"],
        "chain_id": network["chain_id"],
        "source_manifest": str(manifest_path),
        "source_genesis": genesis_ref,
        "bootstrap_seed": bootstrap_seed,
        "files": [
            "manifest.json",
            "genesis.json",
            "bootstrap-seed.txt",
            "README.md",
            "SMOKE-CHECKLIST.md",
            "participant-join.sh",
            "participant-bds-node.sh",
        ],
    }
    if includes_privacy_catalog:
        bundle_metadata["files"].append(str(privacy_catalog["path"]))
    write_json(
        output_dir / "operator-bundle.json",
        bundle_metadata,
        force=args.force,
    )

    archive_path = None
    if args.archive:
        candidate = output_dir.with_suffix(output_dir.suffix + ".tar.gz")
        if candidate.exists() and not args.force:
            raise FileExistsError(f"{candidate} already exists; pass --force to overwrite")
        archive_path = shutil.make_archive(
            str(output_dir),
            "gztar",
            root_dir=output_dir.parent,
            base_dir=output_dir.name,
        )

    result = {
        "bundle_dir": str(output_dir),
        "manifest_path": str(output_dir / "manifest.json"),
        "genesis_path": str(output_dir / "genesis.json"),
        "network": network["name"],
        "chain_id": network["chain_id"],
        "archive_path": archive_path,
    }
    print(json.dumps(result, indent=2))
    return 0
