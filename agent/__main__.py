import argparse
import json
import os
import sys
import time

from .api import make_server
from .config import load
from .runner import Cooldown, Runner
from .storage import Busy, Store


def main():
    parser = argparse.ArgumentParser(description="Stolas iperf3 monitor")
    parser.add_argument("command", choices=["run", "serve", "schedule", "history", "validate"])
    parser.add_argument("--config", default=os.getenv("STOLAS_CONFIG"))
    parser.add_argument("--data", default=os.getenv("STOLAS_DATA_DIR", "data"))
    args = parser.parse_args()
    try:
        cfg = load(args.config)
        if args.command == "validate":
            print(json.dumps({"valid": True, "route_configured": bool(cfg["route"]["expected_public_cidrs"])}))
            return 0
        store = Store(args.data)
        runner = Runner(cfg, store)
        if args.command == "serve":
            server = make_server(runner, (os.getenv("STOLAS_LISTEN", "127.0.0.1"), int(os.getenv("STOLAS_PORT", "8080"))), os.getenv("STOLAS_API_TOKEN", ""))
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
    except (ValueError, OSError, Busy, Cooldown) as e:
        # Configuration errors contain field names, never dump environment or secrets.
        print(json.dumps({"error": type(e).__name__, "message": "Configuration, storage or runtime unavailable"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
