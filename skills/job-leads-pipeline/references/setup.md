# First-time setup

Installation gives the agent instructions and tools. It does **not** give it permission to read Gmail, send mail, or run every day.

## Check the machine

The **skill folder** is the directory containing `SKILL.md`, `requirements.txt`, and `scripts/`. A downloaded repository puts it at `skills/job-leads-pipeline/`. Codex and Claude may install it elsewhere; in that case, find the installed `SKILL.md` and use its parent folder's **absolute path** for every script and dependency command. The private candidate project is separate and is never the command's starting point.

Use Python 3.10 or newer. From a downloaded repository's top folder, install packages into a private Python environment with `python -m pip install -r skills/job-leads-pipeline/requirements.txt`. Then run `python skills/job-leads-pipeline/scripts/doctor.py --host codex` or use `--host claude`. With an installed copy, replace `skills/job-leads-pipeline/` in those commands with the absolute skill-folder path; quote it if it has spaces. After creating the candidate project, add `--project-root` with its exact absolute path. Doctor is read-only. Its token-store check inspects the active backend; Gmail setup stops if that backend is not an approved OS credential store. Its Gmail permission and scheduler results remain **not checked** until real permission and schedule tests are done. Do not call them working based on installed Python packages.

## Gmail

Read [Gmail setup](gmail.md). First inspect the host's connected Gmail tool. A text-only send or a send whose PDF bytes cannot be proved is not enough. If exact attachment send and Sent-mail verification are unavailable, use the guided personal Gmail API path. Google consent is completed by the user in their own browser; the skill never asks them to paste a password or token into chat. Keep OAuth client and tokens in an operating-system protected location outside the candidate project and Git checkout.

## Schedule

Ask for the candidate's IANA time zone, local send time, and whether weekends are included. Show the exact schedule before creating it. Keep it paused while setup and a separately approved test are incomplete. The scheduled prompt names only the private project path, daily mode, local date, and `$job-leads-pipeline` (or the installed Claude skill command).

From the repository's top folder, check the chosen wall-clock time with `python skills/job-leads-pipeline/scripts/check_schedule.py --timezone IANA_ZONE --time HH:MM` (add `--weekdays-only` if requested). For an installed copy, use the absolute path to `scripts/check_schedule.py` inside the skill folder. If it reports a missing or repeated daylight-saving time, ask the candidate for a different time. This check previews the next year; it does not create a task.

- In Codex, use its recurring automation feature attached to the machine that can access the private project and Gmail authorization.
- In Claude Code, use a **Desktop local scheduled task** for a persistent daily run. The computer must be on. The terminal `/loop` is session-scoped and is not the daily setup; a cloud routine gets a fresh checkout without the private local project. See [Claude's scheduling guide](https://code.claude.com/docs/en/scheduled-tasks).

Before enabling a live daily schedule, prove that it uses the chosen local time across a daylight-saving change, can read the exact private project, and honors paused status and existing receipts. Ask for explicit candidate approval of sender, recipient, cadence, candidate profile, and activation. A one-time test has a separate one-send approval and leaves the recurring schedule paused.

## If something goes wrong

- **No job links pass:** do not claim a role is live. Keep the failed evidence and report what could not be checked.
- **A résumé fact is uncertain:** ask the candidate; do not invent it. Mark that résumé unavailable until the fact is resolved.
- **Gmail times out after submit:** do not press send again. Check Sent mail and reconcile the claim and receipt first.
- **The computer was off:** inspect the missed run. Do not send a catch-up email without current approval.
- **Want to stop:** pause the private project and the host schedule. Preserve receipts so restart cannot send a duplicate.
