# RetroSync

Hands-off save backup from retro flash carts to Google Drive.

Two hardware targets so far:

- **FXPak Pro** (SNES) over USB — continuous, ~30s polling.
- **Analogue Pocket** (SNES core) over USB mass-storage — on-demand, fired
  by udev when you flip the Pocket into "Mount as USB Drive".

Both feed into the same Google Drive bucket so a save made on either
side picks up where the other left off. The foundation is built so adding
EverDrives (N64 / GB / MD) and other handheld emulator targets later
doesn't require redesign.

See [docs/design.pdf](docs/design.pdf) for the full design and
[docs/pocket-sync-design.md](docs/pocket-sync-design.md) for the
bidirectional / Pocket Sync extension.

---

## What it does

- A daemon on a Raspberry Pi polls the cart over USB every ~30 seconds.
- Save files (`.srm`) are hashed; changed saves go through a stability
  debounce (3 consecutive identical reads, ~90 s) to avoid uploading torn
  writes mid-flush.
- Each new save state is uploaded to Google Drive at a deterministic
  versioned path:

  ```
  gdrive:retro-saves/<system>/<game-id>/
      current.srm
      manifest.json
      versions/
          2026-04-25T18-32-04Z--a3f1c290.srm
          2026-04-25T19-07-18Z--b88e0144.srm
          ...
  ```

- A small CLI on the Pi lets you list versions and restore any one of them
  back to the cart.
- The daemon survives power loss; on next boot it reconciles its SQLite
  state against what's actually in Drive and keeps going.

---

## Install (4 steps)

You need a Raspberry Pi 4 or 5, a microSD card, a USB cable to the FXPak
Pro, and a Google account. Setup is headless — no monitor or keyboard
ever attaches to the Pi.

### Step 1 — image the SD card from your Mac

Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on
your Mac. Insert the microSD card.

In Imager:

1. **Operating System** → "Raspberry Pi OS Lite (64-bit)".
2. **Storage** → your microSD card.
3. Click **Next** → **Edit settings** (or press ⌘+Shift+X) and configure:
   - Hostname: `retrosync`
   - Username: `pi`, choose a strong password
   - WiFi: your SSID + password (set country)
   - SSH: enabled, "Allow public-key authentication only" — paste in
     your Mac's `~/.ssh/id_ed25519.pub` (or generate one first with
     `ssh-keygen -t ed25519`).
4. **Save** → **Yes** to apply settings → **Yes** to write.

Eject the SD card. Insert it into the Pi. Plug the Pi into power.

### Step 2 — SSH in from your Mac

Wait ~60 s for first boot, then from your Mac:

```bash
ssh pi@retrosync.local
```

If `.local` doesn't resolve, find the Pi's IP from your router's admin
page and `ssh pi@<ip-address>`.

### Step 3 — run the installer

On the Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/indiefan/RetroSync/main/install/setup.sh \
  | sudo bash
```

It will:

- Install apt dependencies, SNI, rclone, and the daemon.
- Walk you through `rclone config` for Google Drive — when it asks for the
  scope, choose **`drive.file`** so RetroSync can only see files it
  creates. The OAuth flow gives you a URL; open it in your Mac browser,
  sign in, copy the verification code back to the Pi.
- Enable the systemd services and start the daemon.

### Step 4 — plug in the FXPak Pro

Connect the FXPak Pro's USB port to a USB port on the Pi. (No need to
power-cycle the SNES.)

Verify:

```bash
retrosync test-cart fxpak-pro-1   # should print device + firmware
retrosync test-cloud              # should print 'OK ...'
journalctl -u retrosync.service -f
```

You're done. The Pi will resume polling automatically on every boot, no
matter how many times it loses power.

---

## Operator commands

The `retrosync` command is a wrapper that auto-elevates to the
`retrosync` system user, so you don't need `sudo` for any of these:

```bash
retrosync status                          # daemon-wide summary (incl. open conflicts)
retrosync list                            # every (source, save) on record
retrosync show fxpak-pro-1 /Mario.srm     # version history for one save
retrosync sync-status                     # last-synced hash per (source, game)
retrosync test-cart fxpak-pro-1           # smoke-test cart connection
retrosync test-cloud                      # smoke-test rclone remote

# Restore a save:
retrosync pull <cloud-path-from-show> /tmp/restored.srm
retrosync push fxpak-pro-1 /Mario.srm /tmp/restored.srm --confirm

# Conflicts (when bidirectional sync sees both sides change):
retrosync conflicts list                  # open conflicts only
retrosync conflicts list --all            # incl. resolved (auto + manual)
retrosync conflicts show <id>
retrosync conflicts resolve <id> --winner {cloud | device | <hash>}

# By default `conflict_winner: device` in config.yaml — divergences are
# auto-resolved by promoting the device's bytes; the previous cloud
# bytes stay in versions/ for recovery. Set `conflict_winner: preserve`
# to require manual resolve instead.

# Pocket sync (normally fired by udev; manual override):
sudo retrosync pocket-sync --device /dev/sda1

# Migrate the legacy unknown_*/<crc32>_* cloud layout:
retrosync migrate-paths --dry-run        # plan only
retrosync migrate-paths                  # apply

# Get the latest source + re-apply the installer in one step.
retrosync upgrade
```

Configuration: `/etc/retrosync/config.yaml`. Restart with
`sudo systemctl restart retrosync` after changes.

### Pocket sync setup

1. Plug the Pocket into the Pi. On the Pocket: **Tools → USB → Mount as
   USB Drive**.
2. From the Pi, capture the Pocket's USB IDs:

   ```bash
   lsusb | grep -i analogue
   # → Bus 001 Device 005: ID XXXX:YYYY Analogue Pocket
   ```

3. Edit `/etc/udev/rules.d/99-retrosync-pocket.rules`, replacing the
   `XXXX:YYYY` placeholders with the IDs from `lsusb`. Then:

   ```bash
   sudo udevadm control --reload
   sudo udevadm trigger
   ```

4. Optional but recommended: add a `pocket-1` source to
   `/etc/retrosync/config.yaml` so its options (core name, file
   extension) are stored alongside the FXPak entry:

   ```yaml
   sources:
     - id: pocket-1
       adapter: pocket
       options:
         # The openFPGA SNES core writes saves under Saves/snes/common/
         # rather than its own per-core folder.
         core: snes/common
         file_extension: .sav
   ```

5. Flip on bidirectional sync once you've verified Pocket and FXPak save
   formats round-trip cleanly (see
   [docs/pocket-sync-design.md §10](docs/pocket-sync-design.md)):

   ```yaml
   cloud_to_device: true
   ```

Each plug-in fires a one-shot sync. Watch progress with
`journalctl -u 'retrosync-pocket-sync@*'`.

---

## Project layout

```
retrosync/
├── retrosync/             # the Python package
│   ├── sources/
│   │   ├── base.py        # SaveSource protocol — the extension point
│   │   ├── usb2snes.py    # WebSocket client for SNI / QUsb2snes
│   │   ├── fxpak.py       # FXPak Pro adapter
│   │   ├── pocket.py      # Analogue Pocket adapter (mounted SD)
│   │   └── registry.py    # config 'adapter' string -> ctor
│   ├── pocket/            # udev-fired one-shot Pocket sync runner
│   ├── game_id.py         # canonical slug + alias resolution
│   ├── state.py           # SQLite store
│   ├── cloud.py           # rclone wrapper, path scheme, manifest v2
│   ├── orchestrator.py    # FXPak poll/diff/debounce loop
│   ├── sync.py            # bidirectional engine (shared)
│   ├── conflicts.py       # conflict storage + resolve helpers
│   ├── migrate.py         # one-shot legacy-layout migration
│   ├── daemon.py          # systemd entry point
│   ├── cli.py             # operator CLI
│   └── config.py
├── install/
│   ├── setup.sh           # one-shot installer
│   ├── systemd/           # unit files (incl. Pocket sync template)
│   └── udev/              # 99-retrosync-pocket.rules
├── docs/
│   ├── design.pdf                # original design
│   ├── pocket-sync-design.md     # Pocket Sync extension
│   ├── architecture.png          # diagram
│   └── imaging.md
├── tests/
└── pyproject.toml
```

## Adding a new source

The whole orchestrator is built around the `SaveSource` protocol in
[`retrosync/sources/base.py`](retrosync/sources/base.py). Implement it,
register the adapter in `registry.py`, and add a stanza to your
`config.yaml`. The bidirectional sync engine in `retrosync/sync.py`
takes over from there — the per-source code only has to know how to
read, write, and list saves.

The Pocket adapter ([`retrosync/sources/pocket.py`](retrosync/sources/pocket.py))
is the simplest current example: it backs onto a directory, so the
whole adapter is ~80 lines.

## License

MIT.
