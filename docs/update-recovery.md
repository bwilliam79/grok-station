# One-time update recovery (after main was rewritten)

Stations on 0.8.10 or older can't update from the admin page after main on GitHub was rewritten: their updater
only accepts a fast-forward. Run this once on the station over ssh. Settings in `/etc/grok-station` and
`/var/lib/grok-station` are not touched, and untracked files in the copy are kept.

```bash
SRC=$(cat /etc/grok-station/source-dir)
OWNER=$(stat -c %U "$SRC")
sudo -u "$OWNER" git -C "$SRC" remote get-url origin   # must be the grok-station GitHub repo
sudo -u "$OWNER" git -C "$SRC" fetch origin
sudo -u "$OWNER" git -C "$SRC" checkout main
sudo -u "$OWNER" git -C "$SRC" reset --hard origin/main
sudo GROK_STATION_DIR="$SRC" GROK_STATION_NONINTERACTIVE=1 sh "$SRC/install.sh"
```

`install.sh` installs the new version and restarts the station's services. After this, updates from the admin
page work again, including after any future rewrite.
