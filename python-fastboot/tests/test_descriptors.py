import struct
import unittest

from termuxadb.usb.descriptors import (
    ADB_CLASS,
    ADB_PROTOCOL,
    ADB_SUBCLASS,
    FASTBOOT_PROTOCOL,
    configuration_modes,
    parse_config_descriptor,
    parse_device_descriptor,
)


def device_descriptor(
    vendor_id: int = 0x18D1,
    product_id: int = 0x4EE7,
    configurations: int = 1,
) -> bytes:
    return struct.pack(
        "<BBHBBBBHHHBBBB",
        18,
        1,
        0x0200,
        0,
        0,
        0,
        64,
        vendor_id,
        product_id,
        0x0100,
        1,
        2,
        3,
        configurations,
    )


def interface_descriptor(number: int, protocol: int) -> bytes:
    return struct.pack(
        "<BBBBBBBBB",
        9,
        4,
        number,
        0,
        2,
        ADB_CLASS,
        ADB_SUBCLASS,
        protocol,
        0,
    )


def endpoint_descriptor(address: int) -> bytes:
    return struct.pack("<BBBBHB", 7, 5, address, 0x02, 512, 0)


def configuration_descriptor(protocol: int = ADB_PROTOCOL) -> bytes:
    body = (
        interface_descriptor(0, protocol)
        + endpoint_descriptor(0x81)
        + endpoint_descriptor(0x01)
    )
    header = struct.pack("<BBHBBBBB", 9, 2, 9 + len(body), 1, 1, 0, 0x80, 50)
    return header + body


class DescriptorTests(unittest.TestCase):
    def test_device_descriptor(self):
        device = parse_device_descriptor(device_descriptor())
        self.assertEqual(device.vendor_id, 0x18D1)
        self.assertEqual(device.product_id, 0x4EE7)
        self.assertEqual(device.num_configurations, 1)
        self.assertEqual(device.serial_index, 3)

    def test_adb_interface(self):
        config = parse_config_descriptor(configuration_descriptor())
        interface = config.find_interface(ADB_CLASS, ADB_SUBCLASS, ADB_PROTOCOL)
        self.assertIsNotNone(interface)
        self.assertEqual(interface.number, 0)
        self.assertEqual(interface.bulk_in.address, 0x81)
        self.assertEqual(interface.bulk_out.address, 0x01)
        self.assertEqual(configuration_modes(config), ["adb"])

    def test_fastboot_interface(self):
        config = parse_config_descriptor(configuration_descriptor(FASTBOOT_PROTOCOL))
        interface = config.find_interface(ADB_CLASS, ADB_SUBCLASS, FASTBOOT_PROTOCOL)
        self.assertIsNotNone(interface)
        self.assertEqual(configuration_modes(config), ["fastboot"])

    def test_missing_interface(self):
        config = parse_config_descriptor(configuration_descriptor())
        self.assertIsNone(
            config.find_interface(ADB_CLASS, ADB_SUBCLASS, FASTBOOT_PROTOCOL)
        )

    def test_truncated_configuration(self):
        data = configuration_descriptor()[:-1]
        with self.assertRaises(ValueError):
            parse_config_descriptor(data)


if __name__ == "__main__":
    unittest.main()
