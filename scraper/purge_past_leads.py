"""
purge_past_leads.py
Removes Guadalupe leads where sale_date (foreclosure/HOA-lien auction date)
has already passed. Guadalupe has no automated daily scraper (records are
manually transcribed from HOA lien notice PDFs), so nothing purges this on
its own the way Bexar/Nueces do -- run manually as needed.

Keeps: any lead with a future sale_date, any with GHL activity
(dash_phone or ghl_pushed), and anything with no sale_date at all (manual
entries without a parseable date shouldn't be silently dropped).

Usage:
  python scraper/purge_past_leads.py
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

RECORDS_PATH = Path("docs/records.json")
# 2026-09-01: TODAY = datetime.now() used the local/runner clock rather
# than Central time -- confirmed live in bexar-leads this class of bug
# purged 418 leads the instant UTC crossed midnight into a sale date,
# hours before that date started in Central time. Guadalupe's own
# has_ghl_activity() check would have protected worked leads either way,
# but unworked leads could still have been dropped a day early. Use
# Central time and compare calendar dates only (see should_purge below).
TODAY = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)

# v1.1: "if we haven't worked them by now, it's too late" -- user's own
# words, 2026-08-26, matching the same standing rule added to Nueces's
# purge_past_auctions() the same day. An unworked lead this close to its
# own auction is dead the same way a past auction is.
TOO_SOON_TO_WORK_DAYS = 5


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y")
    except Exception:
        return None


def has_ghl_activity(rec):
    return bool(rec.get("dash_phone") or rec.get("ghl_pushed"))


def should_purge(rec):
    if has_ghl_activity(rec):
        return False, "has_ghl_activity"
    dt = parse_date(rec.get("sale_date", ""))
    if dt and dt.date() < TODAY.date():
        return True, f"auction_passed ({rec['sale_date']})"
    if dt and 0 <= (dt.date() - TODAY.date()).days <= TOO_SOON_TO_WORK_DAYS:
        return True, f"too_soon_to_work ({rec['sale_date']})"
    return False, "keep"


def main():
    if not RECORDS_PATH.exists():
        log.error(f"records.json not found at {RECORDS_PATH}")
        return

    records = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
    log.info(f"Loaded {len(records)} records")

    keep, purged, reasons = [], [], {}
    for rec in records:
        purge, reason = should_purge(rec)
        if purge:
            purged.append(rec)
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            keep.append(rec)

    log.info(f"Keeping: {len(keep)} | Purging: {len(purged)}")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        log.info(f"  {reason}: {count}")

    if not purged:
        log.info("Nothing to purge — records.json unchanged")
        return

    for rec in purged[:10]:
        log.info(f"  purged: {rec.get('address','—')} | sale={rec.get('sale_date','—')}")

    RECORDS_PATH.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    log.info(f"records.json saved: {len(keep)} records remaining")


if __name__ == "__main__":
    main()
