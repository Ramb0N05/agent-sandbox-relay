#!/bin/sh
# relay/VERSION auf den aktuellen origin/main-Commit setzen.
# Nach jedem Push auf main ausfuehren:  tools/bump-version.sh && git add relay/VERSION && git commit -m "version bump" && git push origin main
# (Der Container liest relay/VERSION beim Start; ohne Build-Args ist das die einzige
#  verlaessliche Quelle fuer den laufenden Commit - Portainer baut aus der Git-URL am HEAD.)
set -eu
cd "$(dirname "$0")/.."
sha="$(git rev-parse origin/main)"
printf '%s\n' "$sha" > relay/VERSION
echo "relay/VERSION = $sha"
