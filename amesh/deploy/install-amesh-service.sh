#!/usr/bin/env bash
# Install amesh-serve.service as a systemd --user unit and start it.
#
# Usage:
#   ./install-amesh-service.sh [--python /path/to/python] [--home /path/to/amesh-home]
#
# Defaults: python3 on PATH; home $HOME/.config/amesh (overridable via
# AMESH_PYTHON / AMESH_HOME environment variables).
set -euo pipefail

PYTHON="${AMESH_PYTHON:-}"
HOME_PATH="${AMESH_HOME:-$HOME/.config/amesh}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON="$2"
      shift 2
      ;;
    --home)
      HOME_PATH="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "amesh: no usable Python; pass --python or set AMESH_PYTHON" >&2
  exit 1
fi
if ! "$PYTHON" -c "import amesh" 2>/dev/null; then
  echo "amesh: the selected Python cannot import the amesh package" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
sed \
  -e "s|@AMESH_PYTHON@|$PYTHON|" \
  -e "s|@AMESH_HOME@|$HOME_PATH|" \
  "$(dirname "$0")/amesh-serve.service" > "$UNIT_DIR/amesh-serve.service"

systemctl --user daemon-reload
systemctl --user enable --now amesh-serve.service

echo "amesh-serve.service installed for home $HOME_PATH"
systemctl --user --no-pager status amesh-serve.service
