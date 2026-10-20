from __future__ import annotations

import base64
import getpass
import hashlib
import json
import math
import os
import secrets
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_RSA_BITS = 2048
_RSA_BYTES = _RSA_BITS // 8
_RSA_WORDS = _RSA_BYTES // 4
_PUBLIC_EXPONENT = 65537
_SHA1_DIGEST_INFO = bytes.fromhex("3021300906052b0e03021a05000414")
_SMALL_PRIMES = (
    3,
    5,
    7,
    11,
    13,
    17,
    19,
    23,
    29,
    31,
    37,
    41,
    43,
    47,
    53,
    59,
    61,
    67,
    71,
    73,
    79,
    83,
    89,
    97,
    101,
    103,
    107,
    109,
    113,
    127,
    131,
    137,
    139,
    149,
    151,
    157,
    163,
    167,
    173,
    179,
    181,
    191,
    193,
    197,
    199,
    211,
    223,
    227,
    229,
    233,
    239,
    241,
    251,
)


class AdbKeyError(RuntimeError):
    pass


def _is_probable_prime(value: int, rounds: int = 16) -> bool:
    if value < 2:
        return False
    for prime in _SMALL_PRIMES:
        if value % prime == 0:
            return value == prime

    odd = value - 1
    shift = 0
    while odd % 2 == 0:
        odd //= 2
        shift += 1

    for _ in range(rounds):
        base = secrets.randbelow(value - 3) + 2
        witness = pow(base, odd, value)
        if witness in (1, value - 1):
            continue
        for _ in range(shift - 1):
            witness = pow(witness, 2, value)
            if witness == value - 1:
                break
        else:
            return False
    return True


def _generate_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if candidate % _PUBLIC_EXPONENT == 1:
            continue
        if math.gcd(candidate - 1, _PUBLIC_EXPONENT) != 1:
            continue
        if _is_probable_prime(candidate):
            return candidate


def _default_comment() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "termux"
    try:
        host = socket.gethostname()
    except Exception:
        host = "android"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in f"{user}@{host}")
    return safe or "termux@android"


def _json_key_path() -> Path:
    return Path.home() / ".config" / "termux-pyadb" / "adbkey.json"


def _key_choice_path() -> Path:
    return Path.home() / ".config" / "termux-pyadb" / "key_path"


def default_key_path() -> Path:
    override = os.environ.get("PYADB_KEY")
    if override:
        return Path(override).expanduser()

    choice_path = _key_choice_path()
    try:
        selected = Path(choice_path.read_text(encoding="utf-8").strip()).expanduser()
    except OSError:
        selected = None
    if selected is not None and selected.is_file():
        return selected

    android_key = Path.home() / ".android" / "adbkey"
    json_key = _json_key_path()
    chosen = android_key if android_key.is_file() else json_key
    try:
        choice_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        choice_path.write_text(str(chosen) + "\n", encoding="utf-8")
        os.chmod(choice_path, 0o600)
    except OSError:
        pass
    return chosen


def _der_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    if offset + 2 > len(data):
        raise AdbKeyError("truncated DER value")
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or offset + count > len(data):
            raise AdbKeyError("invalid DER length")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise AdbKeyError("truncated DER payload")
    return tag, data[offset:end], end


def _parse_private_key_der(der: bytes) -> tuple[int, int, int]:
    tag, body, _end = _der_tlv(der, 0)
    if tag != 0x30:
        raise AdbKeyError("invalid private key sequence")

    offset = 0
    tag, _version, offset = _der_tlv(body, offset)
    if tag != 0x02:
        raise AdbKeyError("invalid private key version")
    tag, first, offset = _der_tlv(body, offset)

    if tag == 0x30:
        tag, encrypted, offset = _der_tlv(body, offset)
        if tag != 0x04:
            raise AdbKeyError("invalid PKCS#8 private key")
        tag, body, _end = _der_tlv(encrypted, 0)
        if tag != 0x30:
            raise AdbKeyError("invalid RSA private key sequence")
        offset = 0
        tag, _version, offset = _der_tlv(body, offset)
        if tag != 0x02:
            raise AdbKeyError("invalid RSA private key version")
        tag, first, offset = _der_tlv(body, offset)

    if tag != 0x02:
        raise AdbKeyError("invalid RSA modulus")
    modulus = first
    tag, exponent, offset = _der_tlv(body, offset)
    if tag != 0x02:
        raise AdbKeyError("invalid RSA public exponent")
    tag, private_exponent, offset = _der_tlv(body, offset)
    if tag != 0x02:
        raise AdbKeyError("invalid RSA private exponent")
    return (
        int.from_bytes(modulus, "big"),
        int.from_bytes(exponent, "big"),
        int.from_bytes(private_exponent, "big"),
    )


@dataclass(frozen=True)
class RsaKey:
    n: int
    e: int
    d: int
    comment: str

    @classmethod
    def generate(cls, comment: Optional[str] = None) -> "RsaKey":
        while True:
            p = _generate_prime(_RSA_BITS // 2)
            q = _generate_prime(_RSA_BITS // 2)
            if p == q:
                continue
            n = p * q
            if n.bit_length() != _RSA_BITS:
                continue
            phi = (p - 1) * (q - 1)
            if math.gcd(_PUBLIC_EXPONENT, phi) != 1:
                continue
            d = pow(_PUBLIC_EXPONENT, -1, phi)
            return cls(
                n=n,
                e=_PUBLIC_EXPONENT,
                d=d,
                comment=comment or _default_comment(),
            )

    @classmethod
    def load(cls, path: Path) -> "RsaKey":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdbKeyError(f"failed to read ADB key {path}: {exc}") from exc
        if text.lstrip().startswith("-----BEGIN"):
            return cls.load_android(path)
        try:
            data = json.loads(text)
            n = int(data["n"], 16)
            e = int(data["e"])
            d = int(data["d"], 16)
            comment = str(data.get("comment") or _default_comment())
        except (ValueError, KeyError, TypeError) as exc:
            raise AdbKeyError(f"failed to read ADB key {path}: {exc}") from exc
        return cls._validated(n, e, d, comment, path)

    @classmethod
    def load_android(cls, path: Path) -> "RsaKey":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdbKeyError(f"failed to read ADB key {path}: {exc}") from exc
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("-----")
        ]
        try:
            der = base64.b64decode("".join(lines), validate=True)
        except (ValueError, TypeError) as exc:
            raise AdbKeyError(f"invalid PEM in {path}") from exc
        n, e, d = _parse_private_key_der(der)
        comment = _default_comment()
        public_path = path.with_name(path.name + ".pub")
        try:
            public_line = public_path.read_text(encoding="utf-8").strip()
        except OSError:
            public_line = ""
        if " " in public_line:
            comment = public_line.split(" ", 1)[1].strip() or comment
        return cls._validated(n, e, d, comment, path)

    @classmethod
    def _validated(
        cls,
        n: int,
        e: int,
        d: int,
        comment: str,
        path: Path,
    ) -> "RsaKey":
        if e != _PUBLIC_EXPONENT or n.bit_length() != _RSA_BITS:
            raise AdbKeyError(f"unsupported ADB key in {path}")
        if pow(pow(2, e, n), d, n) != 2:
            raise AdbKeyError(f"invalid ADB key in {path}")
        return cls(n=n, e=e, d=d, comment=comment)

    @classmethod
    def load_or_create(cls, path: Optional[Path] = None) -> "RsaKey":
        key_path = path or default_key_path()
        if key_path.exists():
            try:
                return cls.load(key_path)
            except AdbKeyError:
                if path is not None or key_path == _json_key_path():
                    raise
        json_path = _json_key_path()
        if json_path.exists():
            return cls.load(json_path)
        key = cls.generate()
        key.save(json_path)
        return key

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "version": 1,
            "n": f"{self.n:x}",
            "e": self.e,
            "d": f"{self.d:x}",
            "comment": self.comment,
        }
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def sign_sha1(self, data: bytes) -> bytes:
        digest_info = _SHA1_DIGEST_INFO + hashlib.sha1(data).digest()
        padding_length = _RSA_BYTES - len(digest_info) - 3
        if padding_length < 8:
            raise AdbKeyError("RSA modulus is too small")
        encoded = (
            b"\x00\x01"
            + b"\xff" * padding_length
            + b"\x00"
            + digest_info
        )
        signature = pow(int.from_bytes(encoded, "big"), self.d, self.n)
        return signature.to_bytes(_RSA_BYTES, "big")

    def verify_sha1(self, data: bytes, signature: bytes) -> bool:
        if len(signature) != _RSA_BYTES:
            return False
        digest_info = _SHA1_DIGEST_INFO + hashlib.sha1(data).digest()
        padding_length = _RSA_BYTES - len(digest_info) - 3
        expected = (
            b"\x00\x01"
            + b"\xff" * padding_length
            + b"\x00"
            + digest_info
        )
        recovered = pow(
            int.from_bytes(signature, "big"),
            self.e,
            self.n,
        ).to_bytes(_RSA_BYTES, "big")
        return recovered == expected

    def public_key_blob(self) -> bytes:
        modulus = self.n.to_bytes(_RSA_BYTES, "little")
        r = 1 << (_RSA_WORDS * 32)
        rr = pow(r, 2, self.n).to_bytes(_RSA_BYTES, "little")
        n0inv = (-pow(self.n & 0xFFFFFFFF, -1, 1 << 32)) & 0xFFFFFFFF
        return struct.pack(
            "<II256s256sI",
            _RSA_WORDS,
            n0inv,
            modulus,
            rr,
            self.e,
        )

    def public_key_line(self) -> bytes:
        encoded = base64.b64encode(self.public_key_blob())
        return encoded + b" " + self.comment.encode("utf-8")

    def public_key_payload(self) -> bytes:
        return self.public_key_line() + b"\x00"


def load_or_create_key(path: Optional[Path] = None) -> RsaKey:
    return RsaKey.load_or_create(path)
