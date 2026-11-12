# AGENTS.md (grok-station)

## What this is
Raspberry Pi voice station: wake word (wake/), display app (app/),
relay (relay/), admin web UI (admin/), helper scripts (scripts/).
Release notes and versioning: docs/versioning.md. Bot setup flow:
docs/bot-lifecycle.md.

## Workflow
- One task = one branch = one PR. Never push to main. Never force push.
- Bump VERSION, scripts/update-manifest.txt and deploy pins together;
  run tools/release-check.sh before saying done.
- Document user-visible changes in README.md.
- No phone layouts unless the task says so.

## Commits
- No trailers of any kind (no Co-authored-by). Before the first push:
  `git log --format=%B main..HEAD | grep -ci co-authored` must print 0.
- No personal names or hostnames in commit messages.
- No em dashes in code, docs or commit messages.

## Tests
- Every bug fix ships with a test that fails on the old code and passes
  on the new code. Report both runs.
- Layout tests measure painted pixels at 1280x800 and 2560x1706,
  not font boxes.
- Chrome tests use tests/chrome_harness.py (own temp profile,
  20 s cap, cleaned up on exit). Never leave Chrome processes or
  temp profiles behind.
- Run the full non-Chrome suite before opening or updating the PR.

## Security
- No secrets, tokens or webhook URLs in code, tests, logs or output.
- Anything run as root (scripts/gs-usb-reset, sudoers) needs strict
  input checks and no env overrides; call it out in the PR body.
- New dependency: one-line reason in the PR body; run pip-audit or
  npm audit on any lockfile change.

## Progress posts
- First shell command: Slack "start" post to $SLACK_WEBHOOK_DEVELOPMENT
  with `curl -s -o /dev/null -w '%{http_code}'`. Never echo the URL.
- Post only start, blocker, PR opened, done. Include the HTTP codes
  in the final report.

## Out of scope
- Do not install or deploy to the Pi; the owner installs releases.
- Do not merge the PR.
