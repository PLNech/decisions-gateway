#!/bin/sh
# Render the launchd templates in deploy/launchd/ into OUTDIR with the placeholders replaced.
#
# Usage: deploy/render-launchd.sh OUTDIR
# Values come from the environment, with defaults for the current account:
#   PYTHON       (default: command -v python3; must be Python 3.11 or later)
#   DG_HOME      (default: $HOME)       DG_PREFIX    (default: the checkout this script lives in)
#   BACKEND_DIR  (default: /opt/model-server)
#   MODEL_DIR    (default: $BACKEND_DIR/models)
# Writes OUTDIR/decisions-gateway.plist and OUTDIR/backend.plist. Never writes anywhere else.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 OUTDIR" >&2; exit 64; }
out=$1
here=$(cd "$(dirname "$0")" && pwd)

PYTHON=${PYTHON:-$(command -v python3 || true)}
DG_HOME=${DG_HOME:-$HOME}
DG_PREFIX=${DG_PREFIX:-$(dirname "$here")}
BACKEND_DIR=${BACKEND_DIR:-/opt/model-server}
MODEL_DIR=${MODEL_DIR:-$BACKEND_DIR/models}

for pair in "PYTHON=$PYTHON" "DG_HOME=$DG_HOME" "DG_PREFIX=$DG_PREFIX" "BACKEND_DIR=$BACKEND_DIR" "MODEL_DIR=$MODEL_DIR"; do
    case ${pair#*=} in
        /*) ;;
        *) echo "${pair%%=*} must be an absolute path" >&2; exit 65 ;;
    esac
    # Also keeps the values XML-safe: no & < > or quotes can reach the plist.
    case ${pair#*=} in
        *[!A-Za-z0-9._/-]*) echo "${pair%%=*} may only hold letters, digits and . _ - /" >&2; exit 65 ;;
    esac
done
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
    || { echo "PYTHON=$PYTHON is not Python 3.11 or later" >&2; exit 65; }

mkdir -p "$out"
render() {
    sed -e "s|@PYTHON@|$PYTHON|g" -e "s|@HOME@|$DG_HOME|g" -e "s|@PREFIX@|$DG_PREFIX|g" \
        -e "s|@BACKEND_DIR@|$BACKEND_DIR|g" -e "s|@MODEL_DIR@|$MODEL_DIR|g" \
        "$1" > "$2"
    if grep -n '@[A-Z_]*@' "$2" >&2; then
        echo "unreplaced placeholder in $2" >&2
        exit 1
    fi
    echo "$2"
}
render "$here/launchd/decisions-gateway.plist" "$out/decisions-gateway.plist"
render "$here/launchd/backend.plist.example" "$out/backend.plist"
