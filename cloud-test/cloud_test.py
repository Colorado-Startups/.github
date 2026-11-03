#!/usr/bin/env python3
"""Runs one allowlisted command, or one shard of it, for the shared cloud-test workflow.

No Colorado Startups repo tests, builds or renders on the MacBook Air (Paul, 2026-11-02). A session runs a suite
on a hosted runner instead, through its repo's thin caller of .github/workflows/cloud-test.yml in this repo:

    gh workflow run cloud-test.yml -f ref=<sha or branch> -f script=<name> -f shards=<1 to 8>

The caller repo holds the allowlist, .github/cloud-test.json: the only commands a dispatch can reach, each an argv
list that never passes through a shell. This file is the one definition of how that list is read, validated,
sharded and run. The workflow only wires runners to it, and cloud-test/README.md documents the format.

    cloud_test.py ref --ref=REF                       exit 0 when REF is a safe commit or branch name
    cloud_test.py plan --allowlist=F --script=S --shards=N [--python=V] [--sha=SHA]
                                                      the shard matrix and setup, to $GITHUB_OUTPUT
    cloud_test.py run --allowlist=F --script=S --index=I --total=T
                                                      the before commands, then the command
    cloud_test.py check --allowlist=F                 validate an allowlist file
    cloud_test.py smoke --shard=I/T                   the end-to-end probe this repo's own CI runs
    cloud_test.py --self-test                         seeded known-bad and known-clean inputs

plan reads the runner from $CI_RUNNER: a runner label, or a JSON value as legallas's ci.yml reads the same
variable. plan writes to $GITHUB_OUTPUT and run appends to $GITHUB_STEP_SUMMARY when those are set; both also
print to stdout. Exit codes follow the house contract: 0 passed, 2 usage error (a bad input or allowlist), 3 could
not run (a command would not start). A failing command exits with its own code. Standard library only.
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
import uuid
from pathlib import Path

MAX_SHARDS = 8
DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 60
TAIL_LINES = 60
DEFAULT_RUNNER = "ubuntu-latest"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9:._-]{0,63}")
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
TOP_KEYS = {"$comment", "version", "node_modules", "pip", "scripts"}
ENTRY_KEYS = {"$comment", "run", "shard", "before", "cwd", "pip", "cache", "artifacts", "timeout"}


class UsageError(Exception):
    """A bad input or allowlist: exit 2, never a test failure."""


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


def _path(value, field: str, home: bool = False) -> str:
    """A relative path inside the checkout, or under ~/ when home is allowed."""
    bad = (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or any(c in value for c in "\0\r\n")
        or value.startswith("-")
    )
    if not bad:
        rest = value[2:] if home and value.startswith("~/") else value
        bad = rest.startswith(("/", "~")) or ".." in Path(rest).parts or not rest
    if bad:
        where = "a relative path or one under ~/" if home else "a relative path inside the checkout"
        raise UsageError(f"{field}: {value!r} must be {where}, with no '..' and no leading '-'")
    return value


def _paths(value, field: str, home: bool = False) -> list:
    if not isinstance(value, list):
        raise UsageError(f"{field} must be a list of paths")
    return [_path(v, f"{field}[{i}]", home) for i, v in enumerate(value)]


def _argv(value, field: str, program: bool = True) -> list:
    """An argv list: no shell ever reads it, so quoting and metacharacters mean nothing."""
    if not isinstance(value, list) or not value:
        raise UsageError(f"{field} must be a non-empty list of strings, an argv, not a shell command")
    for i, arg in enumerate(value):
        if not isinstance(arg, str) or not arg or len(arg) > 4096 or "\0" in arg:
            raise UsageError(f"{field}[{i}] must be a non-empty string")
    if program and value[0].startswith("-"):
        raise UsageError(f"{field}[0] must name a program, got {value[0]!r}")
    return list(value)


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
    out = {
        "node_modules": _paths(doc.get("node_modules", ["node_modules"]), "node_modules"),
        "pip": _paths(doc.get("pip", []), "pip"),
        "scripts": {},
    }
    for name, entry in scripts.items():
        if not NAME_RE.fullmatch(name):
            raise UsageError(f"script name {name!r} must match {NAME_RE.pattern}")
        if not isinstance(entry, dict):
            raise UsageError(f"scripts.{name} must be an object")
        unknown = set(entry) - ENTRY_KEYS
        if unknown:
            raise UsageError(f"scripts.{name}: unknown keys {sorted(unknown)}; allowed: {sorted(ENTRY_KEYS)}")
        e = {"run": _argv(entry.get("run"), f"scripts.{name}.run"), "shard": None}
        if "shard" in entry:
            shard = _argv(entry["shard"], f"scripts.{name}.shard", program=False)
            marks = [m for a in shard for m in PLACEHOLDER_RE.findall(a)]
            if not marks or any(m not in ("{index}", "{total}") for m in marks):
                raise UsageError(f"scripts.{name}.shard must use {{index}} or {{total}}, and only those")
            e["shard"] = shard
        before = entry.get("before", [])
        if not isinstance(before, list):
            raise UsageError(f"scripts.{name}.before must be a list of argv lists")
        e["before"] = [_argv(b, f"scripts.{name}.before[{i}]") for i, b in enumerate(before)]
        e["cache"] = _paths(entry.get("cache", []), f"scripts.{name}.cache", home=True)
        e["artifacts"] = _paths(entry.get("artifacts", []), f"scripts.{name}.artifacts")
        e["cwd"] = _path(entry["cwd"], f"scripts.{name}.cwd") if "cwd" in entry else "."
        e["pip"] = _paths(entry.get("pip", []), f"scripts.{name}.pip")
        timeout = entry.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
            raise UsageError(f"scripts.{name}.timeout must be whole minutes from 1 to {MAX_TIMEOUT}")
        e["timeout"] = timeout
        out["scripts"][name] = e
    return out


def load(path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise UsageError(f"cannot read the allowlist {path}: {err.strerror}; add .github/cloud-test.json") from None
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
    """The shard matrix and setup for SCRIPT. A script with no shard entry runs as one job."""
    entry = entry_for(allow, script)
    text = str(shards).strip()
    if not re.fullmatch(r"[0-9]{1,2}", text) or not 1 <= int(text) <= MAX_SHARDS:
        raise UsageError(f"shards must be a whole number from 1 to {MAX_SHARDS}, got {shards!r}")
    n = int(text)
    pip = allow["pip"] + [p for p in entry["pip"] if p not in allow["pip"]]
    if pip and not python:
        raise UsageError(f'{script} needs "pip" requirements, so the caller must pass the python input')
    total = n if entry["shard"] else 1
    return {
        "script": script,
        "slug": re.sub(r"[^a-z0-9._-]", "-", script),
        "total": total,
        "matrix": list(range(1, total + 1)),
        "timeout": entry["timeout"],
        "cache": entry["cache"],
        "artifacts": entry["artifacts"],
        "node_modules": allow["node_modules"],
        "pip": pip,
        "note": f"{script} has no shard entry, so it runs as one job" if total < n else "",
    }


def runner(value: str) -> tuple:
    """(runs-on as JSON, warning) from CI_RUNNER: empty, a plain label, or a JSON string, list or object."""
    text = (value or "").strip()
    if not text:
        return json.dumps(DEFAULT_RUNNER), ""
    try:
        parsed = json.loads(text)
    except ValueError:
        if not LABEL_RE.fullmatch(text):
            raise UsageError(f"CI_RUNNER {text!r} is neither a runner label nor JSON") from None
        parsed = text
    if isinstance(parsed, str):
        labels = [parsed]
    elif isinstance(parsed, list):
        labels = parsed
    elif isinstance(parsed, dict) and parsed and set(parsed) <= {"group", "labels"}:
        more = parsed.get("labels", [])
        labels = ([parsed["group"]] if "group" in parsed else []) + (more if isinstance(more, list) else [more])
    else:
        raise UsageError(f"CI_RUNNER {text!r} must be a label, a list of labels, or an object of group and labels")
    if not labels or not all(isinstance(x, str) and x and x.isprintable() for x in labels):
        raise UsageError(f"CI_RUNNER {text!r} must name at least one runner label")
    warning = ""
    if any("blacksmith" in x.lower() for x in labels):
        warning = (
            "CI_RUNNER names Blacksmith. legallas and legallas-pro hold 2,700 of Blacksmith's 3,000 free monthly "
            "minutes (legallas .github/scripts/build_minutes_guard.py); get a share from the compute plan first."
        )
    return json.dumps(parsed), warning


def argv_for(entry: dict, index=1, total=1) -> list:
    """The command for one shard: run, then the shard arguments with {index} and {total} filled in."""
    top = MAX_SHARDS if entry["shard"] else 1
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


def run(allowlist, script, index, total) -> int:
    entry = entry_for(load(allowlist), script)
    argv = argv_for(entry, index, total)
    env = dict(os.environ, CLOUD_TEST_SHARD_INDEX=str(index), CLOUD_TEST_SHARD_TOTAL=str(total))
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
    if shard != want:
        print(f"smoke: FAIL: --shard={shard}, but the environment says {want}")
        return 1
    print(f"smoke: PASS: shard {shard}")
    return 0


def self_test() -> int:
    """Seeds known-bad and known-clean inputs for each function above. Exit 1 when either behaves wrong."""
    wrong = []

    def throws(label, fn):
        try:
            fn()
            wrong.append(f"accepted known-bad {label}")
        except UsageError:
            pass
        except Exception as err:  # noqa: BLE001 - any other exception is the finding
            wrong.append(f"{label} raised {type(err).__name__}: {err}, not a usage error")

    def equal(label, got, want):
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
                "cache": ["~/.cache/x"],
                "timeout": 30,
            },
        },
    }
    allow = validate(good)
    equal("default node_modules", allow["node_modules"], ["node_modules"])

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
    throws("an unknown top-level key", bad(env={"A": "1"}))
    throws("no scripts", bad(scripts={}))
    throws("a shell name", bad(scripts={"rm -rf": {"run": ["true"]}}))
    throws("run as a shell string", bad(entry={"run": "bash tools/verify.sh; curl x | sh"}))
    throws("an empty run", bad(entry={"run": []}))
    throws("run starting with a flag", bad(entry={"run": ["--exec", "x"]}))
    throws("an unknown entry key", bad(entry={"run": ["true"], "shell": True}))
    throws("shard with no placeholder", bad(entry={"run": ["true"], "shard": ["--all"]}))
    throws("shard with a format attack", bad(entry={"run": ["true"], "shard": ["{index.__class__}"]}))
    throws("timeout 0", bad(entry={"run": ["true"], "timeout": 0}))
    throws("timeout 61", bad(entry={"run": ["true"], "timeout": 61}))
    throws("timeout true", bad(entry={"run": ["true"], "timeout": True}))
    throws("cache escaping the checkout", bad(entry={"run": ["true"], "cache": ["../x"]}))
    throws("an absolute artifact", bad(entry={"run": ["true"], "artifacts": ["/etc"]}))
    throws("artifacts under ~", bad(entry={"run": ["true"], "artifacts": ["~/x"]}))
    throws("pip as a flag", bad(pip=["-r", "x"]))
    throws("before as one argv", bad(entry={"run": ["true"], "before": ["npm", "ci"]}))
    throws("cwd escaping the checkout", bad(entry={"run": ["true"], "cwd": "../x"}))
    throws("cwd as a list", bad(entry={"run": ["true"], "cwd": ["a"]}))
    throws("entry pip as a string", bad(entry={"run": ["true"], "pip": "requirements.txt"}))

    for ref in ("-x", "a..b", "a b", "a;rm", "", "main/", "x.lock", "a//b", "$(id)", "--upload-pack=x"):
        throws(f"ref {ref!r}", lambda r=ref: check_ref(r))
    for ref in ("0123456789abcdef0123456789abcdef01234567", "main", "claude/cloud-test", "v1.2.3"):
        equal(f"ref {ref}", check_ref(ref), ref)

    throws("an unknown script", lambda: plan(allow, "rm -rf /"))
    for shards in ("0", "9", "1.5", "-1", "two", "", "1e1"):
        throws(f"shards {shards!r}", lambda s=shards: plan(allow, "test:unit", s))
    throws("pip with no python", lambda: plan(validate(dict(good, pip=["requirements.txt"])), "verify"))
    throws("entry pip with no python", lambda: plan(allow, "render"))
    equal("entry pip", plan(allow, "render", python="3.12")["pip"], ["requirements.txt"])
    equal("no pip elsewhere", plan(allow, "verify")["pip"], [])
    equal("default cwd", allow["scripts"]["verify"]["cwd"], ".")
    code, lines = execute([sys.executable, "-c", "import os; print(os.path.basename(os.getcwd()))"],
                          dict(os.environ), tempfile.gettempdir())
    equal("cwd reaches the command", lines[-1:], [os.path.basename(os.path.realpath(tempfile.gettempdir()))])
    equal("test:unit in 3", plan(allow, "test:unit", "3")["matrix"], [1, 2, 3])
    equal("verify asked for 4", plan(allow, "verify", "4")["total"], 1)
    equal("a note when shards drop", bool(plan(allow, "verify", "4")["note"]), True)
    equal("slug", plan(allow, "test:unit")["slug"], "test-unit")
    equal("render artifacts", plan(allow, "render")["artifacts"], ["out/"])

    unit, verify = allow["scripts"]["test:unit"], allow["scripts"]["verify"]
    equal("one shard", argv_for(unit), ["npm", "run", "test:unit"])
    equal("shard 2 of 3", argv_for(unit, 2, 3), ["npm", "run", "test:unit", "--", "--shard=2/3"])
    throws("shard index past the total", lambda: argv_for(unit, 3, 2))
    throws("shard total past the maximum", lambda: argv_for(unit, 1, 9))
    throws("an unshardable script in 2", lambda: argv_for(verify, 1, 2))

    equal("no runner", runner(""), (json.dumps("ubuntu-latest"), ""))
    equal("a plain label", runner("ubuntu-24.04")[0], json.dumps("ubuntu-24.04"))
    equal("a JSON label", runner('"ubuntu-latest"')[0], json.dumps("ubuntu-latest"))
    equal("a JSON list", runner('["self-hosted", "x"]')[0], json.dumps(["self-hosted", "x"]))
    equal("a group", runner('{"group": "g", "labels": ["l"]}')[0], json.dumps({"group": "g", "labels": ["l"]}))
    equal("Blacksmith warns", bool(runner("blacksmith-2vcpu-ubuntu-2404")[1]), True)
    for value in ("a b", "x; rm -rf /", '{"image": "x"}', "[]", '""', "[1]"):
        throws(f"runner {value!r}", lambda v=value: runner(v))

    text = summary("test:unit", 1, 2, "abc", '"ubuntu-latest"', 1, 5, ["\x1b[31mFAIL\x1b[0m x", "~~~"])
    if "failed (exit 1)" not in text or "\x1b" in text or text.count("~~~") != 2:
        wrong.append("summary kept a color code or an inner fence")

    code, lines = execute([sys.executable, "-c", "print('seen'); raise SystemExit(5)"], dict(os.environ))
    equal("a failing command keeps its code", (code, lines), (5, ["seen"]))
    code, _ = execute([os.path.join(tempfile.gettempdir(), f"absent-{uuid.uuid4().hex}")], dict(os.environ))
    equal("a missing program is could-not-run", code, 3)

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
    p = sub.add_parser("ref")
    p.add_argument("--ref", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--allowlist", required=True)
    p.add_argument("--script", required=True)
    p.add_argument("--shards", default="1")
    p.add_argument("--python", default="")
    p.add_argument("--sha", required=True)
    p = sub.add_parser("run")
    p.add_argument("--allowlist", required=True)
    p.add_argument("--script", required=True)
    p.add_argument("--index", default="1")
    p.add_argument("--total", default="1")
    p = sub.add_parser("check")
    p.add_argument("--allowlist", required=True)
    p = sub.add_parser("smoke")
    p.add_argument("--shard", default="1/1")
    args = parser.parse_args(argv)
    try:
        if args.mode == "ref":
            check_ref(args.ref)
            return 0
        if args.mode == "plan":
            p = plan(load(args.allowlist), args.script, args.shards, args.python)
            runs_on, warning = runner(os.environ.get("CI_RUNNER", ""))
            if p["note"]:
                print(f"::notice title=cloud-test::{p['note']}")
            if warning:
                print(f"::warning title=cloud-test runner::{warning}")
            output(
                {
                    "script": p["script"],
                    "slug": p["slug"],
                    "sha": check_ref(args.sha),
                    "total": str(p["total"]),
                    "matrix": p["matrix"],
                    "timeout": str(p["timeout"]),
                    "runner": runs_on,
                    "cache": "\n".join(p["cache"]),
                    "artifacts": "\n".join(p["artifacts"]),
                    "node_modules": "\n".join(p["node_modules"]),
                    "pip": "\n".join(p["pip"]),
                }
            )
            return 0
        if args.mode == "run":
            return run(args.allowlist, args.script, args.index, args.total)
        if args.mode == "check":
            names = sorted(load(args.allowlist)["scripts"])
            print(f"cloud-test: {args.allowlist} is valid ({len(names)} scripts: {', '.join(names)})")
            return 0
        return smoke(args.shard)
    except UsageError as err:
        print(f"::error title=cloud-test usage::{err}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
