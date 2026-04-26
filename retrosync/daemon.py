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

    # Set by run_all once the orchestrators exist; used by SIGUSR1 to
    # poke every orchestrator into an immediate pass (typically fired
    # by udev when an FXPak Pro USB device appears, so cart-on → first
    # sync latency is sub-second).
    orchestrators: list = []

    def _on_started(orcs: list) -> None:
        orchestrators[:] = orcs

    main_task = loop.create_task(run_all(config, on_started=_on_started))

    def _shutdown() -> None:
        if not main_task.done():
            main_task.cancel()

    def _poke_all() -> None:
        if not orchestrators:
            return
        logging.getLogger(__name__).info(
            "SIGUSR1 received → poking %d orchestrator(s) for an "
            "immediate pass", len(orchestrators))
        for o in orchestrators:
            o.poke()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)
    loop.add_signal_handler(signal.SIGUSR1, _poke_all)

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
