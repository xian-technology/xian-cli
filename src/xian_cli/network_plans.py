from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any


def pick_value(
    explicit: Any,
    inherited: Any,
    default: Any,
) -> Any:
    if explicit is not None:
        return explicit
    if inherited is not None:
        return inherited
    return default


def validate_positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    normalized = int(value)
    if normalized <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return normalized


def validate_non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    normalized = int(value)
    if normalized < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return normalized


def _template_value(
    args: argparse.Namespace,
    template: Mapping[str, Any] | None,
    *,
    arg_name: str,
    template_key: str,
    default: Any,
) -> Any:
    return pick_value(
        getattr(args, arg_name),
        None if template is None else template.get(template_key),
        default,
    )


def _profile_field_value(
    args: argparse.Namespace,
    template: Mapping[str, Any] | None,
    *,
    runtime_services: bool,
    field_name: str,
    arg_name: str | None,
    template_key: str,
    default: Any,
    runtime_only: bool = False,
    validator: Callable[[str, Any], Any] | None = None,
) -> Any:
    if runtime_only and not runtime_services:
        value = default
    elif arg_name is None:
        value = pick_value(
            None,
            None if template is None else template.get(template_key),
            default,
        )
    else:
        value = _template_value(
            args,
            template,
            arg_name=arg_name,
            template_key=template_key,
            default=default,
        )
    if validator is None:
        return value
    return validator(field_name, value)


def build_profile_runtime_fields(
    *,
    args: argparse.Namespace,
    template: Mapping[str, Any] | None,
    runtime_services: bool,
    intentkit_network_id_default: str,
) -> dict[str, Any]:
    specs = (
        (
            "pruning_enabled",
            "enable_pruning",
            "pruning_enabled",
            False,
            True,
            None,
        ),
        (
            "blocks_to_keep",
            "blocks_to_keep",
            "blocks_to_keep",
            100000,
            True,
            None,
        ),
        (
            "transaction_trace_logging",
            "transaction_trace_logging",
            "transaction_trace_logging",
            False,
            False,
            None,
        ),
        (
            "app_log_level",
            "app_log_level",
            "app_log_level",
            "INFO",
            False,
            None,
        ),
        ("app_log_json", "app_log_json", "app_log_json", False, False, None),
        (
            "app_log_rotation_hours",
            "app_log_rotation_hours",
            "app_log_rotation_hours",
            1,
            False,
            validate_positive_int,
        ),
        (
            "app_log_retention_days",
            "app_log_retention_days",
            "app_log_retention_days",
            7,
            False,
            validate_positive_int,
        ),
        (
            "simulation_enabled",
            "simulation_enabled",
            "simulation_enabled",
            True,
            False,
            None,
        ),
        (
            "simulation_max_concurrency",
            "simulation_max_concurrency",
            "simulation_max_concurrency",
            2,
            False,
            validate_positive_int,
        ),
        (
            "simulation_timeout_ms",
            "simulation_timeout_ms",
            "simulation_timeout_ms",
            3000,
            False,
            validate_positive_int,
        ),
        (
            "simulation_max_chi",
            "simulation_max_chi",
            "simulation_max_chi",
            1_000_000,
            False,
            validate_positive_int,
        ),
        (
            "parallel_execution_enabled",
            "parallel_execution_enabled",
            "parallel_execution_enabled",
            False,
            False,
            None,
        ),
        (
            "parallel_execution_workers",
            "parallel_execution_workers",
            "parallel_execution_workers",
            0,
            False,
            validate_non_negative_int,
        ),
        (
            "parallel_execution_min_transactions",
            "parallel_execution_min_transactions",
            "parallel_execution_min_transactions",
            8,
            False,
            validate_non_negative_int,
        ),
        ("service_node", "service_node", "service_node", False, True, None),
        ("operator_profile", None, "operator_profile", None, True, None),
        ("monitoring_profile", None, "monitoring_profile", None, True, None),
        (
            "dashboard_enabled",
            "enable_dashboard",
            "dashboard_enabled",
            False,
            True,
            None,
        ),
        (
            "monitoring_enabled",
            "enable_monitoring",
            "monitoring_enabled",
            False,
            True,
            None,
        ),
        (
            "dashboard_host",
            "dashboard_host",
            "dashboard_host",
            "127.0.0.1",
            True,
            None,
        ),
        (
            "dashboard_port",
            "dashboard_port",
            "dashboard_port",
            8080,
            True,
            None,
        ),
        (
            "intentkit_enabled",
            "enable_intentkit",
            "intentkit_enabled",
            False,
            True,
            None,
        ),
        (
            "intentkit_network_id",
            "intentkit_network_id",
            "intentkit_network_id",
            intentkit_network_id_default,
            True,
            None,
        ),
        (
            "intentkit_host",
            "intentkit_host",
            "intentkit_host",
            "127.0.0.1",
            True,
            None,
        ),
        (
            "intentkit_port",
            "intentkit_port",
            "intentkit_port",
            38000,
            True,
            None,
        ),
        (
            "intentkit_api_port",
            "intentkit_api_port",
            "intentkit_api_port",
            38080,
            True,
            None,
        ),
        (
            "dex_automation_enabled",
            "enable_dex_automation",
            "dex_automation_enabled",
            False,
            True,
            None,
        ),
        (
            "dex_automation_host",
            "dex_automation_host",
            "dex_automation_host",
            "127.0.0.1",
            True,
            None,
        ),
        (
            "dex_automation_port",
            "dex_automation_port",
            "dex_automation_port",
            38280,
            True,
            None,
        ),
        (
            "dex_automation_config",
            "dex_automation_config",
            "dex_automation_config",
            None,
            True,
            None,
        ),
        (
            "shielded_relayer_enabled",
            None,
            "shielded_relayer_enabled",
            False,
            True,
            None,
        ),
        (
            "shielded_relayer_host",
            None,
            "shielded_relayer_host",
            "127.0.0.1",
            True,
            None,
        ),
        (
            "shielded_relayer_port",
            None,
            "shielded_relayer_port",
            38180,
            True,
            None,
        ),
    )

    return {
        field_name: _profile_field_value(
            args,
            template,
            runtime_services=runtime_services,
            field_name=field_name,
            arg_name=arg_name,
            template_key=template_key,
            default=default,
            runtime_only=runtime_only,
            validator=validator,
        )
        for (
            field_name,
            arg_name,
            template_key,
            default,
            runtime_only,
            validator,
        ) in specs
    }
