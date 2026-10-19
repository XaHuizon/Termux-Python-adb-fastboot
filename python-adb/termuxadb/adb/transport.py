from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..usb.device import UsbTransport
from ..usb.usbfs import UsbStall, UsbTimeout, UsbfsError
from . import protocol
from .crypto import RsaKey
from .protocol import AdbMessage, AdbProtocolError, A_AUTH, A_CNXN, A_OKAY, A_OPEN, A_WRTE, A_CLSE, A_STLS


class AdbAuthError(AdbProtocolError):
    pass


class AdbTimeout(AdbProtocolError):
    pass


@dataclass(frozen=True)
class AdbDeviceInfo:
    state: str
    banner: str
    properties: dict[str, str]


class AdbConnection:
    def __init__(self, transport: UsbTransport, key: RsaKey):
        self.transport = transport
        self.key = key
        self.state = "offline"
        self.banner = ""
        self.properties: dict[str, str] = {}
        self.max_payload = protocol.HOST_MAXDATA
        self._next_local_id = 1
        self._streams: dict[int, AdbStream] = {}
        self._write_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._reader_error: Optional[BaseException] = None
        self._closed = False
        self._signature_sent = False
        self._public_key_sent = False

    @property
    def connected(self) -> bool:
        return self.state == "device"

    def send_packet(
        self,
        command: int,
        arg0: int,
        arg1: int,
        payload: bytes = b"",
        timeout: int = 5000,
    ) -> None:
        if self._closed:
            raise AdbProtocolError("ADB connection is closed")
        with self._write_lock:
            protocol.send_message(self.transport, command, arg0, arg1, payload, timeout)

    def connect(self, timeout: float = 30.0) -> AdbDeviceInfo:
        self.transport.drain_input(100)
        self.send_packet(
            A_CNXN,
            protocol.A_VERSION,
            protocol.HOST_MAXDATA,
            b"host::\x00",
        )

        while True:
            try:
                message = protocol.read_message(self.transport, int(timeout * 1000))
            except UsbTimeout:
                self.state = (
                    "unauthorized"
                    if self._signature_sent or self._public_key_sent
                    else "offline"
                )
                return self._info()
            except (UsbStall, UsbfsError):
                if self._public_key_sent:
                    self.state = "unauthorized"
                    return self._info()
                raise

            if message.command == A_CNXN:
                self.max_payload = min(protocol.A_MAXDATA, message.arg1 or protocol.HOST_MAXDATA)
                self.banner, self.properties = protocol.parse_banner(message.payload)
                self.state = "device"
                return self._info()

            if message.command == A_AUTH and message.arg0 == protocol.AUTH_TOKEN:
                if not self._signature_sent:
                    signature = self.key.sign_sha1(message.payload)
                    self.send_packet(
                        A_AUTH,
                        protocol.AUTH_SIGNATURE,
                        0,
                        signature,
                    )
                    self._signature_sent = True
                    continue
                if not self._public_key_sent:
                    self.send_packet(
                        A_AUTH,
                        protocol.AUTH_RSAPUBLICKEY,
                        0,
                        self.key.public_key_payload(),
                    )
                    self._public_key_sent = True
                    self.state = "unauthorized"
                    continue
                self.state = "unauthorized"
                return self._info()

            if message.command == A_STLS:
                raise AdbAuthError("device requires ADB TLS, which is not supported")

            if message.command == A_CLSE:
                self.state = "offline"
                return self._info()

        # The loop only exits through a return or exception.

    def _info(self) -> AdbDeviceInfo:
        return AdbDeviceInfo(
            state=self.state,
            banner=self.banner,
            properties=dict(self.properties),
        )

    def _ensure_reader(self) -> None:
        if self._reader_thread is not None:
            return
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="pyadb-usb-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set():
            try:
                message = protocol.read_message(self.transport, 5000)
            except UsbTimeout:
                continue
            except BaseException as exc:
                self._reader_error = exc
                for stream in list(self._streams.values()):
                    stream._on_error(exc)
                return
            try:
                self._dispatch(message)
            except BaseException as exc:
                self._reader_error = exc
                for stream in list(self._streams.values()):
                    stream._on_error(exc)
                return

    def _dispatch(self, message: AdbMessage) -> None:
        if message.command == A_OKAY:
            stream = self._streams.get(message.arg1)
            if stream is not None:
                stream.remote_id = message.arg0
                stream._on_okay()
            return
        if message.command == A_WRTE:
            stream = self._streams.get(message.arg1)
            if stream is not None:
                stream._on_write(message.payload)
                self.send_packet(A_OKAY, stream.local_id, stream.remote_id)
            return
        if message.command == A_CLSE:
            stream = self._streams.get(message.arg1)
            if stream is not None:
                stream._on_close()
            return

    def open_service(self, service: str, timeout: float = 15.0) -> "AdbStream":
        if not self.connected:
            raise AdbAuthError(f"ADB device is {self.state}, not ready")
        local_id = self._next_local_id
        self._next_local_id += 1
        stream = AdbStream(self, local_id, 0)
        self._streams[local_id] = stream
        self.send_packet(
            A_OPEN,
            local_id,
            0,
            protocol.service_payload(service),
        )
        self._ensure_reader()

        deadline = time.monotonic() + timeout
        with stream._condition:
            while True:
                if stream._okay_event.is_set():
                    return stream
                if stream._error is not None:
                    self._streams.pop(local_id, None)
                    raise stream._error
                if stream._closed:
                    self._streams.pop(local_id, None)
                    raise AdbProtocolError(f"ADB service {service!r} was closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._streams.pop(local_id, None)
                    raise AdbTimeout(f"timed out opening ADB service {service!r}")
                stream._condition.wait(min(remaining, 0.5))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader_stop.set()
        for stream in list(self._streams.values()):
            try:
                stream.close()
            except Exception:
                pass
        self._streams.clear()
        try:
            self.transport.close()
        except Exception:
            pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)

    def __enter__(self) -> "AdbConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class AdbStream:
    def __init__(self, connection: AdbConnection, local_id: int, remote_id: int):
        self.connection = connection
        self.local_id = local_id
        self.remote_id = remote_id
        self._buffer = bytearray()
        self._condition = threading.Condition()
        self._okay_event = threading.Event()
        self._closed = False
        self._error: Optional[BaseException] = None

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _on_write(self, payload: bytes) -> None:
        with self._condition:
            self._buffer.extend(payload)
            self._condition.notify_all()

    def _on_okay(self) -> None:
        with self._condition:
            self._okay_event.set()
            self._condition.notify_all()

    def _on_close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _on_error(self, error: BaseException) -> None:
        with self._condition:
            self._error = error
            self._closed = True
            self._condition.notify_all()

    def send(self, data: bytes, timeout: float = 15.0) -> None:
        if not data:
            return
        if self.closed:
            raise AdbProtocolError("ADB stream is closed")
        chunk_size = max(1, min(self.connection.max_payload, 65536))
        deadline = time.monotonic() + timeout
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdbTimeout("timed out sending ADB data")
            self._okay_event.clear()
            self.connection.send_packet(
                A_WRTE,
                self.local_id,
                self.remote_id,
                chunk,
            )
            if not self._okay_event.wait(remaining):
                raise AdbTimeout("timed out waiting for ADB OKAY")

    def read(self, size: int = 65536, timeout: float = 30.0) -> bytes:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._buffer:
                    data = bytes(self._buffer[:size])
                    del self._buffer[:size]
                    return data
                if self._error is not None:
                    raise self._error
                if self._closed:
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdbTimeout("timed out reading ADB data")
                self._condition.wait(remaining)

    def read_exact(self, size: int, timeout: float = 30.0) -> bytes:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._buffer) < size:
                if self._error is not None:
                    raise self._error
                if self._closed:
                    raise AdbProtocolError("ADB stream closed before enough data arrived")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdbTimeout("timed out reading ADB data")
                self._condition.wait(remaining)
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            return data

    def read_all(self, timeout: float = 60.0) -> bytes:
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdbTimeout("timed out reading ADB stream")
            chunk = self.read(65536, remaining)
            if chunk:
                chunks.append(chunk)
                continue
            if self.closed:
                break
        return b"".join(chunks)

    def close(self) -> None:
        with self._condition:
            was_closed = self._closed
            self._closed = True
            self._condition.notify_all()
        if not was_closed:
            try:
                self.connection.send_packet(
                    A_CLSE,
                    self.local_id,
                    self.remote_id,
                )
            except Exception:
                pass
        self.connection._streams.pop(self.local_id, None)
