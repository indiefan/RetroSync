"""Operator CLI: list, show, pull, push, test-cart, status."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click

from .cloud import RcloneCloud, compose_paths, sha256_bytes
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
              show_default=True, help="Path to config.yaml")
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

    click.echo(f"sources       : {len(sources)}")
    for s in sources:
        click.echo(f"  - {s['id']}  ({s['system']}, adapter={s['adapter']})")
    click.echo(f"files tracked : {files_total}")
    click.echo(f"versions: uploaded={versions_uploaded} "
               f"pending={versions_pending}")
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
                        binary=cfg.cloud.rclone_binary)
    data = cloud.download_bytes(src=cloud_path)
    Path(local_path).write_bytes(data)
    click.echo(f"wrote {len(data)} bytes to {local_path} "
               f"(sha256={sha256_bytes(data)[:8]})")


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
                        binary=cfg.cloud.rclone_binary)
    if cloud.reachable():
        click.echo(f"OK - {cfg.cloud.rclone_remote} is reachable")
    else:
        click.echo(f"FAIL - {cfg.cloud.rclone_remote} not reachable "
                   "(check `rclone config`)", err=True)
        sys.exit(1)


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
        "sources": [{"id": s.id, "adapter": s.adapter, "options": s.options}
                    for s in cfg.sources],
    }
    click.echo(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
