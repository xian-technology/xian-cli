from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

SECRET_FILE_MODE = 0o600
SECRET_DIR_MODE = 0o700


def _safe_flag_list(*flags: str) -> str:
    if len(flags) == 1:
        return flags[0]
    return f"{', '.join(flags[:-1])}, or {flags[-1]}"


def _secret_source_error(
    *,
    secret_name: str,
    direct_flag: str,
    env_flag: str,
    file_flag: str,
    stdin_flag: str,
) -> ValueError:
    safe_flags = _safe_flag_list(env_flag, file_flag, stdin_flag)
    return ValueError(
        f"{secret_name} cannot be passed with {direct_flag}; process arguments "
        f"can leak through shell history and process lists. Use {safe_flags}."
    )


def _mode_is_too_open(mode: int) -> bool:
    return os.name != "nt" and bool(stat.S_IMODE(mode) & 0o077)


def assert_secret_file_permissions(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"{path} does not exist") from None
    if stat.S_ISLNK(file_stat.st_mode):
        raise PermissionError(f"{path} must not be a symlink")
    if not stat.S_ISREG(file_stat.st_mode):
        raise PermissionError(f"{path} must be a regular file")
    if _mode_is_too_open(file_stat.st_mode):
        raise PermissionError(f"{path} permissions are too open; run chmod 600 {path}")


def ensure_secret_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(f"{path} is not a directory")
    else:
        path.mkdir(parents=True, mode=SECRET_DIR_MODE)
    if os.name != "nt":
        path.chmod(SECRET_DIR_MODE)


def _ensure_private_parent(path: Path) -> None:
    parent = path.parent
    if parent.exists():
        if not parent.is_dir():
            raise NotADirectoryError(f"{parent} is not a directory")
        return
    parent.mkdir(parents=True, mode=SECRET_DIR_MODE)
    if os.name != "nt":
        parent.chmod(SECRET_DIR_MODE)


def secure_write_text(path: Path, content: str, *, force: bool = False) -> None:
    _ensure_private_parent(path)
    if path.exists():
        if not force:
            raise FileExistsError(f"{path} already exists; pass --force to overwrite")
        assert_secret_file_permissions(path)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, SECRET_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    if os.name != "nt":
        path.chmod(SECRET_FILE_MODE)


def read_secret_text(path: Path) -> str:
    assert_secret_file_permissions(path)
    return path.read_text(encoding="utf-8")


def _secret_sources(
    args: argparse.Namespace,
    *,
    direct_attr: str,
    env_attr: str,
    file_attr: str,
    stdin_attr: str,
    secret_name: str,
    direct_flag: str,
    env_flag: str,
    file_flag: str,
    stdin_flag: str,
    required: bool,
) -> list[tuple[str, str | None]]:
    if getattr(args, direct_attr, None) is not None:
        raise _secret_source_error(
            secret_name=secret_name,
            direct_flag=direct_flag,
            env_flag=env_flag,
            file_flag=file_flag,
            stdin_flag=stdin_flag,
        )

    sources: list[tuple[str, str | None]] = []
    env_name = getattr(args, env_attr, None)
    if env_name is not None:
        env_name = str(env_name).strip()
        if not env_name:
            raise ValueError(f"{env_flag} requires a non-empty environment variable name")
        sources.append((env_flag, env_name))

    file_path = getattr(args, file_attr, None)
    if file_path is not None:
        file_path = str(file_path).strip()
        if not file_path:
            raise ValueError(f"{file_flag} requires a non-empty path")
        sources.append((file_flag, file_path))

    if bool(getattr(args, stdin_attr, False)):
        sources.append((stdin_flag, None))

    if len(sources) > 1:
        raise ValueError(
            f"provide only one {secret_name} source: "
            f"{_safe_flag_list(env_flag, file_flag, stdin_flag)}"
        )
    if required and not sources:
        raise ValueError(
            f"{secret_name} is required; pass {_safe_flag_list(env_flag, file_flag, stdin_flag)}"
        )
    return sources


def validate_secret_sources(
    args: argparse.Namespace,
    *,
    direct_attr: str,
    env_attr: str,
    file_attr: str,
    stdin_attr: str,
    secret_name: str,
    direct_flag: str,
    env_flag: str,
    file_flag: str,
    stdin_flag: str,
    required: bool = False,
) -> None:
    _secret_sources(
        args,
        direct_attr=direct_attr,
        env_attr=env_attr,
        file_attr=file_attr,
        stdin_attr=stdin_attr,
        secret_name=secret_name,
        direct_flag=direct_flag,
        env_flag=env_flag,
        file_flag=file_flag,
        stdin_flag=stdin_flag,
        required=required,
    )


def load_secret_from_args(
    args: argparse.Namespace,
    *,
    direct_attr: str,
    env_attr: str,
    file_attr: str,
    stdin_attr: str,
    secret_name: str,
    direct_flag: str,
    env_flag: str,
    file_flag: str,
    stdin_flag: str,
    required: bool = True,
) -> str | None:
    sources = _secret_sources(
        args,
        direct_attr=direct_attr,
        env_attr=env_attr,
        file_attr=file_attr,
        stdin_attr=stdin_attr,
        secret_name=secret_name,
        direct_flag=direct_flag,
        env_flag=env_flag,
        file_flag=file_flag,
        stdin_flag=stdin_flag,
        required=required,
    )
    if not sources:
        return None

    source_flag, source_value = sources[0]
    if source_flag == env_flag:
        raw = os.environ.get(source_value or "")
    elif source_flag == file_flag:
        raw = read_secret_text(Path(source_value or ""))
    else:
        raw = sys.stdin.read()

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{secret_name} from {source_flag} is empty")
    return raw.strip()
