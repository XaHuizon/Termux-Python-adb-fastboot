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


USAGE = """用法: py-fastboot 命令

通过 Termux USB 使用 Fastboot。

命令:
  devices                    列出 Fastboot 设备
  getvar 变量                读取单个变量
  getvar all                 读取全部变量
  info [关键词]              读取全部变量，可按关键词过滤
  flash 分区 镜像            刷写分区
  reboot                     重启到 Android
  reboot-bootloader          重启到 bootloader

环境:
  需要 termux-usb 和 Termux:API 才能访问 USB。
"""


def error_text(exc: Exception) -> str:
    text = str(exc)
    if "termux-usb not found" in text:
        return "未找到 termux-usb；请安装 Termux:API 并安装 Termux:API 应用"
    if "timed out waiting for USB permission" in text:
        return "等待 USB 权限超时，请允许 Termux 的 USB 访问请求"
    if "USBDEVFS_CLAIMINTERFACE" in text and "busy" in text:
        return "USB 接口被占用，请先停止 adb 或 termux-adb 服务"
    if text == "no USB devices found":
        return "未找到 USB 设备"
    if text == "no Fastboot USB device found":
        return "未找到 Fastboot USB 设备"
    if "multiple Fastboot USB devices" in text:
        return "发现多个 Fastboot USB 设备，请拔掉多余的设备"
    if text.startswith("image not found: "):
        return "镜像不存在: " + text[len("image not found: ") :]
    if text.startswith("image is empty: "):
        return "镜像为空: " + text[len("image is empty: ") :]
    if text.startswith("image is ") and "max-download-size" in text:
        return "镜像大小超过 bootloader 限制: " + text
    if text == "Fastboot device closed the connection":
        return "Fastboot 设备已断开连接"
    if text == "Fastboot commands must be ASCII":
        return "Fastboot 命令必须为 ASCII"
    if text == "Fastboot command exceeds 64 bytes":
        return "Fastboot 命令超过 64 字节"
    if text == "Fastboot device did not enter data phase":
        return "Fastboot 设备未进入数据传输阶段"
    if text.startswith("invalid Fastboot DATA size: "):
        return "Fastboot 返回的 DATA 大小无效: " + text[len("invalid Fastboot DATA size: ") :]
    if text.startswith("Fastboot requested "):
        return "Fastboot 请求的镜像大小不匹配: " + text
    if text.startswith("unexpected Fastboot response "):
        return "Fastboot 返回了意外响应: " + text[len("unexpected Fastboot response ") :]
    if text.startswith("short Fastboot response: "):
        return "Fastboot 响应过短: " + text[len("short Fastboot response: ") :]
    if text.startswith("FAILED") or text.startswith("remote:"):
        return "Fastboot 操作失败: " + text
    return text


def normalize_info_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    if ":" not in line:
        return line
    name, value = line.split(":", 1)
    value = value.lstrip(" \t")
    return f"{name}: {value}" if value else f"{name}:"


def print_info_lines(lines: list[str], keyword: str | None = None) -> None:
    seen: set[str] = set()
    for raw in lines:
        line = normalize_info_line(raw)
        if not line or line in seen:
            continue
        seen.add(line)
        if keyword is not None and keyword.lower() not in line.lower():
            continue
        print(line)


def open_fastboot(info_callback=None) -> tuple[FastbootClient, object]:
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
            if "fastboot" in info.modes:
                probes.append((info, usbfs))
            else:
                usbfs.close()
        if not probes:
            raise RuntimeError("未找到 Fastboot USB 设备")
        if len(probes) > 1:
            raise RuntimeError("发现多个 Fastboot USB 设备，请拔掉多余的设备")
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


def read_all_info(client: FastbootClient) -> list[str]:
    lines: list[str] = []
    value = client.command("getvar:all", timeout=15000)
    text = value.decode("utf-8", errors="replace").strip()
    if text:
        lines.append(text)
    return lines


def fastboot_command(args: list[str]) -> int:
    action = args[0] if args else "devices"
    if action == "devices":
        client, info = open_fastboot()
        try:
            print(f"fastboot: {info.display_serial}")
        finally:
            client.close()
        return 0
    if action == "getvar":
        if len(args) < 2:
            raise RuntimeError("用法: py-fastboot getvar 变量")
        name = args[1]
        if name == "all":
            lines: list[str] = []
            client, _info = open_fastboot(lines.append)
            try:
                lines.extend(read_all_info(client))
            finally:
                client.close()
            print_info_lines(lines)
            return 0
        client, _info = open_fastboot()
        try:
            value = client.getvar(name)
        finally:
            client.close()
        line = normalize_info_line(
            f"{name}: {value.decode('utf-8', errors='replace').strip()}"
        )
        print(line)
        return 0
    if action == "info":
        keyword = args[1] if len(args) > 1 else None
        lines: list[str] = []
        client, _info = open_fastboot(lines.append)
        try:
            lines.extend(read_all_info(client))
        finally:
            client.close()
        print_info_lines(lines, keyword)
        return 0
    if action == "flash":
        if len(args) < 3:
            raise RuntimeError("用法: py-fastboot flash 分区 镜像")
        partition = args[1]
        image = args[2]
        client, _info = open_fastboot()
        try:
            client.flash(partition, image)
        finally:
            client.close()
        print(f"刷写成功: {partition}")
        return 0
    if action in ("reboot", "reboot-bootloader"):
        mode = "bootloader" if action == "reboot-bootloader" else ""
        client, _info = open_fastboot()
        try:
            client.reboot(mode)
        finally:
            client.close()
        print(f"已发送重启命令: {mode or '系统'}")
        return 0
    raise RuntimeError(f"未知命令: {action!r}")


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
        print(f"py-fastboot: {error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
