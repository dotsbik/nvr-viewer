#!/bin/bash
# Сборка пакета nvrview_<версия>_all.deb без debhelper: только dpkg-deb.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(python3 -I -c "import sys; sys.path.insert(0, '$ROOT'); import nvrview; print(nvrview.__version__)")"
OUT="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
chmod 755 "$STAGE"
trap 'rm -rf "$STAGE"' EXIT
umask 022

install -d "$STAGE/DEBIAN" "$STAGE/usr/lib/nvrview/nvrview/ui" "$STAGE/usr/bin" \
    "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/usr/share/doc/nvrview"

install -m 644 "$ROOT"/nvrview/*.py "$STAGE/usr/lib/nvrview/nvrview/"
install -m 644 "$ROOT"/nvrview/ui/*.py "$STAGE/usr/lib/nvrview/nvrview/ui/"
install -m 644 "$ROOT/run.py" "$STAGE/usr/lib/nvrview/run.py"
cat > "$STAGE/usr/bin/nvrview" <<'EOF'
#!/bin/sh
# -I: изолированный режим Python, чужие модули из PYTHONPATH и текущей папки не подхватываются
exec /usr/bin/python3 -I /usr/lib/nvrview/run.py "$@"
EOF
chmod 755 "$STAGE/usr/bin/nvrview"
install -m 644 "$ROOT/packaging/local.nvrview.desktop" "$STAGE/usr/share/applications/"
install -m 644 "$ROOT/packaging/local.nvrview.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$ROOT/packaging/copyright" "$STAGE/usr/share/doc/nvrview/copyright"
install -m 644 "$ROOT/README.md" "$STAGE/usr/share/doc/nvrview/README.md"
gzip -9n -c "$ROOT/packaging/changelog" > "$STAGE/usr/share/doc/nvrview/changelog.gz"
chmod 644 "$STAGE/usr/share/doc/nvrview/changelog.gz"

SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$ROOT/packaging/control" > "$STAGE/DEBIAN/control"

# Байткод собирается при установке, удаляется при удалении пакета.
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    python3 -m compileall -q /usr/lib/nvrview >/dev/null 2>&1 || true
fi
EOF
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/lib/nvrview -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
EOF
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

mkdir -p "$OUT"
DEB="$OUT/nvrview_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
echo "$DEB"
if command -v lintian >/dev/null; then lintian --no-tag-display-limit "$DEB" || true; fi
