#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import secrets
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
FRAMEOSCOPE_PRODUCT_PREFIX = "Frameoscope"
FRAMEOSCOPE_PRODUCT = "Frameoscope 40MSPS"
FRAMEOSCOPE_MANUFACTURER = "FasterScope"
GENERIC_PRODUCT = "Single RS232-HS"
GENERIC_MANUFACTURER = "FTDI"

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


def is_frameoscope_product(product: str | None) -> bool:
    return bool(product and product.startswith(FRAMEOSCOPE_PRODUCT_PREFIX))


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:80] or "unknown"


def make_serial() -> str:
    return "FS" + secrets.token_hex(4).upper()


def list_ft232h_devices() -> list[dict[str, object]]:
    Ftdi = import_ftdi()
    try:
        found = Ftdi.list_devices("ftdi://ftdi:232h/?")
    except Exception as exc:
        log(f"FT232H enumeration failed: {exc}")
        return []

    devices: list[dict[str, object]] = []
    single = len(found) == 1
    for index, (desc, interface) in enumerate(found, 1):
        serial = getattr(desc, "sn", None) or ""
        product = getattr(desc, "description", None) or ""
        if serial:
            url = f"ftdi://ftdi:232h:{serial}/1"
        elif single:
            url = DEFAULT_URL
        else:
            url = None
        devices.append({
            "index": index,
            "url": url,
            "serial": serial,
            "product": product,
            "bus": getattr(desc, "bus", None),
            "address": getattr(desc, "address", None),
            "interface": interface,
            "tagged": is_frameoscope_product(product),
        })
    return devices


def find_frameoscope_url() -> str | None:
    devices = [d for d in list_ft232h_devices() if d["tagged"]]
    if not devices:
        return None
    if len(devices) > 1:
        log("multiple tagged Frameoscope devices found; using the first one")
    url = devices[0]["url"]
    if not isinstance(url, str):
        log("tagged Frameoscope device has no serial and cannot be selected safely")
        return None
    return url


def wait_for_frameoscope_url(poll_s: float) -> str | None:
    announced = False
    while not stopping:
        url = find_frameoscope_url()
        if url:
            if announced:
                log("tagged Frameoscope FT232H detected")
            return url
        if not announced:
            log("waiting for tagged Frameoscope FT232H")
            announced = True
        time.sleep(poll_s)
    return None


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
        url = wait_for_frameoscope_url(args.poll_seconds)
        if not url:
            break
        try:
            flash_fpga(url, args.spi_frequency)
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


def open_eeprom(url: str, size: int):
    try:
        from pyftdi.eeprom import FtdiEeprom
    except Exception as exc:
        die(f"missing pyftdi EEPROM support: {exc}")
    ee = FtdiEeprom()
    ee.open(url, size=size)
    return ee


def backup_eeprom(ee, label: str) -> Path:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup = APP_DIR / f"ft232h-eeprom-before-{safe_name(label)}-{stamp}.bin"
    backup.write_bytes(bytes(ee.data))
    log(f"saved EEPROM backup to {backup}")
    return backup


def maybe_reset_eeprom_device(ee) -> None:
    try:
        from usb.core import USBError
    except Exception:
        USBError = Exception  # type: ignore
    try:
        ee.reset_device()
    except USBError as exc:  # type: ignore[misc]
        if getattr(exc, "errno", None) != 2:
            raise


def ensure_valid_eeprom_image(ee, args: argparse.Namespace) -> None:
    props = ee.properties
    if args.initialize or not props:
        log("initializing EEPROM image before applying settings")
        ee._eeprom[:] = b"\x00" * len(ee._eeprom)
        ee.initialize()
        ee.set_property("power_max", args.power_max)
        ee.set_property("self_powered", False)
        ee.set_property("remote_wakeup", False)
        ee.set_property("suspend_pull_down", False)
        ee.set_property("out_isochronous", False)
        ee.set_property("in_isochronous", False)


def write_frameoscope_tag(url: str, serial: str, args: argparse.Namespace) -> None:
    if not args.yes:
        die("EEPROM programming is persistent. Re-run with --yes to write tags.")

    ee = open_eeprom(url, args.size)
    try:
        backup_eeprom(ee, serial or url)
        ensure_valid_eeprom_image(ee, args)

        ee.set_property("channel_a_type", "FIFO")
        ee.set_property("channel_a_driver", "D2XX")
        ee.set_manufacturer_name(args.manufacturer)
        ee.set_product_name(args.product)
        if not serial:
            new_serial = make_serial()
            log(f"assigning serial {new_serial}")
            ee.set_serial_number(new_serial)
        ee.sync()
        log(f"writing Frameoscope EEPROM tag to {url}")
        ee.commit(dry_run=False)
        maybe_reset_eeprom_device(ee)
    finally:
        ee.close()


def remove_frameoscope_tag(url: str, serial: str, args: argparse.Namespace) -> None:
    if not args.yes:
        die("EEPROM programming is persistent. Re-run with --yes to remove tags.")

    ee = open_eeprom(url, args.size)
    try:
        backup_eeprom(ee, serial or url)
        ensure_valid_eeprom_image(ee, args)
        ee.set_manufacturer_name(args.manufacturer)
        ee.set_product_name(args.product)
        ee.sync()
        log(f"removing Frameoscope EEPROM tag from {url}")
        ee.commit(dry_run=False)
        maybe_reset_eeprom_device(ee)
    finally:
        ee.close()


def tag_devices(args: argparse.Namespace) -> int:
    devices = list_ft232h_devices()
    if not devices:
        log("no FT232H devices found to tag")
        return 0

    wrote = 0
    skipped = 0
    for dev in devices:
        product = str(dev["product"])
        serial = str(dev["serial"])
        url = dev["url"]
        if dev["tagged"] and not args.refresh_tagged:
            log(f"already tagged: serial={serial or '-'} product={product!r}")
            skipped += 1
            continue
        if not isinstance(url, str):
            log(f"skipping device without unique serial: product={product!r}")
            skipped += 1
            continue
        log(f"tagging FT232H serial={serial or '-'} product={product!r}")
        write_frameoscope_tag(url, serial, args)
        wrote += 1
        time.sleep(0.5)

    log(f"tagging complete: wrote={wrote} skipped={skipped}")
    return 0


def untag_devices(args: argparse.Namespace) -> int:
    devices = [d for d in list_ft232h_devices() if d["tagged"]]
    if not devices:
        log("no tagged Frameoscope devices found")
        return 0

    for dev in devices:
        url = dev["url"]
        if not isinstance(url, str):
            log(f"skipping tagged device without unique serial: product={dev['product']!r}")
            continue
        remove_frameoscope_tag(url, str(dev["serial"]), args)
        time.sleep(0.5)
    log("Frameoscope tags removed from connected devices")
    return 0


def program_eeprom(args: argparse.Namespace) -> int:
    return tag_devices(args)


def doctor(args: argparse.Namespace) -> int:
    ensure_payload()
    print(f"app dir: {APP_DIR}")
    print(f"payload: {'ok' if PAYLOAD.is_file() else 'missing'}")
    print(f"bitstream: {'ok' if BITSTREAM.is_file() else 'missing'}")
    print(f"bridge source: {'ok' if BRIDGE_SRC.is_file() else 'missing'}")
    print(f"gcc: {shutil.which('gcc') or 'missing'}")
    print(f"pkg-config: {shutil.which('pkg-config') or 'missing'}")
    try:
        devices = list_ft232h_devices()
        print("pyftdi: ok")
        if not devices:
            print(f"device {VID:04x}:{PID:04x}: not present")
        for dev in devices:
            print(
                "device "
                f"serial={dev['serial'] or '-'} "
                f"product={dev['product']!r} "
                f"tagged={'yes' if dev['tagged'] else 'no'} "
                f"url={dev['url'] or '-'}"
            )
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

    eeprom = sub.add_parser("program-eeprom", help="alias for tag-devices")
    eeprom.set_defaults(func=program_eeprom, refresh_tagged=True,
                        manufacturer=FRAMEOSCOPE_MANUFACTURER,
                        product=FRAMEOSCOPE_PRODUCT)

    tag = sub.add_parser("tag-devices", help="persistently mark connected FT232H devices as Frameoscope")
    tag.add_argument("--size", type=int, default=256)
    tag.add_argument("--power-max", type=int, default=500)
    tag.add_argument("--manufacturer", default=FRAMEOSCOPE_MANUFACTURER)
    tag.add_argument("--product", default=FRAMEOSCOPE_PRODUCT)
    tag.add_argument("--initialize", action="store_true")
    tag.add_argument("--refresh-tagged", action="store_true")
    tag.add_argument("--yes", action="store_true")
    tag.set_defaults(func=tag_devices)

    untag = sub.add_parser("untag-devices", help="remove Frameoscope product tag from connected devices")
    untag.add_argument("--size", type=int, default=256)
    untag.add_argument("--power-max", type=int, default=500)
    untag.add_argument("--manufacturer", default=GENERIC_MANUFACTURER)
    untag.add_argument("--product", default=GENERIC_PRODUCT)
    untag.add_argument("--initialize", action="store_true")
    untag.add_argument("--yes", action="store_true")
    untag.set_defaults(func=untag_devices)

    for p in (eeprom,):
        p.add_argument("--size", type=int, default=256)
        p.add_argument("--power-max", type=int, default=500)
        p.add_argument("--product", default=FRAMEOSCOPE_PRODUCT)
        p.add_argument("--manufacturer", default=FRAMEOSCOPE_MANUFACTURER)
        p.add_argument("--initialize", action="store_true")
        p.add_argument("--yes", action="store_true")
    eeprom.set_defaults(func=program_eeprom)

    diag = sub.add_parser("doctor", help="check local files and dependencies")
    diag.set_defaults(func=doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
