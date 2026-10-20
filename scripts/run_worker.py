"""Run one durable worker lane alongside the web app."""

import argparse
import signal
from threading import Event

from avd.jobs import Worker
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
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        if args.recover_stale:
            runtime.queue.recover_stale(force=True)
        worker = Worker(
            runtime.queue,
            args.lane,
            runtime.build_registry(),
            on_finished=runtime.on_finished,
        )
        if args.once:
            worker.run_once()
        else:
            worker.run_forever(stop, poll_interval=0.25)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
