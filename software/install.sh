#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/frameoscope-ngscope}"
BASE_URL="${BASE_URL:-https://frame.fasterscope.com}"
SERVICE_NAME="frameoscope-ngscope.service"
SAMPLE_RATE="${SAMPLE_RATE:-40000000}"
INSTALL_DEPS=1
START_SERVICE=1
PROGRAM_EEPROM=0
TMP_DIR=""

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Installs the Frameoscope runtime service. The service waits for the FT232H,
flashes the 40 MSPS FPGA bitstream, then starts the ngscopeclient bridge.

Options:
  --base-url URL     Download companion files from URL if not present locally.
                     Default: https://frame.fasterscope.com
  --no-deps          Do not install OS packages with apt-get.
  --no-start         Install but do not enable/start the systemd service.
  --program-eeprom   Also persistently configure the FT232H EEPROM as FIFO/D2XX.
                     Use this only for boards you intend to ship in this mode.
  -h, --help         Show this help.

After install, use this ngscopeclient connection string:
  frameoscope:dslabs:twinlan:localhost:5025:5026
EOF
}

cleanup() {
    if [[ -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
        rm -rf "${TMP_DIR}"
    fi
}
trap cleanup EXIT

download_file() {
    local url="$1"
    local out="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$out"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$out" "$url"
    else
        echo "curl or wget is required to download ${url}" >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url)
            if [[ $# -lt 2 ]]; then
                echo "--base-url requires an argument" >&2
                exit 2
            fi
            BASE_URL="$2"
            shift
            ;;
        --no-deps)
            INSTALL_DEPS=0
            ;;
        --no-start)
            START_SERVICE=0
            ;;
        --program-eeprom)
            PROGRAM_EEPROM=1
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

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SCRIPT="${SCRIPT_DIR}/frameoscope-service.py"
PAYLOAD="${SCRIPT_DIR}/frameoscope-payload.tar.gz"

if [[ ! -f "${SERVICE_SCRIPT}" || ! -f "${PAYLOAD}" ]]; then
    BASE_URL="${BASE_URL%/}"
    TMP_DIR="$(mktemp -d)"
    SERVICE_SCRIPT="${TMP_DIR}/frameoscope-service.py"
    PAYLOAD="${TMP_DIR}/frameoscope-payload.tar.gz"
    echo "Companion files not found next to install.sh; downloading from ${BASE_URL}"
    download_file "${BASE_URL}/frameoscope-service.py" "${SERVICE_SCRIPT}"
    download_file "${BASE_URL}/frameoscope-payload.tar.gz" "${PAYLOAD}"
fi

if [[ "${INSTALL_DEPS}" -eq 1 ]]; then
    if command -v apt-get >/dev/null 2>&1; then
        "${SUDO[@]}" apt-get update
        "${SUDO[@]}" apt-get install -y \
            python3 \
            python3-venv \
            python3-pip \
            gcc \
            pkg-config \
            libusb-1.0-0-dev
    else
        cat >&2 <<'EOF'
apt-get was not found. Install these dependencies yourself, then re-run with --no-deps:
  python3 python3-venv python3-pip gcc pkg-config libusb-1.0 development headers
EOF
        exit 1
    fi
fi

"${SUDO[@]}" mkdir -p "${APP_DIR}"
"${SUDO[@]}" install -m 0755 "${SERVICE_SCRIPT}" "${APP_DIR}/frameoscope-service.py"
"${SUDO[@]}" install -m 0644 "${PAYLOAD}" "${APP_DIR}/frameoscope-payload.tar.gz"

if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
    "${SUDO[@]}" python3 -m venv "${APP_DIR}/venv"
fi
"${SUDO[@]}" "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip wheel
"${SUDO[@]}" "${APP_DIR}/venv/bin/python" -m pip install --upgrade pyftdi pyusb

if [[ "${PROGRAM_EEPROM}" -eq 1 ]]; then
    echo "Programming FT232H EEPROM. This is persistent."
    "${SUDO[@]}" "${APP_DIR}/venv/bin/python" \
        "${APP_DIR}/frameoscope-service.py" program-eeprom --yes
fi

"${SUDO[@]}" groupadd -f plugdev

UNIT_TMP="$(mktemp)"
cat > "${UNIT_TMP}" <<EOF
[Unit]
Description=Frameoscope ngscopeclient bridge
After=systemd-udevd.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/frameoscope-service.py run --sample-rate ${SAMPLE_RATE}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
"${SUDO[@]}" install -m 0644 "${UNIT_TMP}" "/etc/systemd/system/${SERVICE_NAME}"
rm -f "${UNIT_TMP}"

UDEV_TMP="$(mktemp)"
cat > "${UDEV_TMP}" <<EOF
# Frameoscope FT232H. Starts the bridge service on plug-in and grants local raw USB access.
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", GROUP="plugdev", MODE="0660", TAG+="uaccess", TAG+="systemd", ENV{SYSTEMD_WANTS}+="${SERVICE_NAME}"
EOF
"${SUDO[@]}" install -m 0644 "${UDEV_TMP}" /etc/udev/rules.d/99-frameoscope-ngscope.rules
rm -f "${UDEV_TMP}"

if command -v systemctl >/dev/null 2>&1; then
    "${SUDO[@]}" systemctl daemon-reload
fi
if command -v udevadm >/dev/null 2>&1; then
    "${SUDO[@]}" udevadm control --reload-rules || true
fi

if [[ "${START_SERVICE}" -eq 1 ]]; then
    if command -v systemctl >/dev/null 2>&1; then
        "${SUDO[@]}" systemctl enable --now "${SERVICE_NAME}"
        if command -v udevadm >/dev/null 2>&1; then
            "${SUDO[@]}" udevadm trigger --subsystem-match=usb --attr-match=idVendor=0403 --attr-match=idProduct=6014 || true
        fi
    else
        echo "systemctl not found; start manually with:"
        echo "  ${APP_DIR}/venv/bin/python ${APP_DIR}/frameoscope-service.py run --sample-rate ${SAMPLE_RATE}"
    fi
fi

cat <<EOF

Frameoscope runtime installed.

ngscopeclient connection string:
  frameoscope:dslabs:twinlan:localhost:5025:5026

Useful commands:
  sudo systemctl status ${SERVICE_NAME}
  sudo journalctl -u ${SERVICE_NAME} -f
  sudo systemctl restart ${SERVICE_NAME}

Installed under:
  ${APP_DIR}
EOF
