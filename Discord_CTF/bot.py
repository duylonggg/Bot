# bot.py
import os
import re
import asyncio
import logging
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from icalendar import Calendar
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

# --------- ENV ---------
TOKEN = os.getenv("BOT_TOKEN")
CALENDAR_ICS_URL = os.getenv("CALENDAR_ICS_URL")  # full ICS URL (public .ics)
CATEGORY_NAME = os.getenv("CATEGORY_NAME", "CTF")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "announcement")
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))  # tùy chọn: set ID -> chắc chắn đúng kênh
LOCAL_TZ_NAME = os.getenv("TIMEZONE", "Asia/Bangkok")

if not TOKEN or not CALENDAR_ICS_URL:
    raise SystemExit("Missing BOT_TOKEN or CALENDAR_ICS_URL in .env")

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

# --------- LOGGING ---------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ctf-bot")

# --------- DISCORD ---------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)
# cache: uid -> { uid, summary, start_local, start_utc, end_local, end_utc, url }
events_cache: dict[str, dict] = {}

# =========================================================
# Utils
# =========================================================
def _clean_url_from_description(description: Optional[str]) -> Optional[str]:
    """
    Nhận vào DESCRIPTION (có thể chứa HTML hoặc text thuần),
    trả về URL sạch nếu có, ngược lại None.
    """
    if not description:
        return None

    s = str(description)

    # 1) Nếu có HTML -> bóc bằng BeautifulSoup
    if "<" in s and ">" in s and ("<a" in s.lower() or "</" in s.lower()):
        soup = BeautifulSoup(s, "html.parser")
        a = soup.find("a", href=True)
        if a and a["href"]:
            return a["href"].strip()
        # fallback: lấy text rồi regex
        s = soup.get_text(" ", strip=True)

    # 2) Regex lấy URL đầu tiên
    m = re.search(r"https?://[^\s<>\"]+", s)
    if m:
        url = m.group(0)
        # Loại bỏ ký tự thừa cuối chuỗi nếu có
        url = url.rstrip(").,;\">')")
        return url

    return None


def format_event_block(ev: dict) -> str:
    """
    Dùng chung cho announce + slash command.
    Trả về block text:
    **Title**
    🗓️ dd-mm-YYYY HH:MM [ - dd-mm-YYYY HH:MM]
    🔗 url
    """
    start_local = ev["start_local"]
    end_local = ev.get("end_local")
    time_range = start_local.strftime("%d-%m-%Y %H:%M")
    if end_local:
        time_range += " - " + end_local.strftime("%d-%m-%Y %H:%M")

    url = ev.get("url") or ""
    return f"**{ev['summary']}**\n🗓️ {time_range}\n🔗 {url}"


# =========================================================
# ICS fetch/parse
# =========================================================
async def fetch_events_from_ics(session: aiohttp.ClientSession) -> list[dict]:
    """
    Fetch ICS và trả về list event dict:
    { uid, summary, start_local, start_utc, end_local, end_utc, url }
    - start_local luôn ở LOCAL_TZ
    - start_utc luôn UTC
    - url lấy ưu tiên thuộc tính URL (nếu có), nếu không có thì bóc từ DESCRIPTION
    """
    async with session.get(CALENDAR_ICS_URL) as resp:
        ics_text = await resp.text()

    cal = Calendar.from_ical(ics_text)
    results: list[dict] = []

    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue

        uid = str(comp.get("uid"))
        summary = str(comp.get("summary", "No title"))

        # dtstart / dtend có thể là date hoặc datetime
        try:
            dtstart = comp.decoded("dtstart")
        except Exception:
            continue
        dtend = None
        try:
            dtend = comp.decoded("dtend")
        except Exception:
            dtend = None

        # Date -> Datetime @ 00:00
        if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
            dtstart = datetime.combine(dtstart, time.min)
        if isinstance(dtend, date) and not isinstance(dtend, datetime):
            dtend = datetime.combine(dtend, time.min)

        # Nếu naive -> assume LOCAL_TZ
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=LOCAL_TZ)
        if dtend is not None and dtend.tzinfo is None:
            dtend = dtend.replace(tzinfo=LOCAL_TZ)

        # Chuẩn hóa hai dạng
        start_local = dtstart.astimezone(LOCAL_TZ)
        start_utc = start_local.astimezone(timezone.utc)

        end_local = None
        end_utc = None
        if dtend is not None:
            end_local = dtend.astimezone(LOCAL_TZ)
            end_utc = end_local.astimezone(timezone.utc)

        # Ưu tiên thuộc tính URL trong ICS nếu có
        ical_url = comp.get("url")
        ical_url = str(ical_url) if ical_url else None

        # Nếu không có, bóc từ description
        description = comp.get("description")
        desc_url = _clean_url_from_description(str(description) if description else None)

        final_url = ical_url or desc_url

        results.append({
            "uid": uid,
            "summary": summary,
            "start_local": start_local,
            "start_utc": start_utc,
            "end_local": end_local,
            "end_utc": end_utc,
            "url": final_url,
        })

    # sort theo start_utc
    results.sort(key=lambda e: e["start_utc"])
    return results


# =========================================================
# Announcement helpers
# =========================================================
async def send_initial_announcement(event: dict):
    """
    Gửi thông báo ngay khi phát hiện event mới.
    """
    global announcement_channel
    if not announcement_channel:
        log.warning("No announcement channel; initial announce skipped for %s", event["summary"])
        return

    msg = "📣 **Mới có event:**\n" + format_event_block(event)
    try:
        await announcement_channel.send(msg)
        log.info("Sent initial announcement for %s", event["summary"])
    except Exception:
        log.exception("Failed to send initial announcement for %s", event["summary"])


async def send_update_announcement(event: dict, old_start_utc: datetime):
    """
    Gửi thông báo khi thời gian bắt đầu thay đổi.
    """
    global announcement_channel
    if not announcement_channel:
        log.warning("No announcement channel; update announce skipped for %s", event["summary"])
        return

    new_local = event["start_local"].strftime("%d-%m-%Y %H:%M")
    old_local = old_start_utc.astimezone(LOCAL_TZ).strftime("%d-%m-%Y %H:%M")
    url = event.get("url") or ""
    msg = (
        f"# 🔁 **Event đã thay đổi thời gian:**\n"
        f"**{event['summary']}**\n"
        f"🕒 Cũ: {old_local}\n"
        f"🗓️ Mới: {new_local}\n"
        f"🔗 {url}"
    )
    try:
        await announcement_channel.send(msg)
        log.info("Sent update announcement for %s", event["summary"])
    except Exception:
        log.exception("Failed to send update announcement for %s", event["summary"])


# =========================================================
# Scheduler / Reminder logic
# =========================================================
def schedule_event_reminders(event: dict):
    """
    Với mỗi sự kiện:
      - Nhắc vào 00:00 local các ngày D-1, D-2, D-3
      - Nhắc 1h trước khi bắt đầu
    """
    uid = event["uid"]
    start_local = event["start_local"]
    now_utc = datetime.now(timezone.utc)

    # 3 ngày trước, 00:00 local
    event_date_local = start_local.date()
    for i in range(1, 4):
        remind_date = event_date_local - timedelta(days=i)
        remind_dt_local = datetime.combine(remind_date, time.min).replace(tzinfo=LOCAL_TZ)

        if remind_dt_local.astimezone(timezone.utc) > now_utc:
            job_id = f"{uid}-remind-day-{i}"
            if not scheduler.get_job(job_id):
                scheduler.add_job(send_reminder_job, "date", run_date=remind_dt_local, args=[uid, i], id=job_id)
                log.info("Scheduled 00:00 reminder for %s (D-%d) at %s",
                         event["summary"], i, remind_dt_local.isoformat())

    # 1 giờ trước
    one_hour_before_local = (start_local - timedelta(hours=1)).astimezone(LOCAL_TZ)
    if one_hour_before_local.astimezone(timezone.utc) > now_utc:
        job_id = f"{uid}-remind-hour"
        if not scheduler.get_job(job_id):
            scheduler.add_job(send_reminder_job, "date", run_date=one_hour_before_local, args=[uid, "hour"], id=job_id)
            log.info("Scheduled 1-hour reminder for %s at %s",
                     event["summary"], one_hour_before_local.isoformat())


async def send_reminder(uid: str, which):
    event = events_cache.get(uid)
    if not event:
        log.warning("Event %s not found in cache when sending reminder", uid)
        return

    prefix = "⏰ Còn 1 tiếng nữa!" if which == "hour" else f"🔔 Còn {which} ngày nữa!"
    start_str = event["start_local"].strftime("%d-%m-%Y %H:%M")
    url = event.get("url") or ""
    msg = f"{prefix}\n**{event['summary']}**\n🗓️ Bắt đầu: {start_str}\n🔗 {url}"

    global announcement_channel
    if announcement_channel:
        try:
            await announcement_channel.send(msg)
            log.info("Sent reminder for %s (%s)", event["summary"], which)
        except Exception:
            log.exception("Failed to send reminder for %s", event["summary"])
    else:
        log.warning("No announcement channel; reminder not sent: %s", event["summary"])


def send_reminder_job(uid, which):
    asyncio.create_task(send_reminder(uid, which))


# =========================================================
# Update loop
# =========================================================
async def update_calendar_events():
    """
    - Crawl ICS
    - Cập nhật cache
    - Lên lịch nhắc
    - Thông báo NGAY khi có event mới
    - Thông báo khi thay đổi giờ
    - Xoá cache những event đã bị xoá khỏi ICS
    """
    async with aiohttp.ClientSession() as session:
        try:
            events = await fetch_events_from_ics(session)
        except Exception:
            log.exception("Failed to fetch/parse ICS")
            return

    now = datetime.now(timezone.utc)
    new_count = 0

    current_uids = {ev["uid"] for ev in events}

    # --- xử lý event mới hoặc update ---
    for ev in events:
        if ev["start_utc"] <= now:
            continue

        uid = ev["uid"]
        if uid not in events_cache:
            # Event mới
            events_cache[uid] = ev
            schedule_event_reminders(ev)
            # await send_initial_announcement(ev)  # nếu muốn thông báo ngay khi có event mới
            new_count += 1
        else:
            # Đã có -> kiểm tra thay đổi giờ
            cached = events_cache[uid]
            if cached["start_utc"] != ev["start_utc"]:
                old_start = cached["start_utc"]
                events_cache[uid] = ev
                # Remove jobs cũ
                for jid in [f"{uid}-remind-day-{i}" for i in range(1, 4)] + [f"{uid}-remind-hour"]:
                    job = scheduler.get_job(jid)
                    if job:
                        job.remove()
                # Lên lịch lại
                schedule_event_reminders(ev)
                log.info("Updated schedule for event %s (start changed)", ev["summary"])
                # Thông báo thay đổi giờ
                await send_update_announcement(ev, old_start)

    # --- cleanup: xoá event không còn trong ICS ---
    to_remove = [uid for uid in list(events_cache.keys()) if uid not in current_uids]
    for uid in to_remove:
        removed = events_cache.pop(uid)
        log.info("Removed event not in ICS anymore: %s", removed["summary"])
        # xoá reminder jobs liên quan
        for jid in [f"{uid}-remind-day-{i}" for i in range(1, 4)] + [f"{uid}-remind-hour"]:
            job = scheduler.get_job(jid)
            if job:
                job.remove()

    log.info("Update calendar: scanned %d events, %d new scheduled, %d removed",
             len(events), new_count, len(to_remove))

# =========================================================
# Discord lifecycle
# =========================================================
async def _resolve_announcement_channel() -> None:
    """
    Cố gắng tìm kênh để post announce theo thứ tự:
    1) ANNOUNCE_CHANNEL_ID (nếu set)
    2) Channel theo CATEGORY_NAME + CHANNEL_NAME
    3) Bất kỳ channel nào trùng tên CHANNEL_NAME
    4) Fallback: kênh text đầu tiên mà bot có quyền gửi
    """
    global announcement_channel

    # 1) Theo ID
    if ANNOUNCE_CHANNEL_ID:
        ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            announcement_channel = ch
            log.info("Announcement channel resolved by ID: #%s (%s)", ch.name, ch.guild.name)
            return

    # 2) Theo category + name
    for guild in client.guilds:
        cat = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if cat:
            ch = discord.utils.get(cat.text_channels, name=CHANNEL_NAME)
            if ch:
                announcement_channel = ch
                log.info("Announcement channel resolved by category/name: #%s (%s)", ch.name, guild.name)
                return

    # 3) Bất kỳ channel tên khớp
    for guild in client.guilds:
        ch = discord.utils.get(guild.text_channels, name=CHANNEL_NAME)
        if ch:
            announcement_channel = ch
            log.info("Announcement channel resolved by name: #%s (%s)", ch.name, guild.name)
            return

    # 4) Fallback: kênh đầu có quyền gửi
    for guild in client.guilds:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages:
                announcement_channel = ch
                log.warning("Announcement channel fallback to #%s (%s)", ch.name, guild.name)
                return

    log.warning("Could not resolve any announcement channel.")


@client.event
async def on_ready():
    log.info("Bot online: %s", client.user)

    await _resolve_announcement_channel()

    # Start scheduler
    scheduler.start()

    # Initial load
    await update_calendar_events()
    # Poll ICS mỗi 10 phút (nhanh hơn để “bắt” event mới)
    scheduler.add_job(lambda: asyncio.create_task(update_calendar_events()),
                      "interval", minutes=10, id="periodic-update")

    # Sync slash commands
    await tree.sync()
    log.info("Slash commands synced")

# =========================================================
# Slash command
# =========================================================
@tree.command(name="upcoming_event", description="Liệt kê các CTF sắp tới theo Google Calendar")
async def upcoming_event(interaction: discord.Interaction):
    await update_calendar_events()

    now = datetime.now(timezone.utc)
    upcoming = [ev for ev in events_cache.values() if ev["start_utc"] > now]
    if not upcoming:
        await interaction.response.send_message("❌ Không có sự kiện sắp tới.")
        return

    upcoming.sort(key=lambda e: e["start_utc"])
    blocks = [format_event_block(ev) for ev in upcoming[:10]]
    await interaction.response.send_message("# 📅 Các sự kiện CTF sắp tới:\n\n" + "\n\n".join(blocks))


# =========================================================
# Run
# =========================================================
if __name__ == "__main__":
    client.run(TOKEN)
