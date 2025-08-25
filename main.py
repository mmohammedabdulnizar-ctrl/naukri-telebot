import asyncio
import json
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

from tenacity import retry, stop_after_attempt, wait_fixed

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

# ===================== CONFIG ===================== #

TZ = ZoneInfo("Asia/Kolkata")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

NAUKRI_EMAIL = os.environ.get("NAUKRI_EMAIL", "")
NAUKRI_PASSWORD = os.environ.get("NAUKRI_PASSWORD", "")

SEARCH_KEYWORDS = os.environ.get("SEARCH_KEYWORDS", "Software Engineer")
SEARCH_LOCATION = os.environ.get("SEARCH_LOCATION", "Chennai")
EXCLUDE_KEYWORDS = os.environ.get("EXCLUDE_KEYWORDS", "")  # comma-separated
MAX_APPLICATIONS_PER_RUN = int(os.environ.get("MAX_APPLICATIONS_PER_RUN", "8"))

COOKIES_FILE = Path("cookies.json")
APPLIED_LOG = Path("applied_log.json")

# otp handoff
OTP_WAIT_SECONDS = 300  # 5 minutes
_pending_otp_future: asyncio.Future | None = None

# ===================== UTIL ===================== #

def _now_ist() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def _should_skip(title: str) -> bool:
    if not EXCLUDE_KEYWORDS.strip():
        return False
    bad = [x.strip().lower() for x in EXCLUDE_KEYWORDS.split(",") if x.strip()]
    t = title.lower()
    return any(b in t for b in bad)

def _slug_search_url(keywords: str, location: str) -> str:
    # slug-style URL tends to work; also add k= param as fallback
    k = "-".join(keywords.split())
    l = "-".join(location.split())
    return f"https://www.naukri.com/{k}-jobs-in-{l}?k={quote_plus(keywords)}&l={quote_plus(location)}"

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default

def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))

# ===================== TELEGRAM HANDLERS ===================== #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Save chat id if not set
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_CHAT_ID and update.effective_chat:
        TELEGRAM_CHAT_ID = str(update.effective_chat.id)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ Registered this chat for notifications.\nChat ID: `{TELEGRAM_CHAT_ID}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    await update.message.reply_text(
        "👋 Naukri Auto-Apply Bot ready.\n\n"
        "Commands:\n"
        "/status – last run & config\n"
        "/otp 123456 – send OTP if login asks\n"
        "/runnow – run an apply cycle immediately"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    applied = _load_json(APPLIED_LOG, [])
    msg = (
        f"🕒 IST now: {_now_ist()}\n"
        f"🔎 Keywords: *{SEARCH_KEYWORDS}* | Location: *{SEARCH_LOCATION}*\n"
        f"🚫 Exclude: *{EXCLUDE_KEYWORDS or '—'}*\n"
        f"🎯 Max per run: *{MAX_APPLICATIONS_PER_RUN}*\n"
        f"📦 Cookies saved: *{'Yes' if COOKIES_FILE.exists() else 'No'}*\n"
        f"🗂 Applied log size: *{len(applied)}*"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def runnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Running now…")
    count, notes = await apply_cycle(context.application)
    await update.message.reply_text(f"✅ Done. Applied: *{count}*.\n{notes}", parse_mode=ParseMode.MARKDOWN)

async def otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _pending_otp_future
    parts = update.message.text.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await update.message.reply_text("Send OTP like: `/otp 123456`", parse_mode=ParseMode.MARKDOWN)
        return
    code = parts[1].strip()
    if _pending_otp_future and not _pending_otp_future.done():
        _pending_otp_future.set_result(code)
        await update.message.reply_text("✅ OTP received. Proceeding…")
    else:
        await update.message.reply_text("ℹ️ No OTP was requested right now. I’ll ask again if needed.")

# ===================== CORE BROWSER AUTOMATION ===================== #

async def _ensure_logged_in(page) -> str:
    """Login if needed; tries cookies first, else credentials (may ask OTP). Returns note."""
    # Try restore cookies
    if COOKIES_FILE.exists():
        try:
            cookies = json.loads(COOKIES_FILE.read_text())
            await page.context.add_cookies(cookies)
        except Exception:
            pass

    await page.goto("https://www.naukri.com/", wait_until="domcontentloaded")
    # Check if logged in (avatar / profile link presence heuristic)
    if await page.locator("a[title*='My Naukri'], a[title*='Profile'], img[alt*=profile]").first().is_visible(timeout=2000).catch(lambda _: False):
        return "Used saved cookies"

    # Open login layer
    try:
        await page.click("#login_Layer", timeout=5000)
    except PWTimeout:
        # fallback: find any login trigger
        try:
            await page.get_by_text("Login", exact=False).first.click(timeout=5000)
        except Exception:
            pass

    # Fill form
    email_sel = "input[placeholder*='Email'], input[type='text']"
    pass_sel  = "input[type='password']"
    await page.fill(email_sel, NAUKRI_EMAIL, timeout=10000)
    await page.fill(pass_sel, NAUKRI_PASSWORD, timeout=10000)
    # click Login
    login_btn = page.get_by_role("button", name=lambda n: "login" in n.lower() or "log in" in n.lower())
    try:
        await login_btn.click(timeout=5000)
    except Exception:
        # a generic button
        await page.locator("button:has-text('Login'), button:has-text('LOG IN')").first.click(timeout=10000)

    # OTP?
    otp_input = page.locator("input[placeholder*='OTP'], input[name*='otp'], input[id*='otp']").first
    try:
        if await otp_input.is_visible(timeout=4000):
            # ask via Telegram
            await _notify(f"🔐 Naukri asked for OTP. Send it with `/otp 123456` within {OTP_WAIT_SECONDS//60} min.")
            code = await _wait_for_otp()
            await otp_input.fill(code)
            # submit
            await page.keyboard.press("Enter")
    except PWTimeout:
        pass

    # After login, save cookies
    await page.wait_for_timeout(3000)
    try:
        cookies = await page.context.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies))
    except Exception:
        pass

    return "Logged in with credentials"

async def _wait_for_otp() -> str:
    global _pending_otp_future
    _pending_otp_future = asyncio.get_event_loop().create_future()
    try:
        return await asyncio.wait_for(_pending_otp_future, timeout=OTP_WAIT_SECONDS)
    finally:
        _pending_otp_future = None

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
async def _open_results(page, keywords: str, location: str):
    url = _slug_search_url(keywords, location)
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)

async def _collect_jobs(page):
    # Heuristics: job cards and apply anchors/buttons
    cards = page.locator("article, div.jobTuple, div.srp-jobtuple, div.list, div.row").all()
    jobs = []
    for i in range(min(len(cards), 50)):
        card = cards[i]
        try:
            title = await card.locator("a.title, a[title], a:has(h2), h2").first.text_content(timeout=1000)
            title = (title or "").strip()
            if not title:
                continue
            if _should_skip(title):
                continue

            apply_btn = card.locator("a:has-text('Apply'), button:has-text('Apply')").first
            if not await apply_btn.is_visible(timeout=800).catch(lambda _: False):
                continue

            href = await card.locator("a[href]").first.get_attribute("href")
            jobs.append({"title": title, "apply": apply_btn, "href": href})
        except Exception:
            continue
    return jobs

async def _apply_on_card(apply_btn, context_applied):
    try:
        # Clicking sometimes opens a new tab — listen for page
        async with apply_btn.page.context.expect_page() as new_page_info:
            await apply_btn.click()
        newp = await new_page_info.value
        # If external site, close
        await newp.wait_for_load_state("domcontentloaded", timeout=15000)
        url = newp.url
        if "naukri.com" not in url:
            await newp.close()
            return False, "External site — skipped"
        # Some fast apply flows auto-apply; otherwise look for confirm button
        try:
            confirm = newp.locator("button:has-text('Apply'), button:has-text('Submit'), a:has-text('Apply')")
            if await confirm.first.is_visible(timeout=2000):
                await confirm.first.click()
        except Exception:
            pass
        await asyncio.sleep(1.5)
        await newp.close()
        return True, "Applied"
    except Exception as e:
        # Try simple click without new tab
        try:
            await apply_btn.click()
            await asyncio.sleep(1.5)
            return True, "Applied (same tab)"
        except Exception as ee:
            return False, f"Failed: {ee}"

async def apply_cycle(app):
    """One full run: login -> search -> apply up to N new jobs -> notify"""
    applied_log = _load_json(APPLIED_LOG, [])
    applied_set = set(applied_log)

    notes = []
    count = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context()
        page = await context.new_page()

        note = await _ensure_logged_in(page)
        notes.append(f"🔑 {note}")

        await _open_results(page, SEARCH_KEYWORDS, SEARCH_LOCATION)
        await page.wait_for_timeout(3000)

        jobs = await _collect_jobs(page)
        if not jobs:
            await browser.close()
            await _notify(f"⚠️ No jobs found for *{SEARCH_KEYWORDS}* in *{SEARCH_LOCATION}*.", markdown=True)
            return 0, "\n".join(notes)

        for job in jobs:
            if count >= MAX_APPLICATIONS_PER_RUN:
                break
            key = job["href"] or job["title"]
            if key in applied_set:
                continue
            ok, msg = await _apply_on_card(job["apply"], applied_set)
            if ok:
                count += 1
                applied_set.add(key)
                notes.append(f"✅ {job['title'][:70]}…")
            else:
                notes.append(f"⛔ {job['title'][:70]}… – {msg}")

        # persist log
        _save_json(APPLIED_LOG, list(applied_set))

        await browser.close()

    status = f"🕒 {_now_ist()} IST\n🔎 *{SEARCH_KEYWORDS}* in *{SEARCH_LOCATION}*\n🎯 Applied this run: *{count}*"
    await _notify(status + ("\n" + "\n".join(notes[-10:]) if notes else ""), markdown=True)
    return count, "\n".join(notes)

# ===================== NOTIFY ===================== #

async def _notify(text: str, markdown: bool = False):
    if not TELEGRAM_CHAT_ID:
        return
    from telegram import Bot
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN if markdown else None,
            disable_web_page_preview=True,
        )
    except Exception:
        # Fallback plain text
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

# ===================== APP BOOT ===================== #

async def on_start(app):
    # Schedule at 08:00 and 20:00 IST daily
    sched = AsyncIOScheduler(timezone=TZ)
    sched.add_job(lambda: asyncio.create_task(apply_cycle(app)),
                  CronTrigger(hour=8, minute=0, second=0, timezone=TZ), id="am8")
    sched.add_job(lambda: asyncio.create_task(apply_cycle(app)),
                  CronTrigger(hour=20, minute=0, second=0, timezone=TZ), id="pm8")
    sched.start()
    await _notify("🤖 Bot started. I will auto-apply at 8:00 AM and 8:00 PM IST.", markdown=False)

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("runnow", runnow))
    application.add_handler(CommandHandler("otp", otp))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda *_: None))

    application.post_init = on_start
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
