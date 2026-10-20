from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .descriptors import (
    ADB_CLASS,
    ADB_PROTOCOL,
    ADB_SUBCLASS,
    FASTBOOT_PROTOCOL,
    ConfigurationDescriptor,
    DeviceDescriptor,
    InterfaceDescriptor,
    configuration_modes,
    read_configurations,
    read_current_configuration,
    read_device_descriptor,
    read_strings,
)
from .termux_usb import fallback_serial, list_usb_devices, open_usb_device, parse_device_path
from .usbfs import UsbTimeout, Usbfs, UsbfsError


USBFS_MAX_TRANSFER = 16384


@dataclass(frozen=True)
class UsbDeviceInfo:
    path: str
    bus: int
    address: int
    vendor_id: int
    product_id: int
    manufacturer: str
    product: str
    serial: str
    modes: tuple[str, ...]
    device: DeviceDescriptor
    configurations: tuple[ConfigurationDescriptor, ...]

    @property
    def display_serial(self) -> str:
        return self.serial or fallback_serial(self.path)

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "bus": self.bus,
            "address": self.address,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "manufacturer": self.manufacturer,
            "product": self.product,
            "serial": self.display_serial,
            "modes": list(self.modes),
        }


def probe_device(path: str) -> tuple[UsbDeviceInfo, Usbfs]:
    fd = open_usb_device(path)
    usbfs = Usbfs(fd)
    try:
        device = read_device_descriptor(usbfs)
        configurations = read_configurations(usbfs, device)
        manufacturer, product, serial = read_strings(usbfs, device)
        modes: list[str] = []
        for configuration in configurations:
            for mode in configuration_modes(configuration):
                if mode not in modes:
                    modes.append(mode)
        bus, address = parse_device_path(path)
        info = UsbDeviceInfo(
            path=path,
            bus=bus,
            address=address,
            vendor_id=device.vendor_id,
            product_id=device.product_id,
            manufacturer=manufacturer,
            product=product,
            serial=serial,
            modes=tuple(modes),
            device=device,
            configurations=tuple(configurations),
        )
        return info, usbfs
    except Exception:
        usbfs.close()
        raise


def scan_device(path: str) -> UsbDeviceInfo:
    info, usbfs = probe_device(path)
    usbfs.close()
    return info


def scan_devices(
    paths: Optional[Iterable[str]] = None,
    strict: bool = False,
) -> tuple[list[UsbDeviceInfo], list[tuple[str, Exception]]]:
    if paths is None:
        candidates = list_usb_devices()
    elif isinstance(paths, str):
        candidates = [paths]
    else:
        candidates = list(paths)
    devices: list[UsbDeviceInfo] = []
    errors: list[tuple[str, Exception]] = []
    for path in candidates:
        try:
            devices.append(scan_device(path))
        except Exception as exc:
            if strict:
                raise
            errors.append((path, exc))
    return devices, errors


class UsbTransport:
    def __init__(
        self,
        usbfs: Usbfs,
        interface: InterfaceDescriptor,
        configuration_value: int,
        in_endpoint: int,
        out_endpoint: int,
    ):
        self.usbfs = usbfs
        self.interface = interface
        self.configuration_value = configuration_value
        self.in_endpoint = in_endpoint
        self.out_endpoint = out_endpoint
        self.out_packet_size = 512
        out_descriptor = interface.bulk_out
        if out_descriptor is not None and out_descriptor.max_packet_size:
            self.out_packet_size = max(1, out_descriptor.max_packet_size & 0x7FF)
        self._buffer = bytearray()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def read(self, size: int, timeout: int = 0) -> bytes:
        if size <= 0:
            return b""
        if self._buffer:
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            return chunk
        return self.usbfs.bulk_read(
            self.in_endpoint,
            min(size, USBFS_MAX_TRANSFER),
            timeout,
        )

    def read_exact(self, size: int, timeout: int = 0) -> bytes:
        if size <= 0:
            return b""
        while len(self._buffer) < size:
            request = min(max(4096, size - len(self._buffer)), USBFS_MAX_TRANSFER)
            chunk = self.usbfs.bulk_read(self.in_endpoint, request, timeout)
            if not chunk:
                raise UsbfsError("USB bulk read returned no data", 5)
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def write_all(self, data: bytes, timeout: int = 0) -> None:
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + USBFS_MAX_TRANSFER]
            # 设备端只在收到短包时结束一次接收请求，长度正好是最大包长的整数倍会永久挂起
            if len(chunk) % self.out_packet_size == 0:
                chunk = chunk[:-1]
            sent = self.usbfs.bulk_write(self.out_endpoint, chunk, timeout)
            if sent <= 0:
                raise UsbfsError("USB bulk write transferred no data", 5)
            offset += sent

    def drain_input(self, timeout: int = 100) -> None:
        while True:
            try:
                data = self.usbfs.bulk_read(
                    self.in_endpoint,
                    USBFS_MAX_TRANSFER,
                    timeout,
                )
            except UsbTimeout:
                return
            if not data:
                return

    def clear_halt(self, endpoint: int) -> None:
        self.usbfs.clear_halt(endpoint)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.usbfs.release_interface(self.interface.number)
        finally:
            self.usbfs.close()

    def __enter__(self) -> "UsbTransport":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _find_interface(
    configurations: list[ConfigurationDescriptor],
    protocol: int,
) -> tuple[ConfigurationDescriptor, InterfaceDescriptor]:
    for configuration in configurations:
        interface = configuration.find_interface(
            ADB_CLASS,
            ADB_SUBCLASS,
            protocol,
        )
        if interface is not None:
            return configuration, interface
    raise UsbfsError("USB interface not found", 2)


def open_transport_from_info(
    usbfs: Usbfs,
    info: UsbDeviceInfo,
    mode: str = "adb",
) -> UsbTransport:
    if mode not in ("adb", "fastboot"):
        raise ValueError(f"unsupported USB mode: {mode}")
    protocol = ADB_PROTOCOL if mode == "adb" else FASTBOOT_PROTOCOL

    configuration, interface = _find_interface(list(info.configurations), protocol)
    current_configuration = read_current_configuration(usbfs)
    if (
        current_configuration is not None
        and configuration.value != 0
        and current_configuration != configuration.value
    ):
        usbfs.set_configuration(configuration.value)
    usbfs.claim_interface(interface.number, force=False)
    if interface.alternate != 0:
        usbfs.set_interface(interface.number, interface.alternate)
    bulk_in = interface.bulk_in
    bulk_out = interface.bulk_out
    if bulk_in is None or bulk_out is None:
        raise UsbfsError("USB interface has no bulk endpoint pair", 2)
    return UsbTransport(
        usbfs=usbfs,
        interface=interface,
        configuration_value=configuration.value,
        in_endpoint=bulk_in.address,
        out_endpoint=bulk_out.address,
    )


def open_transport(path: str, mode: str = "adb") -> UsbTransport:
    info, usbfs = probe_device(path)
    try:
        return open_transport_from_info(usbfs, info, mode)
    except Exception:
        usbfs.close()
        raise


def open_adb_transport(path: str) -> UsbTransport:
    return open_transport(path, "adb")


def open_fastboot_transport(path: str) -> UsbTransport:
    return open_transport(path, "fastboot")
