from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
    )


def _find_single_wheel(dist_dir: Path) -> Path:
    wheels = sorted(dist_dir.glob("xian_tech_cli-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(
            "expected exactly one wheel artifact in "
            f"{dist_dir}, found {[path.name for path in wheels]}"
        )
    return wheels[0]


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _installed_script_path(python: Path) -> Path:
    script = (
        "import sys, sysconfig; "
        "from pathlib import Path; "
        "name = 'xian.exe' if sys.platform == 'win32' else 'xian'; "
        "print(Path(sysconfig.get_path('scripts')) / name)"
    )
    return Path(_run(str(python), "-c", script).stdout.strip())


def _assert_help_contains(output: str, command_name: str) -> None:
    required_tokens = ("usage:", "network", "doctor")
    missing = [token for token in required_tokens if token not in output]
    if missing:
        raise SystemExit(f"{command_name} help output missing expected tokens {missing}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the built wheel and verify CLI entrypoints",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=Path("dist"),
        help="directory containing the built wheel artifact",
    )
    args = parser.parse_args(argv)

    wheel_path = _find_single_wheel(args.dist_dir)

    with tempfile.TemporaryDirectory(prefix="xian-cli-smoke-") as temp_dir:
        venv_dir = Path(temp_dir) / "venv"
        _run("uv", "venv", "--python", sys.executable, str(venv_dir))
        python = _venv_python(venv_dir)
        _run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--force-reinstall",
            str(wheel_path),
        )

        console_script = _installed_script_path(python)
        if not console_script.exists():
            raise SystemExit(f"installed console script not found: {console_script}")

        console_help = _run(str(console_script), "--help").stdout.lower()
        module_help = _run(str(python), "-m", "xian_cli", "--help").stdout.lower()

        _assert_help_contains(console_help, str(console_script))
        _assert_help_contains(module_help, "python -m xian_cli")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
