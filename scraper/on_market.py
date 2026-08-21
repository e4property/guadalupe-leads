"""
Flags Guadalupe leads that are already listed for sale/rent elsewhere, using
homeharvest (pip, MIT license) against Realtor.com's public page data -- no
API key, no cost. Same approach as bexar-leads/nueces-leads.

Guadalupe has no automated county scraper (leads are added manually), so
this runs as its own small standalone job against docs/records.json rather
than being a step inside a larger fetch.py pipeline.

Soft dependency: any failure (network, no match, library error) just leaves
on_market unset for that lead rather than breaking the run.
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

RECORDS_PATH = Path("docs/records.json")

ON_MARKET_STATUSES      = {"FOR_SALE", "PENDING", "FOR_RENT"}
# 2026-08-21: bexar-leads hit a hard Realtor.com AuthenticationError wall
# after ~27 consecutive homeharvest requests in one run that never
# recovered. Keeping the combined per-run total (fetch + refresh) well
# under that -- moot for now with only 30 Guadalupe leads total, but keeps
# this safe if the list grows.
ON_MARKET_FETCH_LIMIT   = 20   # max never-checked leads to look up per run
ON_MARKET_REFRESH_DAYS  = 7    # re-check a lead's market status at most this often
ON_MARKET_REFRESH_LIMIT = 10   # max already-checked leads to re-check per run


def main():
    import pandas as pd
    from homeharvest import scrape_property

    def clean(val):
        if val is None or pd.isna(val):
            return None
        s = str(val).strip()
        return None if s in ("", "nan", "<NA>", "None") else val

    records = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))

    cutoff = datetime.utcnow() - timedelta(days=ON_MARKET_REFRESH_DAYS)

    def is_stale(r):
        checked_at = r.get("on_market_checked_at")
        if not checked_at:
            return False
        try:
            return datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ") < cutoff
        except Exception:
            return True

    never_checked = [r for r in records if r.get("address") and not r.get("on_market_checked_at")]
    stale_checked = [r for r in records if r.get("address") and r.get("on_market_checked_at") and is_stale(r)]

    candidates = never_checked[:ON_MARKET_FETCH_LIMIT] + stale_checked[:ON_MARKET_REFRESH_LIMIT]

    if not candidates:
        print("On-market: no eligible leads -- skipping")
        return

    print(f"On-market: {len(never_checked[:ON_MARKET_FETCH_LIMIT])} new + "
          f"{len(stale_checked[:ON_MARKET_REFRESH_LIMIT])} refresh "
          f"(caps={ON_MARKET_FETCH_LIMIT}/{ON_MARKET_REFRESH_LIMIT})")
    changed = 0
    errors  = 0

    for rec in candidates:
        full_addr = f"{rec['address']}, {rec.get('city', '')}, TX {rec.get('zip', '')}".strip(", ")
        try:
            df = scrape_property(location=full_addr)
            now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            was_on_market = bool(rec.get("on_market"))

            if df is None or len(df) == 0:
                rec["on_market_checked_at"] = now_iso
                print(f"  [{rec.get('doc_number')}] {full_addr}: no match on Realtor.com")
                continue

            status = clean(df.iloc[0].get("status")) or ""
            rec["on_market"]            = status in ON_MARKET_STATUSES
            rec["on_market_status"]     = status
            rec["on_market_checked_at"] = now_iso

            if rec["on_market"] != was_on_market:
                changed += 1
            print(f"  [{rec.get('doc_number')}] {full_addr}: status={status} on_market={rec['on_market']}")
        except Exception as e:
            print(f"  [{rec.get('doc_number')}] {full_addr}: error: {e}")
            errors += 1
        finally:
            time.sleep(1)

    print(f"On-market: {changed} status changes, {errors} errors out of {len(candidates)} candidates")

    RECORDS_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
