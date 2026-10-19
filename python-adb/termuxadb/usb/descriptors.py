from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from .usbfs import Usbfs, UsbfsError


ADB_CLASS = 0xFF
ADB_SUBCLASS = 0x42
ADB_PROTOCOL = 0x01
FASTBOOT_PROTOCOL = 0x03

DESCRIPTOR_DEVICE = 1
DESCRIPTOR_CONFIGURATION = 2
DESCRIPTOR_STRING = 3
DESCRIPTOR_INTERFACE = 4
DESCRIPTOR_ENDPOINT = 5
DESCRIPTOR_INTERFACE_ASSOCIATION = 0x0B


class UsbDescriptorError(ValueError):
    pass


@dataclass(frozen=True)
class EndpointDescriptor:
    address: int
    attributes: int
    max_packet_size: int
    interval: int

    @property
    def is_bulk(self) -> bool:
        return (self.attributes & 0x03) == 0x02

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)


@dataclass(frozen=True)
class InterfaceDescriptor:
    number: int
    alternate: int
    class_code: int
    subclass: int
    protocol: int
    interface_string_index: int
    endpoints: tuple[EndpointDescriptor, ...]

    @property
    def bulk_in(self) -> Optional[EndpointDescriptor]:
        for endpoint in self.endpoints:
            if endpoint.is_bulk and endpoint.is_in:
                return endpoint
        return None

    @property
    def bulk_out(self) -> Optional[EndpointDescriptor]:
        for endpoint in self.endpoints:
            if endpoint.is_bulk and not endpoint.is_in:
                return endpoint
        return None

    @property
    def has_bulk_pair(self) -> bool:
        return self.bulk_in is not None and self.bulk_out is not None


@dataclass(frozen=True)
class ConfigurationDescriptor:
    value: int
    attributes: int
    max_power: int
    interfaces: tuple[InterfaceDescriptor, ...]

    def find_interface(
        self,
        class_code: int,
        subclass: int,
        protocol: int,
    ) -> Optional[InterfaceDescriptor]:
        for interface in self.interfaces:
            if (
                interface.class_code == class_code
                and interface.subclass == subclass
                and interface.protocol == protocol
                and interface.has_bulk_pair
            ):
                return interface
        return None


@dataclass(frozen=True)
class DeviceDescriptor:
    usb_version: int
    class_code: int
    subclass: int
    protocol: int
    max_packet_size_0: int
    vendor_id: int
    product_id: int
    device_version: int
    manufacturer_index: int
    product_index: int
    serial_index: int
    num_configurations: int


def parse_device_descriptor(data: bytes) -> DeviceDescriptor:
    if len(data) < 18 or data[1] != DESCRIPTOR_DEVICE:
        raise UsbDescriptorError("invalid device descriptor")
    fields = struct.unpack_from("<BBHBBBBHHHBBBB", data, 0)
    return DeviceDescriptor(
        usb_version=fields[2],
        class_code=fields[3],
        subclass=fields[4],
        protocol=fields[5],
        max_packet_size_0=fields[6],
        vendor_id=fields[7],
        product_id=fields[8],
        device_version=fields[9],
        manufacturer_index=fields[10],
        product_index=fields[11],
        serial_index=fields[12],
        num_configurations=fields[13],
    )


def parse_config_descriptor(data: bytes) -> ConfigurationDescriptor:
    if len(data) < 9 or data[1] != DESCRIPTOR_CONFIGURATION:
        raise UsbDescriptorError("invalid configuration descriptor")

    total_length = struct.unpack_from("<H", data, 2)[0]
    if total_length < 9:
        raise UsbDescriptorError("invalid configuration total length")
    if len(data) < total_length:
        raise UsbDescriptorError("truncated configuration descriptor")

    value = data[5]
    attributes = data[7]
    max_power = data[8]

    interfaces: list[InterfaceDescriptor] = []
    current_number = 0
    current_alternate = 0
    current_class = 0
    current_subclass = 0
    current_protocol = 0
    current_string = 0
    current_endpoints: list[EndpointDescriptor] = []
    in_interface = False

    def flush_interface() -> None:
        nonlocal in_interface, current_endpoints
        if not in_interface:
            return
        interfaces.append(
            InterfaceDescriptor(
                number=current_number,
                alternate=current_alternate,
                class_code=current_class,
                subclass=current_subclass,
                protocol=current_protocol,
                interface_string_index=current_string,
                endpoints=tuple(current_endpoints),
            )
        )
        current_endpoints = []
        in_interface = False

    offset = 0
    while offset + 2 <= total_length:
        length = data[offset]
        descriptor_type = data[offset + 1]
        if length < 2 or offset + length > total_length:
            break
        chunk = data[offset : offset + length]

        if descriptor_type == DESCRIPTOR_INTERFACE and length >= 9:
            flush_interface()
            (
                _,
                _,
                current_number,
                current_alternate,
                _num_endpoints,
                current_class,
                current_subclass,
                current_protocol,
                current_string,
            ) = struct.unpack_from("<BBBBBBBBB", chunk, 0)
            in_interface = True
        elif descriptor_type == DESCRIPTOR_ENDPOINT and length >= 7:
            if in_interface:
                address, attributes, max_packet, interval = struct.unpack_from(
                    "<BBHB", chunk, 2
                )
                current_endpoints.append(
                    EndpointDescriptor(
                        address=address,
                        attributes=attributes,
                        max_packet_size=max_packet,
                        interval=interval,
                    )
                )
        offset += length

    flush_interface()
    return ConfigurationDescriptor(
        value=value,
        attributes=attributes,
        max_power=max_power,
        interfaces=tuple(interfaces),
    )


def interface_mode(interface: InterfaceDescriptor) -> Optional[str]:
    if (
        interface.class_code == ADB_CLASS
        and interface.subclass == ADB_SUBCLASS
        and interface.protocol == ADB_PROTOCOL
    ):
        return "adb"
    if (
        interface.class_code == ADB_CLASS
        and interface.subclass == ADB_SUBCLASS
        and interface.protocol == FASTBOOT_PROTOCOL
    ):
        return "fastboot"
    return None


def configuration_modes(config: ConfigurationDescriptor) -> list[str]:
    modes: list[str] = []
    for interface in config.interfaces:
        mode = interface_mode(interface)
        if mode is not None and mode not in modes:
            modes.append(mode)
    return modes


def read_device_descriptor(usbfs: Usbfs, timeout: int = 1500) -> DeviceDescriptor:
    data = usbfs.control_transfer(0x80, 6, 0x0100, 0, 18, timeout)
    return parse_device_descriptor(data)


def read_config_descriptor(
    usbfs: Usbfs,
    index: int,
    timeout: int = 1500,
) -> ConfigurationDescriptor:
    header = usbfs.control_transfer(0x80, 6, 0x0200 | index, 0, 9, timeout)
    if len(header) < 9:
        raise UsbDescriptorError("short configuration header")
    total_length = struct.unpack_from("<H", header, 2)[0]
    if total_length < 9:
        raise UsbDescriptorError("invalid configuration length")
    if len(header) >= total_length:
        return parse_config_descriptor(header[:total_length])
    data = usbfs.control_transfer(
        0x80,
        6,
        0x0200 | index,
        0,
        total_length,
        timeout,
    )
    return parse_config_descriptor(data)


def read_configurations(
    usbfs: Usbfs,
    device: DeviceDescriptor,
    timeout: int = 1500,
) -> list[ConfigurationDescriptor]:
    configurations: list[ConfigurationDescriptor] = []
    for index in range(max(1, device.num_configurations)):
        try:
            configurations.append(read_config_descriptor(usbfs, index, timeout))
        except (UsbfsError, UsbDescriptorError):
            if not configurations:
                raise
    if not configurations:
        raise UsbDescriptorError("device reported no usable configurations")
    return configurations


def read_language_ids(usbfs: Usbfs, timeout: int = 1000) -> list[int]:
    try:
        data = usbfs.control_transfer(0x80, 6, 0x0300, 0, 255, timeout)
    except UsbfsError:
        return []
    if len(data) < 4 or data[1] != DESCRIPTOR_STRING:
        return []
    length = min(data[0], len(data))
    language_ids: list[int] = []
    for offset in range(2, length - 1, 2):
        language_ids.append(struct.unpack_from("<H", data, offset)[0])
    return language_ids


def _read_string_with_langid(
    usbfs: Usbfs,
    index: int,
    language_id: int,
    timeout: int,
) -> str:
    data = usbfs.control_transfer(
        0x80,
        6,
        0x0300 | index,
        language_id,
        255,
        timeout,
    )
    if len(data) < 2 or data[1] != DESCRIPTOR_STRING:
        raise UsbDescriptorError("invalid string descriptor")
    length = min(data[0], len(data))
    if length <= 2:
        return ""
    return data[2:length].decode("utf-16-le", errors="replace").rstrip("\x00")


def read_string_descriptor(
    usbfs: Usbfs,
    index: int,
    timeout: int = 1000,
) -> str:
    if index == 0:
        return ""
    language_ids = read_language_ids(usbfs, timeout)
    candidates = [0x0409]
    for language_id in language_ids:
        if language_id not in candidates:
            candidates.append(language_id)
    last_error: Optional[Exception] = None
    for language_id in candidates:
        try:
            return _read_string_with_langid(usbfs, index, language_id, timeout)
        except (UsbfsError, UsbDescriptorError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return ""


def read_strings(
    usbfs: Usbfs,
    device: DeviceDescriptor,
) -> tuple[str, str, str]:
    values: list[str] = []
    for index in (
        device.manufacturer_index,
        device.product_index,
        device.serial_index,
    ):
        try:
            values.append(read_string_descriptor(usbfs, index))
        except (UsbfsError, UsbDescriptorError):
            values.append("")
    return values[0], values[1], values[2]


def read_current_configuration(usbfs: Usbfs, timeout: int = 1000) -> Optional[int]:
    try:
        data = usbfs.control_transfer(0x80, 8, 0, 0, 1, timeout)
    except UsbfsError:
        return None
    if not data:
        return None
    return data[0]
