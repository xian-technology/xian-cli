"""Creation and sparse-profile loading share defaults; saved choices stay explicit."""

import subprocess
import sys
import textwrap


def test_profiles_templates_and_plans_follow_the_shared_baseline():
    # A fresh process observes changed defaults before the model classes bind
    # their constructor defaults, just as a new installed release would.
    subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import json
            import sys
            from dataclasses import replace
            from types import SimpleNamespace
            import xian.node_defaults as defaults
            import xian.services.bds.config as bds

            saved = {
                'schema_version': 1, 'name': 'saved', 'network': 'local', 'moniker': 'validator',
                'simulation_timeout_ms': 3000, 'simulation_max_chi': None,
                'app_log_retention_days': 7,
                'advanced': {'metrics': {'port': 9108}},
                'services': {'bds': {'pool_max_size': 10}},
            }
            defaults.RUNTIME_DEFAULTS = replace(
                defaults.RUNTIME_DEFAULTS,
                simulation_timeout_ms=4567, app_log_retention_days=11,
                metrics_port=9208, max_pending_nonces_per_sender=32, simulation_max_chi=1234,
            )
            original_bds = bds.BdsConfig
            bds.BdsConfig = lambda: replace(original_bds(), pool_max_size=23)

            # Reading profiles/help must not need the executor or setup module.
            sys.modules['contracting'] = None
            sys.modules['xian.node_setup'] = None
            from xian_cli.models import (
                NodeProfile, NetworkTemplate, normalize_node_profile,
                normalize_network_template,
            )
            from xian_cli.network_plans import build_profile_runtime_fields
            profile = NodeProfile('new', 'local', 'validator').to_dict()
            template = NetworkTemplate('t', 'Template', 'test').to_dict()
            sparse = normalize_node_profile(
                {'schema_version': 1, 'name': 'sparse', 'network': 'local', 'moniker': 'validator'}
            )
            normalized_template = normalize_network_template(
                {'schema_version': 1, 'name': 't',
                 'display_name': 'Template', 'description': 'test'}
            )
            class Arguments(SimpleNamespace):
                def __getattr__(self, name):
                    return None
            args = Arguments()
            plan = build_profile_runtime_fields(
                args=args, template=None, runtime_services=True,
                intentkit_network_id_default='xian-localnet',
            )
            for value in (profile, template, sparse, normalized_template, plan):
                assert value['simulation_timeout_ms'] == 4567
                assert value['simulation_max_chi'] == 1234
                assert value['app_log_retention_days'] == 11
                assert value['advanced']['metrics']['port'] == 9208
                assert value['advanced']['pending_nonce']['max_per_sender'] == 32
            for value in (profile, sparse, plan):
                assert value['services']['bds']['pool_max_size'] == 23
                assert value['services']['bds']['host'] == ''
                assert value['services']['bds']['user'] == ''
                assert value['services']['bds']['rpc_url'] is None
                assert 'password_file' not in value['services']['bds']

            args.simulation_timeout_ms = 7777
            overridden = build_profile_runtime_fields(
                args=args,
                template={'simulation_timeout_ms': 6789, 'app_log_retention_days': 13},
                runtime_services=True, intentkit_network_id_default='xian-localnet',
            )
            assert overridden['simulation_timeout_ms'] == 7777
            assert overridden['app_log_retention_days'] == 13

            restored = normalize_node_profile(json.loads(json.dumps(saved)))
            assert restored['simulation_timeout_ms'] == 3000
            assert restored['simulation_max_chi'] is None
            assert restored['app_log_retention_days'] == 7
            assert restored['advanced']['metrics']['port'] == 9108
            assert restored['services']['bds']['pool_max_size'] == 10
            assert normalize_node_profile(json.loads(json.dumps(profile))) == sparse | {
                'name': 'new',
            }
        """),
        ],
        check=True,
    )
