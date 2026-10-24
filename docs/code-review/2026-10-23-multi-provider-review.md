# Code Review Report: Provider-neutral review workflow

**Date:** 2026-10-23
**Reviewer:** Code Review Agent
**Component:** `review.yml` provider configuration
**Files Reviewed:** `.github/workflows/review.yml`, `.github/workflows/glm-review.yml`, `caller.yml`, `README.md`, `scripts/test_provider_config.py`
**Ready for Production:** Yes

## Executive Summary

The change makes the endpoint (`base_url`) and key (`MODEL_API_KEY`, with `ZHIPU_API_KEY` still accepted) configurable, adds a key check that replaces `required: true`, and renames the failure-notice header to "Third Party Review". No defects block merge. There are three minor findings: one accepted as is and two left as follow-ups.

**Critical Issues:** 0
**Major Issues:** 0
**Minor Issues:** 3 (1 partly fixed)
**Positive Findings:** 4

---

## Review Context

**Code Type:** CI configuration (reusable GitHub Actions workflow) handling a secret
**Risk Level:** Medium. It routes an API key, and every repo tracking `@main` picks it up.
**Business Constraints:** Backward compatibility with existing callers

### Review Focus Areas

- ✅ Secret handling and where the key is sent, because the key now goes to a caller-chosen URL
- ✅ Failure modes: the key check, the notice branches, the header anchor
- ✅ Behaviour change for existing z.ai callers
- ❌ OWASP LLM: the prompt and tool allowlist are unchanged
- ❌ OWASP Web/ML: there is no web surface and no model code

---

## Priority 3 Issues - Minor 💡

### Background "haiku" calls on z.ai now use the review model
**Location:** `.github/workflows/review.yml` (`ANTHROPIC_DEFAULT_HAIKU_MODEL`)
**Severity:** Minor
**Category:** Behaviour change

**Problem:** z.ai maps Claude Code's haiku model id to a lighter GLM model on its own side. The override now sends those calls to `glm-5.2` on the default endpoint as well.

**Impact:** A small number of short background calls per run use the heavier model's quota.

**Decision:** Accepted. Setting the override only for non-default endpoints depends on Claude Code treating an empty variable as unset, and that isn't verified. The cost is a few calls per run.

### Verdict line and workflow name still say "GLM"
**Location:** `.github/workflows/review.yml` (prompt: `✅ GLM review: no defects found.`; `name: GLM PR Review`; `::error title=GLM review did not run`)
**Severity:** Minor
**Category:** Consistency

**Problem:** The notice header now reads "Third Party Review", but the clean-verdict line it quotes still says "GLM review".

**Decision:** Fixed for the verdict line, which now reads `✅ Third Party Review: no defects found.` in the prompt, the notice body that quotes it, and the test fixture. The workflow name and the error title still say "GLM".

### Old-header notices are folded rather than skipped
**Location:** `.github/workflows/review.yml` (`head -n 1` anchor)
**Severity:** Minor
**Category:** Reliability

**Problem:** A re-run attempt whose tracking comment already holds a notice with the old "GLM review did not complete" header won't match the new anchor.

**Impact:** The old notice is folded into the collapsed "What this comment held" block. Nothing is lost.

**Decision:** Accepted.

---

## Positive Findings ✅

- The key is never interpolated into a `run:` script. It reaches the shell only through `env:`, and `base_url` is used only in `env:` too, so a caller-supplied value can't inject shell commands.
- Callers of `@main` that pass `ZHIPU_API_KEY` keep working, because every place that reads the key uses the `||` fallback, and a test pins that.
- A missing key skips the review step, and the notice names the key instead of falling into the "killed / timeout" branch. A test checks the branch order.
- The permissions and the concurrency group are unchanged, so the #9 workflow-call guard still passes.
