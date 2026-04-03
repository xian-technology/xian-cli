#!/usr/bin/env sh
set -eu

PACKAGE_NAME="${XIAN_CLI_PACKAGE_NAME:-xian-tech-cli}"
VERSION="${XIAN_CLI_VERSION:-}"

case "$VERSION" in
  v*)
    VERSION="${VERSION#v}"
    ;;
esac

PACKAGE_SPEC="$PACKAGE_NAME"
if [ -n "$VERSION" ]; then
  PACKAGE_SPEC="${PACKAGE_NAME}==${VERSION}"
fi

run() {
  printf '>>> %s\n' "$*"
  if [ "${XIAN_CLI_DRY_RUN:-0}" = "1" ]; then
    return 0
  fi
  "$@"
}

if command -v uv >/dev/null 2>&1; then
  run uv tool install --force "$PACKAGE_SPEC"
elif command -v pipx >/dev/null 2>&1; then
  run pipx install --force "$PACKAGE_SPEC"
elif command -v python3 >/dev/null 2>&1; then
  run python3 -m pip install --user --upgrade "$PACKAGE_SPEC"
  USER_BIN="$(
    python3 - <<'PY'
import site
import sys
from pathlib import Path

print(Path(site.USER_BASE) / ("Scripts" if sys.platform == "win32" else "bin"))
PY
  )"
  printf 'Installed with user-site pip. Add %s to PATH if `xian` is not available yet.\n' "$USER_BIN"
else
  printf '%s\n' "Need one of: uv, pipx, or python3" >&2
  exit 1
fi

printf 'Installed %s. Run `xian --help` to verify the CLI.\n' "$PACKAGE_SPEC"
