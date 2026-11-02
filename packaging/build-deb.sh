#!/usr/bin/env bash
# Build the Furnace package: dist/furnace_<version>_all.deb
#
#   packaging/build-deb.sh [OUTPUT_DIR]
#
# Needs dpkg-deb, curl and python3, so run it on Debian or Ubuntu (WSL works too).
# One package fits every supported Debian and Ubuntu: Furnace is pure Python and
# brings its own Flask and gunicorn (packaging/vendor.lock).
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/furnace/__init__.py")"
[ -n "$VERSION" ] || { echo "Cannot read the version from furnace/__init__.py" >&2; exit 1; }
command -v dpkg-deb >/dev/null || { echo "dpkg-deb is missing. Run this on Debian or Ubuntu." >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/furnace"
LIB="$PKG/usr/lib/furnace"

install -d -m 0755 "$PKG/DEBIAN" "$LIB/furnace" "$LIB/templates" "$LIB/bin" "$PKG/usr/bin" \
    "$PKG/lib/systemd/system" "$PKG/usr/lib/tmpfiles.d" "$PKG/usr/share/doc/furnace"

install -m 0644 "$ROOT"/furnace/*.py "$ROOT/furnace/public_suffix_list.dat" "$LIB/furnace/"
install -m 0755 "$ROOT/deploy/setup.sh" "$LIB/setup.sh"
install -m 0644 "$ROOT/deploy/render.py" "$LIB/render.py"
install -m 0644 "$ROOT"/deploy/templates/*.tmpl "$LIB/templates/"
install -m 0755 "$ROOT"/deploy/scripts/* "$ROOT/deploy/furnace-python" "$LIB/bin/"
"$ROOT/packaging/vendor.sh" "$LIB/vendor"   # Flask, gunicorn and what they need (pinned, hash-checked)
find "$LIB/vendor" -type d -exec chmod 0755 {} + && find "$LIB/vendor" -type f -exec chmod 0644 {} +
install -m 0755 "$ROOT/deploy/furnace" "$PKG/usr/bin/furnace"
install -m 0644 "$ROOT"/deploy/systemd/* "$PKG/lib/systemd/system/"
install -m 0644 "$ROOT/deploy/tmpfiles.conf" "$PKG/usr/lib/tmpfiles.d/furnace.conf"
install -m 0644 "$ROOT/README.md" "$ROOT/config.env.example" "$PKG/usr/share/doc/furnace/"

for script in postinst prerm postrm; do
    install -m 0755 "$ROOT/packaging/debian/$script" "$PKG/DEBIAN/$script"
done
SIZE="$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$ROOT/packaging/debian/control" > "$PKG/DEBIAN/control"
chmod 0644 "$PKG/DEBIAN/control"

# Same file dates every build, so the same source gives the same package.
STAMP="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}"
find "$PKG" -exec touch -h -d "@$STAMP" {} +

install -d "$OUT"
DEB="$OUT/furnace_${VERSION}_all.deb"
SOURCE_DATE_EPOCH="$STAMP" dpkg-deb --root-owner-group -Zxz --build "$PKG" "$DEB" >/dev/null
echo "$DEB"
