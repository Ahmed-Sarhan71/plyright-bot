"""Save a Wuzzuf session (cookies) from your real Chrome, bypassing Google's automation block.

Chrome refuses --remote-debugging-port on its default profile, so this uses a dedicated one
(.chrome-cdp/). It stays logged in: rerun any time to refresh wuzzuf_state.json, no prompts
if the session is still alive.

    .venv/bin/python login_cdp.py
"""
import shutil
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT, STATE, HOME = 9222, "wuzzuf_state.json", "https://wuzzuf.net/"

bin = next(b for b in ("chromium", "google-chrome-stable", "google-chrome") if shutil.which(b))
subprocess.Popen(
    [bin, f"--remote-debugging-port={PORT}", f"--user-data-dir={Path('.chrome-cdp').resolve()}",
     HOME]
)
with sync_playwright() as p:
    for _ in range(30):  # chrome needs a moment before the port answers
        try:
            ctx = p.chromium.connect_over_cdp(f"http://localhost:{PORT}").contexts[0]
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit(f"chrome never opened port {PORT}")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if not page.url.startswith("https://wuzzuf.net"):
        page.goto(HOME)
    page.wait_for_load_state("domcontentloaded")
    if page.locator("a[href*='login']").count():  # a "Login" link means logged out
        input("Log into Wuzzuf with Google in that window, then press Enter here... ")
    ctx.storage_state(path=STATE)
print(f"saved {STATE} - you can close that chrome window now")
