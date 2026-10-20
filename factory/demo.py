"""Disposable, offline demonstration with real Git and real failing/passing tests."""

import json
from pathlib import Path

from .config import STAGES, validate
from .engine import Engine, public_task
from .gitrepo import command
from .store import Store


TASK = "Fix calculator.add so it adds two numbers correctly."


def initialize(directory):
    root = Path(directory).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Demo directory must be empty; use a new --directory")
    root.mkdir(parents=True, exist_ok=True)
    seed = root / "seed"
    seed.mkdir()
    hooks = root / "empty-hooks"
    hooks.mkdir()

    def git(*args, cwd=seed):
        result = command(["git", "-c", f"core.hooksPath={hooks}", "-c", "core.autocrlf=false",
                          "-c", "user.name=Factory Demo", "-c", "user.email=demo@localhost", *args], cwd, git=True)
        if result["exit_code"]:
            raise RuntimeError("Demo Git setup failed: " + result["output"])

    git("init", "-b", "main")
    (seed / "calculator.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (seed / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (seed / "tests").mkdir()
    (seed / "tests" / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n\n"
        "class AdditionTests(unittest.TestCase):\n"
        "    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n"
        "    def test_negative(self):\n        self.assertEqual(add(-4, -2), -6)\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 7), 7)\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Create calculator fixture")
    bare = root / "target.git"
    git("clone", "--bare", "--no-local", str(seed), str(bare), cwd=root)
    config = validate({"providers": {"demo": {"kind": "scripted"}},
        "routes": {stage: "demo" for stage in STAGES},
        "repositories": {"calculator": {"source": str(bare), "branch": "main", "merge": "automatic",
            "allowed_paths": ["calculator.py"], "protected_paths": ["tests/*", ".gitignore"],
            "checks": [{"name": "unit", "argv": ["{python}", "-m", "unittest", "discover", "-s", "tests"]}]}},
        "budgets": {"repair_attempts": 3, "model_calls": 20, "active_seconds": 1800}})
    (root / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def run(directory):
    root = Path(directory).resolve()
    config = initialize(root)
    store = Store(root / "state.sqlite3")
    engine = Engine(store, root / "workspaces")
    task = store.create(TASK, "calculator", config)
    for _ in range(30):
        task = store.get(task["id"])
        if task["status"] not in ("queued", "running"):
            break
        engine.run_one()
    task = store.get(task["id"])
    (root / "result.json").write_text(json.dumps(public_task(task), indent=2), encoding="utf-8")
    return task
