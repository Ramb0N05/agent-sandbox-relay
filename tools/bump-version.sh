#!/bin/sh
# relay/VERSION im Stil x.y.z-<tag> bumpen (Default: Patch; Tag nach '-' bleibt erhalten).
# Nach jedem Push auf main ausfuehren:
#   tools/bump-version.sh && git add relay/VERSION && git commit -m "version bump (relay)" && git push origin main
# Gleicher Stil wie vmadmin (VERSION_SRC in files/vmadmin auf worker), aber unabhaengige Version.
set -eu
cd "$(dirname "$0")/.."
bump="${1:-patch}"
v="$(cat relay/VERSION)"
core="${v%%-*}"; tag="${v#*-}"
[ "$tag" = "$v" ] && tag="dev"
x="${core%%.*}"; rest="${core#*.}"; y="${rest%%.*}"; z="${rest#*.}"
case "$bump" in
  major) x=$((x+1)); y=0; z=0 ;;
  minor) y=$((y+1)); z=0 ;;
  *)     z=$((z+1)) ;;
esac
printf '%s.%s.%s-%s\n' "$x" "$y" "$z" "$tag" > relay/VERSION
echo "relay/VERSION = $(cat relay/VERSION)"
