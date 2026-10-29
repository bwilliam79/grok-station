#!/bin/sh
# grok-station single-device installer. Run on the Pi as root from the cloned repo (bootstrap.sh does this for you):
#   sudo ./install.sh              install or update (safe to re-run)
#   sudo ./install.sh --verbose    stream every command's output (-v)
#   sudo ./install.sh --dry-run    print what would run, change nothing (add -v to see the commands)
#   sudo ./install.sh --no-reboot  fresh install: don't offer to reboot at the end
# Fresh install (setup not done yet): services are enabled but not started; they start at the reboot the installer
# offers at the end, so the setup code only appears once. Update (setup done, or run by grok-station-update.service):
# services restart in place, with no prompt and no reboot.
# Everything runs on this Pi: display, wake listener, admin page (HTTPS) and the voice relay (loopback only).
# Secrets are generated here and never printed. The xAI key is entered on the admin page during first-run setup.
# Card image (tools/build-image): the build runs this file in an arm64 chroot with GROK_STATION_IMAGE_BUILD=1
# (network stages only: packages, Python venvs, wake models; no secrets, nothing that needs the Pi). First boot runs
# it again with GROK_STATION_OFFLINE=1: every other stage runs as usual and the network steps are checked, not run.
# Command output goes to $LOG (default /var/log/grok-station-install.log); the screen shows one line per stage.
set -eu
cd "$(dirname "$0")"
DRY=0; VERBOSE=0; NOREBOOT=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -v|--verbose) VERBOSE=1 ;;
    --no-reboot) NOREBOOT=1 ;;
    *) echo "usage: sudo ./install.sh [--verbose] [--dry-run] [--no-reboot]" >&2; exit 2 ;;
  esac
done
run() { if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi; }
sh_() { if [ "$DRY" = 1 ]; then echo "+ sh -c '$1'"; else sh -c "$1"; fi; }
O=/opt/grok-station; S=/etc/grok-station/secrets; V=/var/lib/grok-station
# Stamp the semantic VERSION. A checkout without that file falls back to a short commit id.
VER=
if [ -f VERSION ]; then
  VER=$(tr -d '[:space:]' < VERSION)
fi
if [ -z "$VER" ]; then
  VER="$(git -c safe.directory="$PWD" rev-parse --short HEAD 2>/dev/null || echo dev)"
fi
FLAGS="${TMPDIR:-/tmp}/grok-station-install.$$"; mkdir -p "$FLAGS"; trap 'rm -rf "$FLAGS"' EXIT
# Setup is complete once an admin password and the xAI key exist. Then (or from the update unit) this is an update.
setup_done() { grep -q '"password"' /var/lib/grok-station/admin/auth.json 2>/dev/null && [ -s $S/xai_api_key ]; }
if [ "${GROK_STATION_NONINTERACTIVE:-0}" = 1 ] || setup_done; then MODE=update; else MODE=fresh; fi
IMAGE_BUILD="${GROK_STATION_IMAGE_BUILD:-0}"   # 1: inside the image build chroot
OFFLINE="${GROK_STATION_OFFLINE:-0}"           # 1: first boot of a card image; packages and venvs are already there
IMAGE_MARK=$O/.image-build                      # written by the image build: the VERSION it prepared
[ "$IMAGE_BUILD" = 1 ] && MODE=fresh
REBOOT_FLAG=/run/grok-station-reboot-required  # read by the admin page; gone after the reboot

# ---- output: colors only on a TTY without NO_COLOR
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_B="$(printf '\033[1m')"; C_G="$(printf '\033[32m')"; C_R="$(printf '\033[31m')"; C_Y="$(printf '\033[33m')"
  C_C="$(printf '\033[36m')"; C_0="$(printf '\033[0m')"; TTY=1
else
  C_B=; C_G=; C_R=; C_Y=; C_C=; C_0=; TTY=0
fi
if [ "$DRY" = 1 ]; then LOG="${LOG:-${TMPDIR:-/tmp}/grok-station-install-dry-run.log}"; else LOG="${LOG:-/var/log/grok-station-install.log}"; fi

[ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || { echo "${C_R}✗${C_0} Run with sudo." >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || [ "$DRY" = 1 ] || { echo "${C_R}✗${C_0} Needs 64-bit Raspberry Pi OS (aarch64)." >&2; exit 1; }
if [ "${GROK_STATION_LOG_APPEND:-0}" = 1 ]; then : >> "$LOG"; else : > "$LOG"; fi 2>/dev/null \
  || { echo "${C_R}✗${C_0} Can't write the log at $LOG" >&2; exit 1; }
[ "$DRY" = 1 ] || chmod 0600 "$LOG"
echo "grok-station install $VER, $(date)" >> "$LOG"

fail() {  # fail <stage>
  echo
  echo "${C_R}${C_B}Install failed:${C_0} $1"
  if [ "$VERBOSE" = 0 ]; then echo "Last lines of $LOG:"; tail -n 20 "$LOG" | sed 's/^/  /'; fi
  echo "Full log: $LOG   (re-run with --verbose to see everything)"
  # An update that fails partway must not leave the station half-restarted (admin on new code, relay stopped or on
  # old code). Restart every service from whatever is installed now, so the page and voice keep answering.
  if [ "$MODE" = update ] && [ "$DRY" != 1 ]; then
    echo "Restarting the station services after the failed update" | tee -a "$LOG"
    for u in grok-station-relay grok-station-wake grok-station grok-station-admin; do
      systemctl reset-failed "$u" >/dev/null 2>&1 || true
      systemctl restart "$u" >> "$LOG" 2>&1 || echo "!!   couldn't restart $u" >> "$LOG"
    done
  fi
  exit 1
}

# stage <label> <function>: runs the function in a subshell with set -e, stdin from /dev/null, output to the log
# (or to the screen and the log with --verbose). Prints a check or a cross; on failure shows the log tail and exits.
stage() {
  label=$1; shift
  echo "== $label" >> "$LOG"
  rm -f "$FLAGS/rc"
  set +e
  if [ "$VERBOSE" = 1 ]; then
    echo "${C_C}▸${C_0} ${C_B}$label${C_0}"
    # tee hides the status in POSIX sh, so the subshell writes it on exit
    ( trap 'echo $? > "$FLAGS/rc"' EXIT; set -e; "$@" ) < /dev/null 2>&1 | tee -a "$LOG"
    rc=$(cat "$FLAGS/rc" 2>/dev/null || echo 1)
  else
    ( set -e; "$@" ) < /dev/null >> "$LOG" 2>&1 &
    pid=$!
    if [ "$TTY" = 1 ]; then
      i=0
        # shellcheck disable=SC1003
      while kill -0 "$pid" 2>/dev/null; do
        case $((i % 4)) in 0) f='|' ;; 1) f='/' ;; 2) f='-' ;; *) f='\' ;; esac
        printf '\r%s%s%s %s ' "$C_C" "$f" "$C_0" "$label"; i=$((i + 1)); sleep 0.2
      done
      printf '\r\033[K'
    else
      echo "  ... $label"
    fi
    wait "$pid"; rc=$?
  fi
  set -e
  if [ "$rc" = 0 ]; then echo "${C_G}✓${C_0} $label"; else echo "${C_R}✗${C_0} $label"; fail "$label"; fi
}

# ---- stages
# Offline first boot: the image build already did the network work. Check it matches this VERSION instead.
offline_ready() {
  if [ "$DRY" = 1 ]; then echo "+ check $IMAGE_MARK is $VER (offline: packages and venvs come from the image)"; return 0; fi
  [ "$(cat "$IMAGE_MARK" 2>/dev/null)" = "$VER" ] || { echo "image build marker missing or not $VER"; return 1; }
}
st_packages() {
  if [ "$OFFLINE" = 1 ]; then offline_ready; return; fi
  run apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends plymouth plymouth-themes python3-pygame \
    python3-qrcode python3-venv python3-pip python3-cryptography alsa-utils openssl curl git ca-certificates libegl1 libegl-mesa0 libgles2 \
    libgbm1 libgl1-mesa-dri
}

st_users() {
  sh_ 'id grok >/dev/null 2>&1 || useradd -r -m -s /usr/sbin/nologin grok'
  sh_ 'getent group grok-admin >/dev/null || groupadd -r grok-admin'
  sh_ 'id grok-relay >/dev/null 2>&1 || useradd -r -g grok-admin -M -d /nonexistent -s /usr/sbin/nologin grok-relay'
  run usermod -aG audio grok
  # Every card from one image must get its own secrets, so the image build stops here; first boot makes them.
  [ "$IMAGE_BUILD" = 1 ] && return 0
  # Secrets and state (generated, never printed)
  run install -d -o root -g root -m 0711 /etc/grok-station $S
  gen $S/local_token grok-relay:grok 0440       # display + wake -> relay
  gen $S/admin_token grok-relay:grok-admin 0440 # admin page -> relay bots API
  # Loopback wake control token. Group grok is not service-only (it also reads /etc/grok-station/env and data
  # dirs), so the token gets its own group: grok (wake + display), grok-relay, and grok-admin (added in st_web).
  sh_ 'getent group grok-wake >/dev/null || groupadd -r grok-wake'
  run usermod -aG grok-wake grok
  run usermod -aG grok-wake grok-relay
  gen $S/wake_control_token root:grok-wake 0440
  # Station X25519 key (bots seal replies to it). The relay runs with ProtectSystem=strict, so /etc is read-only to
  # it and it can't create this file itself: make it here, and re-assert owner and mode on every run. A file that
  # is not exactly 32 bytes is unusable and is replaced (any pairing made with it could never have worked).
  sh_ "k=$S/station_box_key; if [ ! -f \$k ] || [ \$(stat -c %s \$k) != 32 ]; then umask 077; t=\$(mktemp $S/.k.XXXXXX); head -c 32 /dev/urandom > \$t; mv -f \$t \$k; echo 'station key created'; fi"
  run chown grok-relay:grok-admin $S/station_box_key
  run chmod 0600 $S/station_box_key
  sh_ "[ -s /etc/grok-station/env ] || { umask 077; printf 'GROK_STATION_TOKEN=%s\nGROK_RELAY_URL=ws://127.0.0.1:8790/ws\n' \$(openssl rand -hex 24) > /etc/grok-station/env; chown root:grok /etc/grok-station/env; chmod 0640 /etc/grok-station/env; }"
  run chown root:grok /etc/grok-station/env   # every run: an older admin save left it root:root 0600 (relay can't read the location)
  run chmod 0640 /etc/grok-station/env
  sh_ "[ -e /etc/grok-station/wake.env ] || { install -m 0640 -o root -g grok /dev/null /etc/grok-station/wake.env; }"
  for d in $V/state $V/bots $V/voice $V/audit $V/personas $V/portraits $V/alerts $V/wakewords; do
    run install -d -o grok-relay -g grok-admin -m 0770 "$d"
  done
  run chmod 0755 $V
  # voice/{minted,pending,replies,ack} sit inside $V/voice, which grok-relay owns: root `install -d -o` there would
  # follow a symlink the relay planted and chown its target (Nix 0.13.6 M-pre). Walk from the root-owned $V instead,
  # O_NOFOLLOW at every step; voice itself is re-asserted on the way. A refusal fails this stage.
  if [ "$DRY" = 1 ]; then echo "+ safe-mkdirs --mode 0770 --group grok-admin grok-relay $V voice/{minted,pending,replies,ack}"; else
    python3 -I - --mode 0770 --group grok-admin grok-relay "$V" voice voice/minted voice/pending voice/replies voice/ack \
      < tools/safe-mkdirs.py \
      || { echo "refusing to set up $V/voice: a path there is a symlink or not a directory"; return 1; }
  fi
  # 0.13.0 home control: the admin writes integrations.json here (0640, group grok-admin via setgid);
  # the relay (group grok-admin) reads it and cannot write. No secrets in it.
  run install -d -o grok-admin -g grok-admin -m 2750 $V/integrations
  # Homebridge "needs attention" flag (not a secret). Admin's group is grok; the relay owns the directory.
  # 2770 setgid so a new flag stays group grok and both can replace it. Mode on the file is 0640.
  run install -d -o grok-relay -g grok -m 2770 $V/home-status
  # Late answers are spoken by the wake listener (user grok). voice/ is grok-admin only, so this
  # directory is group grok and setgid: new files stay readable by grok, not by everyone.
  run install -d -o grok-relay -g grok -m 2770 $V/late
  # Wake-word upload inbox, grok-owned 0750. Not `install -d -o grok`: root would follow a symlink grok planted
  # there and chown and chmod its target (Nix 0.13.6 M1).
  if [ "$DRY" = 1 ]; then echo "+ safe-mkdirs --mode 0750 grok $V enroll/inbox"; else
    python3 -I - --mode 0750 grok "$V" enroll enroll/inbox < tools/safe-mkdirs.py \
      || { echo "refusing to set up $V/enroll: a path there is a symlink or not a directory"; return 1; }
  fi
  # Wi-Fi powersave off when NetworkManager is installed (2 = disabled). Journal cap. Both overwrite the same bytes.
  if [ -d /etc/NetworkManager/conf.d ]; then
    sh_ "printf '%s\n' '[connection]' 'wifi.powersave = 2' > /etc/NetworkManager/conf.d/wifi-powersave-off.conf"
  fi
  run install -d /var/log/journal /etc/systemd/journald.conf.d
  sh_ "printf '%s\n' '[Journal]' 'SystemMaxUse=200M' > /etc/systemd/journald.conf.d/size.conf"
}
gen() {  # gen <file> <owner:group> <mode>: create once with a random token
  sh_ "[ -s $1 ] || { umask 077; t=\$(mktemp $S/.t.XXXXXX); openssl rand -hex 32 > \$t; chown $2 \$t; chmod $3 \$t; mv -f \$t $1; }"
  sh_ "chown $2 $1 && chmod $3 $1"  # re-assert on upgrade: an older owner/mode hid the display's relay icon
}

st_display() {
  run install -d $O $O/assets/fonts $O/assets/portraits $O/assets/brand $O/assets/avatars $O/assets/status
  run install -m755 app/grok_station.py $O/
  run install -m644 app/dimming.py $O/
  run install -m644 app/avatars.py $O/
  run install -m644 app/cards.py $O/
  sh_ "install -m644 assets/fonts/* $O/assets/fonts/"
  sh_ "install -m644 assets/brand/*.png assets/brand/*.svg $O/assets/brand/"
  sh_ "install -m644 assets/avatars/default-*.svg $O/assets/avatars/"
  sh_ "install -m644 assets/status/*.svg $O/assets/status/"
  run install -m644 systemd/grok-station.service /etc/systemd/system/
}

st_relay() {  # native venv, loopback only
  run install -d -o root -g root -m 0755 $O/relay
for f in relay.py handoff.py tools.py t1.py botreply.py late_answer.py voices.py home.py; do run install -m644 relay/$f $O/relay/; done
  sh_ "[ -x $O/relay/venv/bin/python ] || python3 -m venv $O/relay/venv"
  if [ "$OFFLINE" != 1 ]; then run $O/relay/venv/bin/pip install --quiet --require-hashes -r relay/requirements.txt; fi
  run install -m644 systemd/grok-station-relay.service /etc/systemd/system/
  run rm -f $O/relay/release.py  # old updater module, no longer used
}

_vosk_pip() {
  # A failed install must not fail the station: the tile still works by touch.
  # Offline first boot already has the image venv; do not print or run pip.
  if [ "$OFFLINE" = 1 ]; then
    return 0
  fi
  if [ "$DRY" = 1 ]; then
    echo "+ $O/wake/venv/bin/pip install --quiet -r wake/requirements-vosk.txt"
    return 0
  fi
  "$O/wake/venv/bin/pip" install --quiet -r wake/requirements-vosk.txt || echo "stop word: off (on-device recogniser not installed); touch still works"
}

_vosk_model() {
  # Online only. Dry run prints the fetch and never the checksum. A bad zip is deleted and the install continues.
  model=vosk-model-small-en-us-0.15
  sha=30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498
  url=https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
  dir=$O/wake/models/$model
  if [ "$DRY" = 1 ]; then
    echo "+ fetch $url and verify sha256 before unzip into $dir"
    return 0
  fi
  if [ "$OFFLINE" = 1 ]; then
    return 0
  fi
  # Already installed: trust it only as a real grok-owned folder with a matching marker. Root never repairs
  # ownership here (Nix 0.13.5 I2); anything else is reinstalled as grok below.
  if [ -d "$dir" ] && [ ! -L "$dir" ] && [ "$(stat -c %U "$dir")" = grok ] && [ -f "$dir/.sha256" ] && [ ! -L "$dir/.sha256" ]; then
    mark=$(tr -d '[:space:]' < "$dir/.sha256" 2>/dev/null || true)
    if [ "$mark" = "$sha" ]; then
      return 0
    fi
  fi
  # A symlink or a folder not owned by grok in the way: remove the path itself. rm does not follow the link or
  # leave this file system, and nothing writes or chowns through it. A grok-owned folder is replaced by the unpack.
  if [ -L "$dir" ] || { [ -e "$dir" ] && [ "$(stat -c %U "$dir")" != grok ]; }; then
    rm -rf --one-file-system -- "$dir"
  fi
  tmp=$(mktemp 2>/dev/null) || { echo "stop word: off (on-device recogniser not installed); touch still works"; return 0; }
  curl -fsSL --max-time 120 -o "$tmp" "$url" || { rm -f "$tmp"; echo "stop word: off (on-device recogniser not installed); touch still works"; return 0; }
  got=$(sha256sum "$tmp" 2>/dev/null | awk 'NR==1 {print $1}') || true
  if [ "$got" != "$sha" ]; then
    rm -f "$tmp"
    echo "stop word: model checksum mismatch; touch still works"
    return 0
  fi
  # Least privilege (Nix 0.13.5 L1): $O/wake/models is grok-owned, so the unpack runs as grok, never root.
  # Root only verified the SHA above; grok reads a root-owned copy. The script goes in on stdin so grok
  # does not need to read this clone.
  zd=$(mktemp -d 2>/dev/null) || { rm -f "$tmp"; echo "stop word: off (on-device recogniser not installed); touch still works"; return 0; }
  # Root keeps $zd (Nix 0.13.5 L1-b): grok only gets group read on the folder and the zip, never ownership.
  install -d -o root -g grok -m 0750 "$zd" && install -o root -g grok -m 0440 "$tmp" "$zd/model.zip" || { rm -rf "$zd"; rm -f "$tmp"; echo "stop word: off (on-device recogniser not installed); touch still works"; return 0; }
  # grok writes $dir itself, so it is grok-owned without a chown.
  if ! runuser -u grok -- python3 - "$zd/model.zip" "$O/wake/models" "$model" "$sha" < tools/vosk-unpack.py; then
    echo "stop word: off (on-device recogniser not installed); touch still works"
  fi
  rm -rf "$zd"
  rm -f "$tmp"
}

st_wake() {
  # portraits: persona cache the wake listener writes. Not `install -d -o grok`: it follows a symlink grok planted
  # and chowns the target to grok (0.13.6). safe-mkdirs refuses any symlink on the way.
  if [ "$DRY" = 1 ]; then echo "+ safe-mkdirs grok $O wake/models/enrolled portraits"; else
    python3 -I - grok "$O" wake wake/models wake/models/enrolled portraits < tools/safe-mkdirs.py \
      || { echo "refusing to set up $O/wake: a path there is a symlink or not a directory"; return 1; }
  fi
  sh_ "[ -x $O/wake/venv/bin/python ] || python3 -m venv $O/wake/venv"
  # openwakeword stays --no-deps. These three are exact pins; the rest stay lower bounds so pip
  # never considers ancient candidates (e.g. piwheels' joblib-0.7.0d wheel with an invalid name).
  if [ "$OFFLINE" != 1 ]; then
  run $O/wake/venv/bin/pip install --quiet --no-deps "openwakeword==0.6.0"
  run $O/wake/venv/bin/pip install --quiet "onnxruntime==1.31.0" "numpy==2.5.4" tqdm "scipy>=1.14.1" "scikit-learn>=1.6" "joblib>=1.4" \
    requests "websockets>=17"
  run $O/wake/venv/bin/python -c 'import openwakeword.utils as u; u.download_models(["feature-models-only"])'
  fi
  run python3 -I - --put grok "$O" wake wake/grok_wake.py wake/gain.py wake/fewshot.py wake/wait_tone.py wake/stopword.py wake/wakewords.json < tools/safe-mkdirs.py  # dir fds, O_NOFOLLOW (Nix 0.13.6 L3)
  # Only the built-in Hey Grok model ships. Custom words are trained or uploaded into $O/wake/models/enrolled.
  run python3 -I - --put grok "$O" wake/models wake/models/hey_grok.onnx < tools/safe-mkdirs.py  # dir fds, O_NOFOLLOW (Nix 0.13.6 L3)
  # Everyday-speech bank (synthetic voices) that recorded wake words are checked against (wake/fewshot.py).
  run python3 -I - --put grok "$O" wake/models wake/models/fewshot_negatives.npz < tools/safe-mkdirs.py  # dir fds, O_NOFOLLOW (Nix 0.13.6 L3)
  # 0.9.4: drop the starter models 0.9.2 used to ship, and their override entries. Only these six exact files in
  # $O/wake/models (never models/enrolled), and an override is kept when an enrolled model of that name exists.
  sh_ "rm -f $O/wake/models/hey_ash.onnx $O/wake/models/hey_jules.onnx $O/wake/models/hey_kai.onnx $O/wake/models/hey_miles.onnx $O/wake/models/hey_piper.onnx $O/wake/models/hey_rhea.onnx"
  if [ "$DRY" = 1 ]; then echo "+ prune retired starter wake words from $V/admin/wake-overrides.json"; else
    # Read and rewrite without following a symlink: the prune prints the new JSON (nothing when unchanged), and
    # safe-mkdirs --replace writes it through $V/admin's descriptor (temp O_CREAT|O_EXCL|O_NOFOLLOW, fchown/fchmod
    # to the old owner and mode, rename in that dir). A planted symlink fails the stage (Nix 0.13.6 M-pre).
    ov=$(python3 - "$V/admin/wake-overrides.json" "$O/wake/models/enrolled" <<'PY'
import json, os, sys
p, enr = sys.argv[1], sys.argv[2]
old = ["hey_ash", "hey_jules", "hey_kai", "hey_miles", "hey_piper", "hey_rhea"]
try:
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as f:
        ov = json.load(f)
except Exception:
    sys.exit(0)
if not isinstance(ov, dict):
    sys.exit(0)
gone = [n for n in old if n in ov and not os.path.isfile(os.path.join(enr, n + ".onnx"))
        and not os.path.isfile(os.path.join(enr, n + ".fewshot.npz"))]
if gone:
    for n in gone:
        ov.pop(n)
    sys.stdout.write(json.dumps(ov, indent=1))
PY
) || ov=""
    if [ -n "$ov" ]; then
      printf '%s' "$ov" | python3 -I tools/safe-mkdirs.py --replace "$V" admin wake-overrides.json \
        || { echo "refusing to rewrite $V/admin/wake-overrides.json: a path there is a symlink or not a plain file"; return 1; }
    fi
  fi
  # vosk: offline speech recognition so saying stop works during wake-word training; grammar limited to stop, never leaves the station.
  _vosk_pip
  _vosk_model
}

st_boot() {  # boot splash and kiosk; hides the blinking console cursor
  if [ "$DRY" = 1 ]; then echo "+ python3 tools/boot-config.py"; else
    [ "$(python3 tools/boot-config.py)" = reboot ] && touch "$FLAGS/reboot"
  fi
  sh_ "grep -q '^disable_splash=1' /boot/firmware/config.txt || echo disable_splash=1 >> /boot/firmware/config.txt"
  sh_ "if sh tools/install-splash.sh '$PWD' --prepare; then plymouth-set-default-theme -R echo; else plymouth-set-default-theme -R text || true; fi"
  run systemctl mask getty@tty1.service
}

st_web() {  # admin page (HTTPS) and services
  if [ "$MODE" = update ]; then run sh tools/install-files.sh "$PWD" "$VER"
  else run env GROK_STATION_NO_RESTART=1 sh tools/install-files.sh "$PWD" "$VER"; fi
  # Update button on the admin page: a fixed oneshot unit that re-runs bootstrap.sh from this clone.
  sh_ "printf '%s\\n' '$PWD' > /etc/grok-station/source-dir && chmod 0644 /etc/grok-station/source-dir"
  run install -o root -g root -m755 tools/grok-station-update /usr/local/lib/grok-station/grok-station-update
  run install -m644 systemd/grok-station-update.service /etc/systemd/system/
  run usermod -aG grok-admin,audio grok-admin   # admin_token (group grok-admin); audio: list mics and speakers
  run usermod -aG grok-wake grok-admin          # wake_control_token (group grok-wake)
  run systemctl daemon-reload
  run systemctl enable grok-station.service grok-station-wake.service grok-station-relay.service grok-station-admin.service
  if [ "$MODE" = update ]; then
    run systemctl restart grok-station-relay grok-station-wake grok-station-admin grok-station
  else  # fresh: everything starts at the reboot, so the setup code shows once, on a clean boot
    run systemctl stop grok-station-admin grok-station grok-station-wake grok-station-relay || true
  fi
}

st_health() {
  # Post-install check. Update: every service must be active and the admin page must answer on HTTPS.
  # Fresh install: nothing runs until the reboot, so check what the boot needs (units, config, venvs, imports).
  if [ "$DRY" = 1 ]; then echo "+ check grok-station-relay grok-station-admin grok-station grok-station-wake ($MODE)"; return 0; fi
  bad=0
  for u in grok-station-relay grok-station-admin grok-station grok-station-wake; do
    if [ -f /etc/systemd/system/$u.service ] && systemctl is-enabled -q $u; then echo "ok   $u enabled"; else echo "!!   $u is not installed or enabled"; bad=1; fi
  done
  for f in /etc/grok-station/env $S/local_token $S/admin_token $S/wake_control_token /opt/grok-station/VERSION; do
    if [ -s "$f" ]; then echo "ok   $f"; else echo "!!   missing $f"; bad=1; fi
  done
  chk() { if "$@" >/dev/null 2>&1; then echo "ok   $*"; else echo "!!   failed: $*"; "$@" 2>&1 | tail -n 5; bad=1; fi; }
  chk $O/relay/venv/bin/python -c 'import aiohttp, nacl'
  chk $O/relay/venv/bin/python -m py_compile $O/relay/relay.py
  chk $O/wake/venv/bin/python -c 'import openwakeword, onnxruntime, numpy, websockets'
  chk python3 -c 'import pygame, qrcode, cryptography'
  chk python3 -m py_compile /opt/grok-station/grok_station.py /opt/grok-station/dimming.py /opt/grok-station/cards.py /opt/grok-station/admin/grok_admin.py
  if [ "$MODE" = update ]; then
    sleep 3
    for u in grok-station-relay grok-station-admin grok-station grok-station-wake; do
      i=0; until systemctl is-active -q $u || [ $i -ge 20 ]; do sleep 1; i=$((i+1)); done
      if systemctl is-active -q $u; then echo "ok   $u running"; else
        echo "!!   $u is not running. Last log lines:"; journalctl -u $u -n 25 --no-pager; bad=1; fi
    done
    # The admin page talks to the relay on loopback. A running unit that doesn't answer is still a failure.
    i=0; until curl -s -o /dev/null --max-time 3 http://127.0.0.1:8790/healthz || [ $i -ge 15 ]; do sleep 2; i=$((i+1)); done
    if curl -s -o /dev/null --max-time 3 http://127.0.0.1:8790/healthz; then echo "ok   relay answers on loopback"; else
      echo "!!   relay does not answer on 127.0.0.1:8790. Last log lines:"; journalctl -u grok-station-relay -n 25 --no-pager; bad=1; fi
    i=0; until curl -ks -o /dev/null --max-time 5 https://127.0.0.1/ || [ $i -ge 10 ]; do sleep 2; i=$((i+1)); done
    if curl -ks -o /dev/null --max-time 5 https://127.0.0.1/; then echo "ok   admin page answers on https://127.0.0.1"; else
      echo "!!   admin page does not answer on https://127.0.0.1. Last log lines:"; journalctl -u grok-station-admin -n 25 --no-pager; bad=1; fi
  fi
  [ -e /dev/fb0 ] || touch "$FLAGS/nofb"
  [ "$bad" = 0 ]
}

echo "${C_B}grok-station${C_0} installer ($VER)"
[ "$DRY" = 1 ] && echo "${C_Y}Dry run: nothing is changed.${C_0}"
stage "Updating software"               st_packages
stage "Setting up users and secrets"    st_users
if [ "$IMAGE_BUILD" = 1 ]; then
  # Image build: only the stages that need the network. The rest runs on the Pi at first boot.
  stage "Installing voice relay"          st_relay
  stage "Installing wake listener (Hey Grok)" st_wake
  sh_ "printf '%s\n' '$VER' > $IMAGE_MARK"
  echo "${C_G}${C_B}Image prepared ($VER).${C_0} First boot finishes the install offline."
  exit 0
fi
stage "Installing the display"          st_display
stage "Installing voice relay"          st_relay
stage "Installing wake listener (Hey Grok)" st_wake
stage "Setting up boot screen"          st_boot
stage "Setting up web service"          st_web
stage "Checking health"                 st_health

HOST="$(hostname 2>/dev/null || echo grok-station)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
addresses() {
  echo "  ${C_C}https://$HOST.local${C_0}"
  if [ -n "$IP" ]; then echo "  ${C_C}https://$IP${C_0}"; fi
}
echo
if [ "$DRY" = 1 ]; then echo "${C_G}${C_B}Dry run done ($VER, $MODE).${C_0} Commands are in $LOG."; fi
if [ "$MODE" = update ]; then
  [ "$DRY" = 1 ] || echo "${C_G}${C_B}Updated ($VER).${C_0} The display and the admin page are running:"
  addresses
  if [ -e "$FLAGS/reboot" ]; then
    echo "${C_Y}!${C_0} Boot options changed. Reboot when convenient: sudo reboot"
    echo "reboot required: boot options changed" >> "$LOG"
    [ "$DRY" = 1 ] || touch "$REBOOT_FLAG"
  fi
  echo "Log: $LOG"
  exit 0
fi
[ "$DRY" = 1 ] || echo "${C_G}${C_B}Installed ($VER).${C_0} The station starts after a reboot."
echo "After the reboot, open on a phone or laptop:"
addresses
echo "Then enter the setup code shown on the station screen, set an admin password and paste your xAI API key."
echo "(Fallback without a browser: sudo grok-station set-key)   Log: $LOG"
echo
if [ "$DRY" = 1 ] || [ "$NOREBOOT" = 1 ]; then echo "Reboot to finish: sudo reboot"; exit 0; fi
if { : < /dev/tty; } 2>/dev/null && [ -t 1 ]; then
  printf '%sPress Enter to reboot (Ctrl-C to skip)%s ' "$C_B" "$C_0"
  IFS= read -r _ < /dev/tty || { echo; echo "Reboot to finish: sudo reboot"; exit 0; }
  echo "Rebooting…"
  systemctl reboot
else
  echo "Reboot to finish: sudo reboot"
fi
