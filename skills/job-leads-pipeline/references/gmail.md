# Connect Gmail and send one frozen daily message

The skill can write a ready-to-send email with exact PDF attachments. Gmail
still needs your permission. The skill never asks for your Gmail password.

The commands below assume you are in a downloaded repository's **top folder**
(the one with `README.md`). If Codex or Claude installed the skill without a
downloaded repository, find the installed `SKILL.md` and replace
`skills/job-leads-pipeline/` with that skill folder's **absolute path**. Quote
paths containing spaces. The private candidate project is a separate folder.

## Set up your own Gmail connection

1. In your own Google Cloud project, turn on the Gmail API. Create an OAuth
   consent screen and a **Desktop app** OAuth client. Download its JSON file.
   Keep that file outside the public skill folder and outside any Git repo.
2. Install the skill's Python requirements in a local environment. From the
   downloaded repository's top folder, run:

   `python skills/job-leads-pipeline/scripts/doctor.py --host codex`

   Use `--host claude` for Claude Code. Look for `secure_token_store: ok`.
   It must name Windows Credential Manager, macOS Keychain, or a Linux
   desktop secret service. Having the `keyring` package installed is not
   enough: a file-based, fallback, or unknown backend blocks Gmail setup.
   Fix that setup first. The skill never falls back to a plain text token file.

3. When the token store is safe, run:

   `python skills/job-leads-pipeline/scripts/gmail_transport.py oauth-connect --client-secrets PATH_TO_YOUR_DESKTOP_CLIENT.json --sender YOU@gmail.com`

4. A Google browser page opens. Choose the same Gmail account as the approved
   sender. The skill checks that address. Its access token and refresh token go
   into your operating system's keyring, not into the candidate project or Git.
5. Keep the candidate's daily delivery paused until the search settings,
   sender, recipient, time, and a controlled test are approved. The command
   below is only for a fully staged, validated daily manifest and should be
   run by the skill after those approvals:

   `python skills/job-leads-pipeline/scripts/gmail_transport.py send-claimed --root PRIVATE_CANDIDATE_PROJECT --manifest outputs/FROZEN/daily-resume-manifest.json --confirm-send`

   Replace `PRIVATE_CANDIDATE_PROJECT` with the exact absolute private project
   path. The `--manifest` path is relative to **that private project**, not to
   the repository or installed skill folder. The skill must substitute its real
   frozen manifest path; `FROZEN` is an example, not a folder to create.

Never paste OAuth JSON, access tokens, refresh tokens, or a real résumé into
the public repository, an issue, or a scheduled prompt.

## What the send command protects

It reloads and validates the frozen request and every PDF hash, builds a
multipart Gmail message, and checks the decoded PDF bytes. It then asks the
project's delivery guard for the one-send claim. Only a successful claim lets
it call Gmail. A successful Gmail response is saved as a send receipt before
the run ledger is completed; a read-only Sent-mail check compares the decoded
attachments afterward. A saved receipt prevents another email that day.

A timeout, lost connection, missing Gmail IDs, or uncertain provider result
never triggers an automatic resend. Preserve the claim and compare Sent mail,
the receipt, and the ledger by hand. A clear Gmail rejection is marked for a
possible later retry, but the guard must issue a new attempt token for the
same exact frozen manifest. Do not delete claim files to force another send.

## Important Google account details

- Use a personal OAuth client per user. A shared client in this public repo
  would expose its secret and create an unsafe shared dependency.
- Gmail asks for `gmail.send` and `gmail.readonly`: one to send the message,
  one to verify the sent message and support safe reconciliation. Google may
  show an unverified-app warning for a personal project.
- Google's **Testing** consent-screen mode can make a Gmail refresh token
  expire after seven days. Follow Google's current OAuth guidance for your
  personal project before relying on unattended daily runs.
- Revoking the app in your Google Account stops future access. Pausing the
  candidate project stops the skill's scheduled sends; do both if you want
  to disconnect completely.

Official setup: [Gmail API Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python), [Gmail send format](https://developers.google.com/workspace/gmail/api/guides/sending), [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [OAuth token guidance](https://developers.google.com/identity/protocols/oauth2).
