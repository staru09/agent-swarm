#!/usr/bin/env bash
set -euo pipefail

fail=0
check() {
  if "$@" >/dev/null 2>&1; then
    printf 'ok   %s\n' "$*"
  else
    printf 'FAIL %s\n' "$*" >&2
    fail=1
  fi
}

check test "$(uname -s)" = Linux
check test -r /sys/kernel/btf/vmlinux
check test -d /sys/fs/bpf
check docker info
check python3 -c "import sys; assert sys.version_info >= (3, 11)"
check node --version
check sudo -n true

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  printf 'WARN ANTHROPIC_API_KEY is not set; live agents cannot run\n'
fi

exit "$fail"
