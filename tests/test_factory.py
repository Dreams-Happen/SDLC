from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from factory.config import validate
from factory.demo import TASK, initialize
from factory.engine import Engine
from factory.gitrepo import GitRepo, command
from factory.providers import Providers, validate_output
from factory.server import make_server
from factory.store import Conflict, Store


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = initialize(self.root)
        self.store = Store(self.root / "state.sqlite3")
        self.engine = Engine(self.store, self.root / "workspaces")
        self.task = self.store.create(TASK, "calculator", self.config)
        self.repo = GitRepo(self.root / "workspaces", self.task["id"], self.config["repositories"]["calculator"])

    def current(self):
        return self.store.get(self.task["id"])

    def until(self, stage=None, status=None):
        for _ in range(30):
            task = self.current()
            if stage is not None and task["stage"] == stage and (status is None or task["status"] == status):
                return task
            if status is not None and stage is None and task["status"] == status:
                return task
            if task["status"] not in ("queued", "running"):
                self.fail(f"Unexpected terminal task: {task['status']}: {task['data'].get('error')}")
            self.engine.run_one()
        self.fail("Workflow did not converge")

    def modify_data(self, edit):
        task = self.current()
        edit(task["data"])
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task["data"]), task["id"]))

    def test_full_workflow_repairs_real_failure_and_merges(self):
        task = self.until(status="completed")
        data = task["data"]
        self.assertEqual(data["repairs"], 1)
        self.assertEqual(data["model_calls"], 5)
        self.assertEqual(self.repo.target(), data["merged_sha"])
        self.assertTrue(data["artifacts"]["verification"]["passed"])
        failed = data["attempt_history"][1]["previous_verification"]
        self.assertFalse(failed["passed"])
        self.assertIn("FAILED", failed["checks"][0]["output"])

    def test_failed_checks_exhaust_budget_without_merge(self):
        self.modify_data(lambda d: d["config"]["budgets"].update(repair_attempts=0))
        task = self.until(status="failed")
        self.assertIn("Repair-attempt budget exhausted", task["data"]["error"])
        self.assertEqual(self.repo.target(), task["data"]["base"])
        self.assertNotIn("merged_sha", task["data"])

    def test_model_call_budget_is_reserved_before_invocation(self):
        self.modify_data(lambda d: d["config"]["budgets"].update(model_calls=1))
        task = self.until(status="failed")
        self.assertEqual(task["data"]["model_calls"], 1)
        self.assertIn("Model-call budget exhausted", task["data"]["error"])

    def test_stale_verification_cannot_merge(self):
        self.until(stage="merge")
        self.modify_data(lambda d: d["artifacts"]["verification"].update(sha="0" * 40))
        self.engine.run_one()
        task = self.current()
        self.assertEqual(task["status"], "failed")
        self.assertIn("stale", task["data"]["error"])
        self.assertEqual(self.repo.target(), task["data"]["base"])

    def test_worktree_tampering_after_review_cannot_merge(self):
        task = self.until(stage="merge")
        (self.repo.path / "calculator.py").write_text("tampered = True\n")
        self.engine.run_one()
        self.assertEqual(self.current()["status"], "failed")
        self.assertEqual(self.repo.target(), task["data"]["base"])

    def test_manual_approval_is_bound_to_current_candidate(self):
        self.modify_data(lambda d: d["config"]["repositories"]["calculator"].update(merge="manual"))
        task = self.until(status="awaiting_approval")
        with self.assertRaises(Conflict):
            self.store.action(task["id"], "approve", {"sha": "0" * 40})
        self.store.action(task["id"], "approve", {"sha": task["data"]["sha"]})
        self.assertEqual(self.until(status="completed")["data"]["merged_sha"], task["data"]["sha"])

    def advance_target(self):
        seed = self.root / "seed"
        (seed / "README.md").write_text("An independent target-branch change.\n")
        self.repo.git("add", "README.md", cwd=seed)
        self.repo.git("commit", "-m", "Advance target", cwd=seed)
        self.repo.git("push", str(self.root / "target.git"), "HEAD:main", cwd=seed)

    def test_moving_target_invalidates_approval_and_repeats_checks_and_review(self):
        self.modify_data(lambda d: d["config"]["repositories"]["calculator"].update(merge="manual"))
        first = self.until(status="awaiting_approval")
        self.store.action(first["id"], "approve", {"sha": first["data"]["sha"]})
        self.advance_target()
        self.engine.run_one()
        changed = self.current()
        self.assertEqual(changed["stage"], "refresh")
        self.assertNotIn("approval_sha", changed["data"])
        self.assertNotIn("verification", changed["data"]["artifacts"])
        second = self.until(status="awaiting_approval")
        self.assertNotEqual(first["data"]["sha"], second["data"]["sha"])
        self.assertNotEqual(first["data"]["base"], second["data"]["base"])
        self.assertEqual(second["data"]["model_calls"], first["data"]["model_calls"] + 1)
        self.store.action(second["id"], "approve", {"sha": second["data"]["sha"]})
        self.until(status="completed")

    def test_restarts_between_stages_keep_progress(self):
        first = self.until(stage="verify")
        self.engine = Engine(Store(self.root / "state.sqlite3"), self.root / "workspaces")
        task = self.until(status="completed")
        self.assertEqual(task["data"]["base"], first["data"]["base"])
        self.assertEqual(task["data"]["model_calls"], 5)

    def test_expired_worker_lease_is_recovered(self):
        claimed = self.store.claim("dead-worker")
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (claimed["id"],))
        self.until(status="completed")
        self.assertIn("recovered", [event["kind"] for event in self.store.events(claimed["id"])])

    def test_uncertain_merge_result_is_reconciled_without_second_push(self):
        self.until(stage="merge")
        original = GitRepo.merge

        def uncertain(repo, base, sha):
            original(repo, base, sha)
            raise RuntimeError("Connection lost after successful merge")

        with patch.object(GitRepo, "merge", uncertain):
            self.engine.run_one()
        task = self.current()
        self.assertEqual(task["status"], "failed")
        self.assertIn("merge_intent", task["data"])
        with self.assertRaises(Conflict):
            self.store.action(task["id"], "cancel", {})
        self.store.action(task["id"], "resume", {})
        self.modify_data(lambda d: d.update(active_seconds=100000))
        with patch.object(GitRepo, "merge", side_effect=AssertionError("Duplicate merge")):
            self.until(status="completed")

    def test_oversized_diff_cannot_reach_review(self):
        self.repo.prepare()
        base = self.repo.head()
        paths = self.repo.apply([{"path": "calculator.py", "content": "# oversized\n" * 6000}])
        sha = self.repo.commit(paths, "Oversized change")
        with self.assertRaisesRegex(RuntimeError, "64 KB review limit"):
            self.repo.diff(base, sha)

    def test_independent_review_can_force_another_iteration(self):
        class ReviewOnce(Providers):
            rejected = False

            def invoke(inner, config, stage, context, timeout=None):
                result = super(ReviewOnce, inner).invoke(config, stage, context, timeout)
                if stage == "review" and not inner.rejected:
                    inner.rejected = True
                    result["output"] = {"verdict": "approve", "findings": [
                        {"severity": "blocking", "message": "A blocking finding overrides approval"}]}
                return result

        self.engine.providers = ReviewOnce()
        task = self.until(status="completed")
        self.assertEqual(task["data"]["repairs"], 2)
        self.assertEqual(task["data"]["model_calls"], 7)

    def test_scope_waits_for_answer_then_resumes(self):
        class AskOnce(Providers):
            def invoke(inner, config, stage, context, timeout=None):
                result = super(AskOnce, inner).invoke(config, stage, context, timeout)
                if stage == "scope" and not context["answers"]:
                    result["output"]["questions"] = ["Should negative operands be supported?"]
                return result

        self.engine.providers = AskOnce()
        task = self.until(status="awaiting_input")
        self.store.action(task["id"], "answer", {"answer": "Yes, support negative operands."})
        self.until(status="completed")

    def test_cancelled_model_request_cannot_advance_or_merge(self):
        task_id, store = self.task["id"], self.store

        class Cancel(Providers):
            def invoke(inner, config, stage, context, timeout=None):
                store.action(task_id, "cancel", {})
                return super(Cancel, inner).invoke(config, stage, context, timeout)

        self.engine.providers = Cancel()
        self.engine.run_one()
        task = self.current()
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["stage"], "scope")
        self.assertEqual(self.repo.target(), task["data"]["base"])

    def test_duplicate_submission_reuses_task_and_rejects_key_collision(self):
        one = self.store.create(TASK, "calculator", self.config, "same-request")
        two = self.store.create(TASK, "calculator", self.config, "same-request")
        self.assertEqual(one["id"], two["id"])
        with self.assertRaises(Conflict):
            self.store.create("Different task", "calculator", self.config, "same-request")

    def test_concurrent_claims_have_one_owner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(self.store.claim, ["one", "two"]))
        self.assertEqual(sum(task is not None for task in claimed), 1)

    def test_entire_file_proposal_validated_before_any_write(self):
        self.repo.prepare()
        original = (self.repo.path / "calculator.py").read_text()
        for invalid in ("../escape.py", ".git/config", "C:/escape", "tests/test_calculator.py", "./calculator.py"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.repo.apply([{"path": "calculator.py", "content": "changed\n"},
                                 {"path": invalid, "content": "unsafe\n"}])
            self.assertEqual((self.repo.path / "calculator.py").read_text(), original)

    def test_verification_cannot_mutate_candidate(self):
        self.until(stage="verify")
        self.modify_data(lambda d: d["config"]["repositories"]["calculator"].update(checks=[{
            "name": "unit", "argv": ["{python}", "-c", "from pathlib import Path; Path('calculator.py').write_text('changed')"],
            "timeout_seconds": 10}]))
        self.engine.run_one()
        self.assertEqual(self.current()["status"], "failed")
        self.assertIn("changed tracked files", self.current()["data"]["error"])

    def test_credentials_not_inherited_by_checks(self):
        with patch.dict(os.environ, {"FACTORY_PRIVATE_SENTINEL": "must-not-leak"}):
            import sys
            result = command([sys.executable, "-c", "import os; print(os.getenv('FACTORY_PRIVATE_SENTINEL', 'absent'))"], self.root)
        self.assertEqual(result["output"].strip(), "absent")

    def test_http_service_worker_processes_queued_task(self):
        server = make_server("127.0.0.1", 0, self.store, self.config)
        stop = threading.Event()
        api_thread = threading.Thread(target=server.serve_forever, daemon=True)
        worker_thread = threading.Thread(target=self.engine.worker, args=(stop,), daemon=True)
        api_thread.start()
        worker_thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/tasks/{self.task['id']}"
            deadline = time.monotonic() + 45
            result = None
            while time.monotonic() < deadline:
                with urllib.request.urlopen(url, timeout=5) as response:
                    result = json.load(response)
                if result["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)
            self.assertEqual(result["status"], "completed", result["data"].get("error"))
        finally:
            stop.set()
            worker_thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            api_thread.join(timeout=2)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "headers": dict(self.headers), "body": payload})
                output = {"summary": "Implement addition", "criteria": [
                    {"id": "AC1", "description": "Adds two numbers", "check": "unit"}], "questions": []}
                if self.path == "/v1/messages":
                    result = {"content": [{"type": "text", "text": json.dumps(output)}], "usage": {"input_tokens": 10}}
                else:
                    result = {"choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}],
                              "usage": {"total_tokens": 20}}
                raw = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.context = {"checks": [{"name": "unit"}]}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_openai_compatible_adapter_wire_contract(self):
        with patch.dict(os.environ, {"TEST_MODEL_KEY": "test-placeholder"}):
            result = Providers().invoke({"kind": "openai-compatible", "base_url": self.base,
                "model": "chosen-by-user", "api_key_env": "TEST_MODEL_KEY"}, "scope", self.context)
        request = self.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-placeholder")
        self.assertEqual(request["body"]["model"], "chosen-by-user")
        self.assertEqual(result["usage"]["total_tokens"], 20)

    def test_anthropic_adapter_wire_contract(self):
        with patch.dict(os.environ, {"TEST_MODEL_KEY": "test-placeholder"}):
            result = Providers().invoke({"kind": "anthropic", "base_url": self.base,
                "model": "chosen-by-user", "api_key_env": "TEST_MODEL_KEY"}, "scope", self.context)
        self.assertEqual(self.requests[0]["path"], "/v1/messages")
        self.assertIn("system", self.requests[0]["body"])
        self.assertEqual(result["usage"]["input_tokens"], 10)

    def test_missing_credential_fails_before_network_request(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "environment variable"):
            Providers().invoke({"kind": "anthropic", "base_url": self.base,
                "model": "test", "api_key_env": "MISSING_KEY"}, "scope", self.context)
        self.assertEqual(self.requests, [])

    def test_unknown_acceptance_check_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "configured check"):
            validate_output("scope", {"summary": "x", "questions": [], "criteria": [
                {"id": "AC1", "description": "x", "check": "invented"}]}, self.context)

    def test_review_rejection_requires_actionable_finding(self):
        with self.assertRaisesRegex(ValueError, "actionable"):
            validate_output("review", {"verdict": "changes_requested", "findings": []}, {})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-http-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = validate({"providers": {"demo": {"kind": "scripted"}},
            "routes": {stage: "demo" for stage in ("scope", "plan", "build", "repair", "review")},
            "repositories": {"demo": {"source": str(root), "checks": [{"name": "unit", "argv": ["true"]}]}}})
        self.store = Store(root / "state.sqlite3")
        self.server = make_server("127.0.0.1", 0, self.store, self.config, "service-test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, authorized=True, key=None):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer service-test-token"
        if key:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_authentication_required(self):
        self.assertEqual(self.request("/api/tasks", authorized=False)[0], 401)

    def test_submit_inspect_events_and_cancel(self):
        status, task = self.request("/api/tasks", {"task": "Add a feature", "repository": "demo"}, key="once")
        self.assertEqual(status, 202)
        self.assertNotIn("config", task["data"])
        status, duplicate = self.request("/api/tasks", {"task": "Add a feature", "repository": "demo"}, key="once")
        self.assertEqual(task["id"], duplicate["id"])
        self.assertEqual(self.request("/api/tasks/" + task["id"])[0], 200)
        self.assertEqual(len(self.request("/api/tasks/" + task["id"] + "/events")[1]), 1)
        self.assertEqual(self.request("/api/tasks/" + task["id"] + "/cancel", {})[1]["status"], "cancelled")

    def test_invalid_payload_and_unknown_task(self):
        self.assertEqual(self.request("/api/tasks", {"task": "x", "repository": "missing"})[0], 400)
        self.assertEqual(self.request("/api/tasks", ["not", "an", "object"])[0], 400)
        self.assertEqual(self.request("/api/tasks/missing")[0], 404)

    def test_nonlocal_service_requires_token(self):
        with self.assertRaisesRegex(ValueError, "FACTORY_API_TOKEN"):
            make_server("0.0.0.0", 0, self.store, self.config)


if __name__ == "__main__":
    unittest.main()
