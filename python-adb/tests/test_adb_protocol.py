import struct
import unittest

from termuxadb.adb import protocol
from termuxadb.adb.crypto import RsaKey
from termuxadb.adb.transport import AdbConnection
from termuxadb.usb.usbfs import UsbTimeout


def parse_messages(data: bytes):
    messages = []
    offset = 0
    while offset < len(data):
        header = data[offset : offset + 24]
        length = struct.unpack_from("<I", header, 12)[0]
        end = offset + 24 + length
        messages.append(protocol.read_message(FakeTransport(data[offset:end])))
        offset = end
    return messages


class FakeTransport:
    def __init__(self, incoming: bytes = b""):
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()

    def read_exact(self, size: int, timeout: int = 0) -> bytes:
        if len(self.incoming) < size:
            raise UsbTimeout("fake read", 110)
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def write_all(self, data: bytes, timeout: int = 0) -> None:
        self.outgoing.extend(data)

    def drain_input(self, timeout: int = 100) -> None:
        pass


class ProtocolTests(unittest.TestCase):
    def test_pack_and_read_message(self):
        transport = FakeTransport(protocol.pack_message(protocol.A_CNXN, 1, 2, b"abc"))
        message = protocol.read_message(transport)
        self.assertEqual(message.command, protocol.A_CNXN)
        self.assertEqual(message.arg0, 1)
        self.assertEqual(message.arg1, 2)
        self.assertEqual(message.payload, b"abc")

    def test_checksum_is_byte_sum(self):
        self.assertEqual(protocol.checksum(b"\x01\x02\xff"), 0x102)

    def test_bad_magic(self):
        header = struct.pack("<6I", protocol.A_CNXN, 0, 0, 0, 0, 0)
        with self.assertRaises(protocol.AdbProtocolError):
            protocol.read_message(FakeTransport(header))

    def test_parse_banner(self):
        kind, properties = protocol.parse_banner(
            b"device::ro.product.model=Phone;ro.product.device=phone;\x00"
        )
        self.assertEqual(kind, "device")
        self.assertEqual(properties["ro.product.model"], "Phone")


class HandshakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RsaKey.generate("test@localhost")

    def test_cnxn_without_auth(self):
        device_cnxn = protocol.pack_message(
            protocol.A_CNXN,
            protocol.A_VERSION,
            4096,
            b"device::ro.product.model=Phone;\x00",
        )
        transport = FakeTransport(device_cnxn)
        connection = AdbConnection(transport, self.key)
        info = connection.connect(timeout=1.0)
        self.assertEqual(info.state, "device")
        self.assertEqual(info.properties["ro.product.model"], "Phone")
        sent = protocol.read_message(FakeTransport(transport.outgoing))
        self.assertEqual(sent.command, protocol.A_CNXN)
        connection.close()

    def test_auth_signature(self):
        token = b"0123456789abcdefghij"
        auth = protocol.pack_message(protocol.A_AUTH, protocol.AUTH_TOKEN, 0, token)
        device_cnxn = protocol.pack_message(
            protocol.A_CNXN,
            protocol.A_VERSION,
            4096,
            b"device::\x00",
        )
        transport = FakeTransport(auth + device_cnxn)
        connection = AdbConnection(transport, self.key)
        info = connection.connect(timeout=1.0)
        self.assertEqual(info.state, "device")

        messages = parse_messages(bytes(transport.outgoing))
        self.assertEqual(messages[0].command, protocol.A_CNXN)
        signature_message = messages[1]
        self.assertEqual(signature_message.command, protocol.A_AUTH)
        self.assertEqual(signature_message.arg0, protocol.AUTH_SIGNATURE)
        self.assertTrue(self.key.verify_sha1(token, signature_message.payload))
        connection.close()

    def test_auth_public_key_then_unauthorized(self):
        token = b"0123456789abcdefghij"
        auth = protocol.pack_message(protocol.A_AUTH, protocol.AUTH_TOKEN, 0, token)
        transport = FakeTransport(auth + auth)
        connection = AdbConnection(transport, self.key)
        info = connection.connect(timeout=1.0)
        self.assertEqual(info.state, "unauthorized")

        messages = []
        data = bytes(transport.outgoing)
        offset = 0
        while offset < len(data):
            header = data[offset : offset + 24]
            length = struct.unpack_from("<I", header, 12)[0]
            messages.append(protocol.read_message(FakeTransport(data[offset : offset + 24 + length])))
            offset += 24 + length
        self.assertEqual(messages[1].arg0, protocol.AUTH_SIGNATURE)
        self.assertEqual(messages[2].arg0, protocol.AUTH_RSAPUBLICKEY)
        self.assertTrue(messages[2].payload.endswith(b"\x00"))
        connection.close()


if __name__ == "__main__":
    unittest.main()
