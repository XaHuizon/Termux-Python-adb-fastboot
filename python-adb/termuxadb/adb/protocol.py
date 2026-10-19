from __future__ import annotations

import struct
from dataclasses import dataclass
from ..usb.device import UsbTransport


A_SYNC = 0x434E5953
A_CNXN = 0x4E584E43
A_OPEN = 0x4E45504F
A_OKAY = 0x59414B4F
A_CLSE = 0x45534C43
A_WRTE = 0x45545257
A_AUTH = 0x48545541
A_STLS = 0x534C5453

A_VERSION = 0x01000000
A_MAXDATA = 256 * 1024
HOST_MAXDATA = 4096

AUTH_TOKEN = 1
AUTH_SIGNATURE = 2
AUTH_RSAPUBLICKEY = 3

HEADER = struct.Struct("<6I")
MAX_PAYLOAD = 16 * 1024 * 1024


class AdbProtocolError(RuntimeError):
    pass


class AdbConnectionClosed(AdbProtocolError):
    pass


@dataclass(frozen=True)
class AdbMessage:
    command: int
    arg0: int
    arg1: int
    payload: bytes

    @property
    def name(self) -> str:
        return {
            A_SYNC: "SYNC",
            A_CNXN: "CNXN",
            A_OPEN: "OPEN",
            A_OKAY: "OKAY",
            A_CLSE: "CLSE",
            A_WRTE: "WRTE",
            A_AUTH: "AUTH",
            A_STLS: "STLS",
        }.get(self.command, f"0x{self.command:08x}")


def checksum(payload: bytes) -> int:
    return sum(payload) & 0xFFFFFFFF


def pack_header(
    command: int,
    arg0: int,
    arg1: int,
    payload: bytes = b"",
) -> bytes:
    if len(payload) > 0xFFFFFFFF:
        raise AdbProtocolError("ADB payload too large")
    return HEADER.pack(
        command & 0xFFFFFFFF,
        arg0 & 0xFFFFFFFF,
        arg1 & 0xFFFFFFFF,
        len(payload),
        checksum(payload),
        (command ^ 0xFFFFFFFF) & 0xFFFFFFFF,
    )


def pack_message(
    command: int,
    arg0: int,
    arg1: int,
    payload: bytes = b"",
) -> bytes:
    return pack_header(command, arg0, arg1, payload) + payload


def send_message(
    transport: UsbTransport,
    command: int,
    arg0: int,
    arg1: int,
    payload: bytes = b"",
    timeout: int = 0,
) -> None:
    transport.write_all(pack_header(command, arg0, arg1, payload), timeout)
    if payload:
        transport.write_all(payload, timeout)


def read_message(transport: UsbTransport, timeout: int = 0) -> AdbMessage:
    header = transport.read_exact(HEADER.size, timeout)
    command, arg0, arg1, length, data_check, magic = HEADER.unpack(header)
    if magic != ((command ^ 0xFFFFFFFF) & 0xFFFFFFFF):
        raise AdbProtocolError("invalid ADB magic")
    if length > MAX_PAYLOAD:
        raise AdbProtocolError(f"ADB payload too large: {length}")
    payload = transport.read_exact(length, timeout) if length else b""
    if checksum(payload) != data_check:
        raise AdbProtocolError("ADB payload checksum mismatch")
    return AdbMessage(command, arg0, arg1, payload)


def service_payload(service: str) -> bytes:
    return service.encode("utf-8") + b"\x00"


def parse_banner(payload: bytes) -> tuple[str, dict[str, str]]:
    text = payload.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    if "::" not in text:
        return text, {}
    kind, rest = text.split("::", 1)
    properties: dict[str, str] = {}
    for item in rest.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            properties[key] = value
    return kind, properties


def is_closed_error(exc: Exception) -> bool:
    return isinstance(exc, (AdbConnectionClosed, ConnectionError, OSError))
