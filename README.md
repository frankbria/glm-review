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
       # Only review substantial changes (5+ files OR 20+ changed lines)
       if: |
         github.event.pull_request.changed_files >= 5 ||
         github.event.pull_request.additions >= 20 ||
         github.event.pull_request.deletions >= 20
       uses: frankbria/glm-review/.github/workflows/review.yml@main
       permissions:
         contents: read
         pull-requests: write
         issues: write
         id-token: write
       secrets:
         ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
   ```

## Options

| Input | Default | Purpose |
|-------|---------|---------|
| `model` | `glm-5.2` | GLM model id on the z.ai Anthropic-compatible endpoint |
| `extra_instructions` | *(empty)* | Repo-specific review instructions appended to the base prompt |

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
- Inline comments use the action's built-in `github_inline_comment` MCP tool; the model is restricted to read-only `gh pr` commands otherwise.
- The review prompt lives inline in [`.github/workflows/review.yml`](.github/workflows/review.yml) — edit it there and every consuming repo picks up the change (callers pin `@main`).
