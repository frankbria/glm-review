#!/usr/bin/env python3
"""Tests for check_workflow_calls.

The load-bearing test is `test_reproduces_the_266_run_outage`: it rebuilds the
exact caller/callee pair that made `frankbria/narrative-modeling-app` fail to
start 266 times, and asserts this checker names both missing scopes. A checker
that only ever passes on correct input proves nothing -- that mistake was made
once already on this repo with actionlint, which happily accepted a deliberately
broken local reusable-workflow reference.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'
"""

from __future__ import annotations

import io
import textwrap
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

import check_workflow_calls as guard


# The callee as it stood at b877d15a -- the pin narrative-modeling-app was
# stuck on. Four grants; the caller below lists two.
HISTORICAL_CALLEE = """
name: GLM PR Review
on:
  workflow_call:
    secrets:
      ZHIPU_API_KEY:
        required: true
jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
      issues: write
      id-token: write
    steps:
      - run: echo review
"""

CURRENT_CALLEE = """
name: GLM PR Review
on:
  workflow_call:
    secrets:
      ZHIPU_API_KEY:
        required: true
jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - run: echo review
"""


def caller(uses: str, permissions: str | None) -> str:
    block = ""
    if permissions is not None:
        block = "\n" + textwrap.indent(textwrap.dedent(permissions).strip(), "    ")
    return f"""
name: GLM Review
on:
  pull_request:
    types: [opened, synchronize]
jobs:
  review:
    uses: {uses}{block}
    secrets:
      ZHIPU_API_KEY: ${{{{ secrets.ZHIPU_API_KEY }}}}
"""


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def write_callee(self, text: str) -> None:
        (self.root / ".github" / "workflows" / "review.yml").write_text(text)

    def write_caller(self, text: str, name: str = "caller.yml") -> Path:
        path = self.root / ".github" / "workflows" / name
        path.write_text(text)
        return path

    def check(self, path: Path, **kwargs) -> list[guard.Finding]:
        kwargs.setdefault("callee_root", self.root)
        kwargs.setdefault("allow_network", False)
        return guard.check_caller(path, **kwargs)

    def kinds(self, findings: list[guard.Finding]) -> list[str]:
        return [f.kind for f in findings]


class TestTheOutage(GuardTestCase):
    def test_reproduces_the_266_run_outage(self) -> None:
        """The reduced block on the July pin: both missing scopes are named."""
        self.write_callee(HISTORICAL_CALLEE)
        path = self.write_caller(
            caller(
                "frankbria/glm-review/.github/workflows/review.yml@b877d15a",
                "permissions:\n  contents: read\n  pull-requests: write",
            )
        )
        findings = self.check(path)
        under = [f for f in findings if f.kind == "under-grant"]
        self.assertEqual(len(under), 1, findings)
        self.assertEqual(sorted(under[0].scopes), ["id-token", "issues"])
        self.assertIn("needs write, caller grants none", under[0].detail)

    def test_same_caller_passes_against_the_fixed_callee(self) -> None:
        """Moving the pin forward -- not editing the caller -- clears it."""
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            caller(
                "frankbria/glm-review/.github/workflows/review.yml@77dfa9c",
                "permissions:\n  contents: read\n  pull-requests: write",
            )
        )
        self.assertEqual([f for f in self.check(path) if f.fatal], [])


class TestClosedBlockSemantics(GuardTestCase):
    def test_unlisted_scope_is_none_not_inherited(self) -> None:
        """The trap: listing any scope silently sets every other one to none."""
        self.write_callee(HISTORICAL_CALLEE)
        path = self.write_caller(
            caller(
                "o/r/.github/workflows/review.yml@sha",
                "permissions:\n  contents: read\n  pull-requests: write\n  issues: write",
            )
        )
        under = [f for f in self.check(path) if f.kind == "under-grant"]
        self.assertEqual(under[0].scopes, ["id-token"])

    def test_read_is_not_enough_for_write(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            caller(
                "o/r/.github/workflows/review.yml@sha",
                "permissions:\n  contents: read\n  pull-requests: read",
            )
        )
        under = [f for f in self.check(path) if f.kind == "under-grant"]
        self.assertEqual(under[0].scopes, ["pull-requests"])

    def test_empty_block_grants_nothing(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            caller("o/r/.github/workflows/review.yml@sha", "permissions: {}")
        )
        under = [f for f in self.check(path) if f.kind == "under-grant"]
        self.assertEqual(sorted(under[0].scopes), ["contents", "pull-requests"])

    def test_write_all_shorthand_satisfies_everything(self) -> None:
        self.write_callee(HISTORICAL_CALLEE)
        path = self.write_caller(
            caller("o/r/.github/workflows/review.yml@sha", "permissions: write-all")
        )
        self.assertEqual([f for f in self.check(path) if f.fatal], [])

    def test_read_all_shorthand_still_misses_write_scopes(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            caller("o/r/.github/workflows/review.yml@sha", "permissions: read-all")
        )
        under = [f for f in self.check(path) if f.kind == "under-grant"]
        self.assertEqual(under[0].scopes, ["pull-requests"])


class TestInheritance(GuardTestCase):
    def test_caller_workflow_level_block_is_used(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            """
name: GLM Review
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    uses: o/r/.github/workflows/review.yml@sha
"""
        )
        self.assertEqual([f for f in self.check(path) if f.fatal], [])

    def test_callee_job_without_permissions_constrains_nothing(self) -> None:
        self.write_callee(
            """
name: X
on: workflow_call
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
        )
        path = self.write_caller(
            caller("o/r/.github/workflows/review.yml@sha", "permissions: {}")
        )
        self.assertEqual([f for f in self.check(path) if f.fatal], [])

    def test_callee_workflow_level_block_constrains(self) -> None:
        self.write_callee(
            """
name: X
on: workflow_call
permissions:
  issues: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
        )
        path = self.write_caller(
            caller("o/r/.github/workflows/review.yml@sha", "permissions:\n  contents: read")
        )
        under = [f for f in self.check(path) if f.kind == "under-grant"]
        self.assertEqual(under[0].scopes, ["issues"])

    def test_only_the_declaring_callee_job_is_checked(self) -> None:
        self.write_callee(
            """
name: X
on: workflow_call
jobs:
  loose:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
  strict:
    runs-on: ubuntu-latest
    permissions:
      packages: write
    steps:
      - run: echo hi
"""
        )
        path = self.write_caller(
            caller("o/r/.github/workflows/review.yml@sha", "permissions:\n  contents: read")
        )
        under = [f for f in self.check(path) if f.kind == "under-grant"]
        self.assertEqual(len(under), 1)
        self.assertIn("`strict`", under[0].callee)


class TestNonFatalOutcomes(GuardTestCase):
    def test_over_grant_is_a_note_not_a_failure(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            caller(
                "o/r/.github/workflows/review.yml@sha",
                "permissions:\n  contents: read\n  pull-requests: write\n  issues: write",
            )
        )
        findings = self.check(path)
        self.assertEqual([f for f in findings if f.fatal], [])
        notes = [f for f in findings if f.kind == "over-grant"]
        self.assertEqual(len(notes), 1)
        self.assertIn("issues", notes[0].detail)

    def test_over_grant_not_reported_when_a_callee_job_is_undeclared(self) -> None:
        """An undeclared job may legitimately need the extra scope."""
        self.write_callee(
            """
name: X
on: workflow_call
jobs:
  declared:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - run: echo hi
  undeclared:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
        )
        path = self.write_caller(
            caller(
                "o/r/.github/workflows/review.yml@sha",
                "permissions:\n  contents: read\n  packages: write",
            )
        )
        self.assertNotIn("over-grant", self.kinds(self.check(path)))

    def test_absent_permissions_is_undetermined(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            caller("o/r/.github/workflows/review.yml@sha", None)
        )
        findings = self.check(path)
        self.assertEqual(self.kinds(findings), ["undetermined"])
        self.assertEqual([f for f in findings if f.fatal], [])

    def test_non_reusable_jobs_are_ignored(self) -> None:
        path = self.write_caller(
            """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
"""
        )
        self.assertEqual(self.check(path), [])

    def test_remote_callee_skipped_without_network(self) -> None:
        path = self.write_caller(
            caller("other/repo/.github/workflows/x.yml@sha", "permissions: {}")
        )
        findings = guard.check_caller(path, callee_root=None, allow_network=False)
        self.assertEqual(self.kinds(findings), ["skipped"])


class TestLocalPathCallee(GuardTestCase):
    def test_dot_slash_resolves_against_repo_root(self) -> None:
        self.write_callee(HISTORICAL_CALLEE)
        path = self.write_caller(
            caller(
                "./.github/workflows/review.yml",
                "permissions:\n  contents: read\n  pull-requests: write",
            ),
            name="glm-review.yml",
        )
        under = [f for f in self.check(path) if f.kind == "under-grant"]
        self.assertEqual(sorted(under[0].scopes), ["id-token", "issues"])


class TestMalformedInput(GuardTestCase):
    def test_unknown_scope_is_an_error(self) -> None:
        with self.assertRaises(guard.WorkflowError) as ctx:
            guard.normalize_permissions({"contents": "read", "bogus": "write"}, "x")
        self.assertIn("bogus", str(ctx.exception))

    def test_unknown_level_is_an_error(self) -> None:
        with self.assertRaises(guard.WorkflowError) as ctx:
            guard.normalize_permissions({"contents": "maybe"}, "x")
        self.assertIn("maybe", str(ctx.exception))

    def test_unknown_shorthand_is_an_error(self) -> None:
        with self.assertRaises(guard.WorkflowError) as ctx:
            guard.normalize_permissions("all-the-things", "x")
        self.assertIn("all-the-things", str(ctx.exception))

    def test_every_documented_scope_is_accepted(self) -> None:
        for scope in guard.ALL_SCOPES:
            self.assertIsNotNone(guard.normalize_permissions({scope: "write"}, "x"))


class TestExitCodes(GuardTestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = guard.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_under_grant_exits_one(self) -> None:
        self.write_callee(HISTORICAL_CALLEE)
        path = self.write_caller(
            caller(
                "o/r/.github/workflows/review.yml@sha",
                "permissions:\n  contents: read\n  pull-requests: write",
            )
        )
        code, text = self.run_main(
            [str(path), "--callee-root", str(self.root), "--no-network"]
        )
        self.assertEqual(code, 1)
        self.assertIn("startup_failure", text)

    def test_clean_pair_exits_zero(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(
            caller(
                "o/r/.github/workflows/review.yml@sha",
                "permissions:\n  contents: read\n  pull-requests: write",
            )
        )
        code, _ = self.run_main(
            [str(path), "--callee-root", str(self.root), "--no-network"]
        )
        self.assertEqual(code, 0)

    def test_strict_promotes_undetermined_to_failure(self) -> None:
        self.write_callee(CURRENT_CALLEE)
        path = self.write_caller(caller("o/r/.github/workflows/review.yml@sha", None))
        argv = [str(path), "--callee-root", str(self.root), "--no-network"]
        self.assertEqual(self.run_main(argv)[0], 0)
        self.assertEqual(self.run_main(argv + ["--strict"])[0], 1)

    def test_missing_file_exits_two(self) -> None:
        code, _ = self.run_main([str(self.root / "nope.yml")])
        self.assertEqual(code, 2)


# The two groups that actually collided, copied verbatim from
# narrative-modeling-app's glm-review.yml at f6c14ee^ and glm-review's
# review.yml at b877d15a. They are NOT the same string -- that is the point.
REAL_CALLER_GROUP = "glm-review-${{ github.event.pull_request.number }}"
REAL_CALLEE_GROUP = "glm-review-${{ github.event.pull_request.number || github.ref }}"


def callee_with_concurrency(group: str, cancel: bool = True) -> str:
    return f"""
name: X
on: workflow_call
concurrency:
  group: {group}
  cancel-in-progress: {str(cancel).lower()}
jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - run: echo hi
"""


def caller_with_concurrency(group: str | None, cancel: bool = True) -> str:
    block = ""
    if group is not None:
        block = (
            f"concurrency:\n  group: {group}\n"
            f"  cancel-in-progress: {str(cancel).lower()}\n"
        )
    return f"""
name: GLM Review
on: pull_request
{block}jobs:
  review:
    uses: o/r/.github/workflows/review.yml@sha
    permissions:
      contents: read
"""


class TestExpansionModel(unittest.TestCase):
    """`||` yields its first truthy operand, so text equality is the wrong test."""

    def test_alternatives_become_separate_candidates(self) -> None:
        self.assertEqual(
            guard.expand_candidates("g-${{ a || b }}"),
            {"g-a", "g-b"},
        )

    def test_whitespace_inside_an_expression_is_normalized(self) -> None:
        self.assertEqual(
            guard.expand_candidates("g-${{   a   }}"),
            guard.expand_candidates("g-${{a}}"),
        )

    def test_literal_group_has_one_candidate(self) -> None:
        self.assertEqual(guard.expand_candidates("static"), {"static"})

    def test_multiple_slots_multiply(self) -> None:
        self.assertEqual(
            guard.expand_candidates("${{ a || b }}-${{ c || d }}"),
            {"a-c", "a-d", "b-c", "b-d"},
        )

    def test_runaway_expansion_bails_out(self) -> None:
        huge = "-".join("${{ " + " || ".join("abcdef") + " }}" for _ in range(8))
        self.assertIsNone(guard.expand_candidates(huge))

    def test_real_groups_share_an_expansion_despite_differing_text(self) -> None:
        self.assertNotEqual(REAL_CALLER_GROUP, REAL_CALLEE_GROUP)
        collides, shared = guard.concurrency_collision(
            REAL_CALLER_GROUP, REAL_CALLEE_GROUP
        )
        self.assertTrue(collides)
        self.assertEqual(
            shared, {"glm-review-github.event.pull_request.number"}
        )


class TestConcurrencyCollision(GuardTestCase):
    def test_reproduces_the_two_second_self_cancel(self) -> None:
        """The real pair. A text comparison would pass this and miss the bug."""
        self.write_callee(callee_with_concurrency(REAL_CALLEE_GROUP))
        path = self.write_caller(caller_with_concurrency(REAL_CALLER_GROUP))
        findings = self.check(path)
        collisions = [f for f in findings if f.kind == "concurrency-collision"]
        self.assertEqual(len(collisions), 1, findings)
        self.assertIn("cancels its own parent", collisions[0].detail)
        self.assertTrue(collisions[0].fatal)

    def test_identical_groups_collide(self) -> None:
        self.write_callee(callee_with_concurrency(REAL_CALLEE_GROUP))
        path = self.write_caller(caller_with_concurrency(REAL_CALLEE_GROUP))
        self.assertEqual(
            len([f for f in self.check(path) if f.kind == "concurrency-collision"]), 1
        )

    def test_distinct_prefix_does_not_collide(self) -> None:
        self.write_callee(callee_with_concurrency(REAL_CALLEE_GROUP))
        path = self.write_caller(
            caller_with_concurrency("ci-${{ github.event.pull_request.number }}")
        )
        self.assertEqual([f for f in self.check(path) if f.fatal], [])

    def test_caller_without_concurrency_is_clean(self) -> None:
        """The fix narrative-modeling-app shipped: drop the caller's block."""
        self.write_callee(callee_with_concurrency(REAL_CALLEE_GROUP))
        path = self.write_caller(caller_with_concurrency(None))
        self.assertEqual([f for f in self.check(path) if f.fatal], [])

    def test_callee_without_concurrency_is_clean(self) -> None:
        # Permissions deliberately matched to the caller fixture's
        # `contents: read`, so this isolates the concurrency check.
        self.write_callee(
            """
name: X
on: workflow_call
jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - run: echo hi
"""
        )
        path = self.write_caller(caller_with_concurrency(REAL_CALLEE_GROUP))
        self.assertEqual([f for f in self.check(path) if f.fatal], [])

    def test_collision_without_cancel_is_reported_as_a_deadlock(self) -> None:
        self.write_callee(callee_with_concurrency(REAL_CALLEE_GROUP, cancel=False))
        path = self.write_caller(
            caller_with_concurrency(REAL_CALLEE_GROUP, cancel=False)
        )
        collisions = [f for f in self.check(path) if f.kind == "concurrency-collision"]
        self.assertEqual(len(collisions), 1)
        self.assertIn("job timeout", collisions[0].detail)

    def test_job_level_caller_concurrency_is_checked(self) -> None:
        self.write_callee(callee_with_concurrency(REAL_CALLEE_GROUP))
        path = self.write_caller(
            f"""
name: GLM Review
on: pull_request
jobs:
  review:
    uses: o/r/.github/workflows/review.yml@sha
    permissions:
      contents: read
    concurrency:
      group: {REAL_CALLER_GROUP}
      cancel-in-progress: true
"""
        )
        collisions = [f for f in self.check(path) if f.kind == "concurrency-collision"]
        self.assertEqual(len(collisions), 1)
        self.assertIn("job `review`", collisions[0].detail)

    def test_shorthand_string_concurrency_is_parsed(self) -> None:
        self.write_callee(
            """
name: X
on: workflow_call
concurrency: shared-group
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
        )
        path = self.write_caller(
            """
name: GLM Review
on: pull_request
concurrency: shared-group
jobs:
  review:
    uses: o/r/.github/workflows/review.yml@sha
    permissions:
      contents: read
"""
        )
        self.assertEqual(
            len([f for f in self.check(path) if f.kind == "concurrency-collision"]), 1
        )


class TestThisRepository(unittest.TestCase):
    """Integration: the files this repo actually ships must pass."""

    root = Path(__file__).resolve().parent.parent

    def test_self_review_caller_is_consistent(self) -> None:
        findings = guard.check_caller(
            self.root / ".github" / "workflows" / "glm-review.yml",
            callee_root=None,
            allow_network=False,
        )
        self.assertEqual([f for f in findings if f.fatal], [], findings)

    def test_caller_template_matches_the_callee_it_documents(self) -> None:
        """caller.yml pins @main; check it against the working tree instead."""
        findings = guard.check_caller(
            self.root / "caller.yml",
            callee_root=self.root,
            allow_network=False,
        )
        self.assertEqual([f for f in findings if f.fatal], [], findings)


if __name__ == "__main__":
    unittest.main()
