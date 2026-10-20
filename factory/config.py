"""Trusted operator configuration, never supplied by task text or model output."""

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit


STAGES = ("scope", "plan", "build", "repair", "review")


def validate(config):
    config = copy.deepcopy(config)
    providers = config.get("providers", {})
    if not isinstance(providers, dict) or not providers:
        raise ValueError("Configure at least one provider")
    for name, provider in providers.items():
        if provider.get("kind") not in ("scripted", "openai-compatible", "anthropic"):
            raise ValueError(f"Unknown provider kind: {name}")
        if provider["kind"] != "scripted":
            if not isinstance(provider.get("model"), str) or not provider["model"]:
                raise ValueError(f"Provider {name} needs an explicit model name")
            url = urlsplit(provider.get("base_url", ""))
            if (url.scheme not in ("https", "http") or not url.hostname or url.username
                    or url.password or url.query or url.fragment):
                raise ValueError(f"Provider {name} needs a base_url without embedded credentials")
            if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Use HTTPS for non-local model endpoints")
            if provider.get("token_parameter", "max_completion_tokens") not in ("max_tokens", "max_completion_tokens"):
                raise ValueError("token_parameter must be max_tokens or max_completion_tokens")
        for key in ("max_tokens", "timeout_seconds"):
            if key in provider and (type(provider[key]) is not int or not 1 <= provider[key] <= 100000):
                raise ValueError(f"Invalid provider setting: {key}")
    routes = config.get("routes", {})
    for stage in STAGES:
        if routes.get(stage) not in providers:
            raise ValueError(f"Configure a provider route for {stage}")
    repos = config.get("repositories", {})
    if not isinstance(repos, dict):
        raise ValueError("repositories must be an object")
    for name, repo in repos.items():
        if not isinstance(repo.get("source"), str) or not repo["source"] or repo["source"].startswith("-"):
            raise ValueError(f"Repository {name} needs a Git source")
        source = urlsplit(repo["source"])
        if source.username or source.password or source.query:
            raise ValueError("Use a Git credential helper, not credentials in a repository URL")
        branch = repo.get("branch", "main")
        if not isinstance(branch, str) or not branch or branch.startswith("-"):
            raise ValueError("Invalid target branch")
        repo["branch"] = branch
        repo.setdefault("merge", "manual")
        if repo["merge"] not in ("manual", "automatic"):
            raise ValueError("merge must be manual or automatic")
        checks = repo.get("checks", [])
        if not isinstance(checks, list) or not checks:
            raise ValueError(f"Repository {name} requires at least one check")
        names = set()
        for check in checks:
            argv = check.get("argv")
            if not isinstance(check.get("name"), str) or not check["name"] or check["name"] in names:
                raise ValueError("Check names must be nonempty and unique")
            names.add(check["name"])
            if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv):
                raise ValueError("Check argv must be a nonempty list of strings")
            check.setdefault("timeout_seconds", 120)
            if type(check["timeout_seconds"]) is not int or not 1 <= check["timeout_seconds"] <= 3600:
                raise ValueError("Invalid check timeout")
        for key in ("allowed_paths", "protected_paths", "context_exclude"):
            if key in repo and (not isinstance(repo[key], list) or any(not isinstance(p, str) for p in repo[key])):
                raise ValueError(f"{key} must be a list of glob patterns")
    budgets = config.setdefault("budgets", {})
    for key, default, maximum in (("repair_attempts", 3, 20), ("model_calls", 20, 1000), ("active_seconds", 1800, 86400)):
        budgets.setdefault(key, default)
        minimum = 0 if key == "repair_attempts" else 1
        if type(budgets[key]) is not int or not minimum <= budgets[key] <= maximum:
            raise ValueError(f"Invalid budget: {key}")
    return config


def load(path):
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))

