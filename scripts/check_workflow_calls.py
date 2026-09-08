#!/usr/bin/env python3
"""Check that a workflow's reusable-workflow calls can actually start.

Two ways a caller can break a call it looks correctly configured for. Both were
found in one repository (glm-review#9), both were introduced by a single commit
titled "harden(ci)", and between them they cost
`frankbria/narrative-modeling-app` 266 consecutive runs over two months during
which the check appeared configured and was never once executing.

1. UNDER-GRANTED PERMISSIONS. GitHub refuses to start a run whose caller job
   grants *less* than the called workflow's job declares, and it refuses before
   any job exists -- so the run is a `startup_failure` with no log and no
   annotation naming the missing scope.

   The trap is that the under-grant is invisible in the caller's own text.
   Listing a `permissions:` block at all sets every scope you *did not* list to
   `none`, so a block that reads as a tightening --

       permissions:
         contents: read
         pull-requests: write

   -- silently also says `issues: none`, and a callee whose job declares
   `issues: write` can no longer start. Nothing in the caller mentions `issues`.

   The two directions are not symmetric, which is why this check is one-sided:
   granting *more* than the callee declares is harmless (over-grants are notes,
   never failures), granting less is fatal.

2. COLLIDING CONCURRENCY GROUPS. A called reusable workflow joins its own
   `concurrency:` group while the caller is still holding it. If the groups can
   expand to the same string, the callee contends with its own parent: with
   `cancel-in-progress` it cancels the parent on queue (a real run ended in 2
   seconds having started no jobs at all), and without it the two deadlock until
   the job timeout.

   Comparing the group text is not enough, and assuming otherwise would miss the
   real case -- see `expand_candidates`.

Neither check can live inside the reusable workflow it protects: in both failing
cases that workflow never starts, so a guard job inside it would never run
either. This has to be driven from a separate workflow whose own permissions are
trivially satisfiable.

Usage:
    check_workflow_calls.py [options] CALLER_YAML [CALLER_YAML ...]

Options:
    --callee-root DIR   Resolve `owner/repo/...@ref` callees from this local
                        checkout instead of fetching them, ignoring the ref.
                        Use when the callee is this repository and you want the
                        working tree checked rather than what is already pushed.
    --no-network        Never fetch. Remote callees are reported as UNVERIFIED
                        rather than checked; the summary says so.
    --strict            Treat everything the checker could not decide -- a caller
                        with no explicit permissions, an unfetched callee, an
                        undecidable concurrency group -- as a failure.
    --quiet             Only print problems.

Exit status is 1 for a proven defect (under-grant, concurrency collision) or an
unreadable callee, or under --strict for anything undecided; 0 otherwise. A run
that could not verify every call never prints a clean bill of health: an
unchecked call reported as OK is how the outage above stayed invisible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared by callers
    sys.exit("check_workflow_calls: PyYAML is required (pip install pyyaml)")

# Every scope GitHub accepts in a `permissions:` block. The list matters: an
# explicit block sets each scope NOT named here-and-listed to `none`, so a scope
# missing from this list would be silently treated as ungranted and produce a
# false failure. Keep it in sync with
# https://docs.github.com/actions/reference/workflow-syntax-for-github-actions#permissions
ALL_SCOPES = frozenset(
    {
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    }
)

LEVELS = {"none": 0, "read": 1, "write": 2}
LEVEL_NAMES = {0: "none", 1: "read", 2: "write"}

# `uses:` at job level is always a reusable-workflow call (action `uses:` lives
# under `steps:`), so the shape is either a local path or owner/repo/path@ref.
REMOTE_USES = re.compile(
    r"^(?P<owner>[^/@]+)/(?P<repo>[^/@]+)/(?P<path>[^@]+\.ya?ml)@(?P<ref>.+)$"
)


class WorkflowError(Exception):
    """A workflow file could not be read or made sense of."""


@dataclass
class Finding:
    # under-grant | concurrency-collision | unreadable   -> always fatal
    # skipped | undetermined | concurrency-possible       -> fatal only under --strict
    # over-grant                                          -> never fatal
    kind: str
    caller: str
    job: str
    callee: str
    detail: str
    scopes: list[str] = field(default_factory=list)

    @property
    def fatal(self) -> bool:
        return self.kind in {"under-grant", "concurrency-collision", "unreadable"}


def load_workflow(text: str, origin: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{origin}: not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise WorkflowError(f"{origin}: top level is not a mapping")
    return doc


def normalize_permissions(node: object, origin: str) -> dict[str, int] | None:
    """Return scope -> level for an explicit block, or None if the key is absent.

    An explicit block is closed: every scope it does not name is `none`. That is
    the whole reason this check exists, so it is applied here rather than left to
    the caller of this function.
    """
    if node is None:
        return None
    if isinstance(node, str):
        if node == "read-all":
            return {scope: 1 for scope in ALL_SCOPES}
        if node == "write-all":
            return {scope: 2 for scope in ALL_SCOPES}
        # `permissions: none` is not documented but is accepted as all-none.
        if node == "none":
            return {scope: 0 for scope in ALL_SCOPES}
        raise WorkflowError(f"{origin}: unrecognized permissions shorthand {node!r}")
    if isinstance(node, dict):
        grants = {scope: 0 for scope in ALL_SCOPES}
        for scope, level in node.items():
            scope = str(scope)
            if scope not in ALL_SCOPES:
                raise WorkflowError(f"{origin}: unknown permission scope {scope!r}")
            level_name = str(level)
            if level_name not in LEVELS:
                raise WorkflowError(
                    f"{origin}: scope {scope!r} has level {level_name!r}, "
                    f"expected one of {sorted(LEVELS)}"
                )
            grants[scope] = LEVELS[level_name]
        return grants
    raise WorkflowError(f"{origin}: permissions must be a mapping or a string")


def parse_concurrency(node: object, origin: str) -> tuple[str, bool] | None:
    """Return (group, cancel_in_progress) for a `concurrency:` block, or None."""
    if node is None:
        return None
    if isinstance(node, str):
        return node, False
    if isinstance(node, dict):
        group = node.get("group")
        if group is None:
            raise WorkflowError(f"{origin}: `concurrency:` has no `group`")
        cancel = node.get("cancel-in-progress", False)
        # An expression here (`${{ ... }}`) cannot be evaluated statically; treat
        # it as cancelling, which is the dangerous reading.
        if isinstance(cancel, str):
            cancel = cancel.strip().lower() not in {"false", ""}
        return str(group), bool(cancel)
    raise WorkflowError(f"{origin}: `concurrency:` must be a string or a mapping")


# Cap on how many expansions one group expression may produce before the checker
# stops enumerating. Reached only by a group with many `||` alternatives.
MAX_CANDIDATES = 64

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def expand_candidates(group: str) -> set[str] | None:
    """Every string a concurrency group could expand to, or None if too many.

    Textual comparison is not enough, and assuming otherwise would have missed
    the collision this check exists for. The real pair was

        caller:  glm-review-${{ github.event.pull_request.number }}
        callee:  glm-review-${{ github.event.pull_request.number || github.ref }}

    -- different text, identical expansion on a `pull_request` event, because
    `||` yields its first truthy operand and a PR always has a number.

    So each `${{ ... }}` slot is modelled as the set of its `||` alternatives,
    and the candidates are the product across slots. Two groups collide if their
    candidate sets intersect. This over-approximates (it ignores which operand is
    actually truthy), which is the right direction: a false "these could collide"
    costs a comment, a missed collision costs a silent outage.
    """
    parts: list[list[str]] = []
    literal_start = 0
    for match in EXPRESSION.finditer(group):
        parts.append([group[literal_start : match.start()]])
        alternatives = [
            canonical_operand(" ".join(alt.split())) for alt in match.group(1).split("||")
        ]
        parts.append(sorted({alt for alt in alternatives if alt}) or [""])
        literal_start = match.end()
    parts.append([group[literal_start:]])

    total = 1
    for options in parts:
        total *= len(options)
        if total > MAX_CANDIDATES:
            return None

    candidates = {""}
    for options in parts:
        candidates = {prefix + option for prefix in candidates for option in options}
    return candidates


# Context expressions that name the same value, so two groups differing only by
# one of them still collide even though their source text does not match.
# `github.event.number` and `github.event.pull_request.number` are both the PR
# number on a `pull_request` event. Deliberately tiny: only genuinely
# interchangeable pairs belong here, and a wrong entry invents collisions.
# (`github.ref` and `github.ref_name` are NOT interchangeable -- one carries the
# `refs/heads/` prefix.)
CONTEXT_ALIASES = {
    "github.event.number": "github.event.pull_request.number",
}


def canonical_operand(operand: str) -> str:
    return CONTEXT_ALIASES.get(operand, operand)


def literal_skeleton(group: str) -> str:
    """The group with every `${{ ... }}` replaced by a placeholder.

    Two groups whose skeletons differ cannot collide whatever their expressions
    evaluate to (barring an expression that itself supplies the differing
    literal, which no real group does). Two groups sharing a skeleton might.
    """
    return EXPRESSION.sub("\x00", group)


def concurrency_collision(caller_group: str, callee_group: str) -> tuple[str, set[str]]:
    """Return ("none" | "possible" | "definite", shared expansions).

    "definite" means the two groups share an enumerable expansion. "possible"
    means the checker cannot prove they differ: same literal skeleton, but
    operands it cannot evaluate. Reporting "possible" separately keeps a real
    limitation visible instead of resolving it silently to "clean" -- the
    original collision hid for two months precisely because nothing said the
    question had not been answered.
    """
    caller_candidates = expand_candidates(caller_group)
    callee_candidates = expand_candidates(callee_group)

    if caller_candidates is not None and callee_candidates is not None:
        shared = caller_candidates & callee_candidates
        if shared:
            return "definite", shared

    normalized_caller = " ".join(caller_group.split())
    normalized_callee = " ".join(callee_group.split())
    if normalized_caller == normalized_callee:
        return "definite", {normalized_caller}

    if literal_skeleton(normalized_caller) == literal_skeleton(normalized_callee):
        # Same literal text around the expressions, different expressions. They
        # collide iff those expressions agree at run time, which cannot be
        # decided here.
        return "possible", set()
    return "none", set()


def jobs_of(doc: dict, origin: str) -> dict[str, dict]:
    jobs = doc.get("jobs")
    if jobs is None:
        return {}
    if not isinstance(jobs, dict):
        raise WorkflowError(f"{origin}: `jobs` is not a mapping")
    return {name: job for name, job in jobs.items() if isinstance(job, dict)}


def effective_permissions(
    job: dict, doc: dict, origin: str
) -> tuple[dict[str, int] | None, str]:
    """Permissions in force for a job, and where they came from.

    Job level wins over workflow level. If neither is present the answer is None:
    for a caller that means the repository default applies and cannot be read
    from the file; for a callee job it means no constraint, since an undeclared
    callee job simply runs with whatever the caller granted.
    """
    at_job = normalize_permissions(job.get("permissions"), origin)
    if at_job is not None:
        return at_job, "job"
    at_workflow = normalize_permissions(doc.get("permissions"), origin)
    if at_workflow is not None:
        return at_workflow, "workflow"
    return None, "absent"


def fetch_remote(owner: str, repo: str, path: str, ref: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github.raw")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 404:
            hint = (
                " (a private callee needs a token with access to it, "
                "or the ref does not exist)"
            )
        raise WorkflowError(
            f"{owner}/{repo}/{path}@{ref}: HTTP {exc.code}{hint}"
        ) from exc
    except urllib.error.URLError as exc:
        raise WorkflowError(f"{owner}/{repo}/{path}@{ref}: {exc.reason}") from exc


def resolve_callee(
    uses: str, caller_path: Path, callee_root: Path | None, allow_network: bool
) -> tuple[str, str] | None:
    """Return (text, label) for the called workflow, or None if it was skipped."""
    if uses.startswith("./"):
        # A local call resolves against the caller's own commit, so the working
        # tree is the right thing to read.
        local = (repo_root_for(caller_path) / uses[2:]).resolve()
        if not local.is_file():
            raise WorkflowError(f"{uses}: no such file ({local})")
        return local.read_text(encoding="utf-8"), uses

    match = REMOTE_USES.match(uses)
    if not match:
        raise WorkflowError(f"{uses!r}: not a recognizable reusable-workflow reference")
    owner, repo, path, ref = (
        match["owner"],
        match["repo"],
        match["path"],
        match["ref"],
    )

    if callee_root is not None:
        local = (callee_root / path).resolve()
        if local.is_file():
            return local.read_text(encoding="utf-8"), f"{uses} (read from {callee_root})"
        # Fall through: a caller may reference a workflow that is not the repo
        # this root points at.
    if not allow_network:
        return None
    return fetch_remote(owner, repo, path, ref), uses


def repo_root_for(caller_path: Path) -> Path:
    """Directory a `./`-relative `uses:` resolves against.

    Local reusable-workflow calls are relative to the repository root, and a
    caller always lives at .github/workflows/<file>, so the root is two levels up.
    Falls back to the file's own directory for a caller kept elsewhere (this
    repo's caller.yml template is not installed at its final path).
    """
    parent = caller_path.resolve().parent
    if parent.name == "workflows" and parent.parent.name == ".github":
        return parent.parent.parent
    return parent


def check_concurrency(
    caller_doc: dict,
    job: dict,
    job_name: str,
    callee_doc: dict,
    origin: str,
    callee_label: str,
) -> list[Finding]:
    """Flag a caller concurrency group that can collide with its callee's.

    A called reusable workflow joins its own `concurrency:` group while the
    caller is still holding it. If the two groups can expand to the same string,
    the callee contends with its own parent:

    * with `cancel-in-progress: true`, it cancels the parent the moment it
      queues -- run 34084053155 ended in 2 seconds having started no jobs at all;
    * without it, the callee queues behind a caller that cannot finish until the
      callee does, and the pair sits until the job timeout.

    Neither failure names concurrency anywhere in the run.
    """
    findings: list[Finding] = []
    callee_entries: list[tuple[str, tuple[str, bool]]] = []
    workflow_level = parse_concurrency(callee_doc.get("concurrency"), callee_label)
    if workflow_level:
        callee_entries.append(("workflow", workflow_level))
    for callee_job_name, callee_job in jobs_of(callee_doc, callee_label).items():
        job_level = parse_concurrency(callee_job.get("concurrency"), callee_label)
        if job_level:
            callee_entries.append((f"job `{callee_job_name}`", job_level))
    if not callee_entries:
        return findings

    caller_entries: list[tuple[str, tuple[str, bool]]] = []
    caller_workflow = parse_concurrency(caller_doc.get("concurrency"), origin)
    if caller_workflow:
        caller_entries.append(("workflow level", caller_workflow))
    caller_job = parse_concurrency(job.get("concurrency"), origin)
    if caller_job:
        caller_entries.append((f"job `{job_name}`", caller_job))

    for caller_where, (caller_group, caller_cancel) in caller_entries:
        for callee_where, (callee_group, callee_cancel) in callee_entries:
            verdict, shared = concurrency_collision(caller_group, callee_group)
            if verdict == "none":
                continue
            if verdict == "possible":
                findings.append(
                    Finding(
                        "concurrency-possible",
                        origin,
                        job_name,
                        f"{callee_label} ({callee_where})",
                        f"caller's {caller_where} group {caller_group!r} and the "
                        f"callee's {callee_group!r} have the same literal text around "
                        f"their expressions, so they collide if those expressions ever "
                        f"agree — which this checker cannot decide. Verify by hand, or "
                        f"change one group's literal prefix so the question cannot "
                        f"arise.",
                    )
                )
                continue
            example = sorted(shared)[0] if shared else caller_group
            if caller_cancel or callee_cancel:
                consequence = (
                    "the callee cancels its own parent the moment it queues — the "
                    "run ends in seconds having started no jobs, and nothing in it "
                    "mentions concurrency"
                )
            else:
                consequence = (
                    "the callee queues behind a caller that cannot finish until the "
                    "callee does — both sit until the job timeout"
                )
            findings.append(
                Finding(
                    "concurrency-collision",
                    origin,
                    job_name,
                    f"{callee_label} ({callee_where})",
                    f"caller's {caller_where} group {caller_group!r} and the callee's "
                    f"{callee_group!r} can both expand to {example!r}, so "
                    f"{consequence}. Remove the caller's block (the callee's already "
                    f"cancels superseded runs) or give it a group name that cannot "
                    f"collide.",
                )
            )
    return findings


def check_caller(
    caller_path: Path,
    callee_root: Path | None,
    allow_network: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    origin = str(caller_path)
    doc = load_workflow(caller_path.read_text(encoding="utf-8"), origin)

    for job_name, job in jobs_of(doc, origin).items():
        uses = job.get("uses")
        if not isinstance(uses, str):
            continue  # a normal `steps:` job, not a reusable-workflow call

        try:
            resolved = resolve_callee(uses, caller_path, callee_root, allow_network)
        except WorkflowError as exc:
            # Fatal, not skipped. A callee that should be readable and is not
            # leaves this call unverified, and a checker that exits 0 there
            # prints an all-clear over work it never did -- the precise failure
            # this tool exists to prevent, reproduced in the tool itself. A
            # renamed callee that a caller still points at is the realistic
            # case, and it is exactly the drift worth failing on.
            findings.append(
                Finding(
                    "unreadable",
                    origin,
                    job_name,
                    uses,
                    f"could not read callee: {exc}",
                )
            )
            continue
        if resolved is None:
            # Deliberate: --no-network was passed and this callee is remote.
            # Not fatal by itself, but never silently folded into an "OK" --
            # the summary counts it, and --strict promotes it.
            findings.append(
                Finding(
                    "skipped",
                    origin,
                    job_name,
                    uses,
                    "remote callee not fetched (--no-network), so this call is "
                    "UNVERIFIED. Pass --callee-root to resolve it locally, or "
                    "allow network access.",
                )
            )
            continue
        callee_text, callee_label = resolved

        try:
            callee_doc = load_workflow(callee_text, callee_label)
            callee_jobs = jobs_of(callee_doc, callee_label)
        except WorkflowError as exc:
            # Also fatal. An unparseable *caller* already hard-errors; treating
            # an unparseable callee as a skip inverted that asymmetry.
            findings.append(Finding("unreadable", origin, job_name, uses, str(exc)))
            continue

        findings.extend(
            check_concurrency(doc, job, job_name, callee_doc, origin, callee_label)
        )

        granted, source = effective_permissions(job, doc, origin)
        if granted is None:
            findings.append(
                Finding(
                    "undetermined",
                    origin,
                    job_name,
                    callee_label,
                    "job declares no `permissions:` and neither does the workflow, "
                    "so the repository default applies and this file cannot say "
                    "whether the call is safe. Declare the block explicitly.",
                )
            )
            continue

        # Only jobs that declare permissions constrain the caller; an undeclared
        # callee job runs with whatever the caller granted and can never fail to
        # start.
        declared: dict[str, dict[str, int]] = {}
        for callee_job_name, callee_job in callee_jobs.items():
            perms, _ = effective_permissions(callee_job, callee_doc, callee_label)
            if perms is not None:
                declared[callee_job_name] = perms

        for callee_job_name, required in declared.items():
            short = []
            for scope, level in sorted(required.items()):
                if level > granted.get(scope, 0):
                    short.append(
                        f"{scope}: needs {LEVEL_NAMES[level]}, "
                        f"caller grants {LEVEL_NAMES[granted.get(scope, 0)]}"
                    )
            if short:
                findings.append(
                    Finding(
                        "under-grant",
                        origin,
                        job_name,
                        f"{callee_label} (job `{callee_job_name}`)",
                        "; ".join(short),
                        scopes=[entry.split(":")[0] for entry in short],
                    )
                )

        # Over-grants are only computable when every callee job declares its own
        # block; otherwise an undeclared job may legitimately be using the extra
        # scope. Never fatal -- see the module docstring on asymmetry.
        if declared and len(declared) == len(callee_jobs):
            needed_max = {
                scope: max(perms.get(scope, 0) for perms in declared.values())
                for scope in ALL_SCOPES
            }
            extra = [
                f"{scope}: grants {LEVEL_NAMES[granted[scope]]}, callee needs "
                f"{LEVEL_NAMES[needed_max[scope]]}"
                for scope in sorted(ALL_SCOPES)
                if granted.get(scope, 0) > needed_max[scope]
            ]
            if extra:
                findings.append(
                    Finding(
                        "over-grant",
                        origin,
                        job_name,
                        callee_label,
                        "; ".join(extra)
                        + f" (harmless; permissions read from the caller's {source} level)",
                    )
                )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a workflow's reusable-workflow calls can start: "
        "permissions cover what the callee declares, and concurrency groups cannot collide."
    )
    parser.add_argument("callers", nargs="+", type=Path, metavar="CALLER_YAML")
    parser.add_argument("--callee-root", type=Path, default=None)
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    for caller in args.callers:
        if not caller.is_file():
            print(f"error: {caller}: no such file", file=sys.stderr)
            return 2
        try:
            findings.extend(
                check_caller(caller, args.callee_root, not args.no_network)
            )
        except WorkflowError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "kind": f.kind,
                        "caller": f.caller,
                        "job": f.job,
                        "callee": f.callee,
                        "detail": f.detail,
                        "scopes": f.scopes,
                    }
                    for f in findings
                ],
                indent=2,
            )
        )

    failed = [f for f in findings if f.fatal]
    # Everything the checker could not decide. None of these is a proven defect,
    # but each is a call it did not verify, so --strict refuses to call them OK.
    unresolved = [
        f
        for f in findings
        if f.kind in {"undetermined", "skipped", "concurrency-possible"}
    ]
    if args.strict:
        failed = failed + unresolved

    if not args.json:
        for finding in findings:
            if args.quiet and finding.kind == "over-grant":
                continue
            marker = {
                "under-grant": "FAIL",
                "concurrency-collision": "FAIL",
                "unreadable": "FAIL",
                "undetermined": "WARN",
                "concurrency-possible": "WARN",
                "skipped": "SKIP",
                "over-grant": "note",
            }[finding.kind]
            print(f"{marker} {finding.caller} :: job `{finding.job}` -> {finding.callee}")
            print(f"     {finding.detail}")
            if finding.kind == "under-grant":
                print(
                    "     GitHub will refuse to start this run before any job "
                    "exists (`startup_failure`, no log)."
                )

        if failed:
            counts: dict[str, int] = {}
            for finding in failed:
                counts[finding.kind] = counts.get(finding.kind, 0) + 1
            labels = {
                "under-grant": "permission under-grant(s)",
                "concurrency-collision": "concurrency collision(s)",
                "unreadable": "unreadable callee(s)",
                "undetermined": "undeterminable caller(s)",
                "concurrency-possible": "undecidable concurrency group(s)",
                "skipped": "unverified call(s)",
            }
            summary = ", ".join(
                f"{count} {labels[kind]}" for kind, count in sorted(counts.items())
            )
            print(f"\n{summary}.", file=sys.stderr)
            if "under-grant" in counts:
                print(
                    "  Under-grant: add the missing scopes to the caller, or move "
                    "the pin forward to a callee that no longer declares them. "
                    "Those two are not interchangeable.",
                    file=sys.stderr,
                )
            if "concurrency-collision" in counts:
                print(
                    "  Collision: drop the caller's `concurrency:` block, or give "
                    "it a group name the callee's cannot expand to.",
                    file=sys.stderr,
                )
            if "unreadable" in counts:
                print(
                    "  Unreadable callee: the call could not be checked at all. "
                    "Fix the reference (a renamed or moved callee is the usual "
                    "cause) rather than ignoring it — an unchecked call is how the "
                    "outage this tool exists for stayed invisible.",
                    file=sys.stderr,
                )
        elif not args.quiet:
            checked = len(args.callers)
            if unresolved:
                kinds: dict[str, int] = {}
                for finding in unresolved:
                    kinds[finding.kind] = kinds.get(finding.kind, 0) + 1
                detail = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
                print(
                    f"\nNo defect found in {checked} file(s) — but {len(unresolved)} "
                    f"call(s) were NOT verified ({detail}). See the WARN/SKIP lines "
                    "above; this is not a clean bill of health. Re-run with --strict "
                    "to treat them as failures."
                )
            else:
                print(
                    f"\nOK: {checked} file(s) checked — every reusable-workflow call "
                    "grants the scopes its callee declares, and no concurrency group "
                    "can collide with its callee's."
                )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
