import io
import os
import subprocess
import sys
from contextlib import redirect_stderr

import pytest

from xian_cli.models import (
    NetworkTemplate,
    NodeProfile,
    normalize_network_template,
    normalize_node_profile,
)
from xian_cli.runtime import _run_backend_command_with_streamed_stderr
from xian_cli.setup_contract import BackendRequest


@pytest.fixture(params=["profile", "template"])
def document(request):
    if request.param == "profile":
        payload, normalize = NodeProfile("node", "local", "node").to_dict(), normalize_node_profile
    else:
        payload = NetworkTemplate("local", "Local", "Local test").to_dict()
        normalize = normalize_network_template
    payload.pop("app_log_max_file_mb", None)
    payload.pop("app_log_max_total_mb", None)
    return payload, normalize


def test_legacy_document_gets_bounded_log_defaults(document):
    payload, normalize = document
    profile = normalize(payload)
    assert profile["app_log_max_file_mb"] == 50
    assert profile["app_log_max_total_mb"] == 250


@pytest.mark.parametrize("file_mb,total_mb", [(0, 250), (True, 250), (50, 0), (50, 49)])
def test_invalid_log_budgets_rejected(document, file_mb, total_mb):
    profile, normalize = document
    profile.update(app_log_max_file_mb=file_mb, app_log_max_total_mb=total_mb)
    with pytest.raises(ValueError, match="app_log_max"):
        normalize(profile)


def test_streaming_failure_keeps_bounded_tail_but_displays_complete_progress(tmp_path):
    text = "x" * 200_000 + "\nfinal diagnostic\n"
    script = (
        "import sys; sys.stdin.read(); "
        "sys.stderr.write('x'*200000+'\\nfinal diagnostic\\n'); sys.exit(3)"
    )
    displayed = io.StringIO()
    with redirect_stderr(displayed), pytest.raises(subprocess.CalledProcessError) as failure:
        _run_backend_command_with_streamed_stderr(
            [sys.executable, "-c", script],
            stack_dir=tmp_path,
            request=BackendRequest(command="start", options={}),
            env=os.environ,
        )
    assert displayed.getvalue() == text
    assert len(failure.value.stderr) < 66_000
    assert failure.value.stderr.startswith("[earlier backend stderr omitted]")
    assert failure.value.stderr.endswith("final diagnostic\n")
    assert failure.value.stderr.split("\n", 1)[1] == text[-65_536:]


def test_closed_progress_sink_does_not_stop_draining_backend(monkeypatch, tmp_path):
    from types import SimpleNamespace

    class ClosedProgress:
        calls = 0

        def write(self, _text):
            self.calls += 1
            raise BrokenPipeError("progress consumer closed")

        def flush(self):
            raise BrokenPipeError("progress consumer closed")

    diagnostics = "first\n" + "x" * 10_000 + "\nlast\n"
    process = SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO("{}"),
        stderr=io.StringIO(diagnostics),
        wait=lambda: 0,
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    progress = ClosedProgress()
    with redirect_stderr(progress):
        result = _run_backend_command_with_streamed_stderr(
            ["backend"], stack_dir=tmp_path, request=BackendRequest("start", {}), env={}
        )
    assert result.stderr == diagnostics
    assert result.stdout == "{}"
    assert progress.calls == 1
