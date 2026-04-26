"""Operator CLI: list, show, pull, push, test-cart, status, conflicts,
sync-status, pocket-sync."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click

from . import conflicts as conflicts_mod
from . import leases as leases_mod
from . import promote as promote_mod
from .cloud import CloudError, RcloneCloud, compose_paths, hash8, sha256_bytes
from .config import DEFAULT_CONFIG_PATH, Config
from .sources.base import SaveRef
from .sources.registry import build as build_source
from .state import StateStore


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@click.group()
@click.option("--config", "config_path", default=DEFAULT_CONFIG_PATH,
              envvar="RETROSYNC_CONFIG", show_default=True,
              help="Path to config.yaml. Honors RETROSYNC_CONFIG.")
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def main(ctx: click.Context, config_path: str, verbose: bool) -> None:
    """RetroSync operator CLI."""
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["config"] = Config.load(config_path)


# ---------------- inspection ----------------

@main.command("status")
@click.pass_context
def cmd_status(ctx: click.Context) -> None:
    """Summarize what the daemon has done."""
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)

    sources = list(state._conn.execute("SELECT * FROM sources"))
    files_total = state._conn.execute(
        "SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    versions_uploaded = state._conn.execute(
        "SELECT COUNT(*) AS n FROM versions WHERE state='uploaded'").fetchone()["n"]
    versions_pending = state._conn.execute(
        "SELECT COUNT(*) AS n FROM versions "
        "WHERE state IN ('pending','debouncing','ready','uploading')"
    ).fetchone()["n"]

    open_conflicts = state._conn.execute(
        "SELECT COUNT(*) AS n FROM conflicts WHERE resolved_at IS NULL"
    ).fetchone()["n"]

    click.echo(f"sources       : {len(sources)}")
    for s in sources:
        click.echo(f"  - {s['id']}  ({s['system']}, adapter={s['adapter']})")
    click.echo(f"files tracked : {files_total}")
    click.echo(f"versions: uploaded={versions_uploaded} "
               f"pending={versions_pending}")
    click.echo(f"conflicts     : {open_conflicts} open")
    click.echo(f"db            : {cfg.state.db_path}")
    click.echo(f"cloud remote  : {cfg.cloud.rclone_remote}")
    state.close()


@main.command("list")
@click.pass_context
def cmd_list(ctx: click.Context) -> None:
    """List every (source, save file) we have on record."""
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    rows = list(state._conn.execute("""
        SELECT f.source_id, f.path, f.game_id, f.current_hash, f.last_seen,
               (SELECT COUNT(*) FROM versions v
                  WHERE v.source_id=f.source_id AND v.path=f.path
                    AND v.state='uploaded') AS versions
        FROM files f ORDER BY f.source_id, f.path
    """))
    if not rows:
        click.echo("(no files tracked yet)")
        return
    for r in rows:
        h = (r["current_hash"] or "?")[:8]
        click.echo(f"{r['source_id']}  {r['path']}  "
                   f"game={r['game_id']}  hash={h}  "
                   f"versions={r['versions']}  last_seen={r['last_seen']}")
    state.close()


@main.command("versions")
@click.argument("game_id")
@click.option("--from", "from_source",
              help="Restrict to versions uploaded by this source id.")
@click.pass_context
def cmd_versions(ctx: click.Context, game_id: str,
                 from_source: str | None) -> None:
    """Show full version history for GAME_ID across all sources.

    Each line shows when the version was uploaded, its hash and size,
    which device uploaded it, and its parent (the hash this version
    replaced on that device). The trailing per-source block is the
    "last hash we and the cloud agreed on" pointer the engine uses to
    decide upload vs. download vs. conflict on the next sync.

    Example:
      retrosync versions super_metroid
      retrosync versions super_metroid --from pocket-1
    """
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    sql = """
        SELECT v.*, f.game_id
        FROM versions v
        JOIN files f ON v.source_id = f.source_id AND v.path = f.path
        WHERE f.game_id = ? AND v.state = 'uploaded'
    """
    args: tuple = (game_id,)
    if from_source:
        sql += " AND v.source_id = ?"
        args = (game_id, from_source)
    sql += " ORDER BY v.uploaded_at DESC"
    rows = list(state._conn.execute(sql, args))
    if not rows:
        click.echo(f"(no versions for {game_id}"
                   f"{' from ' + from_source if from_source else ''})")
        state.close()
        return
    click.echo("uploaded_at               hash     size    from           "
               "parent   cloud_path")
    for r in rows:
        parent = (r["parent_hash"] or "")[:8] or "-"
        click.echo(f"  {r['uploaded_at']:24}  {r['hash'][:8]} "
                   f"{r['size_bytes']:>6}  {r['source_id']:<14} "
                   f"{parent:<8} {r['cloud_path'] or '-'}")
    # Per-source last-synced pointer for this game.
    sync_rows = list(state._conn.execute(
        "SELECT * FROM source_sync_state WHERE game_id=? "
        "ORDER BY source_id", (game_id,)))
    if sync_rows:
        click.echo()
        click.echo("Per-source last-synced hash (engine's "
                   "'we and cloud agree on this' pointer):")
        for r in sync_rows:
            click.echo(f"  {r['source_id']:<14}  "
                       f"{r['last_synced_hash'][:8]}  at "
                       f"{r['last_synced_at']}")
    state.close()


@main.command("show")
@click.argument("source_id")
@click.argument("path")
@click.pass_context
def cmd_show(ctx: click.Context, source_id: str, path: str) -> None:
    """Show all uploaded versions for a (source, path) pair."""
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    versions = state.list_versions(source_id, path)
    if not versions:
        click.echo("(no versions)")
        return
    for v in versions:
        click.echo(f"v{v.id:<4}  state={v.state:<11} hash={v.hash[:8]}  "
                   f"size={v.size_bytes:>6}  observed={v.observed_at}  "
                   f"cloud={v.cloud_path or '-'}")
    state.close()


# ---------------- pull/push (restore flow) ----------------

@main.command("pull")
@click.argument("cloud_path")
@click.argument("local_path", type=click.Path(dir_okay=False))
@click.pass_context
def cmd_pull(ctx: click.Context, cloud_path: str, local_path: str) -> None:
    """Download a specific cloud version to a local file.

    CLOUD_PATH is the full path as recorded in `retrosync show`,
    e.g. 'gdrive:retro-saves/snes/0a1b_super_metroid/versions/...'.
    """
    cfg: Config = ctx.obj["config"]
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    data = cloud.download_bytes(src=cloud_path)
    Path(local_path).write_bytes(data)
    click.echo(f"wrote {len(data)} bytes to {local_path} "
               f"(sha256={sha256_bytes(data)[:8]})")


@main.command("load")
@click.argument("game_id")
@click.argument("target")
@click.option("--device", help="Block device for pocket (e.g. /dev/sda1). "
              "Auto-detected via /dev/disk/by-id when omitted.")
@click.option("--mount-path", default=None,
              help="Where to mount the Pocket SD when target=pocket.")
@click.option("--system", default=None,
              help="Override the system namespace for the cloud lookup.")
@click.option("-y", "--yes", is_flag=True,
              help="Skip the confirmation prompt.")
@click.pass_context
def cmd_load(ctx: click.Context, game_id: str, target: str,
             device: str | None, mount_path: str | None,
             system: str | None, yes: bool) -> None:
    """Load cloud's current save for GAME_ID onto TARGET device.

    GAME_ID is the canonical game slug (see `retrosync sync-status` or
    `retrosync conflicts list --all` for examples).

    TARGET is one of:

      pocket    : the Analogue Pocket. The Pocket must be plugged in via
                  Tools → USB → Mount as USB Drive. Auto-detects the
                  block device unless --device is given.

      <system>  : a console name like "snes" — written to whichever cart
                  source is configured for that system.

    Examples:

      retrosync load final_fantasy_iii pocket
      retrosync load f_zero snes
    """
    from . import load as load_mod
    cfg: Config = ctx.obj["config"]
    if not yes:
        click.echo(f"about to load {game_id} → {target} "
                   f"(this overwrites the device's save).")
        if not click.confirm("proceed?", default=True):
            return
    def _on_wait():
        click.echo("Plug in the Pocket and enable Tools → USB → Mount as "
                   "USB Drive. Auto-sync is paused for this load.")
    try:
        result = load_mod.load(
            cfg=cfg, game_id=game_id, target=target,
            device=device,
            mount_path=mount_path or load_mod.DEFAULT_POCKET_MOUNT,
            system=system,
            on_wait=_on_wait,
        )
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        raise click.ClickException(str(exc))
    click.echo(
        f"wrote {result.bytes_written} bytes (sha256={result.sha256[:8]}) "
        f"to {result.target} at {result.written_path}")


@main.command("push")
@click.argument("source_id")
@click.argument("cart_path")
@click.argument("local_path", type=click.Path(dir_okay=False, exists=True))
@click.option("--confirm", is_flag=True, required=True,
              help="Required: this writes to your cart and replaces the live save.")
@click.pass_context
def cmd_push(ctx: click.Context, source_id: str, cart_path: str,
             local_path: str, confirm: bool) -> None:
    """Push a local save file to the cart at CART_PATH.

    Captures the cart's current SRAM as a new cloud version BEFORE pushing,
    so a botched restore is itself recoverable.
    """
    cfg: Config = ctx.obj["config"]
    src_cfg = next((s for s in cfg.sources if s.id == source_id), None)
    if src_cfg is None:
        raise click.ClickException(f"unknown source {source_id!r}")

    source = build_source(src_cfg.adapter, id=src_cfg.id, **src_cfg.options)
    new_data = Path(local_path).read_bytes()

    asyncio.run(_do_push(source, cart_path, new_data))
    click.echo(f"wrote {len(new_data)} bytes to cart {cart_path}")


async def _do_push(source, cart_path: str, new_data: bytes) -> None:
    # Belt-and-suspenders: read current cart bytes first so the orchestrator
    # has a chance to record them as a version on its next pass.
    try:
        current = await source.read_save(SaveRef(path=cart_path))
        click.echo(f"cart currently has {len(current)} bytes "
                   f"(sha256={sha256_bytes(current)[:8]})")
    except Exception as exc:
        click.echo(f"warning: could not read current cart save: {exc}")
    await source.write_save(SaveRef(path=cart_path), new_data)


# ---------------- diagnostics ----------------

@main.command("test-cart")
@click.argument("source_id")
@click.pass_context
def cmd_test_cart(ctx: click.Context, source_id: str) -> None:
    """Smoke-test the connection to a source. No writes."""
    cfg: Config = ctx.obj["config"]
    src_cfg = next((s for s in cfg.sources if s.id == source_id), None)
    if src_cfg is None:
        raise click.ClickException(f"unknown source {source_id!r}")
    source = build_source(src_cfg.adapter, id=src_cfg.id, **src_cfg.options)

    async def _go():
        h = await source.health()
        click.echo(f"health: {'OK' if h.ok else 'FAIL'} - {h.detail}")
        if not h.ok:
            sys.exit(1)
        saves = await source.list_saves()
        click.echo(f"found {len(saves)} save file(s):")
        for s in saves[:20]:
            click.echo(f"  {s.path}")
        if len(saves) > 20:
            click.echo(f"  ... and {len(saves)-20} more")

    asyncio.run(_go())


@main.command("test-cloud")
@click.pass_context
def cmd_test_cloud(ctx: click.Context) -> None:
    """Verify rclone can reach the configured remote."""
    cfg: Config = ctx.obj["config"]
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    if cloud.reachable():
        click.echo(f"OK - {cfg.cloud.rclone_remote} is reachable")
    else:
        click.echo(f"FAIL - {cfg.cloud.rclone_remote} not reachable "
                   "(check `rclone config`)", err=True)
        sys.exit(1)


@main.command("upgrade", context_settings={"ignore_unknown_options": True,
                                           "allow_extra_args": True})
@click.pass_context
def cmd_upgrade(ctx: click.Context) -> None:
    """Pull the latest source from GitHub and re-run the installer.

    This is normally intercepted by the /usr/local/bin/retrosync wrapper
    and dispatched to /usr/local/bin/retrosync-upgrade. The Click command
    is here so `retrosync --help` advertises it and so direct invocations
    of the binary inside the venv still work.
    """
    import os
    if os.path.exists("/usr/local/bin/retrosync-upgrade"):
        os.execvp("/usr/local/bin/retrosync-upgrade", ["retrosync-upgrade"])
    click.echo("error: /usr/local/bin/retrosync-upgrade not found. "
               "Run setup.sh from the repo to (re)install the upgrade entry "
               "point.", err=True)
    ctx.exit(1)


# ---------------- pocket-sync ----------------

@main.command("pocket-sync")
@click.option("--device", required=True,
              help="Block device, e.g. /dev/sda1, presented by the Pocket.")
@click.option("--source-id", default="pocket-1", show_default=True,
              help="Source id to record uploads under in state.db.")
@click.option("--mount-path", default="/run/retrosync/pocket-mount",
              show_default=True,
              help="Where to mount the Pocket SD.")
@click.option("--skip-mount", is_flag=True,
              help="Treat --mount-path as already-mounted (testing).")
@click.pass_context
def cmd_pocket_sync(ctx: click.Context, device: str, source_id: str,
                    mount_path: str, skip_mount: bool) -> None:
    """One-shot: mount the Pocket, run a sync, unmount.

    Designed to be invoked by the udev-triggered systemd unit
    `retrosync-pocket-sync@<dev>.service`. Can also be run by hand for
    debugging.
    """
    cfg: Config = ctx.obj["config"]
    # Lazy import: pocket sync requires sources/pocket.py + the runner; we
    # don't want CLI startup to pull either in for non-pocket commands.
    from .pocket.sync_runner import cli_pocket_sync
    rc = cli_pocket_sync(device=device, source_id=source_id,
                         mount_path=mount_path, config=cfg,
                         skip_mount=skip_mount)
    if rc != 0:
        sys.exit(rc)


# ---------------- conflicts ----------------

@main.group("conflicts")
def cmd_conflicts() -> None:
    """List, inspect, and resolve sync conflicts."""


@cmd_conflicts.command("list")
@click.option("--all", "show_all", is_flag=True,
              help="Include already-resolved conflicts.")
@click.pass_context
def cmd_conflicts_list(ctx: click.Context, show_all: bool) -> None:
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    rows = (conflicts_mod.list_all(state) if show_all
            else conflicts_mod.list_open(state))
    if not rows:
        click.echo("(no conflicts)")
        state.close()
        return
    for r in rows:
        status = ("resolved=" + r.resolved_at) if r.resolved_at else "OPEN"
        click.echo(
            f"#{r.id:<4}  game={r.game_id:<24} system={r.system:<6} "
            f"source={r.source_id:<14} "
            f"cloud={hash8(r.cloud_hash)} device={hash8(r.device_hash)} "
            f"detected={r.detected_at}  {status}")
    state.close()


@cmd_conflicts.command("show")
@click.argument("conflict_id", type=int)
@click.pass_context
def cmd_conflicts_show(ctx: click.Context, conflict_id: int) -> None:
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    row = conflicts_mod.get(state, conflict_id)
    if row is None:
        click.echo(f"no such conflict: {conflict_id}", err=True)
        state.close()
        sys.exit(1)
    click.echo(f"id            : {row.id}")
    click.echo(f"game_id       : {row.game_id}")
    click.echo(f"system        : {row.system}")
    click.echo(f"source        : {row.source_id}")
    click.echo(f"detected_at   : {row.detected_at}")
    click.echo(f"base_hash     : {row.base_hash or '(none)'}")
    click.echo(f"cloud_hash    : {row.cloud_hash}")
    click.echo(f"  cloud_path  : {row.cloud_path or '(unknown)'}")
    click.echo(f"device_hash   : {row.device_hash}")
    click.echo(f"  conflict_path: {row.conflict_path or '(unknown)'}")
    if row.resolved_at:
        click.echo(f"resolved_at   : {row.resolved_at}")
        click.echo(f"winner_hash   : {row.winner_hash}")
    else:
        click.echo("status        : OPEN")
        click.echo("Resolve with:  retrosync conflicts resolve "
                   f"{row.id} --winner {{cloud|device|<hash>}}")
    state.close()


@cmd_conflicts.command("resolve")
@click.argument("conflict_id", type=int)
@click.option("--winner", required=True,
              help="One of: cloud, device, or a full version hash.")
@click.pass_context
def cmd_conflicts_resolve(ctx: click.Context, conflict_id: int,
                          winner: str) -> None:
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    try:
        result = conflicts_mod.resolve(
            state=state, cloud=cloud, conflict_id=conflict_id,
            winner=winner, remote=cfg.cloud.rclone_remote,
        )
    except (ValueError, Exception) as exc:  # noqa: BLE001
        click.echo(f"resolve failed: {exc}", err=True)
        state.close()
        sys.exit(1)
    click.echo(f"resolved #{result.conflict_id}: winner_hash="
               f"{hash8(result.winner_hash)}, current is now {result.new_current_path}")
    state.close()


# ---------------- lease ----------------

@main.group("lease")
def cmd_lease() -> None:
    """Inspect and manage active-device leases."""


def _split_source_game(spec: str) -> tuple[str, str]:
    if ":" not in spec:
        raise click.ClickException(
            f"expected <source>:<game-id>, got {spec!r}")
    src, gid = spec.split(":", 1)
    return src, gid


@cmd_lease.command("list")
@click.option("--system", default="snes", show_default=True,
              help="System namespace to scan.")
@click.pass_context
def cmd_lease_list(ctx: click.Context, system: str) -> None:
    """Walk every game manifest under <remote>/<system>/ and print any
    active leases. Shows expired leases too (marked) so you can spot
    crashed-device locks."""
    cfg: Config = ctx.obj["config"]
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    base = f"{cloud.remote.rstrip('/')}/{system}"
    try:
        entries = cloud.lsjson(base)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"listing {base}: {exc}")
    found = 0
    for e in entries:
        if not e.get("IsDir"):
            continue
        game_id = e["Name"]
        paths = compose_paths(remote=cloud.remote, system=system,
                              game_id=game_id, save_filename=f"{game_id}.bin")
        try:
            manifest = cloud.read_manifest(paths)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  {game_id}: ERROR reading manifest ({exc})")
            continue
        if manifest is None or manifest.active_lease is None:
            continue
        found += 1
        click.echo(f"{system}/{game_id}: {leases_mod.describe(manifest.active_lease)}")
    if not found:
        click.echo("(no active leases)")


@cmd_lease.command("show")
@click.argument("spec")
@click.option("--system", default="snes", show_default=True)
@click.pass_context
def cmd_lease_show(ctx: click.Context, spec: str, system: str) -> None:
    """`<source>:<game-id>` — show that game's lease (or `(none)`)."""
    _src, game_id = _split_source_game(spec)
    cfg: Config = ctx.obj["config"]
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    paths = compose_paths(remote=cloud.remote, system=system,
                          game_id=game_id, save_filename=f"{game_id}.bin")
    manifest = cloud.read_manifest(paths)
    lease = manifest.active_lease if manifest else None
    click.echo(leases_mod.describe(lease))


@cmd_lease.command("release")
@click.argument("spec")
@click.option("--system", default="snes", show_default=True)
@click.option("--force", is_flag=True,
              help="Release even if the lease is held by a different source.")
@click.pass_context
def cmd_lease_release(ctx: click.Context, spec: str, system: str,
                      force: bool) -> None:
    """`<source>:<game-id> [--force]` — release the lease.

    The operator escape hatch: when a device crashed and left the
    lease hanging until expiry, --force clears it now."""
    src, game_id = _split_source_game(spec)
    cfg: Config = ctx.obj["config"]
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    paths = compose_paths(remote=cloud.remote, system=system,
                          game_id=game_id, save_filename=f"{game_id}.bin")
    cleared = leases_mod.release(cloud=cloud, paths=paths,
                                 source_id=src, force=force)
    if cleared:
        click.echo(f"released lease on {system}/{game_id}")
    else:
        click.echo(f"no change to {system}/{game_id} lease (held by other "
                   f"source — re-run with --force to override)")


# ---------------- promote ----------------

@main.command("promote")
@click.argument("game_id")
@click.argument("selector")
@click.option("--system", default="snes", show_default=True,
              help="System namespace under the cloud remote.")
@click.option("-y", "--yes", is_flag=True,
              help="Skip the confirmation prompt.")
@click.pass_context
def cmd_promote(ctx: click.Context, game_id: str, selector: str,
                system: str, yes: bool) -> None:
    """Force a historical version to be cloud's `current` save.

    SELECTOR is one of:

    \b
      <hash8>          first 8 hex chars (matches `retrosync versions`)
      <full-sha256>    full hash (unambiguous)
      <cloud-path>     a versions/... path from `retrosync versions`

    \b
    Examples:
      retrosync promote final_fantasy_iii 7def5901
      retrosync promote final_fantasy_iii \\
        gdrive:retro-saves/snes/final_fantasy_iii/versions/snes/2026-...srm

    On the next sync each device sees case 6 (cloud advanced;
    device unchanged) and pulls the promoted bytes down — provided
    `cloud_to_device: true` is set in their config.
    """
    cfg: Config = ctx.obj["config"]
    if not yes:
        click.echo(f"about to promote {selector!r} → cloud current "
                   f"for {system}/{game_id}.")
        if not click.confirm("proceed?", default=True):
            return
    state = StateStore(cfg.state.db_path)
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    try:
        result = promote_mod.promote(
            state=state, cloud=cloud, game_id=game_id,
            selector=selector, system=system)
    except (ValueError, CloudError) as exc:
        state.close()
        raise click.ClickException(str(exc))
    state.close()
    click.echo(f"promoted {hash8(result.promoted_hash)} for "
               f"{game_id} → {result.new_current_path}")
    click.echo(f"  source bytes: {result.promoted_path}")
    click.echo("  next sync on each device pulls these bytes "
               "(case 6: cloud advanced).")


# ---------------- sync-status ----------------

@main.command("sync-status")
@click.option("--source", "source_id",
              help="Restrict to one source id.")
@click.pass_context
def cmd_sync_status(ctx: click.Context, source_id: str | None) -> None:
    """Per-(source, game) last-sync summary from source_sync_state."""
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    sql = "SELECT * FROM source_sync_state"
    args: tuple = ()
    if source_id:
        sql += " WHERE source_id=?"
        args = (source_id,)
    sql += " ORDER BY source_id, game_id"
    rows = list(state._conn.execute(sql, args))
    if not rows:
        click.echo("(no sync state recorded yet)")
        state.close()
        return
    for r in rows:
        click.echo(
            f"{r['source_id']:<14}  {r['game_id']:<28} "
            f"last_synced_hash={hash8(r['last_synced_hash'])} "
            f"at={r['last_synced_at']}")
    state.close()


@main.command("migrate-paths")
@click.option("--system", default="snes", show_default=True,
              help="System namespace to migrate.")
@click.option("--dry-run", is_flag=True,
              help="Print the plan without modifying cloud or state.db.")
@click.pass_context
def cmd_migrate_paths(ctx: click.Context, system: str, dry_run: bool) -> None:
    """One-shot: collapse legacy `<crc32>_<slug>` and `unknown_<slug>`
    cloud folders into the new canonical `<slug>` layout.

    Idempotent: re-running on an already-migrated tree is a no-op.
    """
    from . import migrate as migrate_mod
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    cloud = RcloneCloud(remote=cfg.cloud.rclone_remote,
                        binary=cfg.cloud.rclone_binary,
                        config_path=cfg.cloud.rclone_config_path)
    plan = migrate_mod.plan_migration(cloud=cloud, system=system)
    if not plan:
        click.echo(f"(nothing under {cfg.cloud.rclone_remote}/{system}/)")
        state.close()
        return
    for p in plan:
        click.echo(f"  {p.action:<6}  {p.legacy_id} → {p.canonical_id}")
    if dry_run:
        click.echo("(dry-run; no changes made)")
        state.close()
        return
    counts = migrate_mod.apply_migration(cloud=cloud, plan=plan, state=state,
                                         dry_run=False)
    click.echo("done. summary: " +
               ", ".join(f"{k}={v}" for k, v in counts.items()))
    state.close()


# ---------------- deck (EmuDeck / Steam Deck) ----------------

@main.command("wrap-extract-rom",
              context_settings={"ignore_unknown_options": True,
                                "allow_extra_args": True})
@click.pass_context
def cmd_wrap_extract_rom(ctx: click.Context) -> None:
    """Print the ROM-path argument from an emulator command line.

    Usage:  retrosync wrap-extract-rom -- <emulator-args...>

    Used by the bash wrap dispatcher to find the ROM among the args
    Steam ROM Manager generated. Exits non-zero if no ROM is found
    so the dispatcher can skip the sync."""
    from .deck.wrap import cmd_extract_rom
    sys.exit(cmd_extract_rom(list(ctx.args)))


@main.command("wrap-derive-game-id")
@click.argument("rom_path")
@click.pass_context
def cmd_wrap_derive_game_id(ctx: click.Context, rom_path: str) -> None:
    """Print `<system>:<game_id>` for ROM_PATH.

    System is the EmuDeck `roms/<system>/` directory the ROM lives
    under; game_id is the canonical slug of the ROM filename."""
    from .deck.wrap import cmd_derive_game_id
    cfg: Config = ctx.obj["config"]
    sys.exit(cmd_derive_game_id(rom_path, config=cfg))


@main.command("wrap-pre")
@click.argument("source_id")
@click.argument("system_game")
@click.option("--timeout", "timeout_sec", type=float, default=10.0,
              show_default=True)
@click.pass_context
def cmd_wrap_pre(ctx: click.Context, source_id: str, system_game: str,
                 timeout_sec: float) -> None:
    """Pre-launch sync + lease grab for SYSTEM:GAME_ID under SOURCE_ID."""
    if ":" not in system_game:
        raise click.ClickException("expected system:game_id")
    system, game_id = system_game.split(":", 1)
    from .deck.wrap import cmd_wrap_pre
    cfg: Config = ctx.obj["config"]
    sys.exit(cmd_wrap_pre(source_id=source_id, system=system,
                          game_id=game_id, config=cfg,
                          timeout_sec=timeout_sec))


@main.command("wrap-post")
@click.argument("source_id")
@click.argument("system_game")
@click.option("--timeout", "timeout_sec", type=float, default=30.0,
              show_default=True)
@click.pass_context
def cmd_wrap_post(ctx: click.Context, source_id: str, system_game: str,
                  timeout_sec: float) -> None:
    """Post-exit cleanup for SYSTEM:GAME_ID under SOURCE_ID."""
    if ":" not in system_game:
        raise click.ClickException("expected system:game_id")
    system, game_id = system_game.split(":", 1)
    from .deck.wrap import cmd_wrap_post
    cfg: Config = ctx.obj["config"]
    sys.exit(cmd_wrap_post(source_id=source_id, system=system,
                           game_id=game_id, config=cfg,
                           timeout_sec=timeout_sec))


@main.command("flush")
@click.option("--timeout", "timeout_sec", type=float, default=10.0,
              show_default=True,
              help="Hard cap so suspend isn't blocked.")
@click.pass_context
def cmd_flush(ctx: click.Context, timeout_sec: float) -> None:
    """Drain in-flight uploads. Run before suspend by the systemd hook."""
    from .deck.flush import flush as do_flush
    cfg: Config = ctx.obj["config"]
    res = do_flush(config=cfg, timeout_sec=timeout_sec)
    click.echo(f"flush: attempted={res.attempted} succeeded={res.succeeded} "
               f"failed={res.failed} timed_out={res.timed_out}")


@main.command("sync-pending")
@click.pass_context
def cmd_sync_pending(ctx: click.Context) -> None:
    """Re-attempt any uploads that errored during an offline window.

    Fired by NetworkManager-dispatcher on `up` so the daemon doesn't
    have to wait for the next inotify burst to discover that it can
    reach Drive again."""
    from .deck.flush import sync_pending
    cfg: Config = ctx.obj["config"]
    res = sync_pending(config=cfg)
    click.echo(f"sync-pending: attempted={res.attempted} "
               f"succeeded={res.succeeded} failed={res.failed}")


# ---------------- filename-map ----------------

@main.group("filename-map")
def cmd_filename_map() -> None:
    """Inspect / invalidate the per-source save filename cache."""


@cmd_filename_map.command("list")
@click.option("--source", "source_id", help="Restrict to one source.")
@click.pass_context
def cmd_filename_map_list(ctx: click.Context,
                          source_id: str | None) -> None:
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    rows = state.list_filename_map(source_id=source_id)
    if not rows:
        click.echo("(no filename map entries)")
        state.close()
        return
    for r in rows:
        rom = r.get("rom_stem") or "-"
        click.echo(f"{r['source_id']:<14}  {r['game_id']:<28} "
                   f"{r['filename']:<40} rom_stem={rom} "
                   f"observed={r['observed_at']}")
    state.close()


@cmd_filename_map.command("invalidate")
@click.argument("source_id")
@click.argument("game_id", required=False)
@click.pass_context
def cmd_filename_map_invalidate(ctx: click.Context, source_id: str,
                                game_id: str | None) -> None:
    """`<source>` clears that source; `<source> <game-id>` one entry."""
    cfg: Config = ctx.obj["config"]
    state = StateStore(cfg.state.db_path)
    n = state.invalidate_filename_map(source_id, game_id)
    state.close()
    click.echo(f"invalidated {n} entry/entries")


# ---------------- deck patch-srm ----------------

@main.group("deck")
def cmd_deck() -> None:
    """Steam Deck / EmuDeck-specific operations."""


@cmd_deck.command("patch-srm")
@click.option("--config-path", default=None,
              help="Path to userConfigurations.json. Default: "
                   "~/.config/steam-rom-manager/userData/userConfigurations.json")
@click.option("--wrapper-path", default=None,
              help="Path to retrosync-wrap. Default: ~/.local/bin/retrosync-wrap")
@click.option("--unpatch", is_flag=True,
              help="Restore the parsers' original executables.")
@click.option("--dry-run", is_flag=True,
              help="Print the new config without writing it.")
@click.pass_context
def cmd_deck_patch_srm(ctx: click.Context,
                       config_path: str | None,
                       wrapper_path: str | None,
                       unpatch: bool, dry_run: bool) -> None:
    """Patch Steam ROM Manager parsers to call retrosync-wrap.

    After patching, re-run SRM's "Save to Steam" so every shortcut
    bakes in the wrapper. Idempotent — re-running is a no-op."""
    from .deck import srm as srm_mod
    cp = Path(config_path) if config_path else srm_mod.DEFAULT_SRM_CONFIG_PATH
    wp = Path(wrapper_path) if wrapper_path else srm_mod.DEFAULT_WRAPPER_PATH
    try:
        summary, _ = srm_mod.patch_srm_config(
            config_path=cp, wrapper_path=wp,
            unpatch=unpatch, write=not dry_run)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"parsers={summary.parsers_total} "
               f"patched={summary.patched} "
               f"already_patched={summary.already_patched} "
               f"unpatched={summary.unpatched} "
               f"skipped={summary.skipped}")
    if not dry_run and not unpatch and summary.patched > 0:
        click.echo()
        click.echo("Open Steam ROM Manager (EmuDeck → Tools → Steam ROM "
                   "Manager) and re-run \"Add Games\" → \"Parse\" → "
                   "\"Save to Steam\" to bake the wrapper into your "
                   "shortcuts.")


@cmd_deck.command("detect-paths")
@click.option("--system", default="snes", show_default=True)
@click.option("--emudeck-root", default=None,
              help="Override the auto-detected EmuDeck root. Use when "
                   "your Emulation/ directory isn't in any of the "
                   "default locations.")
@click.pass_context
def cmd_deck_detect_paths(ctx: click.Context, system: str,
                          emudeck_root: str | None) -> None:
    """Print the EmuDeck root, saves dir, and ROM dir we'd use.

    Useful for validating the install before running the daemon — if
    this prints sensible paths, the daemon will too."""
    from .deck import emudeck_paths
    override = Path(emudeck_root) if emudeck_root else None
    paths = emudeck_paths.detect_paths(system=system,
                                       emudeck_root_override=override)
    if paths is None:
        click.echo("EmuDeck install not detected — checked:")
        if override is not None:
            click.echo(f"  {override} (--emudeck-root)")
        for p in emudeck_paths.EMUDECK_ROOT_CANDIDATES:
            click.echo(f"  {p}")
        sys.exit(1)
    click.echo(f"emudeck_root  : {paths.emudeck_root}")
    click.echo(f"saves_root    : {paths.saves_root}")
    click.echo(f"roms_root     : {paths.roms_root}")
    click.echo(f"retroarch_cfg : {paths.retroarch_cfg or '(not found)'}")
    if paths.retroarch_cfg is not None:
        warnings = emudeck_paths.check_core_save_overrides(
            paths.retroarch_cfg)
        for w in warnings:
            click.echo(f"WARNING: {w.detail}")


@main.command("dump-config")
@click.pass_context
def cmd_dump_config(ctx: click.Context) -> None:
    """Print the loaded config (for debugging)."""
    cfg: Config = ctx.obj["config"]
    out = {
        "config_path": ctx.obj["config_path"],
        "cloud": cfg.cloud.__dict__,
        "orchestrator": cfg.orchestrator.__dict__,
        "state": cfg.state.__dict__,
        "lease": cfg.lease.__dict__,
        "sources": [{"id": s.id, "adapter": s.adapter, "options": s.options}
                    for s in cfg.sources],
    }
    click.echo(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
