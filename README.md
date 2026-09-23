# Job Leads Pipeline

Job Leads Pipeline helps you find open jobs that fit. It can make a checked, one-page résumé for a match and send one daily email with the results. It works in Codex and Claude Code. It never applies for you or contacts employers.

Your résumé and search notes stay in a private folder, not in this public repo. If you use the Gmail API setup, its tokens stay in an approved secure store on your computer. The skill never asks for your Gmail password. Installing it does **not** start emails or a daily schedule.

## How it works

1. Tell the skill what work you want. Share your résumé, then choose your roles, pay, location, and dealbreakers. Check the facts and job rules before they are saved.
2. It finds jobs and opens each link to check that the post is still live. It shows you why a job may fit.
3. For each strong match, it makes a one-page PDF résumé from facts you confirmed. A separate review checks the words and the PDF. If a résumé fails, you can still see the job, but the unchecked PDF is not sent.
4. After you connect Gmail, approve a test, and turn on a daily schedule, it can email the checked jobs and PDFs to you. Its send record blocks an automatic second email from the skill that day.

If Gmail may have sent an email but does not say for sure, the skill stops. It does not try again until someone checks Sent mail. For the full steps, see the [daily résumé flow](skills/job-leads-pipeline/references/daily-resumes.md) and [send safety rules](skills/job-leads-pipeline/references/delivery-and-run-guards.md).

## Start here

Give this link to Codex or Claude Code:

`https://github.com/anmancini90/job-leads-pipeline`

Then say:

> Install the `job-leads-pipeline` skill from this repository. Start the job-search onboarding with me. Keep email and the daily schedule paused until I approve them.

It asks for your résumé, the jobs you want, where you want to work, pay, and jobs to skip. LinkedIn and portfolio links are optional. It shows you the facts it found and some sample jobs. You can fix mistakes before it saves your search.

It also helps you choose a **private folder outside Git and this repository**. That is where your personal files go. You can then connect Gmail, approve a test, and choose a daily time. Nothing sends until you approve the sender, the inbox, and the schedule.

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
- A Gmail account you own. Your agent will check that its Gmail connection can attach the exact PDFs and read back the sent email. If it cannot, follow the [Gmail setup guide](skills/job-leads-pipeline/references/gmail.md) to approve your own Gmail API connection in Google's browser screen. Do not paste a password or token into chat.
- A local daily task in Codex or Claude Code Desktop. Claude Code's `/loop` stops with its session, so it is not the durable daily choice. Your computer must be on for a Desktop task. [Scheduling details](skills/job-leads-pipeline/references/setup.md)
- Your approval for one controlled test email, then a separate approval for recurring delivery. The skill remains paused until these checks pass.

Google may require a personal Cloud project, an OAuth consent screen, and a desktop app client. If an OAuth app stays in Google's *Testing* state, its refresh token can expire after seven days. The setup guide explains the durable personal-use path. [Google OAuth guidance](https://developers.google.com/identity/protocols/oauth2)

## Safety

The skill emails only you. It never applies for jobs, sends your résumé to an employer, or writes to a recruiter. Résumé claims must come from checked sources; unsupported claims and unchecked PDFs are not sent. The skill does not use age, race, or other protected traits to rank jobs.

If a job link cannot be checked, that set of jobs is not sent. If a résumé fact is in doubt, the skill asks you or leaves it out. If the résumé still cannot pass review, its PDF is left out. A past send blocks another automatic email from the skill that day. You can pause emails at any time; the send history stays in place to help stop duplicates.

If Gmail gives no clear answer after a send, the skill stops. Check Sent mail before any retry. If your computer misses a run, it will not send a late email without your approval.

## About me

I'm Ant Mancini. This grew out of the job searches my wife and I were doing for ourselves. We each use a daily version of it. I put the reusable parts here so you can spend less time sorting job posts and more time deciding which ones are worth your effort. You can learn more about me at [antmancini.com](https://antmancini.com).

## If you want to help

Open an issue with a **made-up example**, never a real résumé, token, email, job-search history, or receipt. The reusable code is MIT licensed; see [LICENSE](LICENSE). Tests use synthetic data. The [skill entry point](skills/job-leads-pipeline/SKILL.md) explains how agents should use this package.
