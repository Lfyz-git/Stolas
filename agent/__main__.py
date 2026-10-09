import argparse
import json
import os
import signal
import sys
import time

from .api import make_server
from .config import load
from .runner import Cooldown, Runner
from .storage import Busy, Store
from . import events


class Stopped(Exception):
    pass


def stop(signum, frame):
    raise Stopped()


def main():
    parser = argparse.ArgumentParser(description="Stolas iperf3 monitor")
    parser.add_argument("command", choices=["run", "serve", "schedule", "history", "validate"])
    parser.add_argument("--config", default=os.getenv("STOLAS_CONFIG"))
    parser.add_argument("--data", default=os.getenv("STOLAS_DATA_DIR", "data"))
    args = parser.parse_args()
    server = None
    configured_logging = False
    try:
        events.configure("stolas")
        configured_logging = True
        cfg = load(args.config)
        events.configure(cfg["node"])
        events.emit("configuration_loaded", "DEBUG")
        if args.command == "validate":
            print(json.dumps({"valid": True, "route_configured": bool(cfg["route"]["expected_public_cidrs"])}))
            return 0
        store = Store(args.data)
        runner = Runner(cfg, store)
        if args.command == "serve":
            server = make_server(runner, (os.getenv("STOLAS_LISTEN", "127.0.0.1"), int(os.getenv("STOLAS_PORT", "8080"))), os.getenv("STOLAS_API_TOKEN", ""))
            signal.signal(signal.SIGTERM, stop)
            events.emit("service_started")
            server.serve_forever()
        elif args.command == "history":
            print(json.dumps(store.history(), ensure_ascii=False))
        elif args.command == "schedule":
            while True:
                try:
                    print(json.dumps(runner.run(), ensure_ascii=False), flush=True)
                except (Busy, Cooldown):
                    pass
                time.sleep(10800)
        else:
            result = runner.run()
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "ok" else 2
        return 0
    except (Stopped, KeyboardInterrupt):
        return 0
    except Exception as e:
        if not configured_logging:
            events.logger.handlers[:] = [events.EventHandler("stolas")]
            events.logger.setLevel("INFO")
        events.emit("application_failed", "CRITICAL", reason=type(e).__name__)
        return 1
    finally:
        if server:
            server.server_close()
            events.emit("service_stopped")


if __name__ == "__main__":
    sys.exit(main())
