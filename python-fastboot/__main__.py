#!/data/data/com.termux/files/usr/bin/python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from termuxadb import __version__
from termuxadb.fastboot.protocol import FastbootClient
from termuxadb.usb.broker import main as broker_main
from termuxadb.usb.device import open_transport_from_info, probe_device
from termuxadb.usb.termux_usb import list_usb_devices


USAGE = """usage: py-fastboot [COMMAND]

Fastboot over Termux USB.

commands:
  devices                 list Fastboot devices (default)
  getvar NAME             read one variable
  getvar all              read all variables
  info                    read all variables
  reboot                  reboot to Android
  reboot-bootloader       reboot to the bootloader

environment:
  termux-usb and Termux:API are required for USB access.
"""


def open_fastboot(info_callback=None) -> tuple[FastbootClient, object]:
    paths = list_usb_devices()
    if not paths:
        raise RuntimeError("no USB devices found")
    probes = []
    try:
        for path in paths:
            try:
                info, usbfs = probe_device(path)
            except Exception as exc:
                print(f"probe {path}: {exc}", file=sys.stderr)
                continue
            if "fastboot" in info.modes:
                probes.append((info, usbfs))
            else:
                usbfs.close()
        if not probes:
            raise RuntimeError("no Fastboot USB device found")
        if len(probes) > 1:
            raise RuntimeError("multiple Fastboot USB devices; unplug one")
        info, usbfs = probes[0]
        transport = open_transport_from_info(usbfs, info, "fastboot")
        return FastbootClient(transport, info_callback=info_callback), info
    except Exception:
        for _info, usbfs in probes:
            try:
                usbfs.close()
            except Exception:
                pass
        raise


def fastboot_command(args: list[str]) -> int:
    action = args[0] if args else "devices"
    if action == "devices":
        client, info = open_fastboot()
        try:
            print(f"{info.display_serial}\tfastboot")
        finally:
            client.close()
        return 0
    if action == "getvar":
        if len(args) < 2:
            raise RuntimeError("usage: py-fastboot getvar NAME")
        name = args[1]
        if name == "all":
            client, _info = open_fastboot(lambda line: print(line, flush=True))
            try:
                value = client.command("getvar:all", timeout=15000)
            finally:
                client.close()
            text = value.decode("utf-8", errors="replace").strip()
            if text:
                print(text)
            return 0
        client, _info = open_fastboot()
        try:
            value = client.getvar(name)
        finally:
            client.close()
        print(f"{name}: {value.decode('utf-8', errors='replace').strip()}")
        return 0
    if action == "info":
        client, _info = open_fastboot(lambda line: print(line, flush=True))
        try:
            value = client.command("getvar:all", timeout=15000)
        finally:
            client.close()
        text = value.decode("utf-8", errors="replace").strip()
        if text:
            print(text)
        return 0
    if action in ("reboot", "reboot-bootloader"):
        mode = "bootloader" if action == "reboot-bootloader" else ""
        client, _info = open_fastboot()
        try:
            client.reboot(mode)
        finally:
            client.close()
        print(f"reboot {mode or 'system'} sent")
        return 0
    raise RuntimeError(f"unknown fastboot command {action!r}")


def main() -> int:
    if "--usb-broker" in sys.argv:
        return broker_main()
    args = sys.argv[1:]
    if args and args[0] in ("help", "--help", "-h"):
        print(USAGE, end="")
        return 0
    if args and args[0] in ("version", "--version"):
        print(f"py-fastboot {__version__}")
        return 0
    try:
        return fastboot_command(args or ["devices"])
    except Exception as exc:
        print(f"py-fastboot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
