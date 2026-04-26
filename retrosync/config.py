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


@dataclass
class StateConfig:
    db_path: str = "/var/lib/retrosync/state.db"


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
    # Per-device-kind byte-count threshold for the "drift filter" — when
    # the engine sees a fast-forward upload AND the device's bytes differ
    # from cloud by ≤ this many bytes, treat as in-sync rather than
    # uploading. Default empty (no filtering). Suggested for Pocket
    # because its openFPGA cores tick in-game counters in SRAM even
    # when the operator isn't actively playing.
    #   drift_threshold:
    #     pocket: 4
    drift_threshold: dict[str, int] = field(default_factory=dict)

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
        return cls(
            cloud=cloud, orchestrator=orch, state=state, sources=sources,
            game_aliases=aliases,
            cloud_to_device=bool(raw.get("cloud_to_device", False)),
            conflict_winner=str(raw.get("conflict_winner", "device")),
            cloud_wins_on_unknown_device=bool(raw.get(
                "cloud_wins_on_unknown_device", False)),
            drift_threshold=drift,
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
"""

    def write_example_to(self, path: str | os.PathLike) -> None:
        Path(path).write_text(self.example_yaml())
