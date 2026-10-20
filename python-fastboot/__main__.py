#!/data/data/com.termux/files/usr/bin/python3
from __future__ import annotations

import os
import shutil
import sys
import time
import unicodedata
from typing import Optional

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


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _clip_left(text: str, width: int) -> str:
    """只保留尾部，超出宽度的前缀用省略号代替。"""
    if _display_width(text) <= width:
        return text
    if width <= 1:
        return "…"
    kept: list[str] = []
    used = 1
    for ch in reversed(text):
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > width:
            break
        kept.append(ch)
        used += step
    return "…" + "".join(reversed(kept))


def format_size(value: int) -> str:
    size = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1000
    return f"{size:.1f}GB"


class ProgressBar:
    MIN_BAR = 5
    MAX_BAR = 30

    def __init__(self, label: str, source: str, target: str):
        self.label = label
        self.source = source
        self.target = target
        self.started = False
        self.drawn = False
        self.started_at = 0.0
        self.done = 0
        self.total = 0

    @staticmethod
    def _interactive() -> bool:
        try:
            return bool(sys.stdout.isatty())
        except (AttributeError, ValueError):
            return False

    def _bar_width(self) -> int:
        columns = shutil.get_terminal_size((80, 24)).columns
        return max(self.MIN_BAR, min(self.MAX_BAR, columns - 8))

    def _header(self) -> str:
        columns = shutil.get_terminal_size((80, 24)).columns
        prefix = f"{self.label}: "
        separator = " -> "
        room = columns - _display_width(prefix) - _display_width(separator)
        if room >= _display_width(self.source) + _display_width(self.target):
            return f"{prefix}{self.source}{separator}{self.target}"
        target = _clip_left(self.target, max(1, room // 2))
        source = _clip_left(self.source, max(1, room - _display_width(target)))
        return f"{prefix}{source}{separator}{target}"

    def _bar_line(self, width: int) -> str:
        ratio = 0.0 if self.total <= 0 else min(self.done / self.total, 1.0)
        filled = int(ratio * width)
        return "[" + "#" * filled + "-" * (width - filled) + f"][{ratio * 100:3.0f}%]"

    def _stats_line(self) -> str:
        if self.total <= 0:
            return f"- {format_size(self.done)}"
        elapsed = time.monotonic() - self.started_at
        speed = self.done / elapsed / 1_000_000 if elapsed > 0.05 else 0.0
        return f"- {format_size(self.done)}/{format_size(self.total)}  {speed:.1f}MB/s"

    def _redraw(self, bar_line: str, stats_line: str) -> None:
        if not self.drawn:
            sys.stdout.write(bar_line + "\n" + stats_line)
            self.drawn = True
        else:
            sys.stdout.write("\r\x1b[2K\x1b[1A\r\x1b[2K" + bar_line + "\n" + stats_line)
        sys.stdout.flush()

    def update(self, done: int, total: int) -> None:
        if not self.started:
            self.started = True
            self.started_at = time.monotonic()
            if self._interactive():
                print(self._header(), flush=True)
        self.done = max(0, int(done))
        self.total = max(0, int(total))
        if self._interactive():
            self._redraw(self._bar_line(self._bar_width()), self._stats_line())

    def finish(self, message: Optional[str] = None) -> None:
        if not self.started:
            if message:
                print(message)
            return
        self.started = False
        if not self._interactive():
            if message:
                print(message)
            return
        bar_line = self._bar_line(self._bar_width())
        self._redraw(bar_line, self._stats_line())
        sys.stdout.write("\n")
        if message:
            # [OKAY] 的右边缘与进度条右边缘对齐
            room = _display_width(bar_line) - _display_width(message) - 8
            if room >= 1:
                sys.stdout.write(f"{message} {'.' * room} [OKAY]\n")
            else:
                sys.stdout.write(f"{message} [OKAY]\n")
        sys.stdout.flush()
        self.drawn = False


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
        bar = ProgressBar("刷写", os.path.basename(image), partition)
        try:
            client.flash(partition, image, progress=bar.update)
        except Exception:
            bar.finish()
            raise
        else:
            bar.finish(f"刷写成功: {partition}")
        finally:
            client.close()
        return 0
    if action in ("reboot", "reboot-bootloader"):
        mode = "bootloader" if action == "reboot-bootloader" else ""
        client, _info = open_fastboot()
        try:
            client.reboot(mode)
        finally:
            client.close()
        print(f"- 已发送重启命令: [{mode or '系统'}]")
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
