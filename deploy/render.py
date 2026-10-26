#!/usr/bin/env python3
"""Fill a template: every {{NAME}} is replaced by the environment variable R_NAME.

Usage: R_THIS_IP=192.0.2.11 python3 render.py template.tmpl output
Stops with an error if a placeholder has no value, so no file is ever half filled.
"""

import os
import re
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: render.py TEMPLATE OUTPUT", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as handle:
        text = handle.read()
    missing = sorted({name for name in re.findall(r"\{\{([A-Z0-9_]+)\}\}", text)
                      if f"R_{name}" not in os.environ})
    if missing:
        print(f"Template {sys.argv[1]} has no value for: {', '.join(missing)}", file=sys.stderr)
        return 1
    text = re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda m: os.environ[f"R_{m.group(1)}"], text)
    output = sys.argv[2]
    if output == "-":
        sys.stdout.write(text)
    else:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
