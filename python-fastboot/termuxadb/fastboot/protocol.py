from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..usb.device import UsbTransport


FASTBOOT_PACKET_SIZE = 64
FASTBOOT_DATA_CHUNK = 64 * 1024


class FastbootError(RuntimeError):
    pass


class FastbootFail(FastbootError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class FastbootClient:
    def __init__(
        self,
        transport: UsbTransport,
        info_callback: Optional[Callable[[str], None]] = None,
    ):
        self.transport = transport
        self.info_callback = info_callback

    def _emit_info(self, message: bytes, text_mode: bool = False) -> None:
        text = message.decode("utf-8", errors="replace").rstrip("\x00")
        if self.info_callback is not None:
            self.info_callback(text)

    def _send_command(self, command: str, timeout: int = 5000) -> None:
        try:
            encoded = command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise FastbootError("Fastboot commands must be ASCII") from exc
        if len(encoded) > FASTBOOT_PACKET_SIZE:
            raise FastbootError("Fastboot command exceeds 64 bytes")
        packet = encoded.ljust(FASTBOOT_PACKET_SIZE, b"\x00")
        self.transport.write_all(packet, timeout)

    def _read_packet(self, timeout: int = 5000) -> bytes:
        packet = self.transport.read(FASTBOOT_PACKET_SIZE, timeout)
        if not packet:
            raise FastbootError("Fastboot device closed the connection")
        return packet.rstrip(b"\x00")

    def _read_status(self, timeout: int = 5000) -> tuple[bytes, bytes]:
        packet = self._read_packet(timeout)
        if len(packet) < 4:
            raise FastbootError(f"short Fastboot response: {packet!r}")
        return packet[:4], packet[4:]

    def _read_until_terminal(
        self,
        timeout: int = 5000,
        allow_data: bool = False,
    ) -> tuple[bytes, bytes]:
        while True:
            status, payload = self._read_status(timeout)
            if status == b"INFO":
                self._emit_info(payload)
                continue
            if status == b"TEXT":
                self._emit_info(payload, text_mode=True)
                continue
            if status == b"OKAY":
                return status, payload
            if status == b"FAIL":
                raise FastbootFail(payload.decode("utf-8", errors="replace"))
            if status == b"DATA" and allow_data:
                return status, payload
            raise FastbootError(
                f"unexpected Fastboot response {status!r}: "
                f"{payload.decode('utf-8', errors='replace')}"
            )

    def command(self, command: str, timeout: int = 5000) -> bytes:
        self._send_command(command, timeout)
        _status, payload = self._read_until_terminal(timeout)
        return payload

    def getvar(self, name: str, timeout: int = 5000) -> bytes:
        return self.command(f"getvar:{name}", timeout)

    def oem(self, argument: str, timeout: int = 10000) -> bytes:
        return self.command(f"oem {argument}", timeout)

    def flashing(self, argument: str, timeout: int = 10000) -> bytes:
        return self.command(f"flashing {argument}", timeout)

    def download(self, data: bytes, timeout: int = 30000) -> None:
        self._send_command(f"download:{len(data):08x}", timeout)
        status, payload = self._read_until_terminal(timeout, allow_data=True)
        if status != b"DATA":
            raise FastbootError("Fastboot device did not enter data phase")
        try:
            expected = int(payload.decode("ascii"), 16)
        except (ValueError, UnicodeDecodeError) as exc:
            raise FastbootError(f"invalid Fastboot DATA size: {payload!r}") from exc
        if expected != len(data):
            raise FastbootError(
                f"Fastboot requested {expected} bytes but image has {len(data)} bytes"
            )
        self.transport.write_all(data, timeout)
        self._read_until_terminal(timeout)

    def get_max_download_size(self, timeout: int = 5000) -> Optional[int]:
        try:
            value = self.getvar("max-download-size", timeout)
        except FastbootError:
            return None
        text = value.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return int(text, 0)
        except ValueError:
            try:
                return int(text, 16)
            except ValueError:
                return None

    def download_file(
        self,
        image_path: str,
        timeout: int = 60000,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        path = Path(image_path)
        if not path.is_file():
            raise FastbootError(f"image not found: {image_path}")
        size = path.stat().st_size
        if size == 0:
            raise FastbootError(f"image is empty: {image_path}")
        maximum = self.get_max_download_size()
        if maximum is not None and size > maximum:
            raise FastbootError(
                f"image is {size} bytes, bootloader max-download-size is {maximum}"
            )

        self._send_command(f"download:{size:08x}", timeout)
        status, payload = self._read_until_terminal(timeout, allow_data=True)
        if status != b"DATA":
            raise FastbootError("Fastboot device did not enter data phase")
        try:
            expected = int(payload.decode("ascii"), 16)
        except (ValueError, UnicodeDecodeError) as exc:
            raise FastbootError(f"invalid Fastboot DATA size: {payload!r}") from exc
        if expected != size:
            raise FastbootError(
                f"Fastboot requested {expected} bytes but image has {size} bytes"
            )
        sent = 0
        if progress is not None:
            progress(sent, size)
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(FASTBOOT_DATA_CHUNK)
                if not chunk:
                    break
                self.transport.write_all(chunk, timeout)
                sent += len(chunk)
                if progress is not None:
                    progress(sent, size)
        self._read_until_terminal(timeout)

    def flash(
        self,
        partition: str,
        image_path: str,
        timeout: int = 60000,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        self.download_file(image_path, timeout, progress=progress)
        return self.command(f"flash:{partition}", timeout)

    def reboot(self, mode: str = "", timeout: int = 2000) -> None:
        if mode == "bootloader":
            command = "reboot-bootloader"
        elif mode:
            command = f"reboot {mode}"
        else:
            command = "reboot"
        self._send_command(command, timeout)
        try:
            self._read_until_terminal(timeout)
        except FastbootFail:
            raise
        except Exception:
            pass

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "FastbootClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
