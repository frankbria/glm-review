#!/usr/bin/env python3
"""Tests for the review workflow's "Read the agent's verdict" step.

The motivating run (frankbria/podcaststudiohub, 2026-10-13, run 38354510891): the
provider rejected the request with a 429 quota error, the agent's result message
was `subtype: success` with `is_error: true` after one turn, the review step
therefore succeeded, no "did not complete" notice fired, and the check went
green having reviewed nothing. The result message below is that run's, verbatim.

These tests run the step's own shell script, extracted from review.yml, so the
assertion is about what ships, not a copy of it.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

REVIEW_YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "review.yml"

PROVIDER_REJECTION = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "duration_ms": 669,
    "num_turns": 1,
    "result": "API Error: Request rejected (429) · [1310][Weekly/Monthly Limit Exhausted. "
    "Your limit will reset at 2026-10-19 13:35:09][2026101314024395236e1ab65b482f]",
}
REAL_VERDICT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 23,
    "result": "✅ Third Party Review: no defects found.",
}


def _verdict_step_script() -> str:
    workflow = yaml.safe_load(REVIEW_YML.read_text())
    steps = workflow["jobs"]["fallback"]["steps"]
    [step] = [s for s in steps if s.get("id") == "verdict"]
    return step["run"]


def _run(messages) -> dict:
    """Run the step against an execution file holding `messages` (None: no file)."""
    with TemporaryDirectory() as tmp:
        output = Path(tmp) / "github_output"
        output.touch()
        env = {**os.environ, "GITHUB_OUTPUT": str(output), "EXECUTION_FILE": ""}
        if messages is not None:
            execution = Path(tmp) / "claude-execution-output.json"
            execution.write_text(json.dumps(messages))
            env["EXECUTION_FILE"] = str(execution)
        subprocess.run(["bash", "-c", _verdict_step_script()], env=env, check=True)
        values = {}
        lines = output.read_text().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if "<<" in line:  # multiline: name<<DELIM ... DELIM
                name, delim = line.split("<<", 1)
                body = []
                i += 1
                while lines[i] != delim:
                    body.append(lines[i])
                    i += 1
                values[name] = "\n".join(body)
            elif "=" in line:
                name, value = line.split("=", 1)
                values[name] = value
            i += 1
        return values


class VerdictStepTest(unittest.TestCase):
    def test_a_provider_rejection_is_a_provider_error_with_its_message(self):
        values = _run([{"type": "system", "subtype": "init"}, PROVIDER_REJECTION])

        self.assertEqual(values["provider_error"], "true")
        self.assertIn("429", values["provider_message"])
        self.assertIn("Limit Exhausted", values["provider_message"])

    def test_a_real_verdict_is_not(self):
        values = _run([{"type": "system", "subtype": "init"}, REAL_VERDICT])

        self.assertEqual(values["provider_error"], "false")

    def test_no_execution_file_is_left_to_the_incomplete_review_notice(self):
        """A killed step writes no file; that case is the existing notice's, keyed
        off the step outcome, and must not be reclassified here."""
        values = _run(None)

        self.assertEqual(values["provider_error"], "false")


class WiringTest(unittest.TestCase):
    """The verdict must reach the notice and the failover report, not the check status."""

    def setUp(self):
        self.steps = yaml.safe_load(REVIEW_YML.read_text())["jobs"]["fallback"]["steps"]

    def test_the_incomplete_review_notice_fires_on_a_provider_error(self):
        [notice] = [s for s in self.steps if s.get("name") == "Report an incomplete review"]
        self.assertIn("steps.verdict.outputs.provider_error == 'true'", notice["if"])

    def test_a_provider_error_is_reported_without_failing_the_check(self):
        # Failover replaced the red check: the PR comment carries the failure.
        self.assertFalse(any("exit 1" in s.get("run", "") and "provider_error" in s.get("if", "")
                             for s in self.steps))
        [report] = [s for s in self.steps if s.get("name") == "Report reviewer failover"]
        self.assertIn("steps.verdict.outputs.provider_message", report["env"]["GLM_PROVIDER_MESSAGE"])
        self.assertIn("steps.kimi_verdict.outputs.provider_message", report["env"]["KIMI_PROVIDER_MESSAGE"])

    def test_kimi_is_read_before_the_last_reviewer_overwrites_its_file(self):
        ids = [s.get("id") for s in self.steps]
        self.assertLess(ids.index("kimi_verdict"), ids.index("review"))
        names = [s.get("name") for s in self.steps]
        self.assertLess(names.index("Upload Kimi review transcript"), ids.index("review"))

    def test_kimi_gets_the_same_verdict_check(self):
        [kimi] = [s for s in self.steps if s.get("id") == "kimi_verdict"]
        [last] = [s for s in self.steps if s.get("id") == "verdict"]
        self.assertEqual(kimi["run"], last["run"])
        self.assertEqual(kimi["env"]["EXECUTION_FILE"], "${{ steps.kimi.outputs.execution_file }}")


if __name__ == "__main__":
    unittest.main()
