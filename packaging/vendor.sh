#!/usr/bin/env bash
# Put the bundled libraries (Flask, gunicorn and what they need) into a folder.
#
#   packaging/vendor.sh TARGET_DIR
#
# Downloads each file in packaging/vendor.lock once into build/vendor-cache/ (internet needed
# the first time only), checks its SHA-256 hash, and unpacks it into TARGET_DIR.
# Needs: bash, curl, sha256sum, python3.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:?usage: vendor.sh TARGET_DIR}"
CACHE="${VENDOR_CACHE:-$ROOT/build/vendor-cache}"
LOCK="$ROOT/packaging/vendor.lock"

mkdir -p "$CACHE" "$TARGET"
while read -r file sum url; do
    case "$file" in ''|'#'*) continue ;; esac
    if [ ! -f "$CACHE/$file" ] || ! echo "$sum  $CACHE/$file" | sha256sum -c --status; then
        curl -fsSL --retry 3 -o "$CACHE/$file.part" "$url"
        mv -f "$CACHE/$file.part" "$CACHE/$file"
    fi
    echo "$sum  $CACHE/$file" | sha256sum -c --status \
        || { echo "vendor.sh: $file does not match the hash in vendor.lock. Stopped." >&2; exit 1; }
done < "$LOCK"

python3 - "$LOCK" "$CACHE" "$TARGET" <<'PY'
import io
import pathlib
import sys
import tarfile
import zipfile

lock, cache, target = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
for line in open(lock, encoding="utf-8"):
    if not line.strip() or line.startswith("#"):
        continue
    name = line.split()[0]
    path = cache / name
    if name.endswith(".whl"):  # a pure Python wheel is a zip of the installed files
        with zipfile.ZipFile(path) as wheel:
            wheel.extractall(target)
        continue
    # MarkupSafe source: take only the Python files and the license (MarkupSafe falls back
    # to its pure Python code when the C speedups are missing).
    base = name[: -len(".tar.gz")]
    version = base.split("-")[1]
    pkg = target / "markupsafe"
    info = target / f"markupsafe-{version}.dist-info"
    pkg.mkdir(exist_ok=True)
    info.mkdir(exist_ok=True)
    with tarfile.open(path) as src:
        for member in src.getmembers():
            rel = member.name.split("/", 1)[1] if "/" in member.name else ""
            if rel.startswith("src/markupsafe/") and rel.endswith((".py", "py.typed")):
                (pkg / rel.rsplit("/", 1)[1]).write_bytes(src.extractfile(member).read())
            elif rel in ("LICENSE.txt", "LICENSE.rst"):
                (info / rel).write_bytes(src.extractfile(member).read())
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: MarkupSafe\nVersion: {version}\n", encoding="utf-8")
PY
find "$TARGET" -name '__pycache__' -prune -exec rm -rf {} +
find "$TARGET" \( -name '*.so' -o -name '*.c' -o -name '*.pyd' \) -delete
