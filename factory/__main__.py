import argparse
import json
import os
from pathlib import Path
import threading
import uuid

from .config import load
from .demo import initialize, run
from .engine import Engine
from .server import make_server
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Model-independent SDLC factory")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the offline end-to-end demonstration")
    demo.add_argument("--directory", default=None)
    demo.add_argument("--prepare-only", action="store_true", help="Prepare a fixture/config for the HTTP service")
    serve = commands.add_parser("serve", help="Start the HTTP API and one workflow worker")
    serve.add_argument("--config", required=True)
    serve.add_argument("--state", default=".factory/service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        if args.command == "demo":
            directory = Path(args.directory or (".factory/demo-" + uuid.uuid4().hex[:8])).resolve()
            if args.prepare_only:
                initialize(directory)
                print(f"Prepared demo repository and configuration: {directory / 'config.json'}")
                return 0
            task = run(directory)
            print(json.dumps({"id": task["id"], "status": task["status"], "repairs": task["data"]["repairs"],
                "model_calls": task["data"]["model_calls"], "merged_sha": task["data"].get("merged_sha"),
                "error": task["data"].get("error"), "result": str(directory / "result.json")}, indent=2))
            return 0 if task["status"] == "completed" else 1
        config = load(args.config)
        root = Path(args.state).resolve()
        store = Store(root / "state.sqlite3")
        engine = Engine(store, root / "workspaces")
        server = make_server(args.host, args.port, store, config, os.environ.get("FACTORY_API_TOKEN"))
        stop = threading.Event()
        worker = threading.Thread(target=engine.worker, args=(stop,), daemon=True)
        worker.start()
        print(f"SDLC factory listening at http://{args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            server.server_close()
            worker.join(timeout=5)
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
