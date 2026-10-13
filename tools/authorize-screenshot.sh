#!/usr/bin/env bash
# authorize-screenshot.sh — let the dsh-cu bridge use KWin's ScreenShot2.
#
# KWin gates `org.kde.KWin.ScreenShot2` behind a restricted-interface check: the
# calling process is authorized only when its executable is named by the `Exec`
# of a .desktop file that declares
#   X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2
# (see src/utils/serviceutils.h in kwin). Without the grant, KWin answers
# `org.kde.KWin.ScreenShot2.Error.NoAuthorized` and dsh-cu falls back to
# Spectacle, which is slower and cannot capture a window that is not on top.
#
# This script installs that grant for the interpreter the bridge runs under,
# then verifies it. KWin reads the service database through KSycoca; on some
# setups a running KWin does not notice a newly installed user-level .desktop
# file until it is restarted, and the verification says so rather than pretending
# the grant worked.
#
# Usage:
#   tools/authorize-screenshot.sh            install, refresh the cache, verify
#   tools/authorize-screenshot.sh --verify   verify only (change nothing)
#   tools/authorize-screenshot.sh --remove   delete the grant
#
# The grant authorizes every process running that interpreter, because KWin
# matches on the executable. Use DSH_CU_PYTHON to point it at a dedicated
# interpreter (for example a venv created with `python -m venv --copies`) when
# that matters.
set -euo pipefail

PROG=authize-screenshot
DESKTOP_NAME=dsh-cu-bridge.desktop
TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
TARGET="$TARGET_DIR/$DESKTOP_NAME"
INTERPRETER="${DSH_CU_PYTHON:-python3}"
INTERFACE=org.kde.KWin.ScreenShot2

say() { printf '%s: %s\n' "$PROG" "$*"; }
die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }

resolve_interpreter() {
  local resolved
  resolved="$(command -v "$INTERPRETER" 2>/dev/null)" || die "interpreter \"$INTERPRETER\" not found in PATH"
  readlink -f "$resolved"
}

verify() {
  local root bridge output ok
  root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
  bridge="$root/bin/dsh-cu-bridge"
  [ -x "$bridge" ] || die "cannot find $bridge"
  say "probing ScreenShot2 (a 1x1 area capture)…"
  output="$(timeout 60 "$bridge" status '{"refreshScreenshotAuthorization":true}' 2>/dev/null || true)"
  [ -n "$output" ] || die "the bridge produced no output"
  ok="$(printf '%s' "$output" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["screenshot"]["screenShot2"])' 2>/dev/null || echo unknown)"
  if [ "$ok" = "True" ] || [ "$ok" = "true" ]; then
    say "authorized: ScreenShot2 captures will be used."
    return 0
  fi
  say "still not authorized."
  printf '%s' "$output" | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]["screenshot"]; print("  detail:", r.get("screenShot2Detail","(none)"))' 2>/dev/null || true
  cat >&2 <<EOF

The grant is installed at:
  $TARGET
but the running KWin has not picked it up. KWin resolves the caller through the
KSycoca service database, which a long-running KWin can have cached. Either:

  * log out and back in (picks it up everywhere), or
  * restart just the compositor, which closes every app's windows:
      systemctl --user restart plasma-kwin_wayland.service

Until then dsh-cu keeps working through the Spectacle fallback.
EOF
  return 1
}

case "${1:-}" in
  --verify)
    verify
    exit $?
    ;;
  --remove)
    if [ -f "$TARGET" ]; then rm -f "$TARGET"; say "removed $TARGET"
    else say "no grant at $TARGET"; fi
    command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    exit 0
    ;;
  ""|-h|--help)
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
    fi
    ;;
  *)
    die "unknown argument $1 (see --help)"
    ;;
esac

REAL_INTERPRETER="$(resolve_interpreter)"
mkdir -p "$TARGET_DIR"
cat > "$TARGET" <<EOF
[Desktop Entry]
Type=Application
Name=dsh-cu screenshot bridge
Comment=Grants the dsh-cu computer-use bridge KWin's restricted ScreenShot2 interface
Exec=$REAL_INTERPRETER
X-KDE-DBUS-Restricted-Interfaces=$INTERFACE
EOF
say "wrote $TARGET (Exec=$REAL_INTERPRETER)"

if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 --noincremental >/dev/null 2>&1 && say "refreshed the KDE service cache" \
    || say "kbuildsycoca6 reported a problem; continuing"
else
  say "kbuildsycoca6 not found; the grant may need a re-login to take effect"
fi

verify
