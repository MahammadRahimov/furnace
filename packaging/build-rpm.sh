#!/usr/bin/env bash
# Build the Furnace RPM: dist/furnace-<version>-1.noarch.rpm
#
#   packaging/build-rpm.sh [OUTPUT_DIR]
#
# Needs rpmbuild, curl and python3: run it on a Red Hat family system or Fedora
# (for example in a container: dnf install rpm-build python3). One package fits every
# supported RHEL / Rocky / AlmaLinux / Oracle Linux, Fedora and Amazon Linux: Furnace is
# pure Python and brings its own Flask and gunicorn (packaging/vendor.lock).
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/furnace/__init__.py")"
[ -n "$VERSION" ] || { echo "Cannot read the version from furnace/__init__.py" >&2; exit 1; }
command -v rpmbuild >/dev/null || { echo "rpmbuild is missing. Run this on a Red Hat family system or Fedora." >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
LIB="$STAGE/usr/lib/furnace"

install -d -m 0755 "$LIB/furnace" "$LIB/templates" "$LIB/bin" "$STAGE/usr/bin" \
    "$STAGE/usr/lib/systemd/system" "$STAGE/usr/lib/tmpfiles.d" "$STAGE/usr/share/doc/furnace"
install -m 0644 "$ROOT"/furnace/*.py "$LIB/furnace/"
install -m 0755 "$ROOT/deploy/setup.sh" "$LIB/setup.sh"
install -m 0644 "$ROOT/deploy/render.py" "$LIB/render.py"
install -m 0644 "$ROOT"/deploy/templates/*.tmpl "$LIB/templates/"
install -m 0755 "$ROOT"/deploy/scripts/* "$ROOT/deploy/furnace-python" "$LIB/bin/"
"$ROOT/packaging/vendor.sh" "$LIB/vendor"
find "$LIB/vendor" -type d -exec chmod 0755 {} + && find "$LIB/vendor" -type f -exec chmod 0644 {} +
install -m 0755 "$ROOT/deploy/furnace" "$STAGE/usr/bin/furnace"
install -m 0644 "$ROOT"/deploy/systemd/* "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$ROOT/deploy/tmpfiles.conf" "$STAGE/usr/lib/tmpfiles.d/furnace.conf"
install -m 0644 "$ROOT/README.md" "$ROOT/config.env.example" "$STAGE/usr/share/doc/furnace/"

# Same file dates every build, so the same source gives the same package.
STAMP="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}"
find "$STAGE" -exec touch -h -d "@$STAMP" {} +

SOURCE_DATE_EPOCH="$STAMP" rpmbuild -bb --quiet \
    --define "_topdir $WORK/rpmbuild" \
    --define "furnace_version $VERSION" \
    --define "furnace_stage $STAGE" \
    "$ROOT/packaging/rpm/furnace.spec" >/dev/null

install -d "$OUT"
RPM="$OUT/furnace-${VERSION}-1.noarch.rpm"
cp "$WORK"/rpmbuild/RPMS/noarch/furnace-"$VERSION"-1.noarch.rpm "$RPM"
echo "$RPM"
