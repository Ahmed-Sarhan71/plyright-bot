"""apply.py - fill in and submit Wuzzuf job applications with Playwright.

Standalone: it is not linked to the apply bot. See README.md for setup.

    python apply.py --login                 # once: log in by hand, the session is saved
    python apply.py --dry-run --headed      # fill the forms, watch, do NOT submit
    python apply.py                         # apply for real
"""

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from openai import OpenAI
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

STATE_FILE = "wuzzuf_state.json"  # your logged-in session (cookies). Keep it private.
DB_FILE = "applications.db"
SCREENSHOT_DIR = "screenshots"
PAGE_TIMEOUT_MS = 60_000  # a page that takes longer than this is skipped
ACTION_TIMEOUT_MS = 20_000
SUCCESS_RE = re.compile(
    r"(application (has been )?(sent|submitted|received)|successfully (applied|submitted)|you('ve| have) applied|thank you for applying)",
    re.I,
)

# Runs inside the page. Lists every visible, fillable control and tags each with data-fid so we can find it again.
EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const textOf = id => { const e = document.getElementById(id); return e ? clean(e.innerText) : ''; };
  const labelOf = el => {
    const aria = el.getAttribute('aria-label'); if (aria) return clean(aria);
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return clean(l.innerText); }
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = clean(by.split(/\s+/).map(textOf).join(' ')); if (t) return t; }
    return '';
  };
  const textBefore = start => {
    let n = start;
    for (let i = 0; i < 4 && n && n.parentElement; i++, n = n.parentElement) {
      let p = n.previousElementSibling;
      while (p) {
        // prefer a <label>, and skip siblings that only hold buttons/inputs ("Use Previous answers")
        const lb = p.matches('label') ? p : p.querySelector('label');
        const t = clean(lb ? lb.innerText : (p.querySelector('button,a,input,select,textarea') ? '' : p.innerText));
        if (t && t.length < 300) return t;
        p = p.previousElementSibling;
      }
    }
    return '';
  };
  const questionOf = el => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return clean(lg.innerText); }
    const grp = el.closest('[role=radiogroup],[role=group]');
    if (grp) { const a = grp.getAttribute('aria-label') || textOf(grp.getAttribute('aria-labelledby') || ''); if (a) return clean(a); }
    return textBefore(el.closest('label') || el);
  };

  // scan the whole page: picking "the form with the most controls" grabbed Wuzzuf's search
  // form instead of the questions form. Header/nav/footer controls are skipped below anyway.
  const fillable = root => Array.from(root.querySelectorAll('input, select, textarea'));
  const skip = ['hidden', 'submit', 'button', 'reset', 'image', 'search'];
  const out = []; const groups = {}; let n = 0;
  for (const el of fillable(document.body)) {
    const t = (el.tagName === 'INPUT' ? (el.type || 'text') : el.tagName).toLowerCase();
    if (skip.includes(t) || el.disabled || el.closest('header, nav, footer')) continue;
    const box = (t === 'radio' || t === 'checkbox') ? (el.closest('label') || el.parentElement) : el;
    if (t !== 'file' && !shown(el) && !shown(box)) continue;
    const id = 'f' + (n++);
    el.setAttribute('data-fid', id);
    const sel = '[data-fid="' + id + '"]';
    if (t === 'radio' || t === 'checkbox') {
      const key = t + ':' + (el.name || id);
      const optLabel = labelOf(el) || clean(el.value);
      if (!groups[key]) {
        groups[key] = { id, label: questionOf(el), type: t, options: [], required: false, selector: sel, option_selectors: {} };
        out.push(groups[key]);
      }
      const g = groups[key];
      g.options.push(optLabel); g.option_selectors[optLabel] = sel;
      if (el.required || el.getAttribute('aria-required') === 'true') g.required = true;
      continue;
    }
    const label = labelOf(el) || questionOf(el) || clean(el.placeholder || el.name);
    const f = { id, label, type: t, options: null, required: !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label)), selector: sel, option_selectors: null };
    if (t === 'select') f.options = Array.from(el.options).filter(o => o.value !== '').map(o => clean(o.text));
    out.push(f);
  }
  for (const g of out) {
    if (g.type === 'checkbox' && g.options.length === 1) { g.type = 'checkbox-single'; g.options = null; }
    if (/\*\s*$/.test(g.label || '')) g.required = true;
  }
  return out;
}
"""

ERRORS_JS = r"""
() => {
  const sel = '[role=alert], .error, .errors, .invalid-feedback, .field-error, [aria-invalid="true"]';
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const t = (el.innerText || el.getAttribute('aria-errormessage') || '').replace(/\s+/g, ' ').trim();
    if (t && shown(el) && !out.includes(t)) out.push(t);
  }
  return out;
}
"""


# ---------------------------------------------------------------- database

def open_db():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "CREATE TABLE IF NOT EXISTS applications (url TEXT, company TEXT, status TEXT, reason TEXT,"
        " answers_json TEXT, screenshot_path TEXT, timestamp TEXT)"
    )
    return con


def already_applied(con, url):
    return con.execute("SELECT 1 FROM applications WHERE url = ? AND status = 'applied' LIMIT 1", (url,)).fetchone() is not None


def log_result(con, url, result, screenshot):
    con.execute(
        "INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            url,
            result.get("company"),
            result["status"],
            result.get("reason", ""),
            json.dumps(result.get("answers") or {}, ensure_ascii=False),
            screenshot,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    con.commit()


# ---------------------------------------------------------------- the model

def make_llm():
    """Returns (client, model, extra). DeepSeek if DEEPSEEK_API_KEY is set, otherwise OpenAI."""
    if os.environ.get("DEEPSEEK_API_KEY"):
        client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
        return client, os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"), {"thinking": {"type": "disabled"}}
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAI(), os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), None
    return None


SYSTEM_PROMPT = """You fill in a job application form for the candidate described in PROFILE. Reply with JSON only.

Rules:
- Answer EVERY field. A null answer is a failure.
- Be positive: if asked whether the candidate has a skill, tool, qualification or experience, answer Yes (or true for checkbox-single).
- For "do you have" / "are you" questions, the value must be exactly one of the listed options, copied as written.
- Numbers (salary, years, notice): use PROFILE when it has one. If it does not, pick a plausible number consistent with PROFILE's numbers (e.g. a current salary a little below expected_salary).
- For open-ended questions (textarea, or a long free-text question) write 3 to 5 sentences, in the first person, consistent with PROFILE and the positive tone. Mention the asked-about skill as one the candidate has.
- For a field with options (select, radio), the value must be exactly one of the listed options, copied as written. For checkbox, a list of options. For checkbox-single, true or false. If no option fits, pick the closest.
- Skip file fields: they are not in the list.
- Only exception: gender, religion, marital status, disability - answer null there.

Output format: {"answers": {"<field id>": <value or null>, ...}} with one entry for every field id given."""


def ask_model(fields, profile, llm):
    """ONE call: the field list + the profile in, a value (or null) per field out."""
    client, model, extra = llm
    shown = [{k: f[k] for k in ("id", "label", "type", "options", "required")} for f in fields]
    kwargs = {"extra_body": extra} if extra else {}
    reply = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=3000,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "PROFILE:\n" + json.dumps(profile, ensure_ascii=False) + "\n\nFIELDS:\n" + json.dumps(shown, ensure_ascii=False)},
        ],
        **kwargs,
    )
    raw = json.loads(reply.choices[0].message.content)
    return clean_answers(fields, raw.get("answers", raw))


def clean_answers(fields, answers):
    """Keep only answers that fit their field: an option must really be one of the options."""
    out = {}
    for f in fields:
        v = answers.get(f["id"]) if isinstance(answers, dict) else None
        if isinstance(v, str) and not v.strip():
            v = None
        opts = f["options"]
        if v is not None and f["type"] in ("select", "radio"):
            v = next((o for o in opts if isinstance(v, str) and o.strip().lower() == v.strip().lower()), None)
        elif v is not None and f["type"] == "checkbox":
            picked = v if isinstance(v, list) else [v]
            v = [o for o in opts if any(isinstance(p, str) and o.strip().lower() == p.strip().lower() for p in picked)] or None
        elif v is not None and f["type"] == "checkbox-single":
            v = bool(v)
        elif v is not None and not isinstance(v, str):
            v = str(v)
        out[f["id"]] = v
    return out


# ---------------------------------------------------------------- the page

def job_id(url):
    m = re.search(r"/jobs/p/([A-Za-z0-9]+)", url)
    return m.group(1) if m else re.sub(r"\W+", "_", url)[-40:]


def is_supported(url):
    # country-prefixed paths too: wuzzuf.net/saudi/jobs/p/...
    return bool(re.match(r"https?://(www\.)?wuzzuf\.net/(?:[a-z]{2,}/)?jobs/p/", url))


def blocker(page):
    """Returns 'captcha', 'login' or None."""
    if page.locator("iframe[src*='captcha' i], iframe[title*='captcha' i], .g-recaptcha, .h-captcha, [class*='captcha' i]").count():
        return "captcha"
    if re.search(r"/(login|signin|sign-in)\b", page.url) or page.locator("input[type='password']:visible").count():
        return "login"
    return None


def get_company(page):
    try:
        return page.locator("a[href*='/jobs/careers/']").first.inner_text(timeout=3000).strip() or None
    except PlaywrightError:
        return None


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeout:
        pass


# Wuzzuf serves Arabic pages for some countries: "قدّم على الوظيفة" = Apply for the job.
# Applying is two steps: "Apply" then "Complete your application" opens the questions.
APPLY_TEXT = re.compile(r"^\s*(?:apply\b|complete your application|قدّ?م)", re.I)
SUBMIT_TEXT = re.compile(r"^\s*(?:submit|send application|apply\b|قدّ?م|أرسل|إرسال)", re.I)


def js_click(loc):
    """Wuzzuf duplicates its buttons: one copy is parked off-viewport and never scrolls in, so
    locator.click waits forever. Scroll the right copy in and click its real coordinates."""
    page = loc.page
    page.add_style_tag(content="us-widget{display:none!important}")
    x, y = loc.evaluate_all("""els => {
        const seen = el => { const r = el.getBoundingClientRect();
            return r.width && r.height && r.bottom > 0 && r.top < innerHeight };
        const el = els.find(seen) || els[els.length - 1];
        el.scrollIntoView({block: 'center'});
        const r = el.getBoundingClientRect();
        return [r.x + r.width / 2, r.y + r.height / 2];
    }""")
    page.mouse.click(x, y)


def click_apply(page):
    for role in ("button", "link"):
        btn = page.get_by_role(role, name=APPLY_TEXT)
        if btn.count():
            js_click(btn)
            return True
    return False


def fill_field(page, f, value):
    t = f["type"]
    if t == "select":
        page.select_option(f["selector"], label=value)
    elif t == "radio":
        js_click(page.locator(f["option_selectors"][value]))
    elif t == "checkbox":
        for option in value:
            js_click(page.locator(f["option_selectors"][option]))
    elif t == "checkbox-single":
        if value:
            js_click(page.locator(f["selector"]))
    else:
        page.fill(f["selector"], value)


def find_submit(page):
    btn = page.get_by_role("button", name=SUBMIT_TEXT)
    return btn.last if btn.count() else None


def wait_for_outcome(page, url_before):
    """Applied if a confirmation message shows up or the URL changes; failed if inline errors appear."""
    end = time.time() + 15
    while time.time() < end:
        errors = page.evaluate(ERRORS_JS)
        if errors:
            return "failed", "form errors: " + " | ".join(errors)[:300]
        if page.url != url_before or SUCCESS_RE.search(page.inner_text("body")):
            return "applied", ""
        page.wait_for_timeout(500)
    return "failed", "no confirmation message and no URL change after Submit"


def read_fields(page, tries=12):
    """The questions page renders after the Apply click; networkidle fires too early. Poll."""
    for _ in range(tries):
        if f := page.evaluate(EXTRACT_JS):
            return f
        page.wait_for_timeout(500)
    return []


def run_job(page, url, args, profile, llm, progress):
    """One attempt. Returns {status, reason, company, answers}. progress['submitted'] flips once Submit is clicked."""
    page.goto(url, wait_until="domcontentloaded")
    settle(page)
    company = get_company(page)

    def done(status, reason="", answers=None):
        return {"status": status, "reason": reason, "company": company, "answers": answers}

    if (b := blocker(page)):
        return done("skipped", "login required (run: python apply.py --login)" if b == "login" else "captcha detected")
    if page.get_by_role("button", name=re.compile(r"^\s*(applied|you('ve| have) (already )?applied)\s*$", re.I)).count():
        return done("skipped", "already applied on the site")
    if not click_apply(page):
        return done("skipped", "no Apply button found")
    for _ in (1, 2):  # Apply sometimes does not register; click again if the questions never appear
        settle(page)
        if (b := blocker(page)):
            return done("skipped", "login required (run: python apply.py --login)" if b == "login" else "captcha detected")
        fields = read_fields(page)
        if fields or not click_apply(page):
            break
    uploads = [f for f in fields if f["type"] == "file"]
    fields = [f for f in fields if f["type"] != "file"]
    if not fields and not uploads and find_submit(page) is None:
        return done("skipped", "no application form found after clicking Apply")

    answers = ask_model(fields, profile, llm) if fields else {}
    missing = [f["label"] or f["id"] for f in fields if f["required"] and answers.get(f["id"]) is None]
    if missing:
        return done("skipped", "missing info: " + "; ".join(missing), answers)

    for f in uploads:
        page.set_input_files(f["selector"], str(args.cv))
    for f in fields:
        if answers.get(f["id"]) is not None:
            fill_field(page, f, answers[f["id"]])

    if args.dry_run:
        return done("dry-run", "filled, not submitted", answers)
    submit = find_submit(page)
    if submit is None:
        return done("failed", "no Submit button found", answers)
    url_before = page.url
    progress["submitted"] = True  # from here a retry could apply twice, so never retry
    js_click(submit)
    status, reason = wait_for_outcome(page, url_before)
    return done(status, reason, answers)


def apply_one(context, url, args, profile, llm):
    """Runs a job with at most one retry (only if the page failed before Submit was clicked), then saves a screenshot."""
    progress = {"submitted": False}
    result, shot = None, None
    for attempt in (1, 2):
        page = context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
        try:
            result = run_job(page, url, args, profile, llm, progress)
            break
        except (PlaywrightTimeout, PlaywrightError) as e:
            reason = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
            if progress["submitted"] or attempt == 2:
                result = {"status": "failed" if progress["submitted"] else "skipped", "reason": "page error: " + reason}
                break
            print(f"  page error ({reason}); retrying once")
        except Exception as e:  # model error, bad JSON...
            result = {"status": "failed", "reason": f"{type(e).__name__}: {str(e)[:200]}"}
            break
        finally:
            try:
                Path(SCREENSHOT_DIR).mkdir(exist_ok=True)
                path = Path(SCREENSHOT_DIR) / f"{job_id(url)}.png"
                page.screenshot(path=str(path), full_page=True)
                shot = str(path)
            except PlaywrightError:
                pass
            page.close()
    return result, shot


# ---------------------------------------------------------------- main

def read_jobs(path):
    seen, urls = set(), []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in seen:
            seen.add(line)
            urls.append(line)
    return urls


def launch(p, headed):
    kwargs = {"headless": not headed}
    if os.environ.get("CHROMIUM_PATH"):  # e.g. /usr/bin/chromium on Arch
        kwargs["executable_path"] = os.environ["CHROMIUM_PATH"]
    return p.chromium.launch(**kwargs)


def login(args):
    with sync_playwright() as p:
        browser = launch(p, headed=True)
        context = browser.new_context()
        context.new_page().goto("https://wuzzuf.net/login")
        input("Log in to Wuzzuf in the browser window, then press Enter here to save the session... ")
        context.storage_state(path=STATE_FILE)
        browser.close()
    print(f"saved {STATE_FILE}. Keep it private: it is your logged-in session.")


def main():
    ap = argparse.ArgumentParser(description="Apply to Wuzzuf jobs with Playwright.")
    ap.add_argument("--login", action="store_true", help="log in by hand once and save the session")
    ap.add_argument("--headed", action="store_true", help="show the browser (for debugging)")
    ap.add_argument("--dry-run", action="store_true", help="fill the forms but do not click Submit")
    ap.add_argument("--max", type=int, default=20, help="most jobs to process in one run (default 20)")
    ap.add_argument("--jobs", default="jobs.txt")
    ap.add_argument("--profile", default="profile.json")
    ap.add_argument("--cv", default="cv.pdf")
    args = ap.parse_args()

    if args.login:
        return login(args)

    for needed in (args.jobs, args.profile, STATE_FILE):
        if not Path(needed).exists():
            sys.exit(f"missing {needed}" + (" - run: python apply.py --login" if needed == STATE_FILE else ""))
    profile_text = Path(args.profile).read_text(encoding="utf-8")
    if "FILL_ME" in profile_text:
        sys.exit(f"{args.profile} still has FILL_ME placeholders: fill them in first")
    profile = json.loads(profile_text)
    llm = make_llm()
    if llm is None:
        sys.exit("set DEEPSEEK_API_KEY (or OPENAI_API_KEY) in the environment or in .env")

    urls = read_jobs(args.jobs)
    con = open_db()
    counts = {"applied": 0, "dry-run": 0, "skipped": 0, "failed": 0}
    processed = 0
    with sync_playwright() as p:
        browser = launch(p, headed=args.headed)
        context = browser.new_context(storage_state=STATE_FILE)
        for url in urls:
            if processed >= args.max:
                print(f"reached --max {args.max}, stopping")
                break
            if not is_supported(url):
                print(f"unsupported: {url}")
                continue
            if already_applied(con, url):
                print(f"already applied, skipping: {url}")
                continue
            if processed:
                wait = random.uniform(5, 15)
                print(f"waiting {wait:.0f}s ...")
                time.sleep(wait)
            processed += 1
            print(f"[{processed}] {url}")
            result, shot = apply_one(context, url, args, profile, llm)
            log_result(con, url, result, shot)
            counts[result["status"]] += 1
            print(f"  {result['status']}" + (f": {result['reason']}" if result.get("reason") else ""))
        browser.close()
    print(f"\nsummary: {counts['applied']} applied, {counts['dry-run']} dry-run, {counts['skipped']} skipped, {counts['failed']} failed")


if __name__ == "__main__":
    main()
