#!/usr/bin/env python3
"""Tests for review.yml's provider configuration.

The last reviewer in the failover chain runs against any Anthropic-compatible
endpoint: `base_url` and `model` choose it, and the key arrives as MODEL_API_KEY
or, from callers written before that name existed, ZHIPU_API_KEY. Every secret is
optional: a missing key skips that reviewer and the failover report says so.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

import yaml

REVIEW_YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "review.yml"
KEY_EXPR = "${{ secrets.MODEL_API_KEY || secrets.ZHIPU_API_KEY }}"


def _workflow() -> dict:
    return yaml.safe_load(REVIEW_YML.read_text())


def _step(job: str = "fallback", **match) -> dict:
    steps = _workflow()["jobs"][job]["steps"]
    [step] = [s for s in steps if all(s.get(k) == v for k, v in match.items())]
    return step


class PromptTest(unittest.TestCase):
    def _compose(self) -> str:
        with __import__("tempfile").TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            output.touch()
            env = {**os.environ, "GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "o/r",
                   "PR_NUMBER": "1", "RUNNER_TEMP": tmp, "EXTRA_INSTRUCTIONS": "",
                   "KIMI_MODEL": "kimi-x", "MODEL": "glm-x", "CODEX_MODEL": "codex-x"}
            subprocess.run(["bash", "-c", _step(job="review", id="prompts")["run"]], env=env, check=True)
            return output.read_text()

    def test_each_claude_prompt_names_its_reviewer_and_model(self):
        prompts = self._compose()
        self.assertIn("`### Third Party Review — Kimi`", prompts)
        self.assertIn("`Model: kimi-x`", prompts)
        self.assertIn("`### Third Party Review — glm-x`", prompts)
        self.assertIn("`Model: glm-x`", prompts)
        self.assertNotIn("__MODEL__", prompts)
        self.assertNotIn("__REVIEWER__", prompts)


class WiringTest(unittest.TestCase):
    def test_both_secrets_are_accepted_and_neither_is_required(self):
        secrets = _workflow()[True]["workflow_call"]["secrets"]  # YAML 1.1: `on` is True
        self.assertEqual(secrets["MODEL_API_KEY"], {"required": False})
        self.assertEqual(secrets["ZHIPU_API_KEY"], {"required": False})

    def test_every_key_consumer_takes_either_secret(self):
        self.assertEqual(
            _workflow()["jobs"]["fallback"]["env"]["HAS_MODEL_KEY"],
            "${{ (secrets.MODEL_API_KEY || secrets.ZHIPU_API_KEY) != '' }}",
        )
        self.assertEqual(_step(id="quota")["env"]["MODEL_API_KEY"], KEY_EXPR)
        review = _step(id="review")
        self.assertEqual(review["env"]["ANTHROPIC_AUTH_TOKEN"], KEY_EXPR)
        self.assertEqual(review["with"]["anthropic_api_key"], KEY_EXPR)

    def test_endpoint_and_model_come_from_inputs(self):
        inputs = _workflow()[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["base_url"]["default"], "https://api.z.ai/api/anthropic")
        env = _step(id="review")["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "${{ inputs.base_url }}")
        # Background "haiku" calls must not hit a model id the endpoint lacks.
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "${{ inputs.model }}")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "${{ inputs.model }}")

    def test_both_kimi_and_the_last_reviewer_pin_background_calls(self):
        env = _step(id="kimi")["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "${{ inputs.kimi_model }}")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "${{ inputs.kimi_model }}")

    def test_both_comments_record_the_model(self):
        self.assertEqual(_step(id="review")["with"]["prompt"], "${{ needs.review.outputs.glm_prompt }}")
        notice = _step(name="Report an incomplete review")
        self.assertEqual(notice["env"]["MODEL"], "${{ inputs.model }}")
        self.assertIn("Model: \\`$MODEL\\`", notice["run"])


if __name__ == "__main__":
    unittest.main()
