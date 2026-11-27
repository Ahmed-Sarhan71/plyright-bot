# apply.py: Wuzzuf auto-apply (standalone)

Reads job URLs from `jobs.txt`, opens each Wuzzuf job with Playwright, clicks Apply, answers the screening
questions from `profile.json` with ONE model call, and submits. Not linked to the email apply bot.

## Setup

```bash
uv venv && source .venv/bin/activate        # or: python -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt          # or: pip install -r requirements.txt
playwright install chromium
```

On Arch, if Chromium will not start, install your system Chromium and point to it:
`export CHROMIUM_PATH=/usr/bin/chromium`

Files to add next to `apply.py`:

| file | what |
|---|---|
| `cv.pdf` | your resume (uploaded only if a form asks for a file) |
| `profile.json` | your details. Edit every `FILL_ME`; the script refuses to run until none are left |
| `jobs.txt` | one Wuzzuf job URL per line (`https://wuzzuf.net/jobs/p/...`); blank lines and `#` comments are ignored |
| `.env` or environment | `DEEPSEEK_API_KEY` (model: `DEEPSEEK_MODEL`, default `deepseek-flash`) or `OPENAI_API_KEY` (`OPENAI_MODEL`, default `gpt-4o-mini`) |

## Run

```bash
python apply.py --login                  # once: log in to Wuzzuf by hand in the window, press Enter. Saves wuzzuf_state.json
python apply.py --dry-run --headed       # FIRST RUN: watch it fill the form, it does not click Submit
python apply.py                          # apply for real (headless)
python apply.py --max 5                  # process at most 5 jobs this run (default 20)
```

Your password is never stored: `--login` saves only the browser session. If the session expires, jobs are
skipped with "login required": run `--login` again.

## What happens to each URL

1. Not a `wuzzuf.net/jobs/p/...` URL: `unsupported`. Already applied to (in `applications.db`): skipped.
2. Page does not load in 60 s: one retry, then skipped.
3. CAPTCHA or login page: skipped. A required question the profile cannot answer: skipped, and the field is logged.
4. Otherwise: fill, upload the CV if asked, click Submit, then `applied` if a confirmation message shows or the URL
   changes, `failed` if inline errors appear (their text is logged).
5. Always: `screenshots/<job id>.png` and a row in `applications.db`
   (`url, company, status, reason, answers_json, screenshot_path, timestamp`).

A random 5-15 s pause separates jobs. `--max` counts the jobs processed in a run (not the ones skipped as already
applied or unsupported). Dry-run rows are logged but do not count as applied, so a real run still applies.

A retry never happens after Submit was clicked, so one job cannot be sent twice.

## Know the limits

- Built and tested against mock pages that imitate Wuzzuf's flow (Apply, screening questions, Submit), not the live
  site. Run `--dry-run --headed` first and check the screenshots before applying for real.
- It reads native form controls (input, select, textarea, radio, checkbox). Custom dropdowns made of `div`s are not
  seen; if one is required, the job is skipped as "missing info".
- The model only knows `profile.json`. Fill it with true facts only: it is the source of every answer.
- Keep `profile.json`, `wuzzuf_state.json` and `applications.db` private. They are in `.gitignore`.
- Automated applying may be against the site's terms and can get an account flagged. Use `--max` and keep it small.
