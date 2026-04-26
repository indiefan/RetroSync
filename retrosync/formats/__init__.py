"""Per-system save-format translators.

Most systems are single-file (SNES `.srm`, Pocket `.sav`, Genesis
`.srm`, etc.) and need no translation — the device's save bytes are
the cloud's bytes. They're represented in `sync.SYSTEM_FORMATS` with
`combine=None / split=None`.

Multi-file systems (N64, Saturn, Dreamcast) live here. Each module
exposes `combine(saveset) -> bytes` and `split(bytes) -> saveset`,
both pure functions over a system-specific saveset dataclass.
"""
