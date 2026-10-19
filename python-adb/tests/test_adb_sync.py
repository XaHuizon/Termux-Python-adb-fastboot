import struct
import tempfile
import unittest
from pathlib import Path

from termuxadb.adb.sync import SyncClient


class FakeStream:
    def __init__(self, incoming: bytes = b""):
        self.outgoing = bytearray()
        self.incoming = bytearray(incoming)

    def send(self, data: bytes, timeout: float = 0) -> None:
        self.outgoing.extend(data)

    def read_exact(self, size: int, timeout: float = 0) -> bytes:
        if len(self.incoming) < size:
            raise AssertionError("not enough fake sync data")
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result


class SyncTests(unittest.TestCase):
    def test_push(self):
        stream = FakeStream(b"OKAY" + struct.pack("<I", 0))
        client = SyncClient(stream)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            path.write_bytes(b"hello")
            client.push(str(path), "/sdcard/file.txt")
        self.assertIn(b"SEND", stream.outgoing)
        self.assertIn(b"DATA", stream.outgoing)
        self.assertIn(b"hello", stream.outgoing)
        self.assertIn(b"DONE", stream.outgoing)

    def test_pull(self):
        path = "/sdcard/file.txt"
        data = b"hello"
        incoming = (
            b"DATA"
            + struct.pack("<I", len(data))
            + data
            + b"DONE"
            + struct.pack("<I", 0)
        )
        stream = FakeStream(incoming)
        client = SyncClient(stream)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "pulled.txt"
            client.pull(path, str(destination))
            self.assertEqual(destination.read_bytes(), b"hello")

    def test_stat(self):
        name = b"file.txt"
        payload = struct.pack("<III", 0o100644, 5, 123) + name
        incoming = b"STAT" + struct.pack("<I", len(payload)) + payload
        stream = FakeStream(incoming)
        result = SyncClient(stream).stat("/sdcard/file.txt")
        self.assertEqual(result.name, "file.txt")
        self.assertEqual(result.size, 5)
        self.assertEqual(result.mtime, 123)


if __name__ == "__main__":
    unittest.main()
