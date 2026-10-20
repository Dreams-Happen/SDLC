"""Git workspaces, bounded command execution, validated edits, and conditional merge."""

import fnmatch
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath


class TargetMoved(RuntimeError):
    pass


def command(argv, cwd, timeout=60, git=False):
    # Model credentials are never inherited by repository checks.
    allowed = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
               "HOME", "USERPROFILE", "SYSTEMDRIVE", "SSH_AUTH_SOCK")
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull)
    if not git:
        env.pop("SSH_AUTH_SOCK", None)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, **options)
        timed_out = False
        try:
            process.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.kill()
            process.wait(timeout=10)
        log.seek(0)
        raw = log.read(64001)
    output = raw[:64000].decode("utf-8", errors="replace")
    if len(raw) > 64000:
        output += "\n[log truncated at 64000 bytes]"
    if timed_out:
        output += "\n[command timed out]"
    return {"argv": argv, "exit_code": 124 if timed_out else process.returncode, "truncated": len(raw) > 64000,
            "output": output, "log_hash": hashlib.sha256(output.encode()).hexdigest()}


def private_path(path):
    parts = PurePosixPath(path).parts
    return any(part.lower().startswith(".env") or part.lower() in (
        ".git", ".ssh", ".aws", ".codex", ".agents", ".npmrc", ".pypirc")
        or part.lower().startswith(("secrets.", "credentials."))
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx")) for part in parts)


class GitRepo:
    def __init__(self, root, task_id, repo):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / task_id
        if self.path.parent != self.root or len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise ValueError("Invalid workspace identity")
        self.repo = repo
        self.hooks = self.root / "empty-hooks"
        self.hooks.mkdir(exist_ok=True)

    def git(self, *args, cwd=None, timeout=60, required=True):
        argv = ["git", "-c", f"core.hooksPath={self.hooks}", "-c", "core.autocrlf=false",
                "-c", "user.name=SDLC Factory", "-c", "user.email=factory@localhost", *args]
        result = command(argv, cwd or self.path, timeout, git=True)
        if required and result["exit_code"]:
            # Don't persist raw Git errors, which can contain credential-bearing URLs.
            raise RuntimeError(f"Git {args[0]} failed (exit {result['exit_code']}); check repository access and state")
        return result["output"].strip() if required else result

    def prepare(self):
        self.git("check-ref-format", "--branch", self.repo["branch"], cwd=self.root)
        if not self.path.exists():
            staging = self.root / ("clone-" + uuid.uuid4().hex)
            # Rename makes an interrupted clone distinguishable from a usable workspace.
            self.git("clone", "--no-local", "--single-branch", "--branch", self.repo["branch"],
                     "--", self.repo["source"], str(staging), cwd=self.root, timeout=120)
            staging.rename(self.path)
        if not (self.path / ".git").is_dir():
            raise RuntimeError("Workspace is not a valid Git clone")
        self.git("config", "core.hooksPath", str(self.hooks))
        return self.head()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def clean(self, expected):
        return self.head() == expected and not self.git("status", "--porcelain", "--untracked-files=no")

    def snapshot(self):
        names = self.git("ls-files", "-z").split("\0")
        files, omitted, remaining = {}, [], 120000
        for name in names:
            if not name or private_path(name) or any(fnmatch.fnmatchcase(name, p) for p in self.repo.get("context_exclude", [])):
                continue
            path = self.path / name
            if path.is_symlink() or not path.resolve().is_relative_to(self.path) or not path.is_file():
                continue
            if path.stat().st_size > 24000 or remaining < path.stat().st_size:
                omitted.append(name)
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                omitted.append(name)
                continue
            files[name] = content
            remaining -= len(content.encode())
        return {"files": files, "omitted": omitted,
                "note": "Tracked text files only; omitted files cannot be inspected by this MVP"}

    def apply(self, files):
        prepared, seen, size = [], set(), 0
        for edit in files:
            name, content = edit["path"], edit["content"]
            relative = PurePosixPath(name)
            if (not name or relative.is_absolute() or ".." in relative.parts or "\\" in name
                    or ":" in name or name.startswith("-") or str(relative) != name
                    or any(ord(char) < 32 for char in name) or private_path(name)):
                raise ValueError("Unsafe or private file path in model output")
            if name in seen:
                raise ValueError("Duplicate file edits")
            seen.add(name)
            if any(fnmatch.fnmatchcase(name, p) for p in [".github/*", ".gitmodules", ".gitattributes",
                    "factory.local.json", *self.repo.get("protected_paths", [])]):
                raise ValueError(f"Model attempted to edit protected path: {name}")
            allowed = self.repo.get("allowed_paths")
            if allowed is not None and not any(fnmatch.fnmatchcase(name, p) for p in allowed):
                raise ValueError(f"File outside configured allowed_paths: {name}")
            path = self.path / name
            if not path.resolve().is_relative_to(self.path) or any(p.is_symlink() for p in [path, *path.parents] if p != self.path.parent):
                raise ValueError("Symlink file edits are not supported")
            if path.is_dir():
                raise ValueError("File edit points to a directory")
            size += len((content or "").encode())
            if size > 500000:
                raise ValueError("File edits exceed 500 KB")
            prepared.append((path, content))
        # Validate the entire change before writing any file.
        for path, content in prepared:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
        return sorted(seen)

    def commit(self, paths, message):
        self.git("add", "--all", "--", *paths)
        changed = self.git("diff", "--cached", "--quiet", required=False)["exit_code"]
        if changed == 1:
            self.git("commit", "-m", message)
        elif changed != 0:
            raise RuntimeError("Cannot inspect staged changes")
        return self.head()

    def diff(self, base, sha):
        result = self.git("diff", "--no-ext-diff", "--no-textconv", "--find-renames", base, sha, "--", required=False)
        if result["exit_code"]:
            raise RuntimeError("Cannot inspect candidate diff")
        if result["truncated"]:
            raise RuntimeError("Candidate diff exceeds the 64 KB review limit; split the task")
        return result["output"]

    def checks(self, expected, remaining_seconds):
        import time
        results = []
        deadline = time.monotonic() + remaining_seconds
        for check in self.repo["checks"]:
            argv = [sys.executable if arg == "{python}" else arg for arg in check["argv"]]
            result = command(argv, self.path, min(check["timeout_seconds"], max(0.1, deadline - time.monotonic())))
            result.update(name=check["name"], sha=expected)
            results.append(result)
            if time.monotonic() >= deadline:
                raise RuntimeError("Active-time budget exhausted during verification")
        if not self.clean(expected):
            raise RuntimeError("Verification changed tracked files or HEAD; candidate evidence is invalid")
        return results

    def target(self):
        output = self.git("ls-remote", "--refs", "--", self.repo["source"], "refs/heads/" + self.repo["branch"])
        if not output:
            raise RuntimeError("Target branch no longer exists")
        return output.split()[0]

    def refresh(self, base):
        self.git("fetch", "--no-tags", "--", self.repo["source"], "refs/heads/" + self.repo["branch"])
        target = self.git("rev-parse", "FETCH_HEAD")
        result = self.git("rebase", "--onto", target, base, required=False, timeout=120)
        if result["exit_code"]:
            self.git("rebase", "--abort", required=False)
            raise RuntimeError("Target branch changed and rebase conflicted; resolve the task workspace before resuming")
        return target, self.head()

    def contains(self, sha):
        self.git("fetch", "--no-tags", "--", self.repo["source"], "refs/heads/" + self.repo["branch"])
        return self.git("merge-base", "--is-ancestor", sha, "FETCH_HEAD", required=False)["exit_code"] == 0

    def merge(self, base, sha):
        if not self.clean(sha):
            raise RuntimeError("Candidate changed after verification")
        self.git("merge-base", "--is-ancestor", base, sha)
        if self.target() != base:
            raise TargetMoved("Target changed; verification and review must be repeated")
        ref = "refs/heads/" + self.repo["branch"]
        # Fast-forward ancestry was checked above; the lease adds atomic expected-base protection.
        result = self.git("push", "--porcelain", f"--force-with-lease={ref}:{base}",
                          "--", self.repo["source"], f"{sha}:{ref}", required=False, timeout=120)
        if result["exit_code"] and self.target() != sha:
            if self.target() != base:
                raise TargetMoved("Target changed during conditional merge")
            raise RuntimeError("Conditional Git merge failed; check branch rules and access")
        if not self.contains(sha):
            raise RuntimeError("Merge was not confirmed by the target repository")
        return sha
