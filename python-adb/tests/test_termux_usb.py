import os
import socket
import unittest

from termuxadb.usb.termux_usb import (
    fallback_serial,
    parse_device_path,
    parse_usb_list,
    recv_fd,
    send_fd,
)


class TermuxUsbTests(unittest.TestCase):
    def test_parse_device_path(self):
        self.assertEqual(parse_device_path("/dev/bus/usb/001/002"), (1, 2))
        self.assertEqual(parse_device_path("/dev/bus/usb/010/015"), (10, 15))
        self.assertEqual(parse_device_path("usb:1:2"), (0, 0))

    def test_fallback_serial(self):
        self.assertEqual(fallback_serial("/dev/bus/usb/001/002"), "usb:001:002")
        self.assertEqual(fallback_serial("/dev/bus/usb/010/015"), "usb:010:015")
        self.assertEqual(fallback_serial("unknown"), "unknown")

    def test_parse_usb_list(self):
        self.assertEqual(
            parse_usb_list('["/dev/bus/usb/001/002"]'),
            ["/dev/bus/usb/001/002"],
        )
        self.assertEqual(
            parse_usb_list('warning\n["/dev/bus/usb/001/002"]\n'),
            ["/dev/bus/usb/001/002"],
        )
        self.assertEqual(parse_usb_list("[]"), [])

    def test_fd_passing(self):
        read_fd, write_fd = os.pipe()
        sender, receiver = socket.socketpair()
        try:
            os.write(write_fd, b"hello")
            send_fd(sender, read_fd, b"token")
            message, received_fd = recv_fd(receiver)
            self.assertEqual(message, b"token")
            self.assertIsNotNone(received_fd)
            self.assertEqual(os.read(received_fd, 5), b"hello")
        finally:
            if "received_fd" in locals() and received_fd is not None:
                os.close(received_fd)
            sender.close()
            receiver.close()
            os.close(read_fd)
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
