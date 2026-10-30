#!/usr/bin/env bash
# iLO4 Commander installer - one panel for every iLO 4 on the network.
#
#   sudo ./install.sh                       install or upgrade (systemd service)
#   sudo ./install.sh --mode docker         run it in Docker instead
#   sudo ./install.sh --migrate DIR [...]   import old one-VM-per-server installs
#   sudo ./install.sh --uninstall           remove it
#   ./install.sh --help
#
# Re-running it upgrades in place: the code is replaced, .env and data/
# (servers, curves, history) are kept.
set -euo pipefail

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/ilo4-commander}"
SVC_USER="${SVC_USER:-ilo}"
SVC_NAME="ilo4-commander"
MODE="systemd"
ACTION="install"
MIGRATE=()

C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_HI=$'\033[36m'; C_DIM=$'\033[90m'; C_0=$'\033[0m'
say(){  printf '%s\n' "$*"; }
ok(){   printf '  %s✓%s %s\n' "$C_OK" "$C_0" "$*"; }
bad(){  printf '  %s✗%s %s\n' "$C_NO" "$C_0" "$*"; }
step(){ printf '\n%s> %s%s\n' "$C_HI" "$*" "$C_0"; }
die(){  printf '\n%s✗ %s%s\n' "$C_NO" "$*" "$C_0" >&2; exit 1; }
ask(){ local p="$1" d="${2:-}" a; read -rp "  $p${d:+ [$d]}: " a; printf '%s' "${a:-$d}"; }
confirm(){ local a; read -rp "  $1 [y/N]: " a; [[ "$a" =~ ^[Yy] ]]; }
ask_secret(){
  local prompt="$1" pass="" ch
  if [ ! -t 0 ]; then read -r pass; printf '%s' "$pass"; return; fi
  printf '  %s: ' "$prompt" >&2
  while IFS= read -rsn1 ch; do
    [ -z "$ch" ] && break
    if [ "$ch" = $'\177' ] || [ "$ch" = $'\b' ]; then
      [ -n "$pass" ] && { pass="${pass%?}"; printf '\b \b' >&2; }
    elif [[ "$ch" == [[:print:]] ]]; then pass+="$ch"; printf '*' >&2; fi
  done
  printf '\n' >&2; printf '%s' "$pass"
}
env_get(){ grep -m1 "^$1=" "$2" 2>/dev/null | cut -d= -f2- || true; }

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2;;
    --mode=*) MODE="${1#*=}"; shift;;
    --prefix) PREFIX="${2:-}"; shift 2;;
    --migrate) shift; while [ $# -gt 0 ] && [[ "$1" != --* ]]; do MIGRATE+=("$1"); shift; done;;
    --uninstall) ACTION="uninstall"; shift;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown option: $1";;
  esac
done
[[ "$MODE" =~ ^(systemd|docker)$ ]] || die "mode must be systemd or docker"

VERSION="$(grep -m1 '^VERSION' "$SRC/app/config.py" | cut -d'"' -f2)"
say ""
say "  ${C_HI}iLO4 Commander${C_0} ${C_DIM}v${VERSION}${C_0}"
say "  ${C_DIM}HPE ProLiant Gen8/Gen9 - unlocked iLO 4 - every server in one panel${C_0}"

# ---------------------------------------------------------------- uninstall
if [ "$ACTION" = uninstall ]; then
  confirm "Remove iLO4 Commander (service, code AND its data in $PREFIX)?" || exit 0
  if command -v systemctl >/dev/null && systemctl list-unit-files 2>/dev/null | grep -q "^$SVC_NAME"; then
    systemctl disable --now "$SVC_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/$SVC_NAME.service"; systemctl daemon-reload
  fi
  if command -v docker >/dev/null; then (cd "$SRC" && docker compose down 2>/dev/null) || true; fi
  rm -rf "$PREFIX"
  ok "Removed."
  exit 0
fi

# ---------------------------------------------------------------- docker
if [ "$MODE" = docker ]; then
  command -v docker >/dev/null || die "Docker is not installed."
  docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) was not found."
  if [ ! -f "$SRC/.env" ]; then
    step "Panel settings"
    PORT="$(ask "Web port" 8080)"
    UIPW="$(ask_secret "Panel password (empty = no login)")"
    umask 077; printf 'BIND_PORT=%s\nUI_PASSWORD=%s\n' "$PORT" "$UIPW" > "$SRC/.env"; umask 022
  fi
  step "Building and starting the container"
  (cd "$SRC" && docker compose up -d --build)
  PORT="$(env_get BIND_PORT "$SRC/.env")"; PORT="${PORT:-8080}"
  for d in ${MIGRATE[@]+"${MIGRATE[@]}"}; do
    step "Importing $d"
    docker cp "$d" ilo4-commander:/tmp/migrate && \
      docker exec ilo4-commander python -m app.migrate /tmp/migrate && \
      docker exec ilo4-commander rm -rf /tmp/migrate
  done
  [ ${#MIGRATE[@]} -gt 0 ] && (cd "$SRC" && docker compose restart)
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  step "Done"; say "  Panel: ${C_HI}http://${IP:-127.0.0.1}:$PORT${C_0}"
  exit 0
fi

# ---------------------------------------------------------------- systemd
[ "$(id -u)" -eq 0 ] || die "Run this with sudo."
command -v systemctl >/dev/null || die "systemctl not found - this system has no systemd."
command -v python3 >/dev/null || die "Python 3 was not found: sudo apt install python3 python3-venv"
python3 -c "import venv, ensurepip" 2>/dev/null || die "Python's venv module is missing: sudo apt install python3-venv"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is needed."

UPGRADE=0; [ -f "$PREFIX/.env" ] && UPGRADE=1
if [ $UPGRADE = 1 ]; then
  step "Upgrading the existing install in $PREFIX (servers, curves and history are kept)"
else
  step "Panel settings"
  PORT="$(ask "Web port" 8080)"
  UIPW="$(ask_secret "Panel password (empty = no login, fine on a trusted LAN)")"
  SSL_CERT=""; SSL_KEY=""
  say "  ${C_DIM}HTTPS is optional. It lets phones install the panel as an app (PWA).${C_0}"
  if confirm "Serve the panel over HTTPS with an existing certificate?"; then
    SSL_CERT="$(ask "Certificate file (PEM, full chain)")"
    SSL_KEY="$(ask "Private key file (PEM)")"
    [ -r "$SSL_CERT" ] && [ -r "$SSL_KEY" ] || die "Can't read the certificate or key."
  fi
fi

if systemctl is-active --quiet "$SVC_NAME" 2>/dev/null; then
  systemctl stop "$SVC_NAME"
  # Whatever happens below, don't leave the fans without their controller.
  trap 'systemctl start "$SVC_NAME" 2>/dev/null || true' EXIT
fi
WANT_PORT="${PORT:-$(env_get BIND_PORT "$PREFIX/.env")}"; WANT_PORT="${WANT_PORT:-8080}"
if command -v ss >/dev/null && ss -ltnH "sport = :$WANT_PORT" | grep -q .; then
  HOLDER="$(ss -ltnpH "sport = :$WANT_PORT" | grep -o 'users:(("[^"]*' | head -1 | cut -d'"' -f2)"
  if [ "$HOLDER" = docker-proxy ] && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx ilo4-commander; then
    die "Port $WANT_PORT is taken by the old iLO4 Commander container.
  Import it, then stop it:   sudo ./install.sh --migrate <its folder>   (see README)
  or stop it first:          docker stop ilo4-commander"
  fi
  die "Port $WANT_PORT is already in use by '${HOLDER:-another program}'. Pick another port or stop it."
fi
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$PREFIX" "$SVC_USER"
mkdir -p "$PREFIX/data"
rm -rf "$PREFIX/app" "$PREFIX/web"
cp -r "$SRC/app" "$SRC/web" "$SRC/requirements.txt" "$PREFIX/"
cp "$SRC/README.md" "$PREFIX/" 2>/dev/null || true

if [ $UPGRADE = 0 ]; then
  umask 077
  cat > "$PREFIX/.env" <<EOF
BIND_HOST=0.0.0.0
BIND_PORT=$PORT
UI_PASSWORD=$UIPW
ILO_DATA_DIR=$PREFIX/data
SSL_CERT=$SSL_CERT
SSL_KEY=$SSL_KEY
# Defaults for servers you add later (each one can override them in the panel)
POLL_SECONDS=10
MIN_PWM_PERCENT=8
WATCHDOG_MARGIN_C=5
ALLOW_RAW_FAN=true
EOF
  umask 022
  ok "Settings written to $PREFIX/.env"
  if [ -n "$SSL_KEY" ]; then
    setfacl -m "u:$SVC_USER:r" "$SSL_KEY" 2>/dev/null || \
      say "  ${C_DIM}Make sure user '$SVC_USER' can read $SSL_KEY${C_0}"
  fi
fi

step "Installing Python dependencies"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
# An upgrade usually needs nothing new: check the pinned versions first, so a
# VM without internet doesn't hang on pip.
if "$PREFIX/venv/bin/python" - "$PREFIX/requirements.txt" <<'PYEOF'
import sys, importlib.metadata as md
for line in open(sys.argv[1]):
    line = line.split("#")[0].strip()
    if not line:
        continue
    name, _, ver = line.partition("==")
    try:
        if md.version(name.split("[")[0]) != ver:
            sys.exit(1)
    except md.PackageNotFoundError:
        sys.exit(1)
PYEOF
then
  ok "Dependencies already up to date"
else
  "$PREFIX/venv/bin/pip" install -q --disable-pip-version-check --timeout 15 --retries 1 \
    -r "$PREFIX/requirements.txt" || die "Couldn't download Python packages - does this machine have internet?"
fi
chown -R "$SVC_USER":"$SVC_USER" "$PREFIX"
chmod 600 "$PREFIX/.env"

for d in ${MIGRATE[@]+"${MIGRATE[@]}"}; do
  step "Importing old install: $d"
  [ -f "$d/.env" ] || { bad "$d has no .env - skipped"; continue; }
  TMP="$(mktemp -d)"; cp -r "$d/." "$TMP/"; chown -R "$SVC_USER" "$TMP"
  (cd "$PREFIX" && sudo -u "$SVC_USER" env ILO_DATA_DIR="$PREFIX/data" \
     "$PREFIX/venv/bin/python" -m app.migrate "$TMP" --name "$(basename "$d")") || bad "Import of $d failed"
  rm -rf "$TMP"
done

sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$SVC_USER|g" "$SRC/$SVC_NAME.service" \
  > "/etc/systemd/system/$SVC_NAME.service"
systemctl daemon-reload
systemctl enable --now "$SVC_NAME" >/dev/null
ok "Service enabled"

PORT="$(env_get BIND_PORT "$PREFIX/.env")"; PORT="${PORT:-8080}"
SCHEME=http; [ -n "$(env_get SSL_CERT "$PREFIX/.env")" ] && SCHEME=https
step "Waiting for the panel"
LIVE=0
for _ in $(seq 1 30); do
  # Check it's really us answering (v3 reports its version), not something else on the port.
  if curl -sk -m 2 "$SCHEME://127.0.0.1:$PORT/api/auth" | grep -q "\"version\":\"$VERSION\""; then LIVE=1; break; fi
  sleep 1
done
[ $LIVE = 1 ] && ok "Panel is up" || bad "No answer yet - check: journalctl -u $SVC_NAME -n 50"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; IP="${IP:-127.0.0.1}"
step "Done"
say "  Panel:   ${C_HI}$SCHEME://$IP:$PORT${C_0}"
say "  Next:    open it and press ${C_HI}+ Add server${C_0} for each iLO."
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  say "  Firewall: ${C_DIM}sudo ufw allow ${PORT}/tcp${C_0}"
fi
say ""
say "  ${C_DIM}logs:     journalctl -u $SVC_NAME -f"
say "  restart:  systemctl restart $SVC_NAME"
say "  import:   sudo ./install.sh --migrate /path/to/old-install-copy"
say "  remove:   sudo ./install.sh --uninstall${C_0}"
say ""
say "  ${C_DIM}Remember to switch off the old per-server VMs, otherwise two programs"
say "  fight over the same fans.${C_0}"
say ""
