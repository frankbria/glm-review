#!/usr/bin/env python3
"""Tests for review.yml's provider configuration.

The reviewer runs against any Anthropic-compatible endpoint: `base_url` and
`model` choose it, and the key arrives as MODEL_API_KEY or, from callers written
before that name existed, ZHIPU_API_KEY. Neither secret can be `required`, so the
"Check the model API key" step stands in for that guarantee, and these tests run
its shell script as extracted from review.yml.

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


def _step(**match) -> dict:
    steps = _workflow()["jobs"]["review"]["steps"]
    [step] = [s for s in steps if all(s.get(k) == v for k, v in match.items())]
    return step


class KeyCheckTest(unittest.TestCase):
    def _run(self, key: str) -> int:
        env = {**os.environ, "MODEL_API_KEY": key}
        return subprocess.run(
            ["bash", "-c", _step(id="key")["run"]], env=env, capture_output=True
        ).returncode

    def test_an_empty_key_fails(self):
        self.assertNotEqual(self._run(""), 0)

    def test_a_key_passes(self):
        self.assertEqual(self._run("sk-test"), 0)

    def test_it_runs_before_the_review(self):
        ids = [s.get("id") for s in _workflow()["jobs"]["review"]["steps"]]
        self.assertLess(ids.index("key"), ids.index("review"))


class WiringTest(unittest.TestCase):
    def test_both_secrets_are_accepted_and_neither_is_required(self):
        secrets = _workflow()[True]["workflow_call"]["secrets"]  # YAML 1.1: `on` is True
        self.assertEqual(secrets["MODEL_API_KEY"], {"required": False})
        self.assertEqual(secrets["ZHIPU_API_KEY"], {"required": False})

    def test_every_key_consumer_takes_either_secret(self):
        self.assertEqual(_step(id="key")["env"]["MODEL_API_KEY"], KEY_EXPR)
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

    def test_a_missing_key_is_named_in_the_notice_not_blamed_on_a_timeout(self):
        notice = _step(name="Report an incomplete review")
        self.assertEqual(notice["env"]["KEY_OUTCOME"], "${{ steps.key.outcome }}")
        run = notice["run"]
        self.assertLess(run.index('"${KEY_OUTCOME:-}" = "failure"'), run.index("PROVIDER_ERROR:-false"))


if __name__ == "__main__":
    unittest.main()
