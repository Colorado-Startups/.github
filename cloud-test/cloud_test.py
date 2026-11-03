#!/usr/bin/env python3
"""Runs one allowlisted command, or one shard of it, for the shared cloud-test workflow.

Cloud first, the Mac last and small (Paul, 2026-11-02): test suites, builds, browsers and renders run on hosted
runners, and the MacBook Air runs only rare, seconds-long checks. A session runs a suite through its repo's thin
caller of .github/workflows/cloud-test.yml in this repo:

    gh workflow run cloud-test.yml --ref <branch> -f script=<name> -f shards=<1 to 8>

The caller repo holds the allowlist, .github/cloud-test.json. The plan job reads it from the caller's default
branch, or on a pull request from the merge commit the caller workflow itself came from; never from a dispatched
`ref` input. It hands the one validated entry to the shards as JSON through env, so no shard reads an allowlist
file from the code under test. Each command is an argv list that no shell reads. This file is the one definition
of how the list is validated, sharded and run; the workflow only wires runners to it, and cloud-test/README.md
documents the format.

    cloud_test.py ref --ref=REF                   exit 0 when REF is a safe commit or branch name
    cloud_test.py resolve --ref=REF               print the commit SHA that REF names now (GitHub API)
    cloud_test.py plan --allowlist=F --script=S --shards=N --sha=SHA [--python=V]
                                                  the matrix, the entry and the setup, to $GITHUB_OUTPUT
    cloud_test.py run --script=S --index=I --total=T
                                                  the entry in $CLOUD_TEST_ENTRY: its before commands, then it
    cloud_test.py check --allowlist=F             validate an allowlist file
    cloud_test.py smoke [--shard=I/T]             the end-to-end probe this repo's own CI runs
    cloud_test.py --self-test                     seeded known-bad and known-clean inputs

plan reads the runner from $CI_RUNNER, a plain label. It accepts only standard GitHub-hosted Linux labels, which
the plan's included minutes cover, and Blacksmith labels with a warning; it refuses larger and self-hosted runners.
plan writes to $GITHUB_OUTPUT and run appends to $GITHUB_STEP_SUMMARY when those are set; both also print to
stdout. Exit codes follow the house contract: 0 passed, 2 usage error (a bad input or allowlist), 3 could not run
(a command would not start, or the API could not be read). A failing command exits with its own code. Standard
library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_SHARDS = 8
DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 60
TAIL_LINES = 60
DEFAULT_RUNNER = "ubuntu-latest"
# Standard GitHub-hosted x64 Linux runners: the Enterprise plan's included minutes cover them. Larger runners bill
# per minute from the first one, so they are not here.
STANDARD_RUNNERS = {"ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-slim"}

NAME_RE = re.compile(r"[a-z0-9][a-z0-9:._-]{0,63}")
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
BLACKSMITH_RE = re.compile(r"blacksmith-[a-z0-9-]{1,60}")
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
TOP_KEYS = {"$comment", "version", "pip", "scripts"}
ENTRY_KEYS = {"$comment", "run", "shard", "before", "cwd", "pip", "artifacts", "timeout"}


class UsageError(Exception):
    """A bad input or allowlist: exit 2, never a test failure."""


class InfraError(Exception):
    """Could not check, such as an unreachable API: exit 3, never a test failure."""


def check_ref(ref: str) -> str:
    """REF when it is a commit SHA or a branch or tag name that git and actions/checkout read only as a ref."""
    if (
        not isinstance(ref, str)
        or not REF_RE.fullmatch(ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", ".", ".lock"))
    ):
        raise UsageError(f"ref must be a commit SHA or a branch or tag name, got {ref!r}")
    return ref


def _path(value, field: str) -> str:
    """A relative path inside the checkout."""
    bad = (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or any(c in value for c in "\0\r\n")
        or value.startswith(("-", "/", "~"))
    )
    if bad or ".." in Path(value).parts:
        raise UsageError(f"{field}: {value!r} must be a relative path inside the checkout, with no '..'")
    return value


def _paths(value, field: str) -> list:
    if not isinstance(value, list):
        raise UsageError(f"{field} must be a list of paths")
    return [_path(v, f"{field}[{i}]") for i, v in enumerate(value)]


def _argv(value, field: str, program: bool = True) -> list:
    """An argv list: no shell ever reads it, so quoting and metacharacters mean nothing."""
    if not isinstance(value, list) or not value:
        raise UsageError(f"{field} must be a non-empty list of strings, an argv, not a shell command")
    for i, arg in enumerate(value):
        if not isinstance(arg, str) or not arg or len(arg) > 4096 or any(c in arg for c in "\0\r\n"):
            raise UsageError(f"{field}[{i}] must be a non-empty string with no line break")
    if program and value[0].startswith("-"):
        raise UsageError(f"{field}[0] must name a program, got {value[0]!r}")
    return list(value)


def validate_entry(name: str, entry) -> dict:
    """One script's entry with defaults filled in, or a UsageError naming the first problem."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise UsageError(f"script name {name!r} must match {NAME_RE.pattern}")
    if not isinstance(entry, dict):
        raise UsageError(f"scripts.{name} must be an object")
    unknown = set(entry) - ENTRY_KEYS
    if unknown:
        raise UsageError(f"scripts.{name}: unknown keys {sorted(unknown)}; allowed: {sorted(ENTRY_KEYS)}")
    e = {"run": _argv(entry.get("run"), f"scripts.{name}.run")}
    if entry.get("shard") is not None:
        shard = _argv(entry["shard"], f"scripts.{name}.shard", program=False)
        marks = [m for a in shard for m in PLACEHOLDER_RE.findall(a)]
        if not marks or any(m not in ("{index}", "{total}") for m in marks):
            raise UsageError(f"scripts.{name}.shard must use {{index}} or {{total}}, and only those")
        e["shard"] = shard
    before = entry.get("before", [])
    if not isinstance(before, list):
        raise UsageError(f"scripts.{name}.before must be a list of argv lists")
    e["before"] = [_argv(b, f"scripts.{name}.before[{i}]") for i, b in enumerate(before)]
    e["cwd"] = _path(entry.get("cwd", "."), f"scripts.{name}.cwd")
    e["pip"] = _paths(entry.get("pip", []), f"scripts.{name}.pip")
    e["artifacts"] = _paths(entry.get("artifacts", []), f"scripts.{name}.artifacts")
    timeout = entry.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        raise UsageError(f"scripts.{name}.timeout must be whole minutes from 1 to {MAX_TIMEOUT}")
    e["timeout"] = timeout
    return e


def validate(doc) -> dict:
    """The allowlist with defaults filled in, or a UsageError naming the first problem."""
    if not isinstance(doc, dict):
        raise UsageError("the allowlist must be a JSON object")
    unknown = set(doc) - TOP_KEYS
    if unknown:
        raise UsageError(f"unknown top-level keys {sorted(unknown)}; allowed: {sorted(TOP_KEYS)}")
    if doc.get("version") != 1 or isinstance(doc.get("version"), bool):
        raise UsageError('"version" must be 1')
    scripts = doc.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        raise UsageError('"scripts" must be an object with at least one entry')
    return {
        "pip": _paths(doc.get("pip", []), "pip"),
        "scripts": {name: validate_entry(name, entry) for name, entry in scripts.items()},
    }


def load(path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise UsageError(
            f"cannot read the allowlist {path}: {err.strerror}. It is read from the default branch, so a new"
            " .github/cloud-test.json works by dispatch only after it merges; a pull request uses its own"
        ) from None
    try:
        doc = json.loads(text)
    except ValueError as err:
        raise UsageError(f"{path} is not valid JSON: {err}") from None
    return validate(doc)


def entry_for(allow: dict, script: str) -> dict:
    entry = allow["scripts"].get(script) if isinstance(script, str) else None
    if entry is None:
        raise UsageError(f"unknown script {script!r}; allowed: {', '.join(sorted(allow['scripts']))}")
    return entry


def plan(allow: dict, script: str, shards="1", python: str = "") -> dict:
    """The shard matrix, the entry and the setup for SCRIPT. A script with no shard entry runs as one job."""
    entry = entry_for(allow, script)
    text = str(shards).strip()
    if not re.fullmatch(r"[0-9]{1,2}", text) or not 1 <= int(text) <= MAX_SHARDS:
        raise UsageError(f"shards must be a whole number from 1 to {MAX_SHARDS}, got {shards!r}")
    n = int(text)
    pip = allow["pip"] + [p for p in entry["pip"] if p not in allow["pip"]]
    if pip and not python:
        raise UsageError(f'{script} needs "pip" requirements, so the caller must pass the python input')
    total = n if "shard" in entry else 1
    return {
        "script": script,
        "slug": re.sub(r"[^a-z0-9._-]", "-", script),
        "total": total,
        "matrix": list(range(1, total + 1)),
        "timeout": entry["timeout"],
        "entry": json.dumps(entry, separators=(",", ":")),
        "artifacts": entry["artifacts"],
        "pip": pip,
        "note": f"{script} has no shard entry, so it runs as one job" if total < n else "",
    }


def entry_from_env(script: str, environ) -> dict:
    """The entry the plan job validated and passed through env. A shard never reads an allowlist file."""
    text = environ.get("CLOUD_TEST_ENTRY", "")
    if not text:
        raise UsageError("CLOUD_TEST_ENTRY is empty; the plan job passes the validated entry there")
    try:
        doc = json.loads(text)
    except ValueError as err:
        raise UsageError(f"CLOUD_TEST_ENTRY is not valid JSON: {err}") from None
    return validate_entry(script, doc)


def runner(value: str) -> tuple:
    """(runs-on as JSON, warning) from CI_RUNNER, a plain label. Only labels the included minutes cover pass."""
    text = (value or "").strip()
    if not text:
        return json.dumps(DEFAULT_RUNNER), ""
    if text in STANDARD_RUNNERS:
        return json.dumps(text), ""
    if BLACKSMITH_RE.fullmatch(text):
        return json.dumps(text), (
            "CI_RUNNER names Blacksmith. Run there only on its free tier after Paul's yes, and only while the"
            " account stops at the free limit instead of billing (house rule ~/.claude/rules/compute.md)."
        )
    raise UsageError(
        f"CI_RUNNER {text!r} is not a plain label for a standard GitHub-hosted Linux runner"
        f" ({', '.join(sorted(STANDARD_RUNNERS))}) or a blacksmith-* label. Larger runners bill per minute and"
        " self-hosted runners or groups run on machines this org does not want running CI, so both are refused."
    )


def check_pin(workflow_ref: str, workflow_repo: str, caller_repo: str) -> str:
    """A warning, or "" when the caller pinned this workflow safely. A cross-repo caller must pin a full SHA.

    A branch or tag pin (job.workflow_ref ends in @refs/heads/... or @refs/tags/...) would let a later push to this
    repo change what every caller runs, so it is refused. A call from this repo itself always passes. A ref in a
    form GitHub has not documented only warns, so a format change can never stop every caller at once.
    """
    if not workflow_repo or not caller_repo or workflow_repo.lower() == caller_repo.lower():
        return ""
    ref = workflow_ref.rsplit("@", 1)[1] if "@" in (workflow_ref or "") else ""
    if SHA_RE.fullmatch(ref):
        return ""
    if ref.startswith(("refs/heads/", "refs/tags/")):
        raise UsageError(
            f"{caller_repo} calls the shared workflow at {ref}; pin `uses:` to a full commit SHA of"
            f" {workflow_repo} main, so a later push here cannot change what runs there"
        )
    return f"could not read the pin from job.workflow_ref {workflow_ref!r}; pin `uses:` to a full commit SHA"


def resolve(ref: str, environ) -> str:
    """The commit SHA REF names now, so every shard tests one commit even if the branch moves."""
    check_ref(ref)
    if SHA_RE.fullmatch(ref):
        return ref
    api = environ.get("GITHUB_API_URL", "https://api.github.com")
    repo, token = environ.get("GITHUB_REPOSITORY", ""), environ.get("GH_TOKEN", "")
    if not repo or not token:
        raise InfraError("GITHUB_REPOSITORY and GH_TOKEN must be set to resolve a branch name")
    url = f"{api}/repos/{repo}/commits/{urllib.parse.quote(ref, safe='/')}"
    headers = {"Accept": "application/vnd.github.sha", "Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            sha = resp.read().decode("ascii", "replace").strip()
    except urllib.error.HTTPError as err:
        raise UsageError(f"ref {ref!r} names no commit in {repo} (HTTP {err.code})") from None
    except (urllib.error.URLError, OSError) as err:
        raise InfraError(f"could not reach the GitHub API to resolve {ref!r}: {err}") from None
    if not SHA_RE.fullmatch(sha):
        raise InfraError(f"the GitHub API answered {sha[:60]!r}, not a commit SHA, for {ref!r}")
    return sha


def argv_for(entry: dict, index=1, total=1) -> list:
    """The command for one shard: run, then the shard arguments with {index} and {total} filled in."""
    top = MAX_SHARDS if "shard" in entry else 1
    try:
        i, t = int(str(index)), int(str(total))
    except ValueError:
        raise UsageError(f"index and total must be whole numbers, got {index!r} and {total!r}") from None
    if not 1 <= t <= top or not 1 <= i <= t:
        raise UsageError(f"shard {index}/{total} is out of range; this script allows up to {top}")
    if t == 1:
        return list(entry["run"])
    # str.replace, never str.format: a format string could reach attributes of its arguments.
    return list(entry["run"]) + [a.replace("{index}", str(i)).replace("{total}", str(t)) for a in entry["shard"]]


def summary(script, index, total, ref, runs_on, code, seconds, lines) -> str:
    """A markdown job summary: the result, then the output's last lines."""
    shard = f" shard {index}/{total}" if total > 1 else ""
    clean = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).replace("~~~", "~ ~ ~") for line in lines]
    return "\n".join(
        [
            f"### cloud-test: `{script}`{shard} {'passed' if code == 0 else f'failed (exit {code})'}",
            "",
            "| Ref | Runner | Exit | Seconds |",
            "| --- | --- | --- | --- |",
            f"| `{ref}` | `{runs_on}` | {code} | {seconds} |",
            "",
            f"<details><summary>Last {len(clean)} lines</summary>",
            "",
            "~~~",
            *clean,
            "~~~",
            "",
            "</details>",
            "",
        ]
    )


def output(pairs: dict) -> None:
    lines = []
    for key, value in pairs.items():
        text = value if isinstance(value, str) else json.dumps(value)
        if "\n" in text:
            mark = f"EOF_{uuid.uuid4().hex}"
            lines += [f"{key}<<{mark}", text, mark]
        else:
            lines.append(f"{key}={text}")
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(text)


def execute(argv: list, env: dict, cwd: str = ".") -> tuple:
    """(exit code, last lines) of one command, streaming its output. A command that will not start is exit 3."""
    tail: list = []
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd)
    except OSError as err:
        return 3, [f"could not start {argv[0]} in {cwd}: {err.strerror}"]
    assert proc.stdout is not None
    for raw in proc.stdout:
        sys.stdout.buffer.write(raw)
        sys.stdout.flush()
        tail.append(raw.decode("utf-8", "replace").rstrip("\n"))
        del tail[:-TAIL_LINES]
    code = proc.wait()
    return (code if code >= 0 else 128 - code), tail


def run(script, index, total) -> int:
    entry = entry_from_env(script, os.environ)
    argv = argv_for(entry, index, total)
    env = dict(os.environ, CLOUD_TEST_SHARD_INDEX=str(index), CLOUD_TEST_SHARD_TOTAL=str(total))
    env.pop("CLOUD_TEST_ENTRY", None)
    start = time.monotonic()
    code, lines = 0, []
    for before in entry["before"] + [argv]:
        print(f"::group::{' '.join(before)}" if before is not argv else f"+ {' '.join(before)}", flush=True)
        code, lines = execute(before, env, entry["cwd"])
        if before is not argv:
            print("::endgroup::", flush=True)
        if code != 0:
            break
    text = summary(
        script,
        int(index),
        int(total),
        os.environ.get("CLOUD_TEST_REF") or os.environ.get("GITHUB_SHA") or "local",
        os.environ.get("CLOUD_TEST_RUNNER") or "local",
        code,
        round(time.monotonic() - start),
        lines,
    )
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    return code


def smoke(shard: str) -> int:
    """The probe this repo's own CI runs through the shared workflow: the shard flag must match the environment."""
    want = f"{os.environ.get('CLOUD_TEST_SHARD_INDEX')}/{os.environ.get('CLOUD_TEST_SHARD_TOTAL')}"
    for tool in ("python3", "node"):
        where = shutil.which(tool)
        version = subprocess.run([where, "--version"], capture_output=True, text=True).stdout.strip() if where else ""
        print(f"smoke: {tool} {version or 'not on PATH'}")
    if os.environ.get("CLOUD_TEST_ENTRY"):
        print("smoke: FAIL: CLOUD_TEST_ENTRY reached the command's environment")
        return 1
    if shard != want:
        print(f"smoke: FAIL: --shard={shard}, but the environment says {want}")
        return 1
    print(f"smoke: PASS: shard {shard}")
    return 0


def self_test() -> int:
    """Seeds known-bad and known-clean inputs for each function above. Exit 1 when either behaves wrong."""
    wrong = []

    def throws(label, fn, kind=UsageError):
        try:
            fn()
            wrong.append(f"accepted known-bad {label}")
        except kind:
            pass
        except Exception as err:  # noqa: BLE001 - any other exception is the finding
            wrong.append(f"{label} raised {type(err).__name__}: {err}, not a {kind.__name__}")

    def equal(label, fn, want):
        try:
            got = fn()
        except Exception as err:  # noqa: BLE001 - an exception on a known-clean input is the finding
            wrong.append(f"{label} raised {type(err).__name__}: {err}")
            return
        if got != want:
            wrong.append(f"{label}: got {got!r}, want {want!r}")

    good = {
        "version": 1,
        "scripts": {
            "verify": {"run": ["bash", "tools/verify.sh"]},
            "test:unit": {"run": ["npm", "run", "test:unit"], "shard": ["--", "--shard={index}/{total}"]},
            "render": {
                "run": ["python3", "build.py"],
                "cwd": "deliverables",
                "pip": ["requirements.txt"],
                "artifacts": ["out/"],
                "timeout": 30,
            },
        },
    }
    allow = validate(good)

    def bad(**change):
        doc = json.loads(json.dumps(good))
        for key, value in change.items():
            if key == "entry":
                doc["scripts"]["verify"] = value
            else:
                doc[key] = value
        return lambda: validate(doc)

    throws("a list, not an object", lambda: validate([]))
    throws("version 2", bad(version=2))
    throws("version true", bad(version=True))
    throws("an unknown top-level key", bad(env={"A": "1"}))
    throws("the retired node_modules key", bad(node_modules=["node_modules"]))
    throws("no scripts", bad(scripts={}))
    throws("a shell name", bad(scripts={"rm -rf": {"run": ["true"]}}))
    throws("run as a shell string", bad(entry={"run": "bash tools/verify.sh; curl x | sh"}))
    throws("an empty run", bad(entry={"run": []}))
    throws("run starting with a flag", bad(entry={"run": ["--exec", "x"]}))
    throws("an argument with a line feed", bad(entry={"run": ["echo", "a\nb"]}))
    throws("an argument with a carriage return", bad(entry={"run": ["echo", "a\rb"]}))
    throws("an unknown entry key", bad(entry={"run": ["true"], "shell": True}))
    throws("the retired cache key", bad(entry={"run": ["true"], "cache": ["x"]}))
    throws("shard with no placeholder", bad(entry={"run": ["true"], "shard": ["--all"]}))
    throws("shard with a format attack", bad(entry={"run": ["true"], "shard": ["{index.__class__}"]}))
    throws("timeout 0", bad(entry={"run": ["true"], "timeout": 0}))
    throws("timeout 61", bad(entry={"run": ["true"], "timeout": 61}))
    throws("timeout true", bad(entry={"run": ["true"], "timeout": True}))
    throws("an absolute artifact", bad(entry={"run": ["true"], "artifacts": ["/etc"]}))
    throws("an artifact under ~", bad(entry={"run": ["true"], "artifacts": ["~/x"]}))
    throws("an artifact escaping the checkout", bad(entry={"run": ["true"], "artifacts": ["a/../../x"]}))
    throws("pip as a flag", bad(pip=["-r", "x"]))
    throws("before as one argv", bad(entry={"run": ["true"], "before": ["npm", "ci"]}))
    throws("cwd escaping the checkout", bad(entry={"run": ["true"], "cwd": "../x"}))
    throws("cwd as a list", bad(entry={"run": ["true"], "cwd": ["a"]}))
    throws("entry pip as a string", bad(entry={"run": ["true"], "pip": "requirements.txt"}))

    for ref in ("-x", "a..b", "a b", "a;rm", "", "main/", "x.lock", "a//b", "$(id)", "--upload-pack=x", "a\nb"):
        throws(f"ref {ref!r}", lambda r=ref: check_ref(r))
    for ref in ("0123456789abcdef0123456789abcdef01234567", "main", "claude/cloud-test", "v1.2.3"):
        equal(f"ref {ref}", lambda r=ref: check_ref(r), ref)
    sha = "0123456789abcdef0123456789abcdef01234567"
    equal("a SHA resolves to itself, offline", lambda: resolve(sha, {}), sha)
    throws("a branch with no API credentials", lambda: resolve("main", {}), InfraError)
    throws("resolving a bad ref", lambda: resolve("-x", {}))

    throws("an unknown script", lambda: plan(allow, "rm -rf /"))
    for shards in ("0", "9", "1.5", "-1", "two", "", "1e1"):
        throws(f"shards {shards!r}", lambda s=shards: plan(allow, "test:unit", s))
    throws("pip with no python", lambda: plan(validate(dict(good, pip=["requirements.txt"])), "verify"))
    throws("entry pip with no python", lambda: plan(allow, "render"))
    equal("entry pip", lambda: plan(allow, "render", python="3.12")["pip"], ["requirements.txt"])
    equal("no pip elsewhere", lambda: plan(allow, "verify")["pip"], [])
    equal("artifacts stay relative to the checkout root", lambda: plan(allow, "render", python="3")["artifacts"],
          ["out/"])
    equal("test:unit in 3", lambda: plan(allow, "test:unit", "3")["matrix"], [1, 2, 3])
    equal("verify asked for 4", lambda: plan(allow, "verify", "4")["total"], 1)
    equal("a note when shards drop", lambda: bool(plan(allow, "verify", "4")["note"]), True)
    equal("slug", lambda: plan(allow, "test:unit")["slug"], "test-unit")

    # The plan hands the shards one entry through env; a shard re-validates it and reads no file.
    passed = plan(allow, "test:unit", "2")["entry"]
    equal("the planned entry round-trips", lambda: entry_from_env("test:unit", {"CLOUD_TEST_ENTRY": passed}),
          allow["scripts"]["test:unit"])
    throws("a shard with no entry", lambda: entry_from_env("verify", {}))
    throws("a shard given a shell string", lambda: entry_from_env("verify", {"CLOUD_TEST_ENTRY": '"rm -rf /"'}))
    throws("a shard given an entry with a shell run",
           lambda: entry_from_env("verify", {"CLOUD_TEST_ENTRY": '{"run": "curl x | sh"}'}))
    throws("a shard given broken JSON", lambda: entry_from_env("verify", {"CLOUD_TEST_ENTRY": "{"}))

    unit, verify = allow["scripts"]["test:unit"], allow["scripts"]["verify"]
    equal("one shard", lambda: argv_for(unit), ["npm", "run", "test:unit"])
    equal("shard 2 of 3", lambda: argv_for(unit, 2, 3), ["npm", "run", "test:unit", "--", "--shard=2/3"])
    throws("shard index past the total", lambda: argv_for(unit, 3, 2))
    throws("shard total past the maximum", lambda: argv_for(unit, 1, 9))
    throws("an unshardable script in 2", lambda: argv_for(verify, 1, 2))

    wf = "Colorado-Startups/.github/.github/workflows/cloud-test.yml"
    org, caller = "Colorado-Startups/.github", "Colorado-Startups/brand"
    equal("a SHA pin from another repo", lambda: check_pin(f"{wf}@{sha}", org, caller), "")
    equal("a call from this repo itself", lambda: check_pin(f"{wf}@refs/pull/7/merge", org, org), "")
    equal("an undocumented pin form warns", lambda: bool(check_pin(f"{wf}@abc", org, caller)), True)
    throws("a branch pin from another repo", lambda: check_pin(f"{wf}@refs/heads/main", org, caller))
    throws("a tag pin from another repo", lambda: check_pin(f"{wf}@refs/tags/v1", org, caller))

    equal("no runner", lambda: runner(""), (json.dumps("ubuntu-latest"), ""))
    equal("a standard label", lambda: runner("ubuntu-24.04"), (json.dumps("ubuntu-24.04"), ""))
    equal("Blacksmith warns", lambda: bool(runner("blacksmith-2vcpu-ubuntu-2404")[1]), True)
    for value in ("self-hosted", '{"group": "g"}', '["ubuntu-latest"]', '"ubuntu-latest"', "ubuntu-latest-4-cores",
                  "macos-latest", "a b", "x; rm -rf /", "linux-large"):
        throws(f"runner {value!r}", lambda v=value: runner(v))

    text = summary("test:unit", 1, 2, "abc", '"ubuntu-latest"', 1, 5, ["\x1b[31mFAIL\x1b[0m x", "~~~"])
    if "failed (exit 1)" not in text or "\x1b" in text or text.count("~~~") != 2:
        wrong.append("summary kept a color code or an inner fence")

    equal("a failing command keeps its code",
          lambda: execute([sys.executable, "-c", "print('seen'); raise SystemExit(5)"], dict(os.environ)),
          (5, ["seen"]))
    equal("a missing program is could-not-run",
          lambda: execute([os.path.join(tempfile.gettempdir(), f"absent-{uuid.uuid4().hex}")], dict(os.environ))[0], 3)
    equal("cwd reaches the command",
          lambda: execute([sys.executable, "-c", "import os; print(os.path.basename(os.getcwd()))"],
                          dict(os.environ), tempfile.gettempdir())[1][-1:],
          [os.path.basename(os.path.realpath(tempfile.gettempdir()))])

    own = Path(__file__).resolve().parent.parent / ".github" / "cloud-test.json"
    if own.is_file():
        try:
            load(own)
        except UsageError as err:
            wrong.append(f"this repo's own allowlist is invalid: {err}")

    if wrong:
        for w in wrong:
            print(f"FAIL [cloud-test self-test]: {w}", file=sys.stderr)
        return 1
    print("cloud-test self-test: PASS")
    return 0


def main(argv: list) -> int:
    if argv == ["--self-test"]:
        return self_test()
    parser = argparse.ArgumentParser(prog="cloud_test.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("ref", "resolve"):
        sub.add_parser(mode).add_argument("--ref", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--allowlist", required=True)
    p.add_argument("--script", required=True)
    p.add_argument("--shards", default="1")
    p.add_argument("--python", default="")
    p.add_argument("--sha", required=True)
    p = sub.add_parser("run")
    p.add_argument("--script", required=True)
    p.add_argument("--index", default="1")
    p.add_argument("--total", default="1")
    sub.add_parser("check").add_argument("--allowlist", required=True)
    sub.add_parser("smoke").add_argument("--shard", default="1/1")
    args = parser.parse_args(argv)
    try:
        if args.mode == "ref":
            check_ref(args.ref)
            return 0
        if args.mode == "resolve":
            print(resolve(args.ref, os.environ))
            return 0
        if args.mode == "plan":
            env = os.environ
            pin = check_pin(env.get("CLOUD_TEST_WORKFLOW_REF", ""), env.get("CLOUD_TEST_WORKFLOW_REPO", ""),
                            env.get("GITHUB_REPOSITORY", ""))
            p = plan(load(args.allowlist), args.script, args.shards, args.python)
            runs_on, warning = runner(env.get("CI_RUNNER", ""))
            if p["note"]:
                print(f"::notice title=cloud-test::{p['note']}")
            if warning:
                print(f"::warning title=cloud-test runner::{warning}")
            if pin:
                print(f"::warning title=cloud-test pin::{pin}")
            output(
                {
                    "script": p["script"],
                    "slug": p["slug"],
                    "sha": check_ref(args.sha),
                    "total": str(p["total"]),
                    "matrix": p["matrix"],
                    "timeout": str(p["timeout"]),
                    "runner": runs_on,
                    "entry": p["entry"],
                    "artifacts": "\n".join(p["artifacts"]),
                    "pip": "\n".join(p["pip"]),
                }
            )
            return 0
        if args.mode == "run":
            return run(args.script, args.index, args.total)
        if args.mode == "check":
            names = sorted(load(args.allowlist)["scripts"])
            print(f"cloud-test: {args.allowlist} is valid ({len(names)} scripts: {', '.join(names)})")
            return 0
        return smoke(args.shard)
    except UsageError as err:
        print(f"::error title=cloud-test usage::{err}")
        return 2
    except InfraError as err:
        print(f"::warning title=cloud-test could not check::{err}")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
