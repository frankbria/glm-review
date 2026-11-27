#!/usr/bin/env python3
"""Tests for the review workflow's "Fetch the PR diff" step.

The motivating runs (frankbria/iris#644, runs 42976860368 and 42978691143): a
test file held a raw ESC byte inside a regex, a change landed within three lines
of it, and `gh pr diff` refused to print the diff:

    the diff contains terminal escape sequences; pass --allow-escape-sequences
    to output it anyway

The step failed, so the Codex review never started and only the fallback
reviewers ran. The diff goes to a file that Codex reads, never to a terminal.

These tests run the step's own shell script, extracted from review.yml, against
a stand-in `gh` that refuses the way the real one does.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

REVIEW_YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "review.yml"

ESC = b"\x1b"
DIFF_WITH_ESCAPE = b"-      expect(text).not.toMatch(/" + ESC + b"\\[/);\n"

# `gh pr diff`, reduced to the one behaviour under test. printf's \033 keeps a
# raw ESC byte out of this file.
STAND_IN_GH = r"""#!/usr/bin/env bash
for argument in "$@"; do
  if [ "$argument" = "--allow-escape-sequences" ]; then
    printf -- '-      expect(text).not.toMatch(/\033\\[/);\n'
    exit 0
  fi
done
echo "the diff contains terminal escape sequences; pass --allow-escape-sequences to output it anyway" >&2
exit 1
"""


def _fetch_step_script() -> str:
    workflow = yaml.safe_load(REVIEW_YML.read_text())
    steps = workflow["jobs"]["review"]["steps"]
    [step] = [s for s in steps if s.get("name") == "Fetch the PR diff"]
    return step["run"]


def _run(script: str) -> tuple[int, bytes]:
    """Run `script` with the stand-in `gh` first on PATH. Returns (exit code, pr.diff)."""
    with TemporaryDirectory() as tmp:
        gh = Path(tmp) / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text(STAND_IN_GH)
        gh.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{gh.parent}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": tmp,
            "PR_NUMBER": "643",
            "GITHUB_REPOSITORY": "frankbria/iris",
        }
        # The runner's default shell for `run:` is `bash -e {0}`.
        result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True)
        diff = Path(tmp) / "pr.diff"
        return result.returncode, diff.read_bytes() if diff.exists() else b""


class FetchDiffStepTest(unittest.TestCase):
    def test_a_diff_holding_an_escape_sequence_is_written_to_the_file(self):
        code, diff = _run(_fetch_step_script())

        self.assertEqual(code, 0)
        self.assertEqual(diff, DIFF_WITH_ESCAPE)

    def test_the_stand_in_refuses_without_the_flag_as_gh_does(self):
        """Guards the test above: if the stand-in accepted anything, it would prove nothing."""
        code, diff = _run('gh pr diff "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" > "$RUNNER_TEMP/pr.diff"')

        self.assertEqual(code, 1)
        self.assertEqual(diff, b"")


if __name__ == "__main__":
    unittest.main()
