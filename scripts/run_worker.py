"""Run one durable worker lane alongside the web app."""

import argparse
import signal
from threading import Event

from avd.observability import configure_logging
from avd.runtime import Runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=["gpu", "api", "cpu"], required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--recover-stale", action="store_true", help="Use only after previous workers have stopped"
    )
    args = parser.parse_args()
    runtime = Runtime()
    configure_logging(runtime.settings.log_format, runtime.settings.log_level)
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        if args.recover_stale:
            runtime.queue.recover_stale(force=True)
        worker = runtime.build_worker(args.lane)
        if args.once:
            worker.run_once(stop)
        else:
            worker.run_forever(stop, poll_interval=0.25)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
