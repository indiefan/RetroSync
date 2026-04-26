"""Configuration loader.

Config is YAML, kept small. The default install puts it at
/etc/retrosync/config.yaml; for development you can pass --config to the CLI.

A minimal config:

    cloud:
      rclone_remote: "gdrive:retro-saves"
    sources:
      - id: fxpak-pro-1
        adapter: fxpak

Everything else has sensible defaults. See `Config.example_yaml()`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "/etc/retrosync/config.yaml"


@dataclass
class CloudConfig:
    rclone_remote: str = "gdrive:retro-saves"
    rclone_binary: str = "rclone"
    # Explicit path to rclone's config file. Lives under /var/lib (not
    # /home) so the daemon's ProtectHome=true doesn't mask it. Both the
    # daemon and the CLI use this exact path so they always agree on
    # which credentials to use.
    rclone_config_path: str = "/var/lib/retrosync/rclone.conf"
    # Per-game retention behavior; the v1 daemon only reads the default.
    # M4 will introduce per-game overrides via this map.
    retention_default: str = "keep"  # keep|prune|archive


@dataclass
class OrchestratorConfig:
    poll_interval_sec: int = 30
    debounce_polls: int = 3       # consecutive identical polls before upload
    upload_retry_max: int = 6     # attempts; backoff is 30s * 2**attempt
    upload_retry_max_age_sec: int = 6 * 3600
    # When a source's health check fails (e.g. the FXPak Pro is powered
    # off / unplugged), wait only this many seconds before re-checking
    # rather than the full poll_interval_sec. Lets the daemon notice
    # "cart just turned on" within ~2s so a sync fires before the
    # operator has a chance to launch a ROM. SNI's health check is
    # cheap (a websocket connect attempt) so the extra polling is
    # negligible while disconnected.
    unhealthy_recheck_sec: int = 2
    # InotifyOrchestrator (Deck-side) periodic re-scan interval. The
    # inotify watcher only fires on local file changes; cross-device
    # promotes / uploads to cloud's current.<ext> are invisible to it
    # until the local file is touched. The periodic re-scan is the
    # safety net: every N seconds, walk the saves dir and run
    # sync_one_game on each, which detects cloud-newer state via the
    # manifest read. Set to 0 to disable.
    inotify_rescan_sec: int = 60


@dataclass
class StateConfig:
    db_path: str = "/var/lib/retrosync/state.db"


@dataclass
class LeaseConfig:
    """Active-device lease tunables. See `retrosync/leases.py` for the
    semantics; the EmuDeck design doc §9 for the full rationale.

    `mode`: 'soft' (warn on contention, proceed) or 'hard' (block).
    `ttl_minutes`: how long a lease lasts without a heartbeat. Auto-
       expires so a crashed device doesn't lock the fleet out.
    `heartbeat_minutes`: how often the holder refreshes the lease
       while activity is live. Should be < ttl_minutes / 2.
    `notify_backend`: where to send "lease contended" notifications.
       'journald' (default) is just structured logs; 'pushover' /
       'discord' are stubs awaiting the operator to plug their
       creds in (see notify.py).
    """
    mode: str = "soft"           # soft | hard
    ttl_minutes: int = 15
    heartbeat_minutes: int = 5
    notify_backend: str = "journald"


@dataclass
class SourceConfig:
    id: str
    adapter: str           # 'fxpak', 'everdrive_n64', 'emulator_dir', ...
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    cloud: CloudConfig = field(default_factory=CloudConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    state: StateConfig = field(default_factory=StateConfig)
    sources: list[SourceConfig] = field(default_factory=list)
    # Top-level alias map shared across all sources. Each source may also
    # accept a `game_aliases` option that overrides this for its own scope.
    # Keys are canonical game ids; values are raw slugs that should resolve
    # to the key.
    game_aliases: dict[str, list[str]] = field(default_factory=dict)
    # When true, the engine is allowed to write cloud-newer saves back to
    # the device. Off by default until a hardware-side compatibility check
    # has confirmed the bytes round-trip correctly. See pocket-sync-design
    # §10.
    cloud_to_device: bool = False
    # How divergences (device != cloud, with no prior agreement OR both
    # moved since last sync) are handled.
    #   "device" (default): the device's bytes win. Become a new versions/
    #     entry and the cloud's `current.<ext>`. The previous cloud bytes
    #     stay in `versions/<previous-hash>.<ext>` for recovery.
    #   "preserve": don't auto-pick. Park the device's bytes in
    #     `conflicts/`, leave cloud current alone, require an operator
    #     `retrosync conflicts resolve` decision.
    conflict_winner: str = "device"
    # When a device with no prior sync_state shows up with bytes that
    # differ from cloud's current AND don't match any known historical
    # version, prefer cloud over device. The device's bytes are still
    # preserved as a versions/* entry (so you can recover them later).
    # Useful for Pockets whose source_id has changed (per-UUID migration)
    # and which now look "unknown" but actually have stale data. Default
    # false (current behavior: device wins).
    cloud_wins_on_unknown_device: bool = False
    # Like cloud_wins_on_unknown_device but for case 7 (both moved
    # since last agreed hash). True is recommended on the Pi/FXPak
    # side: cart-side bytes diverging from h_last are usually session
    # noise (a different game's autosave leftover, a hot-swap
    # artifact, etc.) rather than a deliberate save. Letting cloud
    # win on case 7 means another device's deliberate save survives
    # instead of being overwritten by cart-side noise. Device bytes
    # are still preserved as a versions/* entry for recovery via
    # `retrosync promote <game> <hash>`.
    cloud_wins_on_diverged_device: bool = False
    # Per-device-kind byte-count threshold for the "drift filter" — when
    # the engine sees a fast-forward upload AND the device's bytes differ
    # from cloud by ≤ this many bytes, treat as in-sync rather than
    # uploading. Default empty (no filtering). Suggested for Pocket
    # because its openFPGA cores tick in-game counters in SRAM even
    # when the operator isn't actively playing.
    #   drift_threshold:
    #     pocket: 4
    drift_threshold: dict[str, int] = field(default_factory=dict)
    # Active-device lease (EmuDeck design §9). Used by every source that
    # learns to acquire/release a lease; absent leases are simply absent
    # — no behavior change for sources that don't grab one.
    lease: LeaseConfig = field(default_factory=LeaseConfig)

    # ----------- loading -----------

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        path = str(path or os.environ.get("RETROSYNC_CONFIG")
                   or DEFAULT_CONFIG_PATH)
        with open(path) as fp:
            raw = yaml.safe_load(fp) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        cloud = CloudConfig(**(raw.get("cloud") or {}))
        orch = OrchestratorConfig(**(raw.get("orchestrator") or {}))
        state = StateConfig(**(raw.get("state") or {}))
        aliases = dict(raw.get("game_aliases") or {})
        sources = []
        for s in (raw.get("sources") or []):
            opts = dict(s.get("options") or {})
            # If the source didn't override aliases, inject the global map
            # so the adapter sees a consistent table.
            if aliases and "game_aliases" not in opts:
                opts["game_aliases"] = aliases
            sources.append(SourceConfig(
                id=s["id"], adapter=s["adapter"], options=opts,
            ))
        drift = {str(k): int(v)
                 for k, v in (raw.get("drift_threshold") or {}).items()}
        lease = LeaseConfig(**(raw.get("lease") or {}))
        return cls(
            cloud=cloud, orchestrator=orch, state=state, sources=sources,
            game_aliases=aliases,
            cloud_to_device=bool(raw.get("cloud_to_device", False)),
            conflict_winner=str(raw.get("conflict_winner", "device")),
            cloud_wins_on_unknown_device=bool(raw.get(
                "cloud_wins_on_unknown_device", False)),
            cloud_wins_on_diverged_device=bool(raw.get(
                "cloud_wins_on_diverged_device", False)),
            drift_threshold=drift,
            lease=lease,
        )

    @staticmethod
    def example_yaml() -> str:
        return """\
# RetroSync configuration.

cloud:
  # An rclone remote name. Set up via `rclone config` (the installer does
  # this for you). Path after the colon is the root folder in Drive.
  rclone_remote: "gdrive:retro-saves"
  # rclone's config file. Default is under /var/lib so the daemon's
  # ProtectHome=true doesn't mask it. Change only if you've moved it.
  rclone_config_path: "/var/lib/retrosync/rclone.conf"

orchestrator:
  poll_interval_sec: 30
  # 3 polls * 30s = ~90s of stability required before uploading. Tune lower
  # if your saves trigger inotify-like reads as soon as a game flushes.
  debounce_polls: 3

state:
  db_path: /var/lib/retrosync/state.db

# When true, the daemon can push cloud-newer saves back to the device.
# Default false; flip on after verifying byte-for-byte round-tripping
# between FXPak Pro and Pocket SNES core. See docs/pocket-sync-design.md §10.
cloud_to_device: false

# How divergences are handled (device and cloud disagree).
#   "device"  : device's bytes auto-win, become the new current. The
#               previous cloud bytes stay in versions/ for recovery.
#               Recommended default — no manual intervention required.
#   "preserve": park device bytes in conflicts/, leave cloud current
#               alone, require operator `retrosync conflicts resolve`.
conflict_winner: device

# When a device with no prior sync_state shows up with bytes that differ
# from cloud's current AND don't match any known historical version,
# this flag controls whether to trust the device or trust cloud:
#   false (default): conflict_winner kicks in (typically device wins).
#   true: preserve the device's bytes as a versions/* entry, then make
#         cloud's existing current the winner. Useful for Pockets whose
#         source_id changed (per-physical-device UUID migration) and
#         which now look "unknown" but actually have stale data.
cloud_wins_on_unknown_device: false

# Like cloud_wins_on_unknown_device but for case 7 (both sides moved
# since the last agreed hash). True is RECOMMENDED on the Pi/FXPak
# side: a cart's bytes that diverged from the last agreed hash are
# usually session noise (a different game's autosave leftover, a
# hot-swap artifact, etc.) rather than a deliberate user save.
# Letting cloud win on case 7 means another device's deliberate save
# survives instead of being overwritten by cart-side noise. Device
# bytes are preserved as a versions/* entry for recovery via
# `retrosync promote <game> <hash>`.
cloud_wins_on_diverged_device: false

# Per-device-kind byte-count threshold for the "drift filter". When the
# engine sees a fast-forward upload (cloud unchanged since last sync,
# device advanced) AND the device's bytes differ from cloud's current
# by ≤ this many bytes, treat as in-sync rather than uploading. The
# Analogue Pocket's openFPGA cores tick in-game counters in SRAM even
# when you're not actively playing, so leaving the default of 0
# produces a fresh cloud version on every plug-in. 4 is a good Pocket
# value — covers most counter ticks but still catches a real save's
# first-byte HP/MP/inventory change.
drift_threshold:
  pocket: 4
  # N64 EverDrive 64 X7. Controller Pak data ticks counters across
  # power cycles even when the operator doesn't think they're
  # "playing"; a small threshold lets us ignore that drift the same
  # way Pocket does.
  n64-everdrive: 4

# Active-device lease (EmuDeck sync design §9). When a device starts
# playing a game, it acquires a per-game lease in the cloud manifest
# so other devices know not to overwrite. Auto-expires after
# `ttl_minutes` so a crashed device doesn't lock anyone out.
#   mode=soft (default): warn on contention and proceed (device-wins
#     auto-resolve preserves the loser anyway, so nothing's destroyed).
#   mode=hard: refuse the contended operation. Strict; recommended only
#     once every device in the fleet is lease-aware.
lease:
  mode: soft
  ttl_minutes: 15
  heartbeat_minutes: 5
  notify_backend: journald

# Optional manual alias table for cases where slug normalization can't
# collapse two filenames on its own. Each entry maps a canonical id to
# the list of raw slugs that should resolve to it.
#
# game_aliases:
#   super_metroid:
#     - super_metroid_jpn
#     - super_metroid_usa_europe_en_ja_virtual_console
game_aliases: {}

sources:
  - id: fxpak-pro-1
    adapter: fxpak
    options:
      sni_url: ws://127.0.0.1:23074
      sd_root: /
      save_extensions: [".srm"]

  # Pocket sync runs on-demand, triggered by udev when the Pocket is
  # mounted in 'USB Drive' mode. The mount path is supplied at trigger
  # time by the systemd unit, not here.
  # - id: pocket-1
  #   adapter: pocket
  #   options:
  #     # Path under <mount>/Saves/ where SNES saves live. The openFPGA
  #     # SNES core (agg23.SNES) writes to the shared snes/common/ dir.
  #     core: snes/common
  #     file_extension: .sav

  # EmuDeck (Steam Deck) source. Watches the configured saves dir with
  # inotify; uploads on each in-game save (~5s debounce). The Deck-
  # side `setup-deck.sh` writes this stanza for you. saves_root /
  # roms_root usually live under ~/Emulation/.
  # - id: deck-1
  #   adapter: emudeck
  #   options:
  #     saves_root: /home/deck/Emulation/saves/retroarch/saves
  #     roms_root:  /home/deck/Emulation/roms/snes
  #     save_extension: .srm
  #     rom_extensions: [".sfc", ".smc", ".swc", ".fig"]
  #     system: snes

  # EverDrive 64 X7 (N64 flash cart over USB). Plugs into the same
  # Pi as the FXPak Pro; same usb-while-running model: continuous
  # polling, instant-sync via udev poke, lease-aware. Per-format
  # save files (.eep/.sra/.fla/.mp1..mp4) are aggregated into a
  # combined Mupen64Plus-format .srm before upload, and split back
  # on download — see `retrosync/formats/n64.py`.
  # - id: everdrive64-1
  #   adapter: everdrive64
  #   options:
  #     # Transport backends:
  #     #   serial    — pyserial over /dev/ttyUSB* (FT232 carts where
  #     #               the kernel ftdi_sio driver auto-binds; the
  #     #               default and the case verified on real X7
  #     #               hardware so far).
  #     #   pyftdi    — direct USB via libusb (FT245R variants where
  #     #               the kernel doesn't claim the device).
  #     #   unfloader — subprocess wrapper (stub).
  #     #   mock      — in-memory virtual SD; tests only.
  #     transport: serial
  #     serial_path: /dev/ttyUSB0
  #     serial_baud: 9600        # FT232 baud is largely cosmetic
  #     sd_saves_root: /ED64/SAVES
  #     sd_roms_root: /ED64/ROMS
  #     rom_extensions: [".z64", ".n64", ".v64"]
  #     system: n64
  #     # Krikzz's USB tool source doesn't expose a directory-list
  #     # operation, so the adapter can't auto-enumerate saves on
  #     # the SD. Until an OS64 dir-list cmd byte is found, the
  #     # adapter needs to know which ROM filenames to look for.
  #     # Easiest by far: point local_rom_dir at a directory on the
  #     # Pi (or wherever the daemon runs) containing copies of your
  #     # N64 ROMs. The adapter os.listdir()s it once per pass and
  #     # derives expected save filenames from each. No manual list
  #     # maintenance.
  #     local_rom_dir: /var/lib/retrosync/n64-roms
  #     # Optional explicit list — useful for ROMs that only live
  #     # on the cart's SD. Merged with local_rom_dir scan results.
  #     # rom_filenames:
  #     #   - "Cart-Only Game (USA).z64"
  #
  # And a corresponding Deck-side EmuDeck source for N64 (separate
  # from the SNES one above):
  # - id: deck-1-n64
  #   adapter: emudeck
  #   options:
  #     system: n64
  #     saves_root: /home/deck/Emulation/saves/retroarch/saves
  #     roms_root:  /home/deck/Emulation/roms/n64
  #     save_extension: .srm
  #     rom_extensions: [".z64", ".n64", ".v64"]
"""

    def write_example_to(self, path: str | os.PathLike) -> None:
        Path(path).write_text(self.example_yaml())
