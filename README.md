# Job Leads Pipeline

A job search is hard enough. This skill helps you find real openings that fit, makes a checked one-page résumé for each strong match, and can email you one simple daily note. **You stay in charge.** It does not apply for you or contact employers.

The code is public. Your résumé, email, job history, and Gmail access stay in a separate private folder on your computer. Installing the skill does **not** turn on email or a daily schedule.

## Start here

Give this link to Codex or Claude Code:

`https://github.com/anmancini90/job-leads-pipeline`

Then say:

> Install the `job-leads-pipeline` skill from this repository. Start the job-search onboarding with me. Keep email and the daily schedule paused until I approve them.

The agent will ask about your résumé, the work you want, where you want to work, pay, and dealbreakers. A LinkedIn or portfolio link is optional. It will show you what it understood and let you fix mistakes. It will also show a few example jobs so you can teach it what “good fit” means to you.

Next, it will help you choose a **private folder that is not inside Git or this repository**. That folder is where your personal information goes. You will then connect Gmail, run a test you approve, and choose a daily time. Nothing sends until you approve the exact sender, recipient, and schedule.

## Install in Codex

Ask Codex to use its skill installer on the `skills/job-leads-pipeline` folder in this repository. The full folder link is:

`https://github.com/anmancini90/job-leads-pipeline/tree/main/skills/job-leads-pipeline`

Then say: `Use $job-leads-pipeline to onboard me.` You can also ask it to run a manual search, check status, pause, or resume later. Codex may ask you to restart so it can see the newly installed skill.

## Install in Claude Code

For the shortest `/job-leads-pipeline` command, ask Claude Code to install `skills/job-leads-pipeline` from this repository as a **personal skill** at `~/.claude/skills/job-leads-pipeline/`. Then run `/job-leads-pipeline`.

Claude Code's plugin marketplace is another choice. Run these commands in Claude Code:

```text
/plugin marketplace add anmancini90/job-leads-pipeline
/plugin install job-leads-pipeline@job-leads-tools
```

With the plugin install, the command is `/job-leads-pipeline:job-leads-pipeline` because Claude Code adds the plugin name. You can also just say “Use the job leads skill to onboard me.” See [Claude Code's plugin guide](https://code.claude.com/docs/en/plugin-marketplaces).

## What setup still needs you

The command examples in this repository assume you are in a downloaded copy's **top folder**, the one with this README. If you installed the skill without downloading the repository, ask your agent to find the installed `SKILL.md` and use absolute paths to the `requirements.txt` and `scripts/` beside it. Your private candidate folder is a different place; do not run relative skill paths from there.

- A computer with Python 3.10 or newer, internet access, and the packages in [requirements.txt](skills/job-leads-pipeline/requirements.txt). From the repository's top folder, the agent can run `python -m pip install -r skills/job-leads-pipeline/requirements.txt`, then `python skills/job-leads-pipeline/scripts/doctor.py --host codex` (or `--host claude`) to check what is missing.
- A Gmail account you own. Your agent will check whether its Gmail connection can really attach and verify the exact PDF files. If it cannot, you will follow the [Gmail setup guide](skills/job-leads-pipeline/references/gmail.md) to approve your own Gmail API connection in Google's browser screen. Do not paste a password or token into chat.
- A local daily task in Codex or Claude Code Desktop. Claude Code's `/loop` stops with its session, so it is not the durable daily choice. Your computer must be on for a Desktop task. [Scheduling details](skills/job-leads-pipeline/references/setup.md)
- Your approval for one controlled test email, then a separate approval for recurring delivery. The skill remains paused until these checks pass.

Google may require a personal Cloud project, an OAuth consent screen, and a desktop app client. If an OAuth app stays in Google's *Testing* state, its refresh token can expire after seven days. The setup guide explains the durable personal-use path. [Google OAuth guidance](https://developers.google.com/identity/protocols/oauth2)

## How it works

**Like you are five:** Think of a helper with a notebook. You tell it what job you want. It looks for open jobs, throws away the ones that do not fit, checks that the good ones are still open, and makes a résumé using only things you really did. Another helper checks the résumé. When you say it is okay, the helper puts the jobs and résumés in one envelope for you. It writes down that it sent the envelope so it cannot send it twice.

**Technical version:** One private profile holds confirmed preferences and résumé facts. Each run searches public sources, deduplicates and scores roles, validates exact job links, freezes source hashes, drafts role-specific PDFs, gets an independent factual approval, checks PDF text and appearance, then freezes an email and its exact attachments. Lifecycle approval and an atomic claim guard the send. Gmail confirmation produces an immutable receipt and ledger event. An ambiguous send is reconciled against Sent mail before any retry. See [daily résumé flow](skills/job-leads-pipeline/references/daily-resumes.md) and [delivery guards](skills/job-leads-pipeline/references/delivery-and-run-guards.md).

## Safety and control

The skill never sends a job application, creates an employer account, or writes to a recruiter. It never makes up experience or uses protected traits to rank jobs. A failed or uncertain job link blocks that lead set. A missing fact blocks the résumé claim. A prior success receipt blocks another email for that local day. You can pause or revoke delivery without deleting the history that prevents duplicates.

If Gmail says “maybe sent” after a timeout, the skill stops. Do not retry until Sent mail and the saved claim are checked. If your computer misses a run, do not send a catch-up note without current approval.

## If you want to help

Open an issue with a **made-up example**, never a real résumé, token, email, job-search history, or receipt. The reusable code is MIT licensed; see [LICENSE](LICENSE). Tests use synthetic data. The [skill entry point](skills/job-leads-pipeline/SKILL.md) explains how agents should use this package.
