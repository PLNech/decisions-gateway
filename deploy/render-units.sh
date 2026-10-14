#!/bin/sh
# Render the systemd templates in deploy/systemd/ into OUTDIR with the placeholders replaced.
#
# Usage: deploy/render-units.sh OUTDIR
# Values come from the environment, with defaults for the current account:
#   DG_USER      (default: id -un)      DG_GROUP     (default: id -gn)
#   DG_HOME      (default: $HOME)       DG_PREFIX    (default: the checkout this script lives in)
#   BACKEND_DIR  (default: /opt/model-server)
#   MODEL_DIR    (default: $BACKEND_DIR/models)
# Writes OUTDIR/decisions-gateway.service and OUTDIR/backend.service. Never writes anywhere else.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 OUTDIR" >&2; exit 64; }
out=$1
here=$(cd "$(dirname "$0")" && pwd)

DG_USER=${DG_USER:-$(id -un)}
DG_GROUP=${DG_GROUP:-$(id -gn)}
DG_HOME=${DG_HOME:-$HOME}
DG_PREFIX=${DG_PREFIX:-$(dirname "$here")}
BACKEND_DIR=${BACKEND_DIR:-/opt/model-server}
MODEL_DIR=${MODEL_DIR:-$BACKEND_DIR/models}

for pair in "DG_USER=$DG_USER" "DG_GROUP=$DG_GROUP"; do
    case ${pair#*=} in
        ''|*[!A-Za-z0-9._-]*) echo "${pair%%=*} must be a plain account name" >&2; exit 65 ;;
    esac
done
for pair in "DG_HOME=$DG_HOME" "DG_PREFIX=$DG_PREFIX" "BACKEND_DIR=$BACKEND_DIR" "MODEL_DIR=$MODEL_DIR"; do
    case ${pair#*=} in
        /*) ;;
        *) echo "${pair%%=*} must be an absolute path" >&2; exit 65 ;;
    esac
    case ${pair#*=} in
        *[!A-Za-z0-9._/-]*) echo "${pair%%=*} may only hold letters, digits and . _ - /" >&2; exit 65 ;;
    esac
done

mkdir -p "$out"
render() {
    sed -e "s|@USER@|$DG_USER|g" -e "s|@GROUP@|$DG_GROUP|g" -e "s|@HOME@|$DG_HOME|g" \
        -e "s|@PREFIX@|$DG_PREFIX|g" -e "s|@BACKEND_DIR@|$BACKEND_DIR|g" -e "s|@MODEL_DIR@|$MODEL_DIR|g" \
        "$1" > "$2"
    if grep -n '@[A-Z_]*@' "$2" >&2; then
        echo "unreplaced placeholder in $2" >&2
        exit 1
    fi
    echo "$2"
}
render "$here/systemd/decisions-gateway.service" "$out/decisions-gateway.service"
render "$here/systemd/backend.service.example" "$out/backend.service"
