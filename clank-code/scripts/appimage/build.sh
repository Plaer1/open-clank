#!/bin/sh
# Usage: build.sh verified-release.tar.gz appimagetool runtime-x86_64 output.AppImage
set -eu
test "$#" = 4
archive=$(realpath "$1")
tool=$(realpath "$2")
runtime=$(realpath "$3")
out=$(realpath -m "$4")
test ! -e "$out"
root=$(cd "$(dirname "$0")/../.." && pwd)
tar -xOzf "$archive" openclank/release.json | python3 -c 'import sys,json; assert json.load(sys.stdin)["channel"] == "Beta 3"'
expected=$(sha256sum "$archive" | cut -d" " -f1)

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
app="$stage/AppDir"
mkdir -p "$app/ui/static/brand/open-clank/installer" "$app/ui/static/brand/open-clank/SVG" "$app/ui/static/fonts"
cp "$archive" "$app/payload.tar.gz"
printf '%s\n' "$expected" > "$app/payload.sha256"
for file in AppRun install.py setup_host.py; do cp "$root/scripts/appimage/$file" "$app/"; done
cp "$root/static/icons/open-clank-appimage-eye.png" "$app/open-clank.png"
ln -s open-clank.png "$app/.DirIcon"
mkdir -p "$app/usr/share/icons/hicolor/512x512/apps"
cp "$app/open-clank.png" "$app/usr/share/icons/hicolor/512x512/apps/open-clank.png"
ui="$app/ui/static/brand/open-clank/installer"
mkdir -p "$app/ui/static/icons"
cp "$app/open-clank.png" "$app/ui/static/icons/open-clank-appimage-eye.png"
python3 "$root/scripts/installer_notices.py" "$archive" "$ui/notices.js"
cp "$root/scripts/appimage/applet.css" "$root/scripts/appimage/index.html" "$root/static/brand/open-clank/installer/setup.js" "$root/static/brand/open-clank/installer/theme.css" "$ui/"
for file in open-clank-pyramid-eye.svg open-clank-wordmark-wide.svg; do cp "$root/static/brand/open-clank/SVG/$file" "$ui/../SVG/"; done
for file in ComicClank-Regular.woff2 ComicClank-Bold.woff2 ComicClankMono-Regular.woff2 ComicClank-OFL.txt; do cp "$root/static/fonts/$file" "$app/ui/static/fonts/"; done
cp "$root/../LICENSE" "$app/"
cat > "$app/open-clank.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Open Clank Setup
Exec=AppRun
Icon=open-clank
Categories=Education;
Terminal=false
DESKTOP
# The payload is already compressed. Avoid recompressing half a gigabyte.
chmod -R a+rX "$app"
ARCH=x86_64 "$tool" --appimage-extract-and-run --runtime-file "$runtime" --comp zstd --mksquashfs-opt -noD "$app" "$out"
chmod +x "$out"
