from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from xian_cli.models import DEFAULT_ADVANCED_RUNTIME, DEFAULT_SERVICES


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


def _nested_template_value(
    template: Mapping[str, Any] | None,
    section: str,
    key: str,
    default: Any,
) -> Any:
    if template is None:
        return default
    section_value = template.get(section)
    if not isinstance(section_value, Mapping):
        return default
    item_value = section_value.get(key)
    return default if item_value is None else item_value


def _merge_mapping_defaults(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mapping_defaults(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _service_template_value(
    template: Mapping[str, Any] | None,
    service: str,
    key: str,
    default: Any,
) -> Any:
    services = None if template is None else template.get("services")
    if not isinstance(services, Mapping):
        return default
    service_value = services.get(service)
    if not isinstance(service_value, Mapping):
        return default
    item_value = service_value.get(key)
    return default if item_value is None else item_value


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
            4,
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
        ("operator_profile", None, "operator_profile", None, True, None),
        ("monitoring_profile", None, "monitoring_profile", None, True, None),
    )

    fields = {
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
    services = deepcopy(DEFAULT_SERVICES)
    services["bds"]["enabled"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.bds.enabled",
        arg_name="enable_bds",
        template_key="unused",
        default=_service_template_value(template, "bds", "enabled", False),
        runtime_only=True,
    )
    services["dashboard"]["enabled"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.dashboard.enabled",
        arg_name="enable_dashboard",
        template_key="unused",
        default=_service_template_value(template, "dashboard", "enabled", False),
        runtime_only=True,
    )
    services["dashboard"]["host"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.dashboard.host",
        arg_name="dashboard_host",
        template_key="unused",
        default=_service_template_value(template, "dashboard", "host", "127.0.0.1"),
        runtime_only=True,
    )
    services["dashboard"]["port"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.dashboard.port",
        arg_name="dashboard_port",
        template_key="unused",
        default=_service_template_value(template, "dashboard", "port", 8080),
        runtime_only=True,
    )
    services["monitoring"]["enabled"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.monitoring.enabled",
        arg_name="enable_monitoring",
        template_key="unused",
        default=_service_template_value(template, "monitoring", "enabled", False),
        runtime_only=True,
    )
    services["intentkit"]["enabled"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.intentkit.enabled",
        arg_name="enable_intentkit",
        template_key="unused",
        default=_service_template_value(template, "intentkit", "enabled", False),
        runtime_only=True,
    )
    services["intentkit"]["network_id"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.intentkit.network_id",
        arg_name="intentkit_network_id",
        template_key="unused",
        default=_service_template_value(
            template,
            "intentkit",
            "network_id",
            intentkit_network_id_default,
        ),
        runtime_only=True,
    )
    services["intentkit"]["host"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.intentkit.host",
        arg_name="intentkit_host",
        template_key="unused",
        default=_service_template_value(template, "intentkit", "host", "127.0.0.1"),
        runtime_only=True,
    )
    services["intentkit"]["port"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.intentkit.port",
        arg_name="intentkit_port",
        template_key="unused",
        default=_service_template_value(template, "intentkit", "port", 38000),
        runtime_only=True,
    )
    services["intentkit"]["api_port"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.intentkit.api_port",
        arg_name="intentkit_api_port",
        template_key="unused",
        default=_service_template_value(template, "intentkit", "api_port", 38080),
        runtime_only=True,
    )
    services["dex_automation"]["enabled"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.dex_automation.enabled",
        arg_name="enable_dex_automation",
        template_key="unused",
        default=_service_template_value(template, "dex_automation", "enabled", False),
        runtime_only=True,
    )
    services["dex_automation"]["host"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.dex_automation.host",
        arg_name="dex_automation_host",
        template_key="unused",
        default=_service_template_value(template, "dex_automation", "host", "127.0.0.1"),
        runtime_only=True,
    )
    services["dex_automation"]["port"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.dex_automation.port",
        arg_name="dex_automation_port",
        template_key="unused",
        default=_service_template_value(template, "dex_automation", "port", 38280),
        runtime_only=True,
    )
    services["dex_automation"]["config"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.dex_automation.config",
        arg_name="dex_automation_config",
        template_key="unused",
        default=_service_template_value(template, "dex_automation", "config", None),
        runtime_only=True,
    )
    services["shielded_relayer"]["enabled"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.shielded_relayer.enabled",
        arg_name="enable_shielded_relayer",
        template_key="unused",
        default=_service_template_value(template, "shielded_relayer", "enabled", False),
        runtime_only=True,
    )
    services["shielded_relayer"]["host"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.shielded_relayer.host",
        arg_name="shielded_relayer_host",
        template_key="unused",
        default=_service_template_value(template, "shielded_relayer", "host", "127.0.0.1"),
        runtime_only=True,
    )
    services["shielded_relayer"]["port"] = _profile_field_value(
        args,
        template,
        runtime_services=runtime_services,
        field_name="services.shielded_relayer.port",
        arg_name="shielded_relayer_port",
        template_key="unused",
        default=_service_template_value(template, "shielded_relayer", "port", 38180),
        runtime_only=True,
    )
    fields["services"] = services
    advanced = deepcopy(DEFAULT_ADVANCED_RUNTIME)
    if template is not None and isinstance(template.get("advanced"), Mapping):
        advanced = _merge_mapping_defaults(advanced, template["advanced"])
    fields["advanced"] = advanced
    if fields["parallel_execution_enabled"] and fields["parallel_execution_workers"] <= 0:
        raise ValueError(
            "parallel_execution_workers must be greater than zero when "
            "parallel_execution_enabled is true"
        )
    return fields
