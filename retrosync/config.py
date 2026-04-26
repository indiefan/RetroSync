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
        sources = [
            SourceConfig(
                id=s["id"],
                adapter=s["adapter"],
                options=s.get("options") or {},
            )
            for s in (raw.get("sources") or [])
        ]
        return cls(cloud=cloud, orchestrator=orch, state=state, sources=sources)

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

sources:
  - id: fxpak-pro-1
    adapter: fxpak
    options:
      sni_url: ws://127.0.0.1:23074
      sd_root: /
      save_extensions: [".srm"]
      cache_dir: /var/lib/retrosync/fxpak-cache

  # Future:
  # - id: retroarch-deck
  #   adapter: emulator_dir
  #   options:
  #     system: snes
  #     directory: /home/deck/.config/retroarch/saves
"""

    def write_example_to(self, path: str | os.PathLike) -> None:
        Path(path).write_text(self.example_yaml())
