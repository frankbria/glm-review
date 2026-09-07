# glm-review

CodeRabbit-style bug-hunting PR reviewer powered by the [z.ai GLM coding plan](https://docs.z.ai/scenario-example/develop-tools/claude) (GLM-5.2) through the [claude-code-action](https://github.com/anthropics/claude-code-action) harness.

It posts **inline comments on the exact defective lines** with severity tags, concrete failure scenarios, and committable ` ```suggestion ` blocks — plus one summary comment per review. It reports only confirmed defects (bugs, security, data loss, races); style/architecture/test-coverage review is deliberately out of scope so it composes with a general-purpose reviewer.

## Install in a repo

1. Set the secret (once per repo):

   ```sh
   gh secret set ZHIPU_API_KEY --repo <owner>/<repo>
   ```

2. Add `.github/workflows/glm-review.yml`:

   ```yaml
   name: GLM Review
   on:
     pull_request:
       types: [opened, synchronize]
       paths-ignore:
         - "**/*.md"
         - ".gitignore"

   jobs:
     review:
       # Skip bot-authored PRs, then only review substantial changes
       # (5+ files, or 20+ additions, or 20+ deletions)
       if: |
         github.event.pull_request.user.type == 'User' &&
         (github.event.pull_request.changed_files >= 5 ||
          github.event.pull_request.additions >= 20 ||
          github.event.pull_request.deletions >= 20)
       uses: frankbria/glm-review/.github/workflows/review.yml@main
       permissions:
         contents: read
         pull-requests: write
       secrets:
         ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
   ```

   > **Those two permissions are the whole set — but only on `@main` or a pin at
   > `5003ffb` or newer.** Neither `id-token: write` nor `issues: write` is
   > required: the reviewer passes an explicit `github_token` so the OIDC →
   > App-token exchange never runs, and it only ever comments on pull requests,
   > which `pull-requests: write` covers.
   >
   > The two were dropped one PR apart, so the floor is the *later* of them:
   > `id-token: write` went at `fc16d4f` (merged as `5864495`) but `issues: write`
   > survived until `5003ffb` (merged as `00c986e`). Pinning at `5864495` with this
   > block under-grants `issues: write` and startup-fails.
   >
   > The two directions are not symmetric, so keep the pin and this block in
   > step. Granting *more* than the callee declares is harmless, so callers still
   > listing either permission keep working — drop them on the next edit. Granting
   > *less* is fatal: GitHub refuses to start a run whose caller grants less than
   > the called job requests, and it refuses before any job exists, so every run
   > is a `startup_failure` with no log to explain it. Copying this block onto a
   > pin older than `5003ffb` (whose job still declared `issues: write`) means the check
   > never runs at all — that is [#9](https://github.com/frankbria/glm-review/issues/9),
   > where one repo sat at 20/20 `startup_failure`.

   > **The bot guard is not optional if the repo uses Dependabot.**
   > `claude-code-action` refuses any run whose actor is not a `User` and fails
   > the step to say so, which paints a PR red that was never reviewed. The
   > action's `allowed_bots` input is not the fix here: GitHub runs
   > Dependabot-triggered `pull_request` workflows with a read-only token and the
   > separate Dependabot secret store, so `ZHIPU_API_KEY` arrives empty and the
   > summary comment could not be posted regardless. Skipping reports these grey
   > and honest. Testing `user.type` rather than a bot name covers all of them.

## Options

| Input | Default | Purpose |
|-------|---------|---------|
| `model` | `glm-5.2` | GLM model id on the z.ai Anthropic-compatible endpoint |
| `extra_instructions` | *(empty)* | Repo-specific review instructions appended to the base prompt |
| `upload_transcript` | `true` | Keep the agent's turn-by-turn execution log as a 14-day workflow artifact |
| `debug_full_output` | `false` | Log every SDK message to the workflow log — the only evidence channel that survives a step timeout |

```yaml
       uses: frankbria/glm-review/.github/workflows/review.yml@main
       with:
         extra_instructions: |
           This repo is a FastAPI service; pay extra attention to async misuse.
       secrets:
         ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
```

## How it works

- `review.yml` runs `anthropics/claude-code-action` with `ANTHROPIC_BASE_URL` pointed at `https://api.z.ai/api/anthropic`, so all usage bills to the GLM coding plan — no Anthropic charges.
- Inline comments use the action's built-in `github_inline_comment` MCP tool; the model is restricted to read-only `gh pr` commands otherwise. `--allowedTools` is *additive* to the action's own defaults rather than a replacement for them, so the write verbs it silently re-adds (`Write`, `Edit`, `git add|commit|rm`) are denied explicitly via `--disallowedTools`.
- Each run uploads its execution transcript as a workflow artifact (`glm-review-transcript-<run>`, 14-day retention). The workflow log records tool calls but not model turns, so this is the only record of *why* a review said what it said. The artifact inherits the calling repo's visibility and contains the PR diff plus any file the reviewer opened — set `upload_transcript: false` where that is not acceptable.
- Four limits bound a review, and they nest deliberately: a 20-minute per-request `API_TIMEOUT_MS` < a 35-minute timeout on the review *step* < a 40-minute job timeout, with `--max-turns 60` inside all of them. Each inner limit exists so the outer one never has to fire, because each outer limit destroys more evidence than the one inside it. The turn cap is the one meant to bind — turns cost roughly 22s each, and measured 60-turn reviews on this repo ran 14m10s and 25m13s, so the step clock sits above the spread rather than beside it. The clock is on the step, not the job, on purpose: a job-level timeout cancels every remaining step, `if: always()` included, so nothing downstream of it could report what happened.
- The transcript covers a review that *errored*, not one that was *killed*. `claude-code-action` writes the execution file only after its message loop returns, so a step timeout produces no artifact — and by default the workflow log carries only the `init` and `result` messages, so a timed-out run leaves no evidence of where the time went at all. Two things address that: `API_TIMEOUT_MS` is held below the step timeout so a stuck request fails as an error (which does write a transcript) rather than as a wall-clock kill, and `debug_full_output: true` streams every message to the workflow log, which is written incrementally and survives the kill.
- A review that does not finish rewrites its own tracking comment into an explicit **"GLM review did not complete"** notice naming the reason (turn cap, step timeout, mid-run error). The "in progress" stub is never the final state, so *no review* can never be mistaken for *no defects found*. Whatever the comment already held is folded into a collapsed block rather than discarded: a run can post findings and *then* be killed, and nothing in the step outcome distinguishes that from an untouched stub.
- The review prompt lives inline in [`.github/workflows/review.yml`](.github/workflows/review.yml) — edit it there and every consuming repo picks up the change (callers pin `@main`).
