import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from termuxadb.usb.termux_usb import open_usb_device


FAKE_TERMUX_USB = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import os
    import subprocess
    import sys

    args = sys.argv[1:]
    callback = None
    index = 0
    while index < len(args):
        if args[index] == "-e":
            callback = args[index + 1]
            index += 2
        else:
            index += 1
    if not callback:
        sys.exit(2)
    fd = os.open("/dev/null", os.O_RDONLY)
    os.set_inheritable(fd, True)
    env = os.environ.copy()
    env["TERMUX_USB_FD"] = str(fd)
    result = subprocess.run(callback.split(), env=env, close_fds=False)
    os.close(fd)
    sys.exit(result.returncode)
    """
)


class TermuxBridgeTests(unittest.TestCase):
    def test_fd_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "termux-usb"
            script.write_text(FAKE_TERMUX_USB, encoding="utf-8")
            script.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = directory + os.pathsep + environment["PATH"]
            with mock.patch.dict(os.environ, environment, clear=True):
                fd = open_usb_device("/dev/bus/usb/001/002", timeout=10)
            try:
                self.assertGreaterEqual(fd, 0)
                self.assertEqual(os.read(fd, 1), b"")
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
