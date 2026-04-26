"""Daemon entry point — invoked by the systemd unit and `retrosyncd`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .config import DEFAULT_CONFIG_PATH, Config
from .orchestrator import run_all


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="retrosyncd")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="Path to config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    config = Config.load(args.config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(run_all(config))

    def _shutdown() -> None:
        if not main_task.done():
            main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
