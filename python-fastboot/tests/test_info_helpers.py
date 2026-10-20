import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("pyfastboot_cli", ROOT / "__main__.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class FakeInfo:
    display_serial = "abc1234"


class FakeClient:
    def __init__(self):
        self.flashed = None
        self.closed = False
        self.callback = None

    def flash(self, partition, image, progress=None):
        self.flashed = (partition, image)

    def command(self, command, timeout=0):
        for line in ("unlocked:yes", "secure:yes"):
            if self.callback is not None:
                self.callback(line)
        return b""

    def close(self):
        self.closed = True


class InfoHelpersTests(unittest.TestCase):
    def test_devices_output(self):
        client = FakeClient()
        original = CLI.open_fastboot
        CLI.open_fastboot = lambda info_callback=None: (client, FakeInfo())
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                CLI.fastboot_command(["devices"])
        finally:
            CLI.open_fastboot = original
        self.assertEqual(output.getvalue(), "fastboot: abc1234\n")
        self.assertTrue(client.closed)

    def test_info_filter(self):
        client = FakeClient()
        original = CLI.open_fastboot

        def open_fake(info_callback=None):
            client.callback = info_callback
            return client, FakeInfo()

        CLI.open_fastboot = open_fake
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                CLI.fastboot_command(["info", "lock"])
        finally:
            CLI.open_fastboot = original
        self.assertEqual(output.getvalue(), "unlocked: yes\n")

    def test_flash(self):
        client = FakeClient()
        original = CLI.open_fastboot
        CLI.open_fastboot = lambda info_callback=None: (client, FakeInfo())
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                CLI.fastboot_command(["flash", "boot", "boot.img"])
        finally:
            CLI.open_fastboot = original
        self.assertEqual(client.flashed, ("boot", "boot.img"))
        self.assertEqual(output.getvalue(), "刷写成功: boot\n")

    def test_normalize_adds_space_after_colon(self):
        self.assertEqual(CLI.normalize_info_line("unlocked:yes"), "unlocked: yes")
        self.assertEqual(CLI.normalize_info_line("secure: yes"), "secure: yes")
        self.assertEqual(CLI.normalize_info_line("no-colon"), "no-colon")

    def test_keyword_filter_is_substring_match(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            CLI.print_info_lines(
                ["unlocked:yes", "secure:yes", "kernel:uefi"],
                "lock",
            )
        self.assertEqual(output.getvalue(), "unlocked: yes\n")

    def test_no_filter_prints_normalized_lines(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            CLI.print_info_lines(["unlocked:yes", "no-colon"])
        self.assertEqual(output.getvalue(), "unlocked: yes\nno-colon\n")


if __name__ == "__main__":
    unittest.main()
