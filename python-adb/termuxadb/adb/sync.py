from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .transport import AdbStream


ID_STAT = b"STAT"
ID_LIST = b"LIST"
ID_SEND = b"SEND"
ID_RECV = b"RECV"
ID_DENT = b"DENT"
ID_DONE = b"DONE"
ID_DATA = b"DATA"
ID_OKAY = b"OKAY"
ID_FAIL = b"FAIL"
ID_QUIT = b"QUIT"

SYNC_DATA_MAX = 64 * 1024
DEFAULT_FILE_MODE = 0o100644


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    mode: int
    size: int
    mtime: int

    @property
    def is_directory(self) -> bool:
        return (self.mode & 0o170000) == 0o040000


@dataclass(frozen=True)
class FileStat:
    name: str
    mode: int
    size: int
    mtime: int


class SyncClient:
    def __init__(self, stream: AdbStream):
        self.stream = stream

    def _send_request(self, request_id: bytes, path: str) -> None:
        encoded = path.encode("utf-8")
        self.stream.send(request_id + struct.pack("<I", len(encoded)) + encoded)

    def _read_header(self) -> tuple[bytes, int]:
        header = self.stream.read_exact(8)
        request_id = header[:4]
        length = struct.unpack_from("<I", header, 4)[0]
        return request_id, length

    def _read_failure(self, length: int) -> None:
        message = self.stream.read_exact(length).decode("utf-8", errors="replace")
        raise SyncError(message)

    def stat(self, path: str) -> FileStat:
        self._send_request(ID_STAT, path)
        request_id, length = self._read_header()
        if request_id == ID_FAIL:
            self._read_failure(length)
        if request_id != ID_STAT or length < 12:
            raise SyncError(f"unexpected sync stat response: {request_id!r}")
        mode, size, mtime = struct.unpack("<III", self.stream.read_exact(12))
        name_length = length - 12
        name = self.stream.read_exact(name_length).decode("utf-8", errors="replace")
        return FileStat(name=name, mode=mode, size=size, mtime=mtime)

    def listdir(self, path: str) -> list[DirectoryEntry]:
        self._send_request(ID_LIST, path)
        entries: list[DirectoryEntry] = []
        while True:
            request_id, length = self._read_header()
            if request_id == ID_DONE:
                return entries
            if request_id == ID_FAIL:
                self._read_failure(length)
            if request_id != ID_DENT or length < 12:
                raise SyncError(f"unexpected sync list response: {request_id!r}")
            mode, size, mtime = struct.unpack("<III", self.stream.read_exact(12))
            name_length = length - 12
            name = self.stream.read_exact(name_length).decode("utf-8", errors="replace")
            entries.append(
                DirectoryEntry(name=name, mode=mode, size=size, mtime=mtime)
            )

    def push(
        self,
        local_path: str,
        remote_path: str,
        mode: int = DEFAULT_FILE_MODE,
        mtime: Optional[int] = None,
    ) -> None:
        path_and_mode = f"{remote_path},{mode}".encode("utf-8")
        self.stream.send(ID_SEND + struct.pack("<I", len(path_and_mode)) + path_and_mode)
        with open(local_path, "rb") as handle:
            while True:
                chunk = handle.read(SYNC_DATA_MAX)
                if not chunk:
                    break
                self.stream.send(ID_DATA + struct.pack("<I", len(chunk)) + chunk)
        timestamp = int(time.time()) if mtime is None else int(mtime)
        self.stream.send(ID_DONE + struct.pack("<I", timestamp & 0xFFFFFFFF))
        request_id, length = self._read_header()
        if request_id == ID_FAIL:
            self._read_failure(length)
        if request_id != ID_OKAY:
            raise SyncError(f"unexpected sync send response: {request_id!r}")

    def pull(self, remote_path: str, local_path: str) -> None:
        self._send_request(ID_RECV, remote_path)
        destination = Path(local_path)
        if destination.parent and not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as handle:
            while True:
                request_id, length = self._read_header()
                if request_id == ID_DONE:
                    return
                if request_id == ID_FAIL:
                    self._read_failure(length)
                if request_id != ID_DATA:
                    raise SyncError(f"unexpected sync recv response: {request_id!r}")
                remaining = length
                while remaining:
                    chunk = self.stream.read_exact(min(remaining, SYNC_DATA_MAX))
                    handle.write(chunk)
                    remaining -= len(chunk)

    def quit(self) -> None:
        try:
            self.stream.send(ID_QUIT + struct.pack("<I", 0))
        except Exception:
            pass


def _sync_stream(connection) -> AdbStream:
    return connection.open_service("sync:")


def push_file(
    connection,
    local_path: str,
    remote_path: str,
    mode: int = DEFAULT_FILE_MODE,
) -> None:
    stream = _sync_stream(connection)
    client = SyncClient(stream)
    try:
        client.push(local_path, remote_path, mode)
        client.quit()
    finally:
        stream.close()


def pull_file(connection, remote_path: str, local_path: str) -> None:
    stream = _sync_stream(connection)
    client = SyncClient(stream)
    try:
        client.pull(remote_path, local_path)
        client.quit()
    finally:
        stream.close()


def stat_file(connection, remote_path: str) -> FileStat:
    stream = _sync_stream(connection)
    client = SyncClient(stream)
    try:
        result = client.stat(remote_path)
        client.quit()
        return result
    finally:
        stream.close()


def list_directory(connection, remote_path: str) -> list[DirectoryEntry]:
    stream = _sync_stream(connection)
    client = SyncClient(stream)
    try:
        result = client.listdir(remote_path)
        client.quit()
        return result
    finally:
        stream.close()
