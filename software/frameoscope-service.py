#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

VID = 0x0403
PID = 0x6014
DEFAULT_URL = "ftdi://ftdi:232h/1"

ADBUS_SCK = 0x01
ADBUS_MOSI = 0x02
ADBUS_MISO = 0x04
ADBUS_CS_N = 0x08
LOW_DIR = ADBUS_SCK | ADBUS_MOSI | ADBUS_CS_N
LOW_IDLE = ADBUS_CS_N

ACBUS_CRESET_N = 0x80
HIGH_DIR = ACBUS_CRESET_N
HIGH_IDLE = ACBUS_CRESET_N

MPSSE_CHUNK = 65536
APP_DIR = Path(__file__).resolve().parent
PAYLOAD = APP_DIR / "frameoscope-payload.tar.gz"
BITSTREAM = APP_DIR / "top_adc_40m_stream.bin"
BRIDGE_SRC = APP_DIR / "frameoscope_ngscope_bridge.c"
BRIDGE_EXE = APP_DIR / "frameoscope_ngscope_bridge"

stopping = False
bridge_process: subprocess.Popen[bytes] | None = None


def log(message: str) -> None:
    print(f"[frameoscope] {message}", flush=True)


def die(message: str, code: int = 1) -> None:
    log(f"error: {message}")
    raise SystemExit(code)


def safe_extract_tar(path: Path, target_dir: Path) -> None:
    root = target_dir.resolve()
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            dest = (target_dir / member.name).resolve()
            if dest != root and not str(dest).startswith(str(root) + os.sep):
                die(f"payload contains unsafe path: {member.name}")
        tar.extractall(target_dir)


def ensure_payload() -> None:
    if BITSTREAM.is_file() and BRIDGE_SRC.is_file():
        return
    if not PAYLOAD.is_file():
        die(f"missing payload: {PAYLOAD}")
    log(f"extracting payload from {PAYLOAD}")
    safe_extract_tar(PAYLOAD, APP_DIR)
    if not BITSTREAM.is_file() or not BRIDGE_SRC.is_file():
        die("payload did not provide bridge source and 40 MSPS bitstream")


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        die(f"missing command `{name}`; install gcc, pkg-config, and libusb-1.0-dev")
    return path


def pkg_config(args: list[str]) -> list[str]:
    try:
        out = subprocess.check_output(["pkg-config", *args, "libusb-1.0"], text=True)
        return out.split()
    except Exception:
        return []


def compile_bridge() -> Path:
    ensure_payload()
    if BRIDGE_EXE.is_file() and BRIDGE_EXE.stat().st_mtime >= BRIDGE_SRC.stat().st_mtime:
        return BRIDGE_EXE

    require_command("gcc")
    cflags = pkg_config(["--cflags"])
    libs = pkg_config(["--libs"])
    if not libs:
        libs = ["-lusb-1.0"]
    if not cflags and Path("/usr/include/libusb-1.0").is_dir():
        cflags = ["-I/usr/include/libusb-1.0"]

    cmd = [
        "gcc",
        "-O3",
        "-Wall",
        "-Wextra",
        "-pthread",
        *cflags,
        "-o",
        str(BRIDGE_EXE),
        str(BRIDGE_SRC),
        *libs,
    ]
    log("compiling ngscope bridge")
    try:
        subprocess.check_call(cmd, cwd=APP_DIR)
    except subprocess.CalledProcessError as exc:
        die(f"bridge compile failed with exit code {exc.returncode}")
    BRIDGE_EXE.chmod(0o755)
    return BRIDGE_EXE


def import_ftdi():
    try:
        from pyftdi.ftdi import Ftdi
    except Exception as exc:
        die(f"missing pyftdi: {exc}. Re-run install.sh so the venv is created.")
    return Ftdi


def device_present() -> bool:
    try:
        import usb.core
    except Exception:
        return False
    return usb.core.find(idVendor=VID, idProduct=PID) is not None


def wait_for_device(poll_s: float) -> bool:
    announced = False
    while not stopping:
        if device_present():
            if announced:
                log("FT232H detected")
            return True
        if not announced:
            log(f"waiting for FT232H {VID:04x}:{PID:04x}")
            announced = True
        time.sleep(poll_s)
    return False


class Ice40MpsseProgrammer:
    def __init__(self, url: str, frequency: float) -> None:
        self.Ftdi = import_ftdi()
        self.url = url
        self.frequency = frequency
        self.ftdi = self.Ftdi()
        self.low = LOW_IDLE
        self.high = HIGH_IDLE
        self.opened = False

    def open(self) -> None:
        direction = LOW_DIR | (HIGH_DIR << 8)
        initial = LOW_IDLE | (HIGH_IDLE << 8)
        actual = self.ftdi.open_mpsse_from_url(
            self.url,
            direction=direction,
            initial=initial,
            frequency=self.frequency,
            latency=2,
        )
        self.opened = True
        self.ftdi.enable_adaptive_clock(False)
        self.ftdi.enable_3phase_clock(False)
        self.ftdi.purge_buffers()
        log(f"MPSSE opened at {actual:.0f} Hz")
        self.set_pins(cs_n=True, creset_n=True)

    def close(self) -> None:
        if not self.opened:
            return
        try:
            self.set_pins(cs_n=True, creset_n=True)
        finally:
            self.ftdi.close()
            self.opened = False

    def set_pins(self, *, cs_n: bool | None = None, creset_n: bool | None = None) -> None:
        if cs_n is not None:
            self.low = self.low | ADBUS_CS_N if cs_n else self.low & ~ADBUS_CS_N
        if creset_n is not None:
            self.high = self.high | ACBUS_CRESET_N if creset_n else self.high & ~ACBUS_CRESET_N
        cmd = bytes((
            self.Ftdi.SET_BITS_LOW, self.low, LOW_DIR,
            self.Ftdi.SET_BITS_HIGH, self.high, HIGH_DIR,
        ))
        self.ftdi.write_data(cmd)

    def spi_write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            chunk = view[:MPSSE_CHUNK]
            n = len(chunk)
            cmd = bytearray((
                self.Ftdi.WRITE_BYTES_NVE_MSB,
                (n - 1) & 0xff,
                ((n - 1) >> 8) & 0xff,
            ))
            cmd.extend(chunk)
            self.ftdi.write_data(cmd)
            view = view[n:]

    def dummy_clocks(self) -> None:
        self.spi_write(b"\xff" * 6)
        self.ftdi.write_data(bytes((self.Ftdi.WRITE_BITS_NVE_MSB, 0, 0xff)))

    def program_sram(self, bitstream: Path) -> None:
        data = bitstream.read_bytes()
        log(f"programming FPGA SRAM from {bitstream.name} ({len(data)} bytes)")
        self.set_pins(cs_n=True, creset_n=True)
        time.sleep(0.100)
        self.set_pins(cs_n=False, creset_n=False)
        time.sleep(0.001)
        self.set_pins(cs_n=False, creset_n=True)
        time.sleep(0.002)
        for offset in range(0, len(data), 4096):
            self.spi_write(data[offset:offset + 4096])
        self.dummy_clocks()
        self.set_pins(cs_n=True, creset_n=True)
        time.sleep(0.010)
        log("FPGA SRAM programming complete")


def flash_fpga(url: str, spi_frequency: float) -> None:
    ensure_payload()
    programmer = Ice40MpsseProgrammer(url, spi_frequency)
    try:
        programmer.open()
        programmer.program_sram(BITSTREAM)
    finally:
        programmer.close()


def terminate_child() -> None:
    global bridge_process
    proc = bridge_process
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    bridge_process = None


def on_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
    global stopping
    stopping = True
    terminate_child()


def run_service(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    bridge = compile_bridge()
    log("ready for ngscopeclient connection: frameoscope:dslabs:twinlan:localhost:5025:5026")

    while not stopping:
        if not wait_for_device(args.poll_seconds):
            break
        try:
            flash_fpga(args.url, args.spi_frequency)
        except Exception as exc:
            log(f"flash failed: {exc}; retrying")
            time.sleep(args.retry_seconds)
            continue

        cmd = [
            str(bridge),
            "--sample-rate",
            str(args.sample_rate),
            "--ring-mib",
            str(args.ring_mib),
        ]
        log("starting ngscope bridge")
        global bridge_process
        bridge_process = subprocess.Popen(cmd, cwd=APP_DIR)
        while not stopping and bridge_process.poll() is None:
            time.sleep(0.5)
        if stopping:
            break
        rc = bridge_process.returncode
        bridge_process = None
        log(f"bridge exited with code {rc}; waiting before retry")
        time.sleep(args.retry_seconds)

    terminate_child()
    return 0


def program_eeprom(args: argparse.Namespace) -> int:
    try:
        from pyftdi.eeprom import FtdiEeprom
        from usb.core import USBError
    except Exception as exc:
        die(f"missing pyftdi/pyusb EEPROM support: {exc}")

    if not args.yes:
        die("EEPROM programming is persistent. Re-run with --yes if this board should be configured.")

    ee = FtdiEeprom()
    ee.open(args.url, size=args.size)
    try:
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        backup = APP_DIR / f"ft232h-eeprom-before-{stamp}.bin"
        backup.write_bytes(bytes(ee.data))
        log(f"saved EEPROM backup to {backup}")

        props = ee.properties
        if args.initialize or not props:
            log("initializing EEPROM image before applying FIFO settings")
            ee._eeprom[:] = b"\x00" * len(ee._eeprom)
            ee.initialize()
            ee.set_property("power_max", args.power_max)
            ee.set_property("self_powered", False)
            ee.set_property("remote_wakeup", False)
            ee.set_property("suspend_pull_down", False)
            ee.set_property("out_isochronous", False)
            ee.set_property("in_isochronous", False)

        ee.set_property("channel_a_type", "FIFO")
        ee.set_property("channel_a_driver", "D2XX")
        ee.set_product_name(args.product)
        ee.sync()
        log("writing FT232H EEPROM FIFO/D2XX configuration")
        ee.commit(dry_run=False)
        try:
            ee.reset_device()
        except USBError as exc:
            if getattr(exc, "errno", None) != 2:
                raise
        log("EEPROM updated; unplug/replug the board")
    finally:
        ee.close()
    return 0


def doctor(args: argparse.Namespace) -> int:
    ensure_payload()
    print(f"app dir: {APP_DIR}")
    print(f"payload: {'ok' if PAYLOAD.is_file() else 'missing'}")
    print(f"bitstream: {'ok' if BITSTREAM.is_file() else 'missing'}")
    print(f"bridge source: {'ok' if BRIDGE_SRC.is_file() else 'missing'}")
    print(f"gcc: {shutil.which('gcc') or 'missing'}")
    print(f"pkg-config: {shutil.which('pkg-config') or 'missing'}")
    print(f"device {VID:04x}:{PID:04x}: {'present' if device_present() else 'not present'}")
    try:
        import_ftdi()
        print("pyftdi: ok")
    except SystemExit:
        print("pyftdi: missing")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frameoscope ngscopeclient runtime service")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="flash FPGA then run/retry the ngscope bridge")
    run.add_argument("--url", default=DEFAULT_URL)
    run.add_argument("--sample-rate", type=int, default=40_000_000)
    run.add_argument("--ring-mib", type=int, default=192)
    run.add_argument("--spi-frequency", type=float, default=6_000_000)
    run.add_argument("--poll-seconds", type=float, default=1.0)
    run.add_argument("--retry-seconds", type=float, default=2.0)
    run.set_defaults(func=run_service)

    eeprom = sub.add_parser("program-eeprom", help="persistently configure FT232H channel A as FIFO/D2XX")
    eeprom.add_argument("--url", default=DEFAULT_URL)
    eeprom.add_argument("--size", type=int, default=256)
    eeprom.add_argument("--power-max", type=int, default=500)
    eeprom.add_argument("--product", default="Frameoscope 40MSPS")
    eeprom.add_argument("--initialize", action="store_true")
    eeprom.add_argument("--yes", action="store_true")
    eeprom.set_defaults(func=program_eeprom)

    diag = sub.add_parser("doctor", help="check local files and dependencies")
    diag.set_defaults(func=doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
