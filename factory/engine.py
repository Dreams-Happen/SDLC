"""Durable stage orchestration. Models cannot advance stages or authorize merge."""

import contextlib
import os
import threading
import time
import uuid

from .gitrepo import GitRepo, TargetMoved
from .providers import Providers
from .store import LostLease


class Engine:
    def __init__(self, store, workspace_root, providers=None):
        self.store = store
        self.workspace_root = workspace_root
        self.providers = providers or Providers()
        self._local_lock = threading.Lock()

    @contextlib.contextmanager
    def workspace_lock(self, repo):
        """An expired database lease must not permit concurrent filesystem writes."""
        path = repo.root / (repo.path.name + ".lock")
        with path.open("a+b") as handle:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise LostLease("Workspace still held by another worker") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def run_one(self):
        with self._local_lock:
            return self._run_one()

    def _run_one(self):
        owner = uuid.uuid4().hex
        task = self.store.claim(owner)
        if task is None:
            return False
        data = task["data"]
        executed_stage = task["stage"]
        repo_config = data["config"]["repositories"][data["repository"]]
        repo = GitRepo(self.workspace_root, task["id"], repo_config)
        stop = threading.Event()
        lease_lost = threading.Event()

        def heartbeat():
            while not stop.wait(10):
                if not self.store.heartbeat(task["id"], owner):
                    lease_lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        started = time.monotonic()
        previous_start = data.pop("active_started", None)
        if previous_start is not None:
            data["active_seconds"] += max(0, time.time() - previous_start)
        data["active_started"] = time.time()

        def checkpoint(kind, details=None):
            if lease_lost.is_set():
                raise LostLease("Worker lease expired")
            self.store.save(task, owner, kind, details)

        try:
            with self.workspace_lock(repo):
                checkpoint("stage_started", {"stage": task["stage"]})
                remaining = data["config"]["budgets"]["active_seconds"] - data["active_seconds"]
                if not data.get("base"):
                    if remaining <= 0:
                        raise RuntimeError("Active-time budget exhausted")
                    data["base"] = data["sha"] = repo.prepare()
                    checkpoint("workspace_prepared", {"base": data["base"]})
                else:
                    repo.prepare()
                # Confirm an already-requested side effect even when recovery exhausted
                # the execution budget. This is read-only reconciliation, not a new push.
                if task["stage"] == "merge" and data.get("merge_intent") and repo.contains(data["sha"]):
                    data["merged_sha"] = data["sha"]
                    task["status"] = "completed"
                else:
                    if remaining <= 0:
                        raise RuntimeError("Active-time budget exhausted")
                    self.step(task, repo, checkpoint, lambda: remaining - (time.monotonic() - started))
                if task["status"] == "running":
                    task["status"] = "queued"
        except LostLease:
            # A cancelled or reassigned worker may not overwrite the new state.
            if self.store.heartbeat(task["id"], owner):
                task["status"] = "queued"
                self.store.save(task, owner, "workspace_busy", release=True)
            return True
        except Exception as exc:
            task["status"] = "failed"
            data["error"] = str(exc)[:1000]
        finally:
            stop.set()
            thread.join(timeout=1)
        data.pop("active_started", None)
        data["active_seconds"] += time.monotonic() - started
        try:
            self.store.save(task, owner, "stage_finished", {"stage": executed_stage, "next_stage": task["stage"],
                            "status": task["status"], "error": data.get("error")}, release=True)
        except LostLease:
            pass
        return True

    def context(self, task, repo):
        data = task["data"]
        return {"request": data["request"], "answers": data["answers"], "repair_reason": data.get("repair_reason"),
                "repository": repo.snapshot(), "checks": repo.repo["checks"],
                "allowed_paths": repo.repo.get("allowed_paths"),
                "protected_paths": repo.repo.get("protected_paths", []),
                "artifacts": data["artifacts"], "base": data["base"], "candidate": data["sha"],
                "diff": repo.diff(data["base"], data["sha"])}

    def model(self, task, repo, checkpoint, remaining):
        data, stage = task["data"], task["stage"]
        if data["model_calls"] >= data["config"]["budgets"]["model_calls"]:
            raise RuntimeError("Model-call budget exhausted")
        if remaining() <= 0:
            raise RuntimeError("Active-time budget exhausted")
        provider_name = data["config"]["routes"][stage]
        provider = data["config"]["providers"][provider_name]
        data["model_calls"] += 1
        # Reserve before calling: an interrupted request still consumes its call budget.
        checkpoint("model_requested", {"stage": stage, "provider": provider_name,
                   "call": data["model_calls"]})
        result = self.providers.invoke(provider, stage, self.context(task, repo), timeout=remaining())
        if not self.store.heartbeat(task["id"], task["owner"]):
            raise LostLease("Task cancelled while model request was in progress")
        data.setdefault("usage", []).append({"stage": stage, "provider": provider_name,
                    "model": result["model"], "usage": result["usage"]})
        return result["output"]

    @staticmethod
    def invalidate(data):
        data.pop("approval_sha", None)
        data.pop("merge_intent", None)
        data["artifacts"].pop("verification", None)
        data["artifacts"].pop("review", None)

    def repair(self, task, reason):
        data = task["data"]
        if data["repairs"] >= data["config"]["budgets"]["repair_attempts"]:
            raise RuntimeError("Repair-attempt budget exhausted: " + reason)
        data["repairs"] += 1
        data["repair_reason"] = reason
        task["stage"] = "repair"

    def step(self, task, repo, checkpoint, remaining):
        data, stage = task["data"], task["stage"]
        artifacts = data["artifacts"]
        if stage in ("scope", "plan", "build", "repair", "review"):
            output = self.model(task, repo, checkpoint, remaining)
            if stage == "scope":
                artifacts["scope"] = output
                if output["questions"]:
                    task["status"] = "awaiting_input"
                else:
                    task["stage"] = "plan"
            elif stage == "plan":
                artifacts["plan"] = output
                task["stage"] = "build"
            elif stage in ("build", "repair"):
                if not self.store.heartbeat(task["id"], task["owner"]):
                    raise LostLease("Task cancelled before file edits")
                paths = repo.apply(output["files"])
                data["sha"] = repo.commit(paths, f"factory: {output['summary'][:180]}")
                # Keep failure evidence available to the repair request, then retire it.
                data.setdefault("attempt_history", []).append({"stage": stage, "sha": data["sha"],
                    "summary": output["summary"], "previous_verification": artifacts.get("verification"),
                    "previous_review": artifacts.get("review")})
                self.invalidate(data)
                artifacts["implementation"] = {"sha": data["sha"], "summary": output["summary"], "paths": paths}
                task["stage"] = "verify"
            else:
                artifacts["review"] = output | {"sha": data["sha"], "base": data["base"]}
                if output["verdict"] == "changes_requested" or any(f["severity"] == "blocking" for f in output["findings"]):
                    self.repair(task, "Independent review requested changes")
                else:
                    task["stage"] = "merge"
        elif stage == "verify":
            if not repo.clean(data["sha"]):
                raise RuntimeError("Candidate changed before verification")
            results = repo.checks(data["sha"], remaining())
            passed = all(result["exit_code"] == 0 for result in results)
            artifacts["verification"] = {"sha": data["sha"], "base": data["base"],
                                         "passed": passed, "checks": results}
            if passed:
                task["stage"] = "review"
            else:
                self.repair(task, "Required checks failed")
        elif stage == "merge":
            self.merge(task, repo, checkpoint)
        elif stage == "refresh":
            if data["refreshes"] >= 3:
                raise RuntimeError("Target branch moved too often; retry with a new task")
            data["refreshes"] += 1
            checkpoint("refresh_started", {"old_base": data["base"]})
            data["base"], data["sha"] = repo.refresh(data["base"])
            self.invalidate(data)
            task["stage"] = "verify"
        else:
            raise RuntimeError("Unknown workflow stage")

    def merge(self, task, repo, checkpoint):
        data = task["data"]
        # Reconcile a crash/timeout after the external side effect before retrying it.
        if data.get("merge_intent") and repo.contains(data["sha"]):
            data["merged_sha"] = data["sha"]
            task["status"] = "completed"
            return
        verification = data["artifacts"].get("verification", {})
        review = data["artifacts"].get("review", {})
        names = {check["name"] for check in repo.repo["checks"]}
        if (not verification.get("passed") or verification.get("sha") != data["sha"]
                or verification.get("base") != data["base"]
                or {c["name"] for c in verification.get("checks", [])} != names
                or any(c["exit_code"] != 0 or c["sha"] != data["sha"] for c in verification["checks"])
                or review.get("sha") != data["sha"] or review.get("base") != data["base"]
                or review.get("verdict") != "approve"
                or any(f["severity"] == "blocking" for f in review.get("findings", []))):
            raise RuntimeError("Merge evidence is missing, stale, or failing")
        if not repo.clean(data["sha"]):
            raise RuntimeError("Candidate changed after checks or review")
        if repo.target() != data["base"]:
            self.invalidate(data)
            task["stage"] = "refresh"
            return
        if repo.repo["merge"] == "manual" and data.get("approval_sha") != data["sha"]:
            task["status"] = "awaiting_approval"
            return
        data["merge_intent"] = {"base": data["base"], "sha": data["sha"]}
        checkpoint("merge_requested", data["merge_intent"])
        try:
            # Check ownership immediately before crossing the external mutation boundary.
            if not self.store.heartbeat(task["id"], task["owner"]):
                raise LostLease("Lease lost before merge")
            data["merged_sha"] = repo.merge(data["base"], data["sha"])
            task["status"] = "completed"
        except TargetMoved:
            if repo.contains(data["sha"]):
                data["merged_sha"] = data["sha"]
                task["status"] = "completed"
            else:
                self.invalidate(data)
                task["stage"] = "refresh"

    def worker(self, stop):
        while not stop.is_set():
            if not self.run_one():
                stop.wait(0.5)


def public_task(task):
    result = {key: value for key, value in task.items() if key not in ("owner", "lease_until", "request_key", "fingerprint")}
    result["data"] = {key: value for key, value in task["data"].items() if key != "config"}
    return result
