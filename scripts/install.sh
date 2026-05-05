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

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "Need uv to install xian-cli" >&2
  exit 1
fi

run uv tool install --force "$PACKAGE_SPEC"
printf 'Installed %s. Run `xian --help` to verify the CLI.\n' "$PACKAGE_SPEC"
