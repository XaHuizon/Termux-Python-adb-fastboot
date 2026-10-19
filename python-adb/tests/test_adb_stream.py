import struct
import threading
import time
import unittest

from termuxadb.adb import protocol
from termuxadb.adb.crypto import RsaKey
from termuxadb.adb.transport import AdbConnection
from termuxadb.usb.usbfs import UsbTimeout


class QueueTransport:
    def __init__(self, incoming: bytes = b""):
        self.buffer = bytearray(incoming)
        self.outgoing = bytearray()
        self.lock = threading.Lock()
        self.closed = False

    def append(self, data: bytes) -> None:
        with self.lock:
            self.buffer.extend(data)

    def read_exact(self, size: int, timeout: int = 0) -> bytes:
        with self.lock:
            if len(self.buffer) < size:
                raise UsbTimeout("queue empty", 110)
            result = bytes(self.buffer[:size])
            del self.buffer[:size]
            return result

    def write_all(self, data: bytes, timeout: int = 0) -> None:
        with self.lock:
            self.outgoing.extend(data)

    def drain_input(self, timeout: int = 100) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def messages(data: bytes):
    result = []
    offset = 0
    while offset < len(data):
        header = data[offset : offset + 24]
        length = struct.unpack_from("<I", header, 12)[0]
        result.append(
            protocol.read_message(QueueTransport(data[offset : offset + 24 + length]))
        )
        offset += 24 + length
    return result


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RsaKey.generate("test@localhost")

    def test_service_data_and_okay(self):
        cnxn = protocol.pack_message(protocol.A_CNXN, protocol.A_VERSION, 4096, b"device::\x00")
        okay = protocol.pack_message(protocol.A_OKAY, 7, 1, b"")
        transport = QueueTransport(cnxn + okay)
        connection = AdbConnection(transport, self.key)
        self.assertEqual(connection.connect(timeout=1).state, "device")

        stream = connection.open_service("shell:echo", timeout=1)
        transport.append(protocol.pack_message(protocol.A_WRTE, 7, 1, b"hello"))
        transport.append(protocol.pack_message(protocol.A_CLSE, 7, 1, b""))
        self.assertEqual(stream.read_all(timeout=2), b"hello")

        stream.close()
        connection.close()
        parsed = messages(bytes(transport.outgoing))
        self.assertEqual(parsed[0].command, protocol.A_CNXN)
        self.assertEqual(parsed[1].command, protocol.A_OPEN)
        self.assertEqual(parsed[1].payload, b"shell:echo\x00")
        okay_packets = [message for message in parsed if message.command == protocol.A_OKAY]
        self.assertTrue(okay_packets)
        self.assertEqual(okay_packets[0].arg0, 1)
        self.assertEqual(okay_packets[0].arg1, 7)

    def test_send_waits_for_okay(self):
        cnxn = protocol.pack_message(protocol.A_CNXN, protocol.A_VERSION, 4096, b"device::\x00")
        okay = protocol.pack_message(protocol.A_OKAY, 7, 1, b"")
        transport = QueueTransport(cnxn + okay)
        connection = AdbConnection(transport, self.key)
        connection.connect(timeout=1)
        stream = connection.open_service("shell:", timeout=1)

        def acknowledge():
            time.sleep(0.05)
            transport.append(protocol.pack_message(protocol.A_OKAY, 7, 1, b""))

        thread = threading.Thread(target=acknowledge)
        thread.start()
        stream.send(b"input")
        thread.join()
        stream.close()
        connection.close()

        parsed = messages(bytes(transport.outgoing))
        writes = [message for message in parsed if message.command == protocol.A_WRTE]
        self.assertEqual(writes[0].arg0, 1)
        self.assertEqual(writes[0].arg1, 7)
        self.assertEqual(writes[0].payload, b"input")


if __name__ == "__main__":
    unittest.main()
