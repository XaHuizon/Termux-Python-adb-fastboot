from __future__ import annotations

import array
import os
import socket
import sys


def main() -> int:
    fd_value = os.environ.get("TERMUX_USB_FD")
    if fd_value is None and len(sys.argv) > 1:
        fd_value = sys.argv[1]
    socket_path = os.environ.get("PYADB_BROKER_SOCKET")
    token = os.environ.get("PYADB_BROKER_TOKEN")

    if not fd_value or not socket_path or not token:
        print(
            "pyadb usb broker: missing TERMUX_USB_FD, PYADB_BROKER_SOCKET "
            "or PYADB_BROKER_TOKEN",
            file=sys.stderr,
        )
        return 2
    try:
        fd = int(fd_value)
    except ValueError:
        print(f"pyadb usb broker: invalid file descriptor {fd_value!r}", file=sys.stderr)
        return 2

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
        rights = array.array("i", [fd])
        sock.sendmsg([token.encode("ascii")], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
    except OSError as exc:
        print(f"pyadb usb broker: {exc}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
