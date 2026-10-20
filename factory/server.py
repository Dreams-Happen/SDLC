"""HTTP control plane. Bind locally by default; authenticate shared deployments."""

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .engine import public_task
from .store import Conflict


def make_server(host, port, store, config, token=None):
    if host not in ("localhost", "127.0.0.1", "::1") and not token:
        raise ValueError("Set FACTORY_API_TOKEN before binding to a non-loopback address")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SDLCFactory/0.1"

        def log_message(self, format, *args):
            # Default request lines can contain credentials mistakenly placed in URLs.
            pass

        def send_json(self, status, data):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def handle_api(self, method):
            try:
                if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.send_json(401, {"error": "Bearer token required"})
                    return
                path = urlsplit(self.path).path.strip("/").split("/")
                if method == "GET":
                    if path == ["health"]:
                        self.send_json(200, {"status": "ok"})
                    elif path == ["api", "repositories"]:
                        self.send_json(200, [{"name": name, "branch": repo["branch"], "merge": repo["merge"],
                            "checks": [c["name"] for c in repo["checks"]]} for name, repo in config["repositories"].items()])
                    elif path == ["api", "tasks"]:
                        self.send_json(200, [{"id": task["id"], "status": task["status"],
                            "stage": task["stage"], "repository": task["data"]["repository"],
                            "task": task["data"]["request"][:300], "sha": task["data"].get("sha"),
                            "error": task["data"].get("error"), "created": task["created"],
                            "updated": task["updated"]} for task in store.list()])
                    elif len(path) == 3 and path[:2] == ["api", "tasks"]:
                        self.send_json(200, public_task(store.get(path[2])))
                    elif len(path) == 4 and path[:2] == ["api", "tasks"] and path[3] == "events":
                        self.send_json(200, store.events(path[2]))
                    else:
                        self.send_json(404, {"error": "Route not found"})
                    return
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.send_json(415, {"error": "Content-Type must be application/json"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100000:
                    self.send_json(413, {"error": "JSON body must contain 1–100000 bytes"})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                if path == ["api", "tasks"]:
                    task = store.create(payload.get("task"), payload.get("repository"), config,
                                        self.headers.get("Idempotency-Key"))
                    self.send_json(202, public_task(task))
                elif len(path) == 4 and path[:2] == ["api", "tasks"] and path[3] in ("answer", "approve", "resume", "cancel"):
                    self.send_json(200, public_task(store.action(path[2], path[3], payload)))
                else:
                    self.send_json(404, {"error": "Route not found"})
            except KeyError:
                self.send_json(404, {"error": "Task not found"})
            except Conflict as exc:
                self.send_json(409, {"error": str(exc)})
            except (ValueError, TypeError):
                self.send_json(400, {"error": "Invalid request; check task, repository, action, and JSON body"})
            except Exception:
                self.send_json(500, {"error": "Internal service error"})

        def do_GET(self):
            self.handle_api("GET")

        def do_POST(self):
            self.handle_api("POST")

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

    return ThreadingHTTPServer((host, port), Handler)
