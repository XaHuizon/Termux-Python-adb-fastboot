import ctypes
import unittest

from termuxadb.usb import usbfs


class UsbfsRequestTests(unittest.TestCase):
    def test_control_request_number(self):
        size = ctypes.sizeof(usbfs._ControlTransfer)
        expected = 0xC0000000 | (size << 16) | (ord("U") << 8)
        self.assertEqual(usbfs.USBDEVFS_CONTROL, expected)
        self.assertEqual(size in (16, 24), True)

    def test_bulk_request_number(self):
        size = ctypes.sizeof(usbfs._BulkTransfer)
        expected = 0xC0000000 | (size << 16) | (ord("U") << 8) | 2
        self.assertEqual(usbfs.USBDEVFS_BULK, expected)

    def test_claim_request_number(self):
        self.assertEqual(usbfs.USBDEVFS_CLAIMINTERFACE, 0x8004550F)
        self.assertEqual(usbfs.USBDEVFS_RELEASEINTERFACE, 0x80045510)
        self.assertEqual(usbfs.USBDEVFS_CLEAR_HALT, 0x80045515)


if __name__ == "__main__":
    unittest.main()
