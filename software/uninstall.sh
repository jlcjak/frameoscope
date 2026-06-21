#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/frameoscope-ngscope}"
SERVICE_NAME="frameoscope-ngscope.service"
UDEV_RULE="/etc/udev/rules.d/99-frameoscope-ngscope.rules"
REMOVE_APP_DIR=1

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [options]

Cleanly removes the Frameoscope ngscopeclient runtime service from this machine.

Options:
  --keep-app-dir     Leave /opt/frameoscope-ngscope in place.
  -h, --help         Show this help.

This removes:
  - frameoscope-ngscope.service
  - /etc/udev/rules.d/99-frameoscope-ngscope.rules
  - /opt/frameoscope-ngscope by default

It does not uninstall system packages such as ngscopeclient, gcc, libusb, or
Python packages outside the Frameoscope venv.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-app-dir)
            REMOVE_APP_DIR=0
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "${EUID}" -eq 0 ]]; then
    SUDO=()
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo "sudo is required when not running as root" >&2
        exit 1
    fi
    SUDO=(sudo)
fi
if [[ "${#SUDO[@]}" -gt 0 ]]; then
    "${SUDO[@]}" -v
fi

echo "Stopping ${SERVICE_NAME} if it is running"
if command -v systemctl >/dev/null 2>&1; then
    "${SUDO[@]}" systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    "${SUDO[@]}" systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
fi

echo "Removing systemd unit"
"${SUDO[@]}" rm -f "/etc/systemd/system/${SERVICE_NAME}"

echo "Removing udev rule"
"${SUDO[@]}" rm -f "${UDEV_RULE}"

if [[ "${REMOVE_APP_DIR}" -eq 1 ]]; then
    echo "Removing ${APP_DIR}"
    "${SUDO[@]}" rm -rf "${APP_DIR}"
else
    echo "Keeping ${APP_DIR}"
fi

if command -v systemctl >/dev/null 2>&1; then
    "${SUDO[@]}" systemctl daemon-reload || true
    "${SUDO[@]}" systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
fi
if command -v udevadm >/dev/null 2>&1; then
    "${SUDO[@]}" udevadm control --reload-rules || true
fi

cat <<EOF

Frameoscope runtime uninstalled.

Not removed:
  - ngscopeclient
  - OS packages installed as dependencies
  - FT232H EEPROM configuration on the hardware
EOF
