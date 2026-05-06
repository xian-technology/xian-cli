from __future__ import annotations

import argparse
from collections.abc import Mapping
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


def build_profile_runtime_fields(
    *,
    args: argparse.Namespace,
    template: Mapping[str, Any] | None,
    runtime_services: bool,
    intentkit_network_id_default: str,
) -> dict[str, Any]:
    runtime_only = runtime_services

    fields: dict[str, Any] = {
        "pruning_enabled": (
            _template_value(
                args,
                template,
                arg_name="enable_pruning",
                template_key="pruning_enabled",
                default=False,
            )
            if runtime_only
            else False
        ),
        "blocks_to_keep": (
            _template_value(
                args,
                template,
                arg_name="blocks_to_keep",
                template_key="blocks_to_keep",
                default=100000,
            )
            if runtime_only
            else 100000
        ),
        "transaction_trace_logging": _template_value(
            args,
            template,
            arg_name="transaction_trace_logging",
            template_key="transaction_trace_logging",
            default=False,
        ),
        "app_log_level": _template_value(
            args,
            template,
            arg_name="app_log_level",
            template_key="app_log_level",
            default="INFO",
        ),
        "app_log_json": _template_value(
            args,
            template,
            arg_name="app_log_json",
            template_key="app_log_json",
            default=False,
        ),
        "app_log_rotation_hours": validate_positive_int(
            "app_log_rotation_hours",
            _template_value(
                args,
                template,
                arg_name="app_log_rotation_hours",
                template_key="app_log_rotation_hours",
                default=1,
            ),
        ),
        "app_log_retention_days": validate_positive_int(
            "app_log_retention_days",
            _template_value(
                args,
                template,
                arg_name="app_log_retention_days",
                template_key="app_log_retention_days",
                default=7,
            ),
        ),
        "simulation_enabled": _template_value(
            args,
            template,
            arg_name="simulation_enabled",
            template_key="simulation_enabled",
            default=True,
        ),
        "simulation_max_concurrency": validate_positive_int(
            "simulation_max_concurrency",
            _template_value(
                args,
                template,
                arg_name="simulation_max_concurrency",
                template_key="simulation_max_concurrency",
                default=2,
            ),
        ),
        "simulation_timeout_ms": validate_positive_int(
            "simulation_timeout_ms",
            _template_value(
                args,
                template,
                arg_name="simulation_timeout_ms",
                template_key="simulation_timeout_ms",
                default=3000,
            ),
        ),
        "simulation_max_chi": validate_positive_int(
            "simulation_max_chi",
            _template_value(
                args,
                template,
                arg_name="simulation_max_chi",
                template_key="simulation_max_chi",
                default=1_000_000,
            ),
        ),
        "parallel_execution_enabled": _template_value(
            args,
            template,
            arg_name="parallel_execution_enabled",
            template_key="parallel_execution_enabled",
            default=False,
        ),
        "parallel_execution_workers": validate_non_negative_int(
            "parallel_execution_workers",
            _template_value(
                args,
                template,
                arg_name="parallel_execution_workers",
                template_key="parallel_execution_workers",
                default=0,
            ),
        ),
        "parallel_execution_min_transactions": validate_non_negative_int(
            "parallel_execution_min_transactions",
            _template_value(
                args,
                template,
                arg_name="parallel_execution_min_transactions",
                template_key="parallel_execution_min_transactions",
                default=8,
            ),
        ),
    }

    fields.update(
        {
            "service_node": (
                _template_value(
                    args,
                    template,
                    arg_name="service_node",
                    template_key="service_node",
                    default=False,
                )
                if runtime_only
                else False
            ),
            "operator_profile": (
                None if template is None else template.get("operator_profile")
            )
            if runtime_only
            else None,
            "monitoring_profile": (
                None if template is None else template.get("monitoring_profile")
            )
            if runtime_only
            else None,
            "dashboard_enabled": (
                _template_value(
                    args,
                    template,
                    arg_name="enable_dashboard",
                    template_key="dashboard_enabled",
                    default=False,
                )
                if runtime_only
                else False
            ),
            "monitoring_enabled": (
                _template_value(
                    args,
                    template,
                    arg_name="enable_monitoring",
                    template_key="monitoring_enabled",
                    default=False,
                )
                if runtime_only
                else False
            ),
            "dashboard_host": (
                _template_value(
                    args,
                    template,
                    arg_name="dashboard_host",
                    template_key="dashboard_host",
                    default="127.0.0.1",
                )
                if runtime_only
                else "127.0.0.1"
            ),
            "dashboard_port": (
                _template_value(
                    args,
                    template,
                    arg_name="dashboard_port",
                    template_key="dashboard_port",
                    default=8080,
                )
                if runtime_only
                else 8080
            ),
            "intentkit_enabled": (
                _template_value(
                    args,
                    template,
                    arg_name="enable_intentkit",
                    template_key="intentkit_enabled",
                    default=False,
                )
                if runtime_only
                else False
            ),
            "intentkit_network_id": (
                _template_value(
                    args,
                    template,
                    arg_name="intentkit_network_id",
                    template_key="intentkit_network_id",
                    default=intentkit_network_id_default,
                )
                if runtime_only
                else intentkit_network_id_default
            ),
            "intentkit_host": (
                _template_value(
                    args,
                    template,
                    arg_name="intentkit_host",
                    template_key="intentkit_host",
                    default="127.0.0.1",
                )
                if runtime_only
                else "127.0.0.1"
            ),
            "intentkit_port": (
                _template_value(
                    args,
                    template,
                    arg_name="intentkit_port",
                    template_key="intentkit_port",
                    default=38000,
                )
                if runtime_only
                else 38000
            ),
            "intentkit_api_port": (
                _template_value(
                    args,
                    template,
                    arg_name="intentkit_api_port",
                    template_key="intentkit_api_port",
                    default=38080,
                )
                if runtime_only
                else 38080
            ),
            "dex_automation_enabled": (
                _template_value(
                    args,
                    template,
                    arg_name="enable_dex_automation",
                    template_key="dex_automation_enabled",
                    default=False,
                )
                if runtime_only
                else False
            ),
            "dex_automation_host": (
                _template_value(
                    args,
                    template,
                    arg_name="dex_automation_host",
                    template_key="dex_automation_host",
                    default="127.0.0.1",
                )
                if runtime_only
                else "127.0.0.1"
            ),
            "dex_automation_port": (
                _template_value(
                    args,
                    template,
                    arg_name="dex_automation_port",
                    template_key="dex_automation_port",
                    default=38280,
                )
                if runtime_only
                else 38280
            ),
            "dex_automation_config": (
                _template_value(
                    args,
                    template,
                    arg_name="dex_automation_config",
                    template_key="dex_automation_config",
                    default=None,
                )
                if runtime_only
                else None
            ),
            "shielded_relayer_enabled": (
                pick_value(
                    None,
                    None
                    if template is None
                    else template.get("shielded_relayer_enabled"),
                    False,
                )
                if runtime_only
                else False
            ),
            "shielded_relayer_host": (
                pick_value(
                    None,
                    None
                    if template is None
                    else template.get("shielded_relayer_host"),
                    "127.0.0.1",
                )
                if runtime_only
                else "127.0.0.1"
            ),
            "shielded_relayer_port": (
                pick_value(
                    None,
                    None
                    if template is None
                    else template.get("shielded_relayer_port"),
                    38180,
                )
                if runtime_only
                else 38180
            ),
        }
    )
    return fields
