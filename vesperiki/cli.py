"""Small command dispatcher for the installed ``vesperiki`` command."""
from __future__ import annotations
import sys
from . import reembed


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "reembed":
        return reembed.main(sys.argv[2:])
    print("usage: vesperiki reembed [--status|--full]", file=sys.stderr)
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
