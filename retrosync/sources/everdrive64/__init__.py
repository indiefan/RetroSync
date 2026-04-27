"""EverDrive 64 X7 source — N64 flash cart over USB.

The cart's USB port (FT245-based) lets us read/write the SD card's
file system while the N64 is powered on. We use this to sync the
EverDrive's per-format save files (`.eep` / `.srm` / `.fla` / `.mpk`)
with the cloud-side combined `.srm` (per `retrosync.formats.n64`).

The actual USB protocol lives in `retrosync.transport.krikzz_ftdi` so
that future Krikzz products (Mega EverDrive Pro / X7 / X3 for Genesis)
can share it.
"""
from .adapter import EverDrive64Source, EverDrive64Config  # noqa: F401
