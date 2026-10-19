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

USAGE = """用法: py-adb 命令

通过 Termux USB 使用 ADB。

命令:
  key                     显示 ADB 公钥指纹
  start                   启动后台守护进程
  status                  显示守护进程和设备状态
  devices                 列出 ADB 设备
  shell [命令 ...]         执行 shell 命令，或进入逐行 REPL
  push 本地 远程           复制文件到设备
  pull 远程 本地           从设备复制文件
  reboot [目标]            重启设备
  stop                    停止后台守护进程
  daemon                  在前台运行守护进程

环境:
  需要 termux-usb 和 Termux:API 才能访问 USB。
"""


def fingerprints(key: RsaKey) -> dict[str, str]:
    blob = key.public_key_blob()
    return {
        "sha256": hashlib.sha256(blob).hexdigest(),
        "md5": hashlib.md5(blob).hexdigest(),
    }


def error_text(exc: Exception) -> str:
    text = str(exc)
    if "termux-usb not found" in text:
        return "未找到 termux-usb；请安装 Termux:API 并安装 Termux:API 应用"
    if "timed out waiting for USB permission" in text:
        return "等待 USB 权限超时，请允许 Termux 的 USB 访问请求"
    if "USBDEVFS_CLAIMINTERFACE" in text and "busy" in text:
        return "USB 接口被占用，请先停止 adb 或 termux-adb 服务"
    return text


def show_key() -> int:
    key = RsaKey.load_or_create()
    values = fingerprints(key)
    print(f"密钥 SHA256={values['sha256']} MD5={values['md5']} 用户={key.comment}")
    return 0


def open_adb() -> tuple[AdbConnection, object]:
    paths = list_usb_devices()
    if not paths:
        raise RuntimeError("未找到 USB 设备")
    probes = []
    try:
        for path in paths:
            try:
                info, usbfs = probe_device(path)
            except Exception as exc:
                print(f"探测 {path}: {error_text(exc)}", file=sys.stderr)
                continue
            if "adb" in info.modes:
                probes.append((info, usbfs))
            else:
                usbfs.close()
        if not probes:
            raise RuntimeError("未找到 ADB USB 设备")
        if len(probes) > 1:
            raise RuntimeError("发现多个 ADB USB 设备，请拔掉多余的设备")
        info, usbfs = probes[0]
        transport = open_transport_from_info(usbfs, info, "adb")
        key = RsaKey.load_or_create()
        connection = AdbConnection(transport, key)
        state = connection.connect(30.0)
        if state.state != "device":
            connection.close()
            raise RuntimeError(f"ADB 设备状态为 {state.state}")
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
            raise ConnectionError("连接已关闭")
        header += chunk
    length = struct.unpack(">I", header)[0]
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ConnectionError("连接已关闭")
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
        f"守护进程就绪 {info.display_serial} "
        f"密钥 SHA256={key_ids['sha256']} MD5={key_ids['md5']}",
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
                    send_json(client, {"ok": False, "error": f"未知操作 {op!r}"})
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


def show_start_result(status: dict) -> None:
    print(f"设备状态: {status.get('state', '?')}")
    print("守护进程启动成功")


def start_daemon(quiet: bool = False) -> int:
    try:
        status = request({"op": "status"}, 1.0)
        if status.get("ok"):
            if not quiet:
                show_start_result(status)
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
            raise RuntimeError(f"守护进程退出：\n{detail.strip()}")
        try:
            status = request({"op": "status"}, 1.0)
        except OSError:
            time.sleep(0.2)
            continue
        if status.get("ok"):
            if not quiet:
                show_start_result(status)
            return 0
        time.sleep(0.2)
    raise RuntimeError("等待守护进程启动超时")


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
            print(result.get("error", "shell 执行失败"), file=sys.stderr)
            continue
        print(result.get("output", ""), end="")


def list_devices() -> int:
    try:
        status = request({"op": "status"}, 1.0)
    except OSError:
        status = None
    if status is not None and status.get("ok"):
        print(f"{status.get('state', '?')}: {status.get('serial', '?')}")
        return 0

    rows: list[str] = []
    for path in list_usb_devices():
        try:
            info, usbfs = probe_device(path)
        except Exception:
            continue
        try:
            if "adb" in info.modes:
                rows.append(f"offline: {info.display_serial}")
        finally:
            usbfs.close()
    for row in rows:
        print(row)
    return 0


def show_status() -> int:
    status = request({"op": "status"}, 10.0)
    print(f"设备状态: {status.get('state', '?')}")
    print(f"序列号: {status.get('serial', '?')}")
    print(f"USB 路径: {status.get('path', '?')}")
    print(f"密钥 SHA256: {status.get('key_sha256', '?')}")
    print(f"密钥 MD5: {status.get('key_md5', '?')}")
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
                raise RuntimeError(result.get("error", "重启失败"))
            print(f"已发送重启命令: {target or '系统'}")
            return 0
        if command == "status":
            try:
                return show_status()
            except OSError:
                print("py-adb: 守护进程未运行，请先执行 py-adb start", file=sys.stderr)
                return 1
        if command == "shell":
            if len(sys.argv) == 2:
                return interactive_shell()
            result = request_started({"op": "shell", "command": " ".join(sys.argv[2:])}, 60.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "shell 执行失败"))
            print(result.get("output", ""), end="")
            return 0
        if command in ("push", "pull"):
            if len(sys.argv) < 4:
                raise RuntimeError(f"用法: py-adb {command} 源 目标")
            if command == "push":
                payload = {"op": "push", "local": sys.argv[2], "remote": sys.argv[3]}
            else:
                payload = {"op": "pull", "remote": sys.argv[2], "local": sys.argv[3]}
            result = request_started(payload, 120.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "推送失败" if command == "push" else "拉取失败"))
            print("推送成功" if command == "push" else "拉取成功")
            return 0
        if command == "stop":
            try:
                request({"op": "stop"})
            except OSError:
                print("py-adb: 守护进程未运行", file=sys.stderr)
                return 1
            print("守护进程已停止")
            return 0
        raise RuntimeError(f"未知命令: {command!r}")
    except Exception as exc:
        print(f"py-adb: {error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
