#!/bin/sh
# VERSION_SRC in files/vmadmin im Stil x.y.z-<tag> bumpen (Default: Patch; Tag nach '-' bleibt erhalten).
# Nach jedem Push auf worker ausfuehren:
#   tools/bump-version.sh && git add files/vmadmin && git commit -m "version bump (vmadmin)" && git push origin worker
# Gleicher Stil wie relayadmin (relay/VERSION auf main), aber unabhaengige Version.
set -eu
cd "$(dirname "$0")/.."
bump="${1:-patch}"
v="$(sed -n 's/^VERSION_SRC = "\([^"]*\)".*/\1/p' files/vmadmin | head -1)"
[ -n "$v" ] || { echo "VERSION_SRC nicht gefunden" >&2; exit 1; }
core="${v%%-*}"; tag="${v#*-}"
[ "$tag" = "$v" ] && tag="dev"
x="${core%%.*}"; rest="${core#*.}"; y="${rest%%.*}"; z="${rest#*.}"
case "$bump" in
  major) x=$((x+1)); y=0; z=0 ;;
  minor) y=$((y+1)); z=0 ;;
  *)     z=$((z+1)) ;;
esac
newv="$x.$y.$z-$tag"
sed -i "s/^VERSION_SRC = \".*\"/VERSION_SRC = \"$newv\"/" files/vmadmin
# eingebettete Kopie in der cloud-init gleich mitbumpen (6-space indent, t20-Sync)
sed -i "s/^      VERSION_SRC = \".*\"/      VERSION_SRC = \"$newv\"/" cloud-init/sandbox-worker-user-data.yaml
grep -q "^      VERSION_SRC = \"$newv\"" cloud-init/sandbox-worker-user-data.yaml || { echo "user-data-Einbettung nicht gebumpt" >&2; exit 1; }
echo "vmadmin VERSION_SRC = $newv (files/vmadmin + user-data)"
