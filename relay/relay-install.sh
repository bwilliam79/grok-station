#!/bin/sh
# Install the voice relay on macOS or Linux.
# Download this file from GitHub releases over HTTPS. Never from the station.
# The xAI key is read with `read -s` and stored mode 600. It is never an
# argument, an environment variable, or a shell-history line.
set -eu
if [ -z "${BASH_VERSION:-}" ] && command -v bash >/dev/null 2>&1; then
  exec bash "$0" "$@"
fi
if [ -n "${BASH_VERSION:-}" ]; then
  set -o pipefail
fi

DRY=0
PREFIX=
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --prefix)
      PREFIX=${2:?prefix}
      shift
      ;;
    --prefix=*) PREFIX=${1#--prefix=} ;;
    *)
      echo "unknown argument" >&2
      exit 2
      ;;
  esac
  shift
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
OS=$(uname -s)
ROOT=${PREFIX:-}

if [ "$OS" = "Darwin" ]; then
  CFG="$ROOT/Library/Application Support/grok-relay"
  STATE="$CFG/state"
  LOG="$ROOT/Library/Logs/grok-relay"
  UNIT=""
  LAUNCH="$ROOT/Library/LaunchDaemons"
else
  CFG="$ROOT/etc/grok-relay"
  STATE="$ROOT/var/lib/grok-relay"
  LOG="$ROOT/var/log/grok-relay"
  UNIT="$ROOT/etc/systemd/system"
  LAUNCH=""
fi
LIB="$ROOT/usr/local/lib/grok-relay"
SHIM="$ROOT/usr/local/bin/grok-relay"

if [ "$DRY" -eq 1 ]; then
  printf '%s\n' "plan: fetch manifest from GitHub releases over HTTPS (not from the station)"
  printf '%s\n' "plan: validate manifest (not expired) with release.py"
  printf '%s\n' "plan: create user grok-relay and add it to group docker"
  printf '%s\n' "plan: pull images by digest from the manifest"
  printf '%s\n' "plan: read the xAI key with a hidden prompt (read -s) and store it mode 600"
  printf '%s\n' "plan: ask for the station address and the pairing code"
  printf '%s\n' "plan: pair over TLS: read the certificate without verifying, then pin it"
  printf '%s\n' "plan: print the station fingerprint and the relay fingerprint"
  printf '%s\n' "plan: install the relay service"
  printf '%s\n' "plan: install the updater timer (every minute)"
  printf '%s\n' "plan: install the daily check with autocheck off"
  printf '%s\n' "plan: install shim $SHIM"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root." >&2
  exit 1
fi

mkdir -p "$CFG" "$STATE" "$LOG" "$LIB" "$(dirname "$SHIM")"
if [ -n "$UNIT" ]; then
  mkdir -p "$UNIT"
fi
if [ -n "$LAUNCH" ]; then
  mkdir -p "$LAUNCH"
fi

if [ "$OS" = "Darwin" ]; then
  id grok-relay >/dev/null 2>&1 || dscl . -create /Users/grok-relay
  dscl . -create /Users/grok-relay UserShell /usr/bin/false
  dseditgroup -o edit -a grok-relay -t user docker
else
  getent group grok-relay >/dev/null 2>&1 || groupadd --system grok-relay
  id grok-relay >/dev/null 2>&1 || useradd --system --gid grok-relay --home-dir "$STATE" --shell /usr/sbin/nologin grok-relay
  usermod -aG docker grok-relay
fi

python3 "$SCRIPT_DIR/install_support.py" fetch "$CFG/manifest.json" > "$STATE/image-refs.txt"
while IFS= read -r ref; do
  [ -n "$ref" ] || continue
  docker pull "$ref"
done < "$STATE/image-refs.txt"

# Hidden prompt. The value is written to one file and then unset.
unset HISTFILE
printf 'xAI key: '
read -s -r key
printf '\n'
if [ -z "${key:-}" ]; then
  echo "Key was not stored." >&2
  exit 1
fi
umask 077
printf '%s\n' "$key" > "$CFG/xai_api_key"
chmod 600 "$CFG/xai_api_key"
key=
unset key

printf 'Station address (host:port): '
read -r station_addr
printf 'Pairing code: '
read -r station_code
pair_tmp=$(mktemp)
chmod 600 "$pair_tmp"
printf '%s\n%s\n' "$station_addr" "$station_code" > "$pair_tmp"
python3 "$SCRIPT_DIR/install_support.py" pair "$CFG" < "$pair_tmp"
rm -f "$pair_tmp"
station_addr=
station_code=
unset station_addr station_code

for name in grok_relay_cli.py release.py release_fetch.py host_update.py control.py pair_client.py pair_transport.py install_support.py compose.yaml Caddyfile; do
  if [ "$name" = "compose.yaml" ] || [ "$name" = "Caddyfile" ]; then
    cp "$SCRIPT_DIR/$name" "$CFG/$name"
  else
    cp "$SCRIPT_DIR/$name" "$LIB/$name"
  fi
done

printf '%s\n' '{"autocheck":false}' > "$CFG/update.json"
chmod 644 "$CFG/update.json"
printf '%s\n' '{"version":"v0.0.0","relay":"v0.0.0","station":"v0.0.0"}' > "$STATE/installed.json"
chmod 644 "$STATE/installed.json"

cat > "$SHIM" <<EOF
#!/bin/sh
exec python3 "$LIB/grok_relay_cli.py" "\$@"
EOF
chmod 755 "$SHIM"

if [ "$OS" = "Darwin" ]; then
  cat > "$LAUNCH/com.grok-relay.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.grok-relay</string>
<key>ProgramArguments</key>
<array><string>/usr/bin/docker</string><string>compose</string><string>-p</string><string>grok-relay</string><string>-f</string><string>$CFG/compose.yaml</string><string>up</string><string>-d</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
EOF
  cat > "$LAUNCH/com.grok-relay.update.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.grok-relay.update</string>
<key>ProgramArguments</key>
<array><string>$SHIM</string><string>update-run</string></array>
<key>StartInterval</key><integer>60</integer>
</dict></plist>
EOF
  cat > "$LAUNCH/com.grok-relay.check.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.grok-relay.check</string>
<key>ProgramArguments</key>
<array><string>$SHIM</string><string>check-update</string></array>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer></dict>
</dict></plist>
EOF
  launchctl bootstrap system "$LAUNCH/com.grok-relay.plist" || true
  launchctl bootstrap system "$LAUNCH/com.grok-relay.update.plist" || true
  launchctl bootstrap system "$LAUNCH/com.grok-relay.check.plist" || true
else
  cat > "$UNIT/grok-relay.service" <<EOF
[Unit]
Description=Voice relay
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
User=grok-relay
Group=grok-relay
SupplementaryGroups=docker
ExecStart=/usr/bin/docker compose -p grok-relay -f $CFG/compose.yaml up -d
ExecStop=/usr/bin/docker compose -p grok-relay -f $CFG/compose.yaml down
[Install]
WantedBy=multi-user.target
EOF
  cat > "$UNIT/grok-relay-update.service" <<EOF
[Unit]
Description=Apply one queued relay update
[Service]
Type=oneshot
User=grok-relay
Group=grok-relay
SupplementaryGroups=docker
ExecStart=$SHIM update-run
EOF
  cat > "$UNIT/grok-relay-update.timer" <<EOF
[Unit]
Description=Relay updater
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=15s
Persistent=true
[Install]
WantedBy=timers.target
EOF
  cat > "$UNIT/grok-relay-check.service" <<EOF
[Unit]
Description=Relay update check
[Service]
Type=oneshot
User=grok-relay
Group=grok-relay
ExecStart=$SHIM check-update
EOF
  cat > "$UNIT/grok-relay-check.timer" <<EOF
[Unit]
Description=Daily relay update check
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now grok-relay.service
  systemctl enable --now grok-relay-update.timer
  systemctl enable --now grok-relay-check.timer
fi

printf '%s\n' "installed"
