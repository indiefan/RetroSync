# N64 Sync — Design Doc

**Status:** Draft for implementation
**Audience:** RetroSync v0.4 implementation agent
**Builds on:** [docs/design.pdf](design.pdf), [docs/pocket-sync-design.md](pocket-sync-design.md) (especially §16 addendum — current shipped behavior), [docs/emudeck-sync-design.md](emudeck-sync-design.md). Read those first if you haven't.

---

## TL;DR

Add **N64** as a new system, with two sources:

1. **EverDrive 64 X7** — flash cart with a USB port, plugged into the
   same Pi as the FXPak Pro. Same usb-while-running model as the
   FXPak: continuous polling, instant-sync via udev poke, lease-aware.
2. **EmuDeck/RetroArch on the Steam Deck** — extends the existing
   `EmuDeckSource` to a second system. Saves live at the same
   `~/Emulation/saves/retroarch/saves/` path RetroArch uses for SNES.

The hard new problem: **N64 saves come in five formats** (4-Kbit
EEPROM, 16-Kbit EEPROM, SRAM, FlashRAM, Controller Pak) and the two
sources store them differently. The EverDrive 64 X7 writes one file
per format with a per-format extension (`.eep`, `.sra`, `.fla`,
`.mpk`). RetroArch's Mupen64Plus-Next core writes a single combined
`.srm` that packs all five formats at fixed offsets. **They are not
byte-equivalent.**

This doc specifies a small `retrosync.formats.n64` module that
translates between the two layouts, plus per-source flags so each
adapter knows what to read from / write to. The cloud's canonical
storage is the combined `.srm` form (matches the existing
`SYSTEM_CANONICAL_EXTENSION = {"snes": ".srm"}` pattern). EverDrive
saves are split into the combined form on upload and split back on
download.

The user has confirmed **only one console is ever powered on at a
time**, which lets us simplify some lease and concurrency logic at
the FXPak/EverDrive boundary on the Pi.

## 1. Goals

- **EverDrive 64 plug-and-go.** Power on the N64 with EverDrive
  inserted, USB cable to Pi already attached. Within ~2 s of cart-on
  detection, the daemon syncs.
- **Cross-source magic for N64.** Save in Super Mario 64 on the N64,
  pick up the Deck a few minutes later, RetroArch loads from the
  cloud-synced state. Reverse direction also works.
- **Format-correctness.** The translator must round-trip without data
  loss: device-side per-format files → combined .srm → device-side
  per-format files yields the original bytes (modulo intentional
  "fill empty slots with zeros" semantics).
- **Drift filter applies.** Like Pocket, N64 SRAM has counter-tick
  byte drift (especially in Controller Pak segments). Reuse
  `drift_threshold` machinery from §16.6 of the Pocket addendum.
- **Lease-aware.** EverDrive grabs lease on cart-attach, releases on
  cart-detach, just like FXPak.

## 2. Non-goals (this iteration)

- **EverDrive 64 native SD-card hot-swap.** If USB-while-running access
  to the EverDrive's SD turns out to be unreliable on this firmware,
  we fall back to a Pocket-style "mount the SD via card reader,
  triggered by udev" flow — but the *primary* shipping path is
  USB-while-running. (See §3.3.)
- **Other EverDrives.** Mega EverDrive, EverDrive GB, etc. each have
  their own USB protocol or no USB. v0.4 ships X7 only.
- **EmuDeck Mupen64Plus standalone.** v0.4 targets RetroArch +
  Mupen64Plus-Next core (EmuDeck's default). Standalone Mupen on the
  Deck has different save paths and is a follow-up.
- **N64 → SNES sharing.** N64 saves never collide with SNES saves at
  the cloud level because they're namespaced under `n64/<game-id>/`.
- **Real-time emulator save extraction from the EverDrive over USB
  while the game is running and writing.** Same torn-write
  considerations as FXPak Pro — the existing 90 s debounce / hash-
  stability pattern handles this, no new mechanism needed.

## 3. Hardware: EverDrive 64 X7 over USB

### 3.1 What the cart is

Krikzz's EverDrive 64 X7 is a flash cart that plays N64 ROMs from a
microSD card. It has a USB Type-B Mini port on the cart edge (visible
when the cart sticks out of the N64). The USB controller is an FTDI
FT245R USB-to-FIFO bridge — the same chip used in the EverDrive 3.0
and many other Krikzz products.

### 3.2 Protocol

The EverDrive 64 X7 firmware speaks a **synchronous request-response
protocol** over the FT245's bulk endpoint pair. Krikzz hasn't
published a complete spec, but the open-source community has
reverse-engineered and documented the relevant subset:

- **`UNFLoader`** (https://github.com/buu342/N64-UNFLoader) — primary
  open-source tool. Implements the `EverDrive 3.0+` protocol family
  for ROM upload, debug printing, and importantly for us, **SD card
  file operations**.
- **`ed64log`** / **`ED64-USB`** — alternative/older clients with
  similar coverage.
- The protocol commands we use (verified against UNFLoader's source):
  - `CMD_TEST` (`'t'`) — handshake; identifies firmware version.
  - `CMD_ROMFILL`, `CMD_ROMWRITE`, `CMD_ROMREAD` — ROM region access
    (we don't use these; we don't write ROMs).
  - `CMD_SDREAD`, `CMD_SDWRITE`, `CMD_DIR_OPEN`, `CMD_DIR_READ`,
    `CMD_FILE_OPEN`, `CMD_FILE_CLOSE`, `CMD_FILE_READ`,
    `CMD_FILE_WRITE` — SD card filesystem operations. **These are
    what we need.**

The implementing agent verifies command IDs against UNFLoader's
`device_everdrive3.c` since some have shifted between firmware
revisions. Krikzz's "OS64" firmware (the v3.x line that the X7 ships
with) is what we target. Older "Cart OS" v2 firmware uses different
command numbers and is out of scope for v0.4.

### 3.3 Implementation strategy

We do **not** want to depend on UNFLoader as a runtime binary —
shelling out to it for every save read/write would be slow and
fragile. Instead, port the relevant SD-file-operation commands to a
small Python module that talks directly to the FTDI device via
`pyftdi` (no kernel driver needed; pyftdi uses libusb).

**Module location: `retrosync/transport/krikzz_ftdi.py` (not
under `sources/everdrive64/`).** The same FT245-based command set is
shared across multiple Krikzz EverDrive products: EverDrive 64 X7,
EverDrive 3.0 (SNES — earlier model not currently used), Mega
EverDrive Pro / X7 / X3 (Genesis), and others. Putting the protocol
under `transport/` rather than under a single source keeps it
reusable when we add Genesis (or revisit SNES with a non-FXPak cart)
later. The §15 "Generalizability" section walks through this.

Adapters consume `KrikzzFtdiTransport` like a service:

```python
class EverDrive64Source:
    def __init__(self, *, transport: KrikzzFtdiTransport, ...):
        self._t = transport
    async def list_saves(self) -> list[SaveRef]:
        entries = await self._t.dir_list("/ED64/SAVES")
        ...
```

The protocol surface needed is small (~6 commands). UNFLoader's C
implementation is ~200 lines for these; the Python port should be
~300–400 with proper error handling and timeouts. The implementing
agent reads UNFLoader's `device_everdrive3.c` and ports the wire
format directly, citing line numbers in code comments.

If pyftdi proves unworkable for some reason (Pi kernel quirks, cart
firmware mismatch, etc.) the **fallback** is shelling out to
UNFLoader — we install it as a system dep and run it via
`subprocess`. This is documented as a config option
(`everdrive64.transport: pyftdi | unfloader`) and the adapter has
both code paths from day one.

### 3.4 Detection

The EverDrive 64 X7 enumerates as USB vendor `0403:6001` (FTDI's
default for unbranded FT245R) — the same VID/PID space as many other
FT245-based devices. We disambiguate by:

- **`udev rule`** matching `idVendor=0403, idProduct=6001` AND
  `ATTRS{product}` matching `EverDrive*` (the FTDI EEPROM on the X7
  is programmed with this product string).
- The udev rule fires `systemctl kill --signal=SIGUSR1
  retrosync.service` (same instant-sync poke pattern as the FXPak
  rule per §16.16 of the Pocket addendum) and sets
  `TAG+="retrosync-everdrive"` so the daemon's startup-time scan can
  find devices already plugged in.

The agent captures the actual product-string at install time
(`udevadm info -q property -n /dev/bus/usb/<bus>/<dev>` on the
operator's hardware) since the EEPROM string varies slightly between
X7 revisions.

### 3.5 Save layout on the EverDrive's SD

The X7 firmware stores per-game saves at:

```
/ED64/SAVES/<rom-stem>.<save-ext>
```

Where `<save-ext>` is determined by the firmware's per-game lookup
table (which save type does this cart use):

| Save type | Extension | Size |
|-----------|-----------|------|
| 4 Kbit EEPROM | `.eep` | 512 bytes |
| 16 Kbit EEPROM | `.eep` | 2 KB |
| SRAM | `.sra` | 32 KB |
| FlashRAM | `.fla` | 128 KB |
| Controller Pak (per port) | `.mpk` (or `.mp1`–`.mp4`) | 32 KB each |

Some games have **both** SRAM/FlashRAM and a Controller Pak — that's
two files per game on the EverDrive. The translator (§4) handles
this case.

ROMs live alongside saves under `/ED64/ROMS/...` — useful for the
ROM-aware filename derivation when bootstrapping (§7.4).

## 4. The save-format problem and translator module

### 4.1 RetroArch's combined `.srm` layout

Mupen64Plus (the upstream library RetroArch's core uses) writes a
single combined save file of fixed size **296,960 bytes** (290 KB).
Layout, verified against `mupen64plus-core/src/main/savestates.c`
and `mupen64plus-libretro-nx`:

```
Offset      Size        Format
0x00000     0x00800     EEPROM (16 Kbit max; 4 Kbit games use first 0x200 + zero pad)
0x00800     0x08000     SRAM (32 KB)
0x08800     0x20000     FlashRAM (128 KB)
0x28800     0x08000     Controller Pak port 1
0x30800     0x08000     Controller Pak port 2
0x38800     0x08000     Controller Pak port 3
0x40800     0x08000     Controller Pak port 4
[end at 0x48800 = 296960]
```

(The agent verifies these offsets against the libretro-nx source
during Step 1 of §11; the layout above is from libretro-nx 2.5.x.
Mupen64Plus standalone uses the same layout.)

Empty regions are zero-filled. A game that uses only EEPROM has
0x00000–0x007FF populated and zeros for the remaining 296 KB. A game
with FlashRAM only populates 0x08800–0x287FF.

**Critical empirical detail:** real `.srm` files in the wild may be
truncated to exactly the bytes a game uses (some emulator builds do
this). The translator's "split" direction must accept inputs of
*any* length up to 296,960 and pad with zeros for missing regions.
The "combine" direction always emits exactly 296,960 bytes.

### 4.2 EverDrive's per-format files

Each save type is its own file at full natural size:

- `Super Mario 64.eep` — 512 bytes (4-Kbit EEPROM)
- `The Legend of Zelda - Ocarina of Time.sra` — 32 KB (SRAM) + maybe
  `.mpk` files for player Controller Pak data
- `Paper Mario.fla` — 128 KB (FlashRAM) + maybe `.mpk` files

EverDrive sizes are exact — no padding.

### 4.3 The translator

`retrosync/formats/n64.py`:

```python
@dataclass(frozen=True)
class N64SaveSet:
    """Logical bundle of save data for one game across all formats."""
    eeprom:    bytes | None     # 0–2 KB (4-Kbit or 16-Kbit, no padding)
    sram:      bytes | None     # 32 KB exactly, or None
    flashram:  bytes | None     # 128 KB exactly, or None
    cpak:      list[bytes | None]   # 4-element list, 32 KB each or None

def combine(save_set: N64SaveSet) -> bytes:
    """Pack into a 296,960-byte mupen64plus-format .srm."""
    ...

def split(srm: bytes) -> N64SaveSet:
    """Unpack a .srm. Tolerates short inputs (zero-pads). Returns
    None for any region whose bytes are all-zero (so the caller doesn't
    write empty per-format files to the EverDrive)."""
    ...

def empty_set() -> N64SaveSet:
    """All-None saveset for newly-bootstrapped games."""
    ...
```

`combine` and `split` are pure functions, fully unit-testable. The
agent writes round-trip property tests (every byte that goes through
`combine(split(...))` and `split(combine(...))` is preserved within
the documented zero-region semantics).

### 4.4 What "current" looks like in cloud

Per §16.4 of the Pocket addendum, cloud's `current.<ext>` is the
**system-canonical extension**. We extend the canonical map:

```python
SYSTEM_CANONICAL_EXTENSION = {
    "snes": ".srm",
    "n64":  ".srm",   # combined Mupen64Plus format
}
```

So all N64 cloud paths look like:

```
gdrive:retro-saves/n64/super_mario_64/
├── current.srm                       # combined 290 KB
├── manifest.json
├── versions/
│   ├── deck/
│   │   └── 2026-...--abc.srm         # combined (deck wrote it directly)
│   └── n64/
│       └── 2026-...--def.srm         # combined (Pi-side combine() output)
└── conflicts/
    └── ...
```

Versions are always stored combined. EverDrive uploads pass through
`combine()` first; downloads pass through `split()` and write the
appropriate per-format files to the SD.

### 4.5 Hashing

Hash is computed over the **combined .srm bytes** (after `combine()`
on the EverDrive side, or directly on the Deck). This means the
EverDrive's hash matches the Deck's hash whenever the underlying save
state is the same — which is exactly what `sync_one_game`'s
hash-equality fast path needs.

The drift filter (per §16.6) compares full combined-srm bytes too.
N64 Controller Pak tick patterns are still <4 bytes for most games, so
`drift_threshold.n64: 4` (default) should be fine.

## 5. Game-ID alignment for N64

### 5.1 ROM identification

N64 ROMs come in three byte orders. The internal header (first 64
bytes of a big-endian z64 ROM) contains:

```
0x000  Endian magic + clock rate + entry point
0x020  Game name (20 ASCII chars, space-padded)
0x03B  Manufacturer ID + Cart ID (3 chars total, e.g. "NSM" for
       Super Mario 64 USA)
0x03E  Country code (1 char: 'E'=USA, 'P'=PAL, 'J'=Japan, ...)
0x03F  Version
```

For canonical-slug derivation, we have two approaches:

- **Filename-based** (simple, matches existing SNES pattern): take
  the ROM/save filename stem, run through `canonical_slug()`. Works
  if the operator uses No-Intro / GoodN64 conventions consistently
  across devices.
- **Header-based** (more robust): read the ROM header's game name +
  cart ID, slug = `<game-name-slug>_<cart-id>` (e.g.
  `super_mario_64_nsm`). Doesn't depend on filename consistency.

**Decision for v0.4:** start with filename-based (matches Pocket and
SNES). The slug normalizer already strips `(USA)` / `(Europe)` /
revision tags, so "Super Mario 64 (USA).z64" and "Super Mario 64.z64"
both canonicalize to `super_mario_64`. If empirical experience shows
filename inconsistency across the operator's libraries, switch to
header-based via a config flag.

### 5.2 Save filename ↔ game-id map

Same story as Pocket (§16.8): the EverDrive's save filename is
ROM-stem-derived. When bootstrap-pulling to a fresh EverDrive, we
look at `/ED64/ROMS/` and find a ROM file whose canonical slug
matches the target game-id; the save filenames go under
`/ED64/SAVES/<rom-stem>.<save-ext>`.

**Generalized `target_save_paths_for(game_id)` API.** Every adapter
returns the same shape — a `dict[str, str | list[str] | None]` keyed
by file-format identifier:

```python
# EverDrive 64 (multi-format):
{
  "eep":  "/ED64/SAVES/Super Mario 64.eep",
  "sra":  None,                                   # game doesn't use SRAM
  "fla":  None,
  "mpk":  ["/ED64/SAVES/Super Mario 64.mp1", ...],  # if game uses cpak
}

# EmuDeck (single-file system, e.g. SNES or Genesis):
{
  "srm":  "/home/deck/Emulation/saves/retroarch/saves/Super Metroid (USA).srm",
}

# Pocket (single-file):
{
  "sav":  "/Saves/snes/common/Super Metroid (USA).sav",
}
```

The translator's output saveset tells multi-format adapters which
formats are in play; the adapter writes only the matching files.
Single-file adapters always return a one-entry dict. The engine's
write path consumes the same shape from any adapter — no source-
specific code paths in the engine.

This is a small refactor of the existing `target_save_path_for`
(singular) on `PocketSource`. The migration: rename, change return
type, callers iterate the dict (which has length 1 for existing
sources). See §11 Step 3 for the migration sequence.

### 5.3 EmuDeck N64 — same logic as SNES

EmuDeck's RetroArch saves N64 to `<saves_root>/<game>.srm` (combined).
The `EmuDeckSource` adapter introduced in the v0.3 doc just gains
`system: n64` configurability; no new code paths. Save filename ↔
game-id map (`device_filename_map`) extends to N64 transparently.

ROM lookup for bootstrap pull: `<emudeck_root>/roms/n64/<game>.<ext>`,
same structure SNES uses. ROM extensions extend to `[".z64",
".n64", ".v64"]`.

## 6. Architecture changes

### 6.1 `system` becomes an adapter property, not a source-class property

Today (post-§16): `FXPakSource.system = "snes"` hardcoded.
`PocketSource.system = "snes"` hardcoded. `EmuDeckSource.system`
configurable.

For v0.4, **all sources** make `system` configurable so:

- `EverDrive64Source.system = "n64"` (only system this adapter handles)
- `EmuDeckSource(system="n64")` is a separate config entry from
  `EmuDeckSource(system="snes")`. The Deck operator configures one
  per system they want synced.

This is a breaking-but-cosmetic change — the Pi's existing FXPak
and Pocket sources stay `snes` by default. Everything else is
additive.

### 6.2 Per-system save-format hooks in the engine

`sync.py` learns to consult per-system hooks for upload/download:

```python
@dataclass
class SystemFormat:
    canonical_extension: str
    combine: Callable[[Any], bytes] | None     # device-format → cloud-format
    split: Callable[[bytes], Any] | None       # cloud-format → device-format
```

Registry, designed to be extensible:

```python
SYSTEM_FORMATS = {
    "snes":    SystemFormat(canonical_extension=".srm", combine=None, split=None),
    "n64":     SystemFormat(canonical_extension=".srm",
                            combine=n64.combine, split=n64.split),
    # Future entries — each is one line. See §15 for what plugging in
    # a new system actually entails.
    # "genesis": SystemFormat(canonical_extension=".srm", combine=None, split=None),
    # "gb":      SystemFormat(canonical_extension=".sav", combine=None, split=None),
    # "saturn":  SystemFormat(canonical_extension=".bin",
    #                         combine=saturn.combine, split=saturn.split),
}
```

`combine=None / split=None` is the **single-file system** case (the
common case — SNES, GB, Genesis, GBA all live here). The engine
treats device bytes as the cloud bytes directly. No translator code
needed at all; the system entry is data, not behavior.

`combine` and `split` are required only for **multi-format systems**
where the device stores multiple files per game and they need to be
packed into a canonical cloud blob. N64 is the first user; Saturn
and Dreamcast (with VMUs / cart RAM) are likely future users when
they're added.

The engine itself is unchanged below the adapter boundary — it still
operates on opaque byte blobs and content-hashes regardless of which
SystemFormat is in play.

### 6.3 Lease behavior with "single console at a time"

The user's "only one console on at a time" guarantee means the
FXPak and EverDrive 64 sources on the Pi can never simultaneously
hold leases for the same game. Practically:

- Both sources still grab/release leases per the §9 of the EmuDeck
  design — so the **Deck** seeing "Pi N64 active" works correctly.
- We don't add any cross-source coordination on the Pi (e.g. "FXPak
  released its lease before EverDrive grabs"). The leases are
  per-game anyway, and the user's guarantee makes contention between
  them effectively impossible.

This was the right place to spend any complexity budget on
cross-source FXPak-vs-EverDrive arbitration; the user's guarantee
buys us out of that decision entirely. Documented here so the agent
doesn't accidentally introduce coordination code "for safety."

## 7. EverDrive64Source adapter

### 7.1 Public surface

```python
class EverDrive64Source:
    system = "n64"
    device_kind = "n64-everdrive"      # cloud subfolder: versions/n64-everdrive/...

    def __init__(self, *, id: str,
                 transport: str = "pyftdi",      # | "unfloader"
                 ftdi_url: str = "ftdi://ftdi:0x6001/1",
                 sd_saves_root: str = "/ED64/SAVES",
                 sd_roms_root: str = "/ED64/ROMS",
                 unfloader_path: str = "/usr/local/bin/UNFLoader"):
        ...

    async def health(self) -> HealthStatus:
        # Try CMD_TEST. If response in <500ms, healthy.
        ...

    async def list_saves(self) -> list[SaveRef]:
        # CMD_DIR_OPEN /ED64/SAVES, iterate, return SaveRefs.
        # ref.path is the full SD path including extension.
        # We expose one SaveRef per per-format file, but the engine
        # treats them as a *grouped saveset* via game_id grouping —
        # see §7.3.
        ...

    async def read_save(self, ref: SaveRef) -> bytes:
        # CMD_FILE_OPEN/READ/CLOSE.
        ...

    async def write_save(self, ref: SaveRef, data: bytes) -> None:
        # CMD_FILE_OPEN/WRITE/CLOSE. Atomic rename pattern not
        # available on the EverDrive's FAT32 — write-and-pray
        # backed by torn-write debounce on the engine side.
        ...

    def resolve_game_id(self, ref: SaveRef) -> str:
        from ..game_id import canonical_slug
        return canonical_slug(Path(ref.path).stem)

    # ---- adapter-specific ----

    async def read_saveset(self, game_id: str) -> n64.N64SaveSet:
        """Aggregate all per-format files for a game into a saveset."""
        ...

    async def write_saveset(self, game_id: str, ss: n64.N64SaveSet) -> None:
        """Split a saveset back into per-format files. Removes files
        for formats that are now empty (None in the saveset)."""
        ...

    async def target_save_paths_for(self, game_id: str) -> dict[str, str | list[str]]:
        """Per §5.2. Looks up matching ROM in /ED64/ROMS, returns
        per-extension paths."""
        ...
```

### 7.2 Sync flow integration

The engine treats N64 saves as **per-game groups**, not per-file. The
poll loop:

1. `list_saves()` returns one SaveRef per per-format file (so e.g. 8
   files might be returned for 4 games).
2. Engine groups SaveRefs by `game_id` (using `resolve_game_id()`).
3. For each group, calls `EverDrive64Source.read_saveset(game_id)`,
   producing an `N64SaveSet`.
4. Combines via `n64.combine(saveset)` → 296,960-byte combined srm.
5. Hashes that. Compares to cloud / runs through the standard sync
   decision matrix.

This is the **only** novel orchestration change. The existing
hash-comparison + decision matrix + drift filter machinery applies
unchanged.

For uploads: combine, hash, store as one cloud-side blob.

For downloads: pull cloud's combined .srm, `split()` into a saveset,
call `write_saveset()` which writes (or removes) per-format files.

### 7.3 Listing-time grouping

`SaveSource.list_saves()` returns a flat list of `SaveRef` today. The
engine doesn't group. We add a per-source grouping hook:

```python
class SaveSource(Protocol):
    ...
    def group_refs(self, refs: list[SaveRef]) -> dict[str, list[SaveRef]]:
        """Default: one group per ref keyed by ref.path. Sources with
        multi-file saves override to group by game_id."""
        return {ref.path: [ref] for ref in refs}
```

`EverDrive64Source.group_refs` groups by `resolve_game_id(ref)`.
Other sources keep the default. The engine iterates groups instead of
refs. Existing single-file sources are unaffected (one group per
file).

### 7.4 Bootstrap pull (cloud → EverDrive, no save yet)

For an EverDrive that's never seen game X but cloud has a save for it:

1. Engine pulls cloud's `current.srm` (combined).
2. `split()` → `N64SaveSet` with whichever regions are populated.
3. `target_save_paths_for(game_id)` walks `/ED64/ROMS/`, finds a
   matching ROM filename, returns the canonical save filenames.
4. `write_saveset()` writes per-format files at those paths.

If no ROM is found on the EverDrive's SD, log a WARNING ("cloud
has a save for `<game-id>` but no matching ROM in /ED64/ROMS/; not
downloading"), skip the bootstrap. Same as the Pocket's §16.8 fallback.

### 7.5 Empty-region handling

If a game starts using SRAM (e.g. operator hadn't played past the
first save point yet), then later begins to use Controller Pak too
(player customizations in Tony Hawk), the saveset gains a new region.
On EverDrive write, we add the new `.mpk` file. On a later sync where
the player resets cpak data, the saveset's `cpak` slot becomes None,
and `write_saveset` deletes the corresponding `.mpk` file.

Deletion vs. zero-fill: the EverDrive's firmware reads `.mpk` files
of zero size as "no controller pak data." Deletion is cleaner.

### 7.6 Torn-write protection

Existing 90s debounce / hash-stability covers this — same as FXPak
Pro. The engine reads the cart, hashes the combined output, debounces
across consecutive polls before promoting to upload. If a game is
mid-write across the multi-file boundary (e.g. wrote `.eep` but not
`.mpk` yet), the next poll's combined hash will differ, debounce
restarts.

## 8. Components / files

```
retrosync/
├── transport/
│   ├── __init__.py            NEW
│   └── krikzz_ftdi.py         NEW — FT245 protocol shared across EverDrive products
│                                    (CMD_TEST, CMD_DIR_*, CMD_FILE_*)
├── sources/
│   ├── everdrive64/
│   │   ├── __init__.py
│   │   ├── adapter.py         NEW — EverDrive64Source (uses KrikzzFtdiTransport)
│   │   └── unfloader.py       NEW — UNFLoader subprocess fallback transport
│   ├── emudeck.py             CHANGED — `system` is now first-class config
│   ├── base.py                CHANGED — group_refs() default;
│                                          target_save_paths_for() generalized to dict
│   ├── pocket.py              CHANGED — target_save_path_for → target_save_paths_for
│   └── ...
├── formats/
│   ├── __init__.py
│   └── n64.py                 NEW — N64SaveSet, combine(), split()
│                                    (future: saturn.py, dreamcast.py, etc.)
├── sync.py                    CHANGED — per-system format hooks; per-group iteration;
│                                          consumes uniform target_save_paths_for shape
├── cloud.py                   CHANGED — SYSTEM_CANONICAL_EXTENSION includes "n64"
├── cli.py                     CHANGED — `retrosync test-cart everdrive64-1` works;
│                                          conflicts/versions support N64
└── ...

install/
├── udev/
│   └── 99-retrosync-everdrive64.rules    NEW — fires SIGUSR1 + tags device
└── setup.sh                   CHANGED — installs udev rule, optionally UNFLoader

docs/
└── n64-sync-design.md         THIS FILE

tests/
├── formats/
│   ├── n64_combine_split_test.py         NEW — round-trip property tests
│   └── n64_real_saves_test.py            NEW — fixture saves from real games
└── dry_run.py                            EXTEND — n64 phase
```

## 9. Configuration

`config.yaml` additions:

```yaml
sources:
  - id: everdrive64-1
    adapter: everdrive64
    options:
      transport: pyftdi              # | unfloader
      ftdi_url: ftdi://ftdi:0x6001/1
      sd_saves_root: /ED64/SAVES
      sd_roms_root: /ED64/ROMS
      rom_extensions: [.z64, .n64, .v64]

  # Deck-side N64 (in addition to existing snes EmuDeck source)
  - id: deck-1-n64
    adapter: emudeck
    options:
      system: n64
      saves_root: /home/deck/Emulation/saves/retroarch/saves
      roms_root: /home/deck/Emulation/roms/n64
      rom_extensions: [.z64, .n64, .v64]
      save_extension: .srm

drift_threshold:
  pocket: 4
  n64-everdrive: 4               # device_kind, not source_id
```

## 10. Operator UX

### 10.1 First-time setup

1. Operator follows the existing Pi setup (already done).
2. Plugs EverDrive 64 into the N64, USB cable to the same Pi.
3. Powers on the N64 with a game inserted.
4. `retrosync test-cart everdrive64-1` → expects `health: OK -
   firmware=v3.05 (or whatever); 5 save files at /ED64/SAVES/...`.
5. Daemon picks it up on next poll (or immediately via udev poke);
   uploads happen.

`setup.sh` is updated to:

- Install the new udev rule.
- Optionally install UNFLoader (operator-prompted, only if they
  selected `transport: unfloader` or want it as a fallback).
- Add `pyftdi` to the apt-installed Python deps.
- Print operator action: "set EverDrive 64 firmware to OS64 v3.x or
  later; older 'Cart OS' firmware is unsupported."

### 10.2 Day-to-day commands

All the existing commands (`retrosync status`, `retrosync versions`,
`retrosync conflicts list/show/resolve`, `retrosync load`, etc.) work
for N64 transparently — the system tag in `<system>/<game-id>/...`
is the only difference.

`retrosync versions super_mario_64` shows version history across
EverDrive and Deck contributions, with `device_kind` distinguishing
them.

## 11. Implementation plan

In dependency order. Each step independently mergeable.

### Step 1 — Format translator
- `retrosync/formats/n64.py` per §4.3.
- Round-trip property tests using random byte fixtures.
- Real-save fixture tests: include 2–3 known saves (Super Mario 64
  EEPROM-only, Zelda OoT SRAM, Paper Mario FlashRAM) byte-comparing
  combined output against a reference Mupen64Plus-generated `.srm`.
- Verify the byte offsets match libretro-nx's source. If they don't,
  the layout in §4.1 is updated and the test fixtures regenerated.
- **Deliverable:** `combine`/`split`/`empty_set` callable from any
  source adapter without touching the engine.

### Step 2 — Per-system format hooks in the engine
- `SYSTEM_FORMATS` registry in `sync.py` per §6.2.
- `SYSTEM_CANONICAL_EXTENSION` extends with `"n64": ".srm"`.
- Existing SNES sources unaffected (combine=split=None passthrough).
- **Test:** dry_run still passes for SNES.

### Step 3 — `group_refs` hook
- Default impl in `SaveSource.base`.
- Engine iterates groups instead of refs; for groups with one ref,
  behavior is identical.
- **Test:** dry_run still passes for SNES (single-ref groups).

### Step 4 — EverDrive 64 USB protocol module
- `retrosync/sources/everdrive64/protocol.py` ports UNFLoader's
  `device_everdrive3.c` SD-file commands to pyftdi.
- Document each command with citation to UNFLoader source line.
- Unit tests with a recorded-USB-traffic fixture (or a mock
  `pyftdi.UsbTools.list_devices` + scripted `read`/`write` responses).
- **Deliverable:** open device, list a directory, read a file, write
  a file, all through pure Python.

### Step 5 — UNFLoader subprocess fallback
- `retrosync/sources/everdrive64/unfloader.py` shells out to
  `UNFLoader -e <cmd>` for each operation.
- Same external API as `protocol.py` (interchangeable).
- Documented as "use this if pyftdi gives you trouble."

### Step 6 — EverDrive64Source adapter
- `retrosync/sources/everdrive64/adapter.py`.
- `read_saveset` / `write_saveset` per §7.1.
- `target_save_paths_for` walks ROMs with `region_preference`.
- Group_refs override.
- **Test:** dry_run extends with an EverDrive fixture (mock USB
  protocol + in-memory SD filesystem layout):

```
tests/fixtures/everdrive64/
├── ED64/
│   ├── ROMS/
│   │   ├── Super Mario 64 (USA).z64
│   │   ├── The Legend of Zelda - Ocarina of Time.z64
│   │   └── Paper Mario.z64
│   └── SAVES/
│       ├── Super Mario 64 (USA).eep
│       └── The Legend of Zelda - Ocarina of Time.sra
```

### Step 7 — udev + instant-sync poke
- `install/udev/99-retrosync-everdrive64.rules` per §3.4.
- Reuses the FXPak SIGUSR1 mechanism (§16.16 of Pocket addendum) — no
  new code, just another rule firing the same signal.
- Document operator capture step for product-string matching.
- **Test:** plug in real cart, observe `journalctl -u retrosync` shows
  poke received within ~1s.

### Step 8 — EmuDeck N64 path (configuration only)
- Verify `EmuDeckSource(system="n64", ...)` works with v0.3's adapter
  unchanged (modulo the §6.1 system-as-config refactor).
- ROM lookup for N64 bootstrap pull (already system-configurable per
  EmuDeck v0.3 design).
- **Test:** dry_run extends with EmuDeck-N64 fixture; cross-source
  bootstrap-pull lands the right combined `.srm` in
  `~/Emulation/saves/retroarch/saves/<rom-stem>.srm`.

### Step 9 — Drift threshold + drift-filter coverage
- Add `n64-everdrive` to default `drift_threshold` map (suggested 4).
- Verify §16.6 coverage matrix applies — N64 should hit case 5/7
  most often (cart Controller Pak counter ticks).

### Step 10 — End-to-end manual test on real hardware
- Save in Super Mario 64 on the N64 via EverDrive.
- Within ~1s of cart poke, observe upload to cloud.
- Launch SM64 on the Deck; pre-launch wrap pulls combined `.srm`.
- RetroArch loads the EEPROM data; resume from saved state.
- Reverse direction: save on Deck, power on N64; daemon detects cart-
  on (poke or 2s recheck), pulls + splits + writes EEPROM file.

### Step 11 — Documentation and edge-case capture
- README adds N64 section.
- This doc gains a §16 addendum (parallel to Pocket addendum)
  capturing implementation surprises.

## 12. Testing strategy

### 12.1 Format translator

Heavy property-based testing. The translator is the highest-risk
new code in v0.4. Test inputs include:

- Random bytes of arbitrary length (combine should never error;
  split tolerates short inputs).
- Real saves from a mix of N64 game types (provided as binary
  fixtures, ~200 KB each).
- Boundary conditions: 0-byte input, single-byte EEPROM, exactly
  296,960 bytes, 296,961 bytes (over by 1 — should error or truncate).

### 12.2 Mock-USB dry-run

`tests/dry_run.py` extends with a synthetic EverDrive scenario:

- **Phase X1**: device boot, sync mode, saves discovered → uploaded.
- **Phase X2**: SRAM tick triggers drift → no upload.
- **Phase X3**: cart power-cycle (health goes false then true) →
  poke fires, immediate sync.
- **Phase X4**: cloud-newer save (from EmuDeck side) → split to
  device, EEPROM file appears.
- **Phase X5**: bootstrap pull when no save yet on cart, ROM
  exists → save files appear with right names.
- **Phase X6**: bootstrap pull when no ROM on cart → log warning,
  skip.

### 12.3 Real-hardware verification

Single user with one N64 + EverDrive 64 X7 + Deck + EmuDeck.
Test matrix:

- 5 N64 games covering all save types (EEPROM 4Kbit, EEPROM 16Kbit,
  SRAM, FlashRAM, Controller Pak).
- 5 round-trips per game: save → cart, upload, pull on Deck, save
  on Deck, upload, pull on cart.
- Verify byte-identical round-trips of the relevant per-format file.

## 13. Open questions / future

- **EverDrive's per-game save-type detection table.** We rely on the
  EverDrive's firmware to write the correct extension for each ROM.
  If the operator's cart firmware has an outdated table for newer
  homebrew, the wrong extension may be written. This is an EverDrive
  firmware issue, not RetroSync's, but documented here.
- **Multi-cart Controller Pak setups.** Some games swap Controller
  Pak data into multiple files (per cart insertion). Out of scope —
  treat as one Controller Pak per port.
- **Standalone Mupen on the Deck.** EmuDeck installs RetroArch's
  Mupen64Plus-Next by default. Operators using standalone
  Mupen64Plus see save files at `~/.local/share/mupen64plus/saves/`
  with per-format files (`.eep` etc., not combined) — same layout as
  EverDrive! That'd actually let us bypass the translator on the
  Deck side. v0.5 candidate.
- **Header-based game ID** for filename-inconsistent libraries (§5.1).
- **Version bytes from FlashRAM at known offsets** as a save-type
  discriminator. Some games (e.g. Pokémon Stadium 2) use FlashRAM
  AND Controller Pak, and the cpak segment has known signatures we
  could verify post-split. v0.5+ polish.
- **Per-port Controller Pak isolation** when only one player is in
  use. Current behavior packs all four ports' worth of cpak slots
  into the combined save; players 2–4 are zero-padded. Fine; just
  documented.

## 14. Generalizability — what's infrastructure vs. what's N64-specific

This section is for whoever picks up "add Genesis next" (or GBA, GG,
Saturn, etc.). It's a map of what already exists as system-agnostic
infrastructure vs. what each new system has to bring.

### 14.1 What's already generic (shipped or specified, no per-system rework)

These mechanisms were built for SNES + Pocket and apply to every new
system without modification. Adding Genesis costs zero changes here:

- **Sync engine, decision matrix, conflict handling, drift filter,
  manifest schema, lease mechanism, version retention.** The whole
  `sync.py` core runs on `(source_id, game_id, hash, parent_hash)`
  tuples and doesn't care what system the bytes came from.
- **Cloud path scheme** (`<system>/<game-id>/...`). System is just
  a string namespace.
- **`device_kind` cloud subfolders** (versions/n64-everdrive/,
  versions/genesis-everdrive/, versions/deck/...). System-agnostic.
- **udev SIGUSR1 poke** for instant cart-on detection. Add another
  rule, done.
- **Per-device-kind drift threshold** in config. One YAML line.
- **Per-game canonical-slug game-id derivation** with operator-
  controlled alias map.
- **EmuDeck adapter** — already system-configurable post-§6.1 of
  this doc. Adding Genesis is a config entry, no code change.
- **`SystemFormat` registry** with `combine=None / split=None` for
  single-file systems. Most systems live here.
- **`KrikzzFtdiTransport`** (new in this doc, §3.3) — shared across
  EverDrive products. Genesis EverDrive (Mega EverDrive Pro / X7 /
  X3) uses the same FT245 protocol family.

### 14.2 What each new system actually has to bring

Per-system, the work is:

1. **One config entry per cloud namespace.** Pick a system string
   (`"genesis"`, `"gba"`, `"gb"`, etc.) and a canonical extension
   for cloud's `current.<ext>`.
2. **One `SystemFormat` registry entry.** For most systems
   `combine=None / split=None`. For multi-file systems (Saturn-
   class), implement a translator module under `formats/`.
3. **Adapter(s).** Usually one per physical device class — e.g.
   "Mega EverDrive Pro" gets a `MegaEverDriveSource` if its USB
   protocol differs from the X7 in non-trivial ways. If the
   protocol is the same wire format, just parameterize an existing
   adapter.
4. **ROM-extension list.** Per-system list passed to the slug /
   filename-map machinery. Genesis = `[".bin", ".gen", ".md",
   ".smd"]`; GBA = `[".gba"]`; etc.
5. **(Optional) game-ID derivation refinements.** Most systems are
   fine with the filename-based slug + alias-map pattern. If a
   system has a stable internal ROM header that's worth using
   (Genesis does — title at $150, region at $1F0), spec the
   header-based fallback in the system's own design doc.

### 14.3 Genesis preview: what the next doc looks like

Worked example showing how lightweight a system's design doc can be
when the infrastructure is in place. (This is *not* the actual
Genesis design doc — when the time comes, write a proper one.)

```yaml
# config.yaml — Pi side
sources:
  - id: mega-everdrive-1
    adapter: mega_everdrive          # NEW adapter, ~150 lines
    options:
      transport: pyftdi
      ftdi_url: ftdi://ftdi:0x6001/1
      sd_saves_root: /SAVES
      sd_roms_root: /ROMS
      rom_extensions: [.bin, .gen, .md, .smd]

# config.yaml — Deck side, EmuDeck source for Genesis (no new adapter)
sources:
  - id: deck-1-genesis
    adapter: emudeck
    options:
      system: genesis
      saves_root: /home/deck/Emulation/saves/retroarch/saves
      roms_root: /home/deck/Emulation/roms/genesis
      rom_extensions: [.bin, .gen, .md, .smd]
      save_extension: .srm

drift_threshold:
  n64-everdrive: 4
  genesis-everdrive: 2          # tighter — Genesis SRAM doesn't tick much
```

```python
# sync.py
SYSTEM_FORMATS = {
    "snes":    SystemFormat(canonical_extension=".srm", combine=None, split=None),
    "n64":     SystemFormat(canonical_extension=".srm",
                            combine=n64.combine, split=n64.split),
    "genesis": SystemFormat(canonical_extension=".srm", combine=None, split=None),  # ← one line
}
```

That's it. The new code is roughly:

- `retrosync/sources/mega_everdrive/adapter.py` — single-file
  `EverDriveSource` analogue. Uses `KrikzzFtdiTransport`. ~150 lines
  including ROM-stem search and `target_save_paths_for` (returning a
  one-entry dict).
- A udev rule entry for the Mega EverDrive's product-string
  variation.

What it *doesn't* need:

- A format translator (Genesis is single-file).
- Engine changes (the path scheme, the decision matrix, the lease
  mechanism, all of it work unchanged).
- Schema migrations.
- Cloud layout changes (just a new `genesis/` namespace appears
  organically).

The Genesis design doc, when written, can therefore be a third the
length of this one. It's mostly documenting the EverDrive Mega
protocol specifics + Genesis ROM header conventions + EmuDeck Genesis
specifics. The shared scaffolding doesn't get re-described.

### 14.4 What would force shared-code changes

To set realistic expectations: future systems most likely to provoke
infrastructure-level changes are:

- **Saturn / Dreamcast** — both have multi-file save state (cart RAM
  + memory cards / VMUs). New `SystemFormat` translator modules,
  but the §6.2 framework absorbs them.
- **Multi-volume saves on a single device** — e.g. a single ROM
  using 8 separate Controller Pak files where the operator wants
  per-cpak versioning. We'd need to extend the manifest's
  per-game model. Not hard, but a real schema change. Defer until
  there's a concrete need.
- **Systems with no good canonical save format** — e.g. real-time
  cart-internal SRAM where the bytes change continuously without
  user interaction. The existing drift filter handles small drift;
  larger drift would need a per-system reference-comparison model.
  Unlikely to come up before Saturn.

### 14.5 Naming convention for system strings

Lowercase, hyphenless, no spaces. Pi-side adapters tag themselves
with `device_kind = "<system>-<cart-flavor>"` so cloud version
subfolders distinguish (e.g. `n64-everdrive` vs. a hypothetical
`n64-64drive`). Common slugs to reserve:

- `snes`, `n64`, `genesis`, `gb`, `gbc`, `gba`, `nes`, `gg`,
  `sms`, `pcecd`, `tg16`, `saturn`, `dreamcast`, `gcn`, `psx`.

Not all of these will ship; documenting now to avoid bikeshedding
when each one lands.

## 15. Appendix A — Why one big combined srm vs. preserving per-format files in cloud

We considered storing per-format files in cloud (mirror EverDrive's
layout):

```
gdrive:retro-saves/n64/super_mario_64/
├── current.eep
├── current.sra            # only if game has SRAM
├── manifest.json
└── versions/
    └── ...
```

Trade-offs:

| | Combined .srm | Per-format files |
|---|---|---|
| Hash equivalence with Deck | Yes (Deck writes combined) | No — would need on-the-fly translation for hash checks |
| File count per game | 1 | up to 6 |
| Translator runs | Once per upload, once per download | Never |
| Drift filter | Operates on combined bytes | Operates per-file (more complex) |
| Manifest size | Stable | Growth proportional to file count |
| Cloud listing | Simple | More entries, more rclone calls |

Combined wins on hash equivalence with the Deck (the dominant cross-
source case), and the translator round-trip cost is negligible
(~300 KB combine/split runs at >1 GB/s). Per-format files would
double the rclone request volume per upload too, which already cost
us during the Pocket rollout (rate-limit work in §16 prep).

**Decision: combined is canonical. Translator runs on the EverDrive
side only. EmuDeck saves bypass translation entirely.**
