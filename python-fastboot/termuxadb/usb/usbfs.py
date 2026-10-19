from __future__ import annotations

import ctypes
import errno
import os
from typing import Union


_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, type_: int, number: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (type_ << _IOC_TYPESHIFT)
        | (number << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _iowr(type_: int, number: int, size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, type_, number, size)


def _ior(type_: int, number: int, size: int) -> int:
    return _ioc(_IOC_READ, type_, number, size)


class _ControlTransfer(ctypes.Structure):
    _fields_ = [
        ("request_type", ctypes.c_uint8),
        ("request", ctypes.c_uint8),
        ("value", ctypes.c_uint16),
        ("index", ctypes.c_uint16),
        ("length", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


class _BulkTransfer(ctypes.Structure):
    _fields_ = [
        ("endpoint", ctypes.c_uint),
        ("length", ctypes.c_uint),
        ("timeout", ctypes.c_uint),
        ("data", ctypes.c_void_p),
    ]


class _SetInterface(ctypes.Structure):
    _fields_ = [
        ("interface", ctypes.c_uint),
        ("altsetting", ctypes.c_uint),
    ]


class _DisconnectClaim(ctypes.Structure):
    _fields_ = [
        ("interface", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("driver", ctypes.c_char * 256),
    ]


USBDEVFS_CONTROL = _iowr(ord("U"), 0, ctypes.sizeof(_ControlTransfer))
USBDEVFS_BULK = _iowr(ord("U"), 2, ctypes.sizeof(_BulkTransfer))
USBDEVFS_CLEAR_HALT = _ior(ord("U"), 21, 4)
USBDEVFS_SETINTERFACE = _ior(ord("U"), 4, ctypes.sizeof(_SetInterface))
USBDEVFS_SETCONFIGURATION = _ior(ord("U"), 5, 4)
USBDEVFS_CLAIMINTERFACE = _ior(ord("U"), 15, 4)
USBDEVFS_RELEASEINTERFACE = _ior(ord("U"), 16, 4)
USBDEVFS_DISCONNECT_CLAIM = _ior(ord("U"), 27, ctypes.sizeof(_DisconnectClaim))

_libc = ctypes.CDLL(None, use_errno=True)
_libc_ioctl = _libc.ioctl
_libc_ioctl.restype = ctypes.c_int
_libc_ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]


class UsbfsError(OSError):
    def __init__(self, operation: str, err: int):
        super().__init__(err, f"{operation}: {os.strerror(err)}")
        self.operation = operation


class UsbTimeout(UsbfsError):
    pass


class UsbStall(UsbfsError):
    pass


def _raise_for_ioctl(operation: str, err: int) -> None:
    if err in (errno.ETIMEDOUT, errno.EAGAIN):
        raise UsbTimeout(operation, err)
    if err in (errno.EPIPE, errno.ESHUTDOWN):
        raise UsbStall(operation, err)
    raise UsbfsError(operation, err)


class Usbfs:
    def __init__(self, fd: int, owned: bool = True):
        self.fd = fd
        self.owned = owned
        self._claimed: list[int] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _ioctl(self, request: int, arg: ctypes.Structure, operation: str) -> int:
        if self._closed:
            raise UsbfsError(operation, errno.EBADF)
        ctypes.set_errno(0)
        ret = _libc_ioctl(self.fd, request, ctypes.byref(arg))
        if ret < 0:
            _raise_for_ioctl(operation, ctypes.get_errno() or errno.EIO)
        return ret

    def control_transfer(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data: Union[int, bytes, bytearray, memoryview],
        timeout: int = 1000,
    ) -> bytes:
        if isinstance(data, int):
            length = data
            buf = ctypes.create_string_buffer(max(1, length))
        else:
            raw = bytes(data)
            length = len(raw)
            buf = ctypes.create_string_buffer(raw, max(1, length))

        if length > 0xFFFF:
            raise ValueError("control transfer length exceeds 65535 bytes")

        xfer = _ControlTransfer(
            request_type & 0xFF,
            request & 0xFF,
            value & 0xFFFF,
            index & 0xFFFF,
            length,
            timeout,
            ctypes.addressof(buf),
        )
        transferred = self._ioctl(USBDEVFS_CONTROL, xfer, "USBDEVFS_CONTROL")
        return bytes(buf.raw[:transferred])

    def bulk_read(self, endpoint: int, size: int, timeout: int = 0) -> bytes:
        if size <= 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        xfer = _BulkTransfer(
            endpoint & 0xFF,
            size,
            timeout,
            ctypes.addressof(buf),
        )
        transferred = self._ioctl(USBDEVFS_BULK, xfer, "USBDEVFS_BULK")
        return bytes(buf.raw[:transferred])

    def bulk_write(self, endpoint: int, data: bytes, timeout: int = 0) -> int:
        if not data:
            return 0
        buf = ctypes.create_string_buffer(data, len(data))
        xfer = _BulkTransfer(
            endpoint & 0xFF,
            len(data),
            timeout,
            ctypes.addressof(buf),
        )
        return self._ioctl(USBDEVFS_BULK, xfer, "USBDEVFS_BULK")

    def claim_interface(self, interface: int, force: bool = False) -> None:
        if interface in self._claimed:
            return
        value = ctypes.c_uint(interface)
        try:
            self._ioctl(USBDEVFS_CLAIMINTERFACE, value, "USBDEVFS_CLAIMINTERFACE")
        except UsbfsError as exc:
            if not force or exc.errno != errno.EBUSY:
                raise
            claim = _DisconnectClaim(interface, 0, b"")
            self._ioctl(
                USBDEVFS_DISCONNECT_CLAIM,
                claim,
                "USBDEVFS_DISCONNECT_CLAIM",
            )
        self._claimed.append(interface)

    def release_interface(self, interface: int) -> None:
        if interface not in self._claimed:
            return
        value = ctypes.c_uint(interface)
        try:
            self._ioctl(USBDEVFS_RELEASEINTERFACE, value, "USBDEVFS_RELEASEINTERFACE")
        except UsbfsError:
            pass
        self._claimed.remove(interface)

    def set_configuration(self, configuration: int) -> None:
        value = ctypes.c_uint(configuration)
        self._ioctl(USBDEVFS_SETCONFIGURATION, value, "USBDEVFS_SETCONFIGURATION")

    def set_interface(self, interface: int, altsetting: int) -> None:
        value = _SetInterface(interface, altsetting)
        self._ioctl(USBDEVFS_SETINTERFACE, value, "USBDEVFS_SETINTERFACE")

    def clear_halt(self, endpoint: int) -> None:
        value = ctypes.c_uint(endpoint)
        self._ioctl(USBDEVFS_CLEAR_HALT, value, "USBDEVFS_CLEAR_HALT")

    def close(self) -> None:
        if self._closed:
            return
        for interface in list(reversed(self._claimed)):
            try:
                value = ctypes.c_uint(interface)
                _libc_ioctl(
                    self.fd,
                    USBDEVFS_RELEASEINTERFACE,
                    ctypes.byref(value),
                )
            except Exception:
                pass
        self._claimed.clear()
        self._closed = True
        if self.owned:
            try:
                os.close(self.fd)
            except OSError:
                pass

    def __enter__(self) -> "Usbfs":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
