#!/data/data/com.termux/files/usr/bin/python3
from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from termuxadb import __version__
from termuxadb.adb.crypto import RsaKey
from termuxadb.adb.sync import pull_file, push_file
from termuxadb.adb.transport import AdbConnection
from termuxadb.usb.broker import main as broker_main
from termuxadb.usb.device import open_transport_from_info, probe_device
from termuxadb.usb.termux_usb import list_usb_devices


CONFIG_DIR = Path.home() / ".config" / "termux-pyadb"
SOCKET_PATH = CONFIG_DIR / "min.sock"
LOG_PATH = CONFIG_DIR / "min-daemon.log"

USAGE = """usage: py-adb COMMAND

ADB over Termux USB.

commands:
  key                     show the ADB public key fingerprint
  start                   start the background daemon
  status                  show daemon/device status
  devices                 list ADB devices
  shell [COMMAND ...]     run a shell command, or start a line REPL
  push LOCAL REMOTE       copy a file to the device
  pull REMOTE LOCAL       copy a file from the device
  reboot [TARGET]         reboot the device
  stop                    stop the background daemon
  daemon                  run the daemon in the foreground

environment:
  termux-usb and Termux:API are required for USB access.
"""


def fingerprints(key: RsaKey) -> dict[str, str]:
    blob = key.public_key_blob()
    return {
        "sha256": hashlib.sha256(blob).hexdigest(),
        "md5": hashlib.md5(blob).hexdigest(),
    }


def show_key() -> int:
    key = RsaKey.load_or_create()
    values = fingerprints(key)
    print(f"key sha256={values['sha256']} md5={values['md5']} {key.comment}")
    return 0


def open_adb() -> tuple[AdbConnection, object]:
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
            if "adb" in info.modes:
                probes.append((info, usbfs))
            else:
                usbfs.close()
        if not probes:
            raise RuntimeError("no ADB USB device found")
        if len(probes) > 1:
            raise RuntimeError("multiple ADB USB devices; unplug one")
        info, usbfs = probes[0]
        transport = open_transport_from_info(usbfs, info, "adb")
        key = RsaKey.load_or_create()
        connection = AdbConnection(transport, key)
        state = connection.connect(30.0)
        if state.state != "device":
            connection.close()
            raise RuntimeError(f"ADB device is {state.state}")
        return connection, info
    except Exception:
        for _info, usbfs in probes:
            try:
                usbfs.close()
            except Exception:
                pass
        raise


def recv_json(sock: socket.socket) -> dict:
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("client closed")
        header += chunk
    length = struct.unpack(">I", header)[0]
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ConnectionError("client closed")
        body += chunk
    return json.loads(body.decode("utf-8"))


def send_json(sock: socket.socket, value: dict) -> None:
    body = json.dumps(value).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def request(payload: dict, timeout: float = 30.0) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(SOCKET_PATH))
        send_json(sock, payload)
        return recv_json(sock)
    finally:
        sock.close()


def run_daemon() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    key = RsaKey.load_or_create()
    key_ids = fingerprints(key)
    connection, info = open_adb()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)
    server.listen(4)
    print(
        f"daemon ready {info.display_serial} "
        f"key sha256={key_ids['sha256']} md5={key_ids['md5']}",
        flush=True,
    )
    try:
        while True:
            client, _address = server.accept()
            try:
                request_value = recv_json(client)
                op = request_value.get("op")
                if op == "status":
                    send_json(
                        client,
                        {
                            "ok": True,
                            "serial": info.display_serial,
                            "state": connection.state,
                            "path": info.path,
                            "key_sha256": key_ids["sha256"],
                            "key_md5": key_ids["md5"],
                        },
                    )
                elif op == "reboot":
                    target = str(request_value.get("target") or "")
                    stream = connection.open_service("reboot:" + target)
                    try:
                        stream.read_all(timeout=3.0)
                    except Exception:
                        pass
                    finally:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    send_json(client, {"ok": True, "target": target})
                    return 0
                elif op == "push":
                    local_path = str(request_value.get("local") or "")
                    remote_path = str(request_value.get("remote") or "")
                    push_file(connection, local_path, remote_path)
                    send_json(client, {"ok": True})
                elif op == "pull":
                    remote_path = str(request_value.get("remote") or "")
                    local_path = str(request_value.get("local") or "")
                    pull_file(connection, remote_path, local_path)
                    send_json(client, {"ok": True})
                elif op == "shell":
                    command = str(request_value.get("command") or "")
                    stream = connection.open_service("shell:" + command)
                    try:
                        output = stream.read_all(timeout=30.0)
                    finally:
                        stream.close()
                    send_json(
                        client,
                        {
                            "ok": True,
                            "output": output.decode("utf-8", errors="replace"),
                        },
                    )
                elif op == "stop":
                    send_json(client, {"ok": True})
                    return 0
                else:
                    send_json(client, {"ok": False, "error": f"unknown op {op!r}"})
            except Exception as exc:
                try:
                    send_json(client, {"ok": False, "error": str(exc)})
                except Exception:
                    pass
            finally:
                client.close()
    finally:
        server.close()
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass
        connection.close()


def request_started(payload: dict, timeout: float = 30.0) -> dict:
    try:
        return request(payload, timeout)
    except (FileNotFoundError, ConnectionRefusedError):
        start_daemon(quiet=True)
        return request(payload, timeout)


def start_daemon(quiet: bool = False) -> int:
    try:
        status = request({"op": "status"}, 1.0)
        if status.get("ok"):
            if not quiet:
                print(json.dumps(status, ensure_ascii=False))
            return 0
    except OSError:
        pass
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = open(LOG_PATH, "ab", buffering=0)
    try:
        process = subprocess.Popen(
            [sys.executable, os.path.abspath(sys.argv[0]), "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
        )
    finally:
        log.close()
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = LOG_PATH.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"daemon exited:\n{detail.strip()}")
        try:
            status = request({"op": "status"}, 1.0)
        except OSError:
            time.sleep(0.2)
            continue
        if status.get("ok"):
            if not quiet:
                print(json.dumps(status, ensure_ascii=False))
            return 0
        time.sleep(0.2)
    raise RuntimeError("timed out waiting for daemon")


def interactive_shell() -> int:
    request_started({"op": "status"}, 1.0)
    while True:
        try:
            line = input("$ ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        command = line.strip()
        if not command:
            continue
        if command in ("exit", "quit"):
            return 0
        result = request_started({"op": "shell", "command": command}, 60.0)
        if not result.get("ok"):
            print(result.get("error", "shell failed"), file=sys.stderr)
            continue
        print(result.get("output", ""), end="")


def list_devices() -> int:
    try:
        status = request({"op": "status"}, 1.0)
    except OSError:
        status = None
    if status is not None and status.get("ok"):
        print("List of devices attached")
        print(f"{status.get('serial', '?')}\t{status.get('state', '?')}")
        return 0

    rows: list[tuple[str, str]] = []
    for path in list_usb_devices():
        try:
            info, usbfs = probe_device(path)
        except Exception:
            continue
        try:
            if "adb" in info.modes:
                rows.append((info.display_serial, "offline"))
        finally:
            usbfs.close()
    print("List of devices attached")
    for serial, state in rows:
        print(f"{serial}\t{state}")
    return 0


def main() -> int:
    if "--usb-broker" in sys.argv:
        return broker_main()
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if command in ("help", "--help", "-h"):
            print(USAGE, end="")
            return 0
        if command in ("version", "--version"):
            print(f"py-adb {__version__}")
            return 0
        if command == "key":
            return show_key()
        if command == "daemon":
            return run_daemon()
        if command == "start":
            return start_daemon()
        if command == "devices":
            return list_devices()
        if command == "reboot":
            target = sys.argv[2] if len(sys.argv) > 2 else ""
            result = request_started({"op": "reboot", "target": target}, 10.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "reboot failed"))
            print(f"reboot {target or 'system'} sent")
            return 0
        if command == "status":
            try:
                print(json.dumps(request({"op": "status"}), ensure_ascii=False))
            except OSError:
                print("py-adb: daemon not running; run py-adb start", file=sys.stderr)
                return 1
            return 0
        if command == "shell":
            if len(sys.argv) == 2:
                return interactive_shell()
            result = request_started({"op": "shell", "command": " ".join(sys.argv[2:])}, 60.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "shell failed"))
            print(result.get("output", ""), end="")
            return 0
        if command in ("push", "pull"):
            if len(sys.argv) < 4:
                raise RuntimeError(f"usage: py-adb {command} SRC DST")
            if command == "push":
                payload = {"op": "push", "local": sys.argv[2], "remote": sys.argv[3]}
            else:
                payload = {"op": "pull", "remote": sys.argv[2], "local": sys.argv[3]}
            result = request_started(payload, 120.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", f"{command} failed"))
            print(f"{command} ok")
            return 0
        if command == "stop":
            try:
                print(json.dumps(request({"op": "stop"}), ensure_ascii=False))
            except OSError as exc:
                print(f"not running: {exc}", file=sys.stderr)
                return 1
            return 0
        raise RuntimeError(f"unknown command {command!r}")
    except Exception as exc:
        print(f"py-adb: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
