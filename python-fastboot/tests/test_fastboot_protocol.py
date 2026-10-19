import unittest

from termuxadb.fastboot.protocol import FastbootClient, FastbootFail
from termuxadb.usb.usbfs import UsbTimeout


def response(status: bytes, payload: bytes = b"") -> bytes:
    return (status + payload).ljust(64, b"\x00")


class FakeFastbootTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.writes = []
        self.closed = False

    def read(self, size: int, timeout: int = 0) -> bytes:
        if not self.responses:
            raise UsbTimeout("fake fastboot read", 110)
        return self.responses.pop(0)

    def write_all(self, data: bytes, timeout: int = 0) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:
        self.closed = True


class FastbootTests(unittest.TestCase):
    def test_getvar(self):
        infos = []
        transport = FakeFastbootTransport(
            [response(b"INFO", b"product:Phone"), response(b"OKAY", b"Phone")]
        )
        client = FastbootClient(transport, infos.append)
        value = client.getvar("product")
        self.assertEqual(value, b"Phone")
        self.assertEqual(infos, ["product:Phone"])
        self.assertEqual(transport.writes[0], b"getvar:product".ljust(64, b"\x00"))

    def test_fail(self):
        transport = FakeFastbootTransport([response(b"FAIL", b"bad partition")])
        client = FastbootClient(transport)
        with self.assertRaises(FastbootFail):
            client.command("flash:boot")

    def test_download(self):
        transport = FakeFastbootTransport(
            [response(b"DATA", b"00000003"), response(b"OKAY")]
        )
        client = FastbootClient(transport)
        client.download(b"abc")
        self.assertEqual(transport.writes[0], b"download:00000003".ljust(64, b"\x00"))
        self.assertEqual(transport.writes[1], b"abc")

    def test_flash(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "boot.img"
            image.write_bytes(b"abc")
            transport = FakeFastbootTransport(
                [
                    response(b"OKAY", b"00001000"),
                    response(b"DATA", b"00000003"),
                    response(b"OKAY"),
                    response(b"OKAY"),
                ]
            )
            client = FastbootClient(transport)
            client.flash("boot", str(image))
            commands = [write.rstrip(b"\x00") for write in transport.writes if len(write) == 64]
            self.assertIn(b"getvar:max-download-size", commands)
            self.assertIn(b"download:00000003", commands)
            self.assertIn(b"flash:boot", commands)


if __name__ == "__main__":
    unittest.main()
