# RetroSync

Hands-off save backup from retro flash carts to Google Drive.

Four hardware targets so far:

- **FXPak Pro** (SNES) over USB — continuous, ~30s polling.
- **Analogue Pocket** (SNES core) over USB mass-storage — on-demand, fired
  by udev when you flip the Pocket into "Mount as USB Drive".
- **Steam Deck (EmuDeck)** over WiFi — inotify-driven, sub-10-second
  push from in-game save to cloud + pre-launch pull via a Steam ROM
  Manager-installed shortcut wrapper.
- **EverDrive 64 X7** (N64) over USB — same model as the FXPak Pro
  (continuous polling, instant-sync via udev poke). Multi-format
  saves (.eep / .sra / .fla / .mp1–.mp4) get packed into a combined
  Mupen64Plus-format `.srm` for cloud-side hash equivalence with the
  Deck's RetroArch-Mupen64Plus-Next save.

All three feed into the same Google Drive bucket so a save made on any
device picks up where the others left off. The foundation is built so
adding EverDrives (N64 / GB / MD) and other emulator targets later
doesn't require redesign.

See [docs/design.pdf](docs/design.pdf) for the full design,
[docs/pocket-sync-design.md](docs/pocket-sync-design.md) for the
bidirectional / Pocket Sync extension,
[docs/emudeck-sync-design.md](docs/emudeck-sync-design.md) for the
Steam Deck / EmuDeck extension (lease coordination, inotify push, SRM
shortcut wrapper), and
[docs/n64-sync-design.md](docs/n64-sync-design.md) for the N64
extension (EverDrive 64 X7 over USB, multi-format save translator,
generalized per-system format hooks).

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

# Load the cloud's current save for a game onto a device by game-id:
retrosync load final_fantasy_iii pocket   # writes to mounted Pocket SD
retrosync load f_zero snes                # writes to FXPak via usb2snes

# Lower-level restore:
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
#
# For multi-device setups where a device's source_id may have changed
# (e.g. Pocket per-physical-UUID migration) and you'd rather inherit
# cloud's latest than regress to stale device data, set:
#   cloud_wins_on_unknown_device: true
# This preserves the unknown device's bytes as a versions/* entry
# (recoverable later) but makes cloud's current the winner.
#
# RECOMMENDED on the Pi: set this to flip the case-7 (both moved)
# default from "device wins" to "cloud wins". A cart's bytes that
# diverged from h_last are usually session noise (a different game's
# autosave leftover, a power-cycle artifact), not a deliberate save —
# letting cloud win means another device's deliberate save survives.
# Device bytes are still preserved in versions/ for recovery.
#   cloud_wins_on_diverged_device: true

# Pocket sync (normally fired by udev; manual override):
sudo retrosync pocket-sync --device /dev/sda1

# Force-promote a historical version to be cloud's current save
# (e.g. revert after a stale device overwrote a real save):
retrosync promote final_fantasy_iii 7def5901
# Devices pull the promoted bytes on their next sync (case 6).

# Active-device leases (cloud-stored coordinator across devices):
retrosync lease list                     # who's holding what (per system)
retrosync lease show deck-1:super_metroid
retrosync lease release deck-1:super_metroid --force

# EmuDeck / Steam Deck:
retrosync deck detect-paths              # debug saves/roms detection
retrosync deck patch-srm                 # (re)apply the SRM wrapper patch
retrosync deck patch-srm --unpatch       # restore originals
retrosync flush --timeout 10             # drain in-flight uploads (suspend)
retrosync sync-pending                   # retry deferred uploads (reconnect)

# Per-source ROM-stem → save filename cache (EmuDeck/Pocket bootstrap):
retrosync filename-map list
retrosync filename-map invalidate deck-1
retrosync filename-map invalidate deck-1 super_metroid

# Migrate the legacy unknown_*/<crc32>_* cloud layout:
retrosync migrate-paths --dry-run        # plan only
retrosync migrate-paths                  # apply

# Get the latest source + re-apply the installer in one step.
retrosync upgrade
```

Configuration: `/etc/retrosync/config.yaml`. Restart with
`sudo systemctl restart retrosync` after changes.

### FXPak Pro instant-sync (optional)

By default the daemon polls every 30s. When the cart is off, it
falls back to a 2s recheck so cart-on → first sync latency is at
most a couple seconds. To make it sub-second, install the FXPak
udev rule with the cart's actual USB IDs:

```bash
# With the SNES powered on (cart connected via USB to the Pi):
lsusb     # find the cart's vendor:product line
sudoedit /etc/udev/rules.d/99-retrosync-fxpak.rules   # replace XXXX:YYYY
sudo udevadm control --reload && sudo udevadm trigger
```

The rule signals the daemon (SIGUSR1) on USB attach, which pokes
every orchestrator into running an immediate pass — useful if you
want cloud-newer saves to land on the cart before you launch a
ROM.

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
         # ROM extensions to scan when picking a filename for a fresh
         # save (the Pocket loads saves by ROM-stem match).
         rom_extensions: [".smc", ".sfc"]
         # Region preference when multiple ROMs match the same game
         # (e.g. USA + Europe + Japan dumps). USA-first by default.
         region_preference: [usa, world, europe, japan]
   ```

5. Flip on bidirectional sync once you've verified Pocket and FXPak save
   formats round-trip cleanly (see
   [docs/pocket-sync-design.md §10](docs/pocket-sync-design.md)):

   ```yaml
   cloud_to_device: true
   ```

Each plug-in fires a one-shot sync. Watch progress with
`journalctl -u 'retrosync-pocket-sync@*'`.

### EverDrive 64 X7 setup

Plugs into the same Pi as the FXPak Pro (separate USB port; no
conflict — different protocols). The cart enumerates as an FT232
serial UART; the kernel auto-binds `ftdi_sio` and exposes it at
`/dev/ttyUSB*`. RetroSync talks to it via `pyserial` over that
serial port.

1. Install pyserial (one-time):

   ```bash
   sudo apt install -y python3-serial
   ```

2. Confirm the cart enumerates and find its serial port:

   ```bash
   ls -l /dev/ttyUSB*               # expect /dev/ttyUSB0 or similar
   sudo dmesg | grep -i ftdi | tail -5
   ```

3. Add a stanza to `/etc/retrosync/config.yaml`:

   ```yaml
   sources:
     - id: everdrive64-1
       adapter: everdrive64
       options:
         transport: serial
         serial_path: /dev/ttyUSB0     # or whatever ttyUSB* came up
         serial_baud: 9600             # FT232 baud is largely cosmetic
         sd_saves_root: /ED64/SAVES
         sd_roms_root: /ED64/ROMS
         rom_extensions: [.z64, .n64, .v64]
         system: n64
   ```

4. Restart and verify the handshake:

   ```bash
   sudo systemctl restart retrosync
   retrosync test-cart everdrive64-1
   # Expect: health: OK - EverDrive 64 (handshake ok, status=...)
   ```

**Status:** handshake + SD-file operations (file_open / read / write
/ close / info) are implemented per Krikzz's `usb64` tool source and
verified against real hardware. The remaining gap is **directory
listing** — Krikzz's tool requires explicit paths and doesn't expose
a dir-list command, so the adapter can't auto-enumerate saves on the
SD. Workaround: declare ROM filenames in config under
`options.rom_filenames` (one per game you care about), and the
adapter uses `file_exists` to enumerate per-format save files for
each. Once an OS64 dir-list byte is reverse-engineered, this becomes
optional.

```yaml
options:
  ...
  rom_filenames:
    - "Super Mario 64 (USA).z64"
    - "The Legend of Zelda - Ocarina of Time (USA).z64"
    - "Paper Mario (USA).z64"
```

Add filenames as they appear on your SD card; the adapter strips the
extension and probes for `<stem>.eep / .sra / .fla / .mp1..mp4`.

### Steam Deck (EmuDeck) setup

The Deck runs its own user-systemd daemon that watches RetroArch's
saves directory with inotify and pushes to the same Drive bucket the
Pi uses. Pre-launch syncs are wired in via a Steam ROM Manager
shortcut wrapper. Full design: [docs/emudeck-sync-design.md](docs/emudeck-sync-design.md).

1. SSH to the Deck (or open a Konsole in Desktop Mode) **as the `deck`
   user — NOT root**. SteamOS keeps `/usr` read-only, so the install is
   user-space.

   ```bash
   git clone https://github.com/indiefan/RetroSync.git
   cd RetroSync
   bash install/setup-deck.sh
   ```

   The installer:
   - Detects EmuDeck and the RetroArch saves dir from `retroarch.cfg`.
   - Drops a static `rclone` binary into `~/.local/bin/`.
   - Builds a venv at `~/.local/share/retrosync/.venv`.
   - Installs the wrap dispatcher (`~/.local/bin/retrosync-wrap`).
   - Writes `~/.config/retrosync/config.yaml` with a pre-filled
     `deck-1` source.
   - Installs user-systemd units (`retrosyncd-deck.service`,
     `retrosyncd-suspend.service`, `retrosync-reconnect.service`).
   - `loginctl enable-linger deck` so the daemon survives Game Mode.
   - Walks you through `rclone config` for Google Drive.
   - Patches Steam ROM Manager parser configs (idempotent).

2. **Re-run Steam ROM Manager once** (EmuDeck → Tools → Steam ROM
   Manager) → Add Games → Parse → Save to Steam. This regenerates
   your shortcuts with the wrapper baked in. After that, every game
   you launch via Steam runs a pre-launch sync first.

3. The lease coordinator (active across FXPak / Pocket / Deck) declares
   "this device is currently playing X" so other devices don't silently
   overwrite. Soft mode (default) just warns on contention; switch to
   hard mode in `config.yaml` once every device runs v0.3+:

   ```yaml
   lease:
     mode: hard
   ```

4. Operational logs:
   ```bash
   journalctl --user -u retrosyncd-deck -f       # watch saves stream
   retrosync deck detect-paths                   # debug path detection
   retrosync lease list                          # who's holding what
   retrosync filename-map list                   # ROM-stem → save cache
   ```

5. Re-running `bash install/setup-deck.sh` is safe (idempotent). The
   SRM patch and config writes detect existing state and leave it alone.

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
│   │   ├── emudeck.py     # EmuDeck (RetroArch saves dir) adapter
│   │   ├── everdrive64/   # EverDrive 64 X7 (N64 over USB)
│   │   └── registry.py    # config 'adapter' string -> ctor
│   ├── transport/         # low-level device transports (USB, etc.)
│   │   └── krikzz_ftdi.py     # FT245 protocol shared across Krikzz carts
│   ├── formats/           # multi-file save translators
│   │   └── n64.py             # N64SaveSet, combine(), split()
│   ├── system_formats.py  # per-system canonical-format registry
│   ├── pocket/            # udev-fired one-shot Pocket sync runner
│   ├── deck/              # Steam Deck / EmuDeck-specific code
│   │   ├── emudeck_paths.py  # auto-detect EmuDeck root + saves dir
│   │   ├── srm.py            # Steam ROM Manager config patcher
│   │   ├── wrap.py           # pre-launch sync + lease grab subcommands
│   │   └── flush.py          # pre-suspend / network-reconnect helper
│   ├── game_id.py         # canonical slug + alias resolution
│   ├── filename_map.py    # ROM-scan + cache for ROM-stem-named saves
│   ├── state.py           # SQLite store
│   ├── cloud.py           # rclone wrapper, path scheme, manifest v3
│   ├── promote.py         # `retrosync promote` — force a version to current
│   ├── orchestrator.py    # FXPak / EverDrive 64 poll/diff/debounce loop
│   ├── inotify_orchestrator.py  # inotify-driven push (EmuDeck)
│   ├── inotify_watch.py   # ctypes inotify wrapper + debouncer
│   ├── sync.py            # bidirectional engine (shared)
│   ├── leases.py          # active-device lease (acquire/heartbeat/release)
│   ├── lease_tracker.py   # per-source held-leases bookkeeping
│   ├── conflicts.py       # conflict storage + resolve helpers
│   ├── migrate.py         # one-shot legacy-layout migration
│   ├── daemon.py          # systemd entry point
│   ├── cli.py             # operator CLI
│   └── config.py
├── install/
│   ├── setup.sh                   # Pi-side installer
│   ├── setup-deck.sh              # Steam Deck installer
│   ├── bin/
│   │   └── retrosync-wrap         # bash dispatcher for SRM shortcuts
│   ├── scripts/                   # ES-DE custom event-script hooks
│   ├── systemd/                   # Pi-side unit files
│   ├── systemd-user/              # Deck-side user units
│   ├── networkmanager/            # Deck-side NM dispatcher (reconnect)
│   └── udev/                      # 99-retrosync-{pocket,fxpak,everdrive64}.rules
├── docs/
│   ├── design.pdf                 # original design
│   ├── pocket-sync-design.md      # Pocket Sync extension
│   ├── emudeck-sync-design.md     # EmuDeck / Steam Deck extension
│   ├── n64-sync-design.md         # N64 / EverDrive 64 extension
│   ├── architecture.png           # diagram
│   └── imaging.md
├── tests/
│   ├── formats/                   # per-system translator tests (n64, ...)
│   └── ...
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
