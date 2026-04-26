# RetroSync

Hands-off save backup from retro flash carts to Google Drive.

The first hardware target is the **FXPak Pro** (SNES) over USB. The
foundation is built so adding EverDrives (N64 / GB / MD) and handheld
emulator sync later doesn't require redesign.

See [docs/design.pdf](docs/design.pdf) for the full design and
[docs/architecture.png](docs/architecture.png) for the diagram.

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
retrosync status                          # daemon-wide summary
retrosync list                            # every (source, save) on record
retrosync show fxpak-pro-1 /Mario.srm     # version history for one save
retrosync test-cart fxpak-pro-1           # smoke-test cart connection
retrosync test-cloud                      # smoke-test rclone remote

# Restore a save:
retrosync pull <cloud-path-from-show> /tmp/restored.srm
retrosync push fxpak-pro-1 /Mario.srm /tmp/restored.srm --confirm

# Get the latest source + re-apply the installer in one step.
# Prompts for sudo password once.
retrosync upgrade
```

Configuration: `/etc/retrosync/config.yaml`. Restart with
`sudo systemctl restart retrosync` after changes.

---

## Project layout

```
retrosync/
├── retrosync/             # the Python package
│   ├── sources/
│   │   ├── base.py        # SaveSource protocol — the extension point
│   │   ├── usb2snes.py    # WebSocket client for SNI / QUsb2snes
│   │   ├── fxpak.py       # FXPak Pro adapter
│   │   └── registry.py    # config 'adapter' string -> ctor
│   ├── state.py           # SQLite store
│   ├── cloud.py           # rclone wrapper, path scheme, manifest
│   ├── orchestrator.py    # poll/diff/debounce/upload
│   ├── daemon.py          # systemd entry point
│   ├── cli.py             # operator CLI
│   └── config.py
├── install/
│   ├── setup.sh           # one-shot installer
│   └── systemd/           # unit files
├── docs/
│   ├── design.pdf         # full design doc
│   ├── architecture.png   # diagram
│   └── imaging.md         # detailed Pi Imager walkthrough with caveats
├── tests/
└── pyproject.toml
```

## Adding a new source (future)

The whole orchestrator is built around the `SaveSource` protocol in
[`retrosync/sources/base.py`](retrosync/sources/base.py). Implement it,
register the adapter in `registry.py`, and add a stanza to your
`config.yaml`. No changes elsewhere.

A LocalDirSource for RetroArch / standalone emulators will land in v2,
along with two-way sync (handheld ↔ FXPak Pro). See the design doc's
appendix for the conflict-resolution plan.

## License

MIT.
