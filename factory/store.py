"""SQLite checkpoints, atomic transitions, worker leases, and an audit trail."""

import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


class Conflict(ValueError):
    pass


class LostLease(RuntimeError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, stage TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    owner TEXT, lease_until REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
                );
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("Task not found")
        item = dict(row)
        item["data"] = json.loads(item["data"])
        return item

    def event(self, db, task_id, kind, data):
        db.execute("INSERT INTO events(task_id, at, kind, data) VALUES (?, ?, ?, ?)",
                   (task_id, time.time(), kind, json.dumps(data)))

    def create(self, request, repo_name, config, request_key=None):
        if not isinstance(request, str) or not request.strip() or len(request) > 20000:
            raise ValueError("Task must contain 1–20000 characters")
        if repo_name not in config["repositories"]:
            raise ValueError("Unknown repository")
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 200):
            raise ValueError("Invalid idempotency key")
        fingerprint = hashlib.sha256(json.dumps([request, repo_name]).encode()).hexdigest()
        task_id, now = uuid.uuid4().hex, time.time()
        data = {"request": request, "repository": repo_name, "config": config,
                "model_calls": 0, "active_seconds": 0, "repairs": 0, "refreshes": 0,
                "answers": [], "artifacts": {}}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                existing = db.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise Conflict("Idempotency key already belongs to a different request")
                    return self.decode(existing)
            db.execute("INSERT INTO tasks VALUES (?, ?, ?, 'queued', 'scope', ?, ?, ?, NULL, NULL)",
                       (task_id, request_key, fingerprint, json.dumps(data), now, now))
            self.event(db, task_id, "created", {"repository": repo_name})
        return self.get(task_id)

    def get(self, task_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list(self):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 200")]

    def events(self, task_id):
        self.get(task_id)
        with self.connect() as db:
            return [dict(row) | {"data": json.loads(row["data"])} for row in db.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))]

    def claim(self, owner, lease_seconds=90):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM tasks WHERE status='queued'
                OR (status='running' AND lease_until < ?) ORDER BY created LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running', owner=?, lease_until=?, updated=? WHERE id=?",
                       (owner, now + lease_seconds, now, row["id"]))
            self.event(db, row["id"], "claimed" if row["status"] == "queued" else "recovered",
                       {"stage": row["stage"]})
            return self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())

    def heartbeat(self, task_id, owner):
        with self.connect() as db:
            cursor = db.execute("""UPDATE tasks SET lease_until=? WHERE id=? AND owner=?
                AND status='running' AND lease_until>=?""", (time.time() + 90, task_id, owner, time.time()))
            return cursor.rowcount == 1

    def save(self, task, owner, kind, details=None, release=False):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone())
            if current["owner"] != owner or current["status"] != "running" or current["lease_until"] < now:
                raise LostLease("Task lease lost or task cancelled")
            db.execute("""UPDATE tasks SET status=?, stage=?, data=?, updated=?, owner=?, lease_until=?
                WHERE id=?""", (task["status"], task["stage"], json.dumps(task["data"]), now,
                                None if release else owner, None if release else now + 90, task["id"]))
            self.event(db, task["id"], kind, details or {})

    def action(self, task_id, action, payload):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            data = task["data"]
            if action == "cancel":
                if task["status"] in ("completed", "cancelled"):
                    raise Conflict("Task is already terminal")
                if data.get("merge_intent"):
                    raise Conflict("Merge may already be in progress; reconcile it before cancelling")
                task["status"] = "cancelled"
            elif action == "answer":
                if task["status"] != "awaiting_input":
                    raise Conflict("Task is not awaiting an answer")
                answer = payload.get("answer")
                if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
                    raise ValueError("Provide an answer of 1–20000 characters")
                data["answers"].append(answer)
                task.update(status="queued", stage="scope")
            elif action == "approve":
                if task["status"] != "awaiting_approval" or payload.get("sha") != data.get("sha"):
                    raise Conflict("Approval must match the current merge candidate")
                data["approval_sha"] = data["sha"]
                task["status"] = "queued"
            elif action == "resume":
                if task["status"] != "failed":
                    raise Conflict("Only failed tasks can be resumed")
                data.pop("error", None)
                task["status"] = "queued"
            else:
                raise ValueError("Unknown action")
            db.execute("UPDATE tasks SET status=?, stage=?, data=?, owner=NULL, lease_until=NULL, updated=? WHERE id=?",
                       (task["status"], task["stage"], json.dumps(data), time.time(), task_id))
            self.event(db, task_id, action, {"sha": data.get("sha")} if action == "approve" else {})
        return self.get(task_id)
