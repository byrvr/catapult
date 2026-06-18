"""Isolated mDNS discovery worker for the native backend."""

import json
import sys

from catapult.device import scan_network


def main() -> int:
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    devices = scan_network(timeout)
    print(json.dumps(devices), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
