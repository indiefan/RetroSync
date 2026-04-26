# Pi Imager walkthrough

Detailed step-by-step for Step 1 of the install. Read this once; you
won't need it again.

## Prerequisites on your Mac

1. **Raspberry Pi Imager** — install from
   <https://www.raspberrypi.com/software/>.
2. **An SSH key.** Run this in Terminal if you don't already have one:

   ```bash
   ls ~/.ssh/id_ed25519.pub
   # If "No such file or directory":
   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
   cat ~/.ssh/id_ed25519.pub        # copy this line
   ```

3. **Your WiFi name and password.** RetroSync's Pi has no monitor or
   keyboard, so it has to know your WiFi at boot time.

## In Imager

Open **Raspberry Pi Imager**. The window has three big buttons:
**Choose Device**, **Choose OS**, **Choose Storage**.

### Choose Device

Pick **Raspberry Pi 4** (or 5 if you have one). This narrows the OS list.

### Choose OS

Open the **Raspberry Pi OS (other)** submenu and select
**Raspberry Pi OS Lite (64-bit)**.

> Why Lite? It has no desktop. The Pi will never have a screen attached;
> a desktop image just wastes resources.
> Why 64-bit? The Pi 4 / 5 are 64-bit capable, and the SNI binary's
> `linux-arm64` build is what the installer will fetch.

### Choose Storage

Pick the microSD card. **Triple-check that this is your SD card and not
some other disk.** Imager will erase everything on it.

### Click Next, then Edit Settings

Imager pops a confirmation dialog. **Don't click Yes yet.** Click
**Edit Settings** (or press ⌘+Shift+X).

The settings dialog has three tabs.

#### General tab

- **Hostname**: `retrosync` (this becomes `retrosync.local` for SSH)
- **Set username and password**: enable
  - Username: `pi`
  - Password: choose something solid; you'll only type this rarely
- **Configure wireless LAN**: enable
  - SSID: your WiFi name (case-sensitive)
  - Password: your WiFi password
  - **Wireless LAN country**: pick the country you're in. The Pi's WiFi
    radio refuses to associate without it.
- **Set locale settings**: enable
  - Time zone: pick yours (matters for log timestamps)
  - Keyboard layout: doesn't really matter; we'll never plug a keyboard in

#### Services tab

- **Enable SSH**: enable
- **Use public-key authentication only**: enable
- **Allowed keys**: paste the contents of `~/.ssh/id_ed25519.pub` from
  your Mac (the whole line that starts with `ssh-ed25519 ...`)

#### Options tab

Defaults are fine.

Click **Save**. Back at the confirmation dialog, click **Yes** to apply
settings, then **Yes** to start writing.

Writing + verifying takes 5-10 minutes. When it says "removed safely,"
pull the SD card.

## Boot the Pi

1. Insert the SD card into the Pi.
2. Plug the Pi into power. (It comes up automatically; there's no power
   button.)
3. Wait ~60 seconds for the first boot.

## SSH in from your Mac

```bash
ssh pi@retrosync.local
```

If `.local` doesn't resolve (some networks don't run mDNS, and some Macs
have it disabled): find the Pi's IP in your router's admin UI under
"Connected devices" or "DHCP leases" — it'll show as hostname `retrosync`.
Then:

```bash
ssh pi@<that-ip>
```

The first time, you'll be asked to confirm the host fingerprint. Type
`yes`. From then on, login is instant and passwordless.

## What can go wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| `ssh: Could not resolve hostname retrosync.local` | No mDNS on your LAN | Find IP in router, use that |
| `Connection refused` | Pi is still booting | Wait another minute |
| `Permission denied (publickey)` | Wrong SSH key in Imager | Re-image with the correct key |
| Pi never appears on network | WiFi country not set, or password wrong | Re-image with WiFi settings corrected |
| Pi appears but takes 5+ min to boot | Slow SD card | A cheap card is fine for OS, but consider a name-brand A2 card if you'll be using it heavily |

## After SSH works

Continue with **Step 3** in the main README.
