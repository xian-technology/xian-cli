"""Generated operator scripts forward manifest data as literal arguments."""

import json
import os
import subprocess
import sys

import pytest

from xian_cli.cli import main


@pytest.mark.parametrize("bds_enabled", [False, True])
@pytest.mark.parametrize(
    "network_name",
    [
        "local-dev",
        "network with spaces",
        "literal-$(printf expanded)-`printf ticks`-$HOME",
        'network\'s "quoted" name',
    ],
)
def test_packaged_join_script_preserves_literal_network_name(
    tmp_path, capsys, bds_enabled, network_name
):
    genesis_path = tmp_path / "genesis.json"
    genesis_path.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": network_name,
                "chain_id": "xian-local-1",
                "genesis": {"kind": "source", "source": str(genesis_path)},
            }
        ),
        encoding="utf-8",
    )
    bundle = tmp_path / "operator bundle"
    assert (
        main(
            [
                "network",
                "package-operator-bundle",
                "catalog-entry",
                "--base-dir",
                str(tmp_path),
                "--network-manifest",
                str(manifest),
                "--output",
                str(bundle),
                "--bootstrap-seed",
                "abc@localhost:26656",
            ]
        )
        == 0
    )
    capsys.readouterr()
    recorder = tmp_path / "record command"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['ARG_LOG'], 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    recorder.chmod(0o700)
    arguments = tmp_path / "arguments.jsonl"
    script = bundle / ("participant-bds-node.sh" if bds_enabled else "participant-join.sh")
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "XIAN_CMD": str(recorder),
            "NODE_NAME": "participant",
            "ARG_LOG": str(arguments),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in arguments.read_text().splitlines()]
    assert len(calls) == 3
    join = calls[0]
    assert join[join.index("--network") + 1] == network_name
    assert ("--enable-bds" in join) is bds_enabled
    assert ("--no-enable-bds" in join) is not bds_enabled
    assert calls[1][:3] == ["node", "start", "participant"]
    assert calls[2][:3] == ["node", "health", "participant"]
