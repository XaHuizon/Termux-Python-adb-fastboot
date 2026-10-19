import os
import tempfile
import unittest
from pathlib import Path

from termuxadb.adb.crypto import RsaKey


class CryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RsaKey.generate("test@localhost")

    def test_public_key_layout(self):
        blob = self.key.public_key_blob()
        self.assertEqual(len(blob), 524)
        self.assertEqual(int.from_bytes(blob[0:4], "little"), 64)
        self.assertEqual(int.from_bytes(blob[-4:], "little"), 65537)
        self.assertEqual(int.from_bytes(blob[8:264], "little"), self.key.n)

    def test_signature(self):
        token = b"0123456789abcdefghij"
        signature = self.key.sign_sha1(token)
        self.assertEqual(len(signature), 256)
        self.assertTrue(self.key.verify_sha1(token, signature))
        self.assertFalse(self.key.verify_sha1(b"different", signature))

    def test_public_key_line(self):
        line = self.key.public_key_line()
        self.assertTrue(line.endswith(b" test@localhost"))
        payload = self.key.public_key_payload()
        self.assertTrue(payload.endswith(b"\x00"))

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adbkey.json"
            self.key.save(path)
            loaded = RsaKey.load(path)
            self.assertEqual(loaded.n, self.key.n)
            self.assertEqual(loaded.e, self.key.e)
            self.assertEqual(loaded.d, self.key.d)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
