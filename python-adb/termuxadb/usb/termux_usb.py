from __future__ import annotations

import array
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Optional


class TermuxUsbError(RuntimeError):
    pass


def parse_usb_list(output: str) -> list[str]:
    text = output.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise TermuxUsbError(f"unexpected termux-usb output: {text[:200]}")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise TermuxUsbError(f"invalid termux-usb JSON: {text[:200]}") from exc
    if not isinstance(value, list):
        raise TermuxUsbError("termux-usb did not return a device list")
    return [item for item in value if isinstance(item, str)]


def parse_device_path(path: str) -> tuple[int, int]:
    match = re.match(r"^/dev/bus/usb/(\d+)/(\d+)$", path)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def fallback_serial(path: str) -> str:
    bus, address = parse_device_path(path)
    if bus and address:
        return f"usb:{bus:03d}:{address:03d}"
    return path


def send_fd(sock: socket.socket, fd: int, payload: bytes = b"") -> None:
    rights = array.array("i", [fd])
    sock.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])


def recv_fd(sock: socket.socket, payload_size: int = 64) -> tuple[bytes, Optional[int]]:
    message, ancillary, _flags, _address = sock.recvmsg(
        payload_size,
        socket.CMSG_SPACE(array.array("i").itemsize),
    )
    received_fd: Optional[int] = None
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            rights = array.array("i")
            usable = len(data) - (len(data) % rights.itemsize)
            rights.frombytes(data[:usable])
            if rights:
                received_fd = rights[0]
                break
    return message, received_fd


def _termux_usb_path() -> str:
    path = shutil.which("termux-usb")
    if path is None:
        raise TermuxUsbError(
            "termux-usb not found; install Termux:API with "
            "`pkg install termux-api` and install the Termux:API app"
        )
    return path


def _broker_command() -> list[str]:
    python = sys.executable or shutil.which("python3") or "python3"
    executable = os.path.abspath(sys.argv[0])
    if os.path.isfile(executable) and not os.path.basename(executable).startswith("python"):
        return [python, executable, "--usb-broker"]
    return [python, "-m", "termuxadb.usb.broker"]


def list_usb_devices() -> list[str]:
    termux_usb = _termux_usb_path()
    try:
        result = subprocess.run(
            [termux_usb, "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TermuxUsbError("termux-usb -l timed out") from exc
    except OSError as exc:
        raise TermuxUsbError(f"failed to run termux-usb: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise TermuxUsbError(message or "termux-usb -l failed")
    return parse_usb_list(result.stdout)


def _stop_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
    try:
        stdout, stderr = process.communicate(timeout=2)
    except (subprocess.TimeoutExpired, ValueError):
        process.kill()
        try:
            stdout, stderr = process.communicate()
        except ValueError:
            stdout, stderr = "", ""
    return stdout or "", stderr or ""


def open_usb_device(path: str, timeout: float = 45.0) -> int:
    termux_usb = _termux_usb_path()
    broker = _broker_command()
    token = os.urandom(16).hex()

    workdir = tempfile.mkdtemp(prefix="pyadb-usb-")
    socket_path = os.path.join(workdir, "sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    process: Optional[subprocess.Popen[str]] = None
    connection: Optional[socket.socket] = None
    received_fd: Optional[int] = None

    try:
        os.chmod(workdir, 0o700)
        server.bind(socket_path)
        server.listen(1)

        env = os.environ.copy()
        env["PYADB_BROKER_SOCKET"] = socket_path
        env["PYADB_BROKER_TOKEN"] = token
        package_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        if os.path.isdir(package_root):
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                package_root
                if not existing_pythonpath
                else package_root + os.pathsep + existing_pythonpath
            )

        callback = " ".join(broker)
        process = subprocess.Popen(
            [termux_usb, "-r", "-E", "-e", callback, path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        deadline = time.monotonic() + timeout
        while True:
            if process.poll() is not None:
                finished = process
                process = None
                stdout, stderr = _stop_process(finished)
                detail = (stderr or stdout).strip()
                raise TermuxUsbError(
                    detail or "termux-usb exited before returning a USB descriptor"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TermuxUsbError(
                    "timed out waiting for USB permission or the Termux:API callback"
                )
            readable, _writable, _exceptional = select.select(
                [server], [], [], min(remaining, 0.5)
            )
            if readable:
                connection, _address = server.accept()
                break

        connection.settimeout(10)
        message, received_fd = recv_fd(connection)
        if received_fd is None:
            raise TermuxUsbError("Termux:API did not return a USB file descriptor")
        if message != token.encode("ascii"):
            os.close(received_fd)
            received_fd = None
            raise TermuxUsbError("USB broker handshake failed")

        return received_fd
    except TermuxUsbError:
        raise
    except OSError as exc:
        raise TermuxUsbError(f"failed to open USB device {path}: {exc}") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        try:
            server.close()
        except OSError:
            pass
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass
        if process is not None:
            _stop_process(process)
