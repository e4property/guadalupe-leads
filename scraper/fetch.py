"""
fetch.py -- Guadalupe County Notice of (Substitute) Trustee's Sale scraper.

Guadalupe does not run a Tyler/publicsearch.us portal like Bexar/Dallas/Nueces.
Instead the county posts one compiled PDF per upcoming sale date at:

    https://www.guadalupetx.gov/page/open/5894/0/<YYYY-MM-DD>

(sale dates are always the first Tuesday of the month; the county appends new
notices to that PDF as they're filed in the weeks before the sale). This
script tries the next N first-Tuesdays, downloads whichever PDFs exist,
splits each into individual notices, and extracts what it can.

Address extraction (confirmed against real cached PDFs, 2026-09-18):
Every notice carries its property address in ONE of three places, tried in
this order:
  1. "Commonly known as: <addr>" line
  2. "Property Address:" / "Property Address/Mailing Address:" line
  3. A mail-merge header line (street + city + TX + zip, no labels) that
     appears a few lines above the "NOTICE OF ... SALE" title on notices
     that otherwise only give a legal description (Exhibit A) in the body.
Some notices genuinely have none of the three (address truly not resolvable
from the PDF) -- those are still included with a NO ADDRESS flag rather than
silently dropped, matching the Dallas/Bexar convention.

Usage:
  python scraper/fetch.py
"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

RECORDS_PATH = Path("docs/records.json")
TODAY_CT = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
RUN_TS = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

PAGE_IDS = ["5894", "7450"]  # county has switched the page id at least once (see 7450_0_2027-01-05 in scratch cache)
BASE = "https://www.guadalupetx.gov/page/open"

CITY_RE = r"(NEW\s+BRAUNFELS|SAN\s+ANTONIO|SEGUIN|CIBOLO|SCHERTZ|SELMA|MARION|MCQUEENEY|KINGSBURY|ZUEHL)"

ADDR_LABEL_RE = re.compile(
    r"(?:Commonly\s+known\s+as:?|Property\s+Address(?:/Mailing\s+Address)?:?)\s*"
    r"([0-9][^\n]{3,60}?)[.,]?\s*" + CITY_RE + r"\.?,?\s*(?:TX|TEXAS)\.?\s*(\d{5})",
    re.IGNORECASE,
)
# same label but address-only on its own line, city/zip missing (rare -- look only at the
# very next non-blank line for a bare "CITY, TX ZIP", never search the whole block (that
# grabbed unrelated addresses from elsewhere in the notice))
ADDR_LABEL_NOCITY_RE = re.compile(
    r"(?:Commonly\s+known\s+as:?|Property\s+Address(?:/Mailing\s+Address)?:?)\s*([0-9][^\n,.]{3,60})\n+\s*"
    + CITY_RE + r",?\s*TX\.?\s*(\d{5})",
    re.IGNORECASE,
)
HEADER_ADDR_RE = re.compile(
    r"^\s*(\d[0-9A-Za-z .'#-]{3,40}?)(?:\s+\d{8,}\b)?\.?\s+" + CITY_RE + r",?\s*TX\.?\s*(\d{5})\s*$",
    re.MULTILINE | re.IGNORECASE,
)
SALE_DATE_RE = re.compile(r"Date\s+of\s+Sale:\s*(\d{1,2})/(\d{1,2})/(\d{4})|Date:\s*([A-Za-z]+ \d{1,2},\s*\d{4})")
LOAN_AMT_RE = re.compile(r"original\s+principal\s+amount\s+of\s+\$([0-9,]+\.\d{2})", re.IGNORECASE)
INSTRUMENT_RE = re.compile(r"(?:INSTRUMENT\s+NO\.?|Instrument\s+No:?)\s*([A-Z0-9-]{6,20})", re.IGNORECASE)
FILE_NO_RE = re.compile(r"File\s+No\.?:?\s*([A-Z0-9-]{5,20})", re.IGNORECASE)

GRANTOR_A_RE = re.compile(r"with\s+([A-Z][A-Z0-9 ,.&'\-]{3,90}?),?\s+grantor\(s\)", re.IGNORECASE)
GRANTOR_B_RE = re.compile(r"Grantor\(s\)/Mortgagor\(s\):\s*\n?([A-Z][A-Za-z0-9 ,.&'\-\n]{3,90}?)\n\s*(?:Original\s+Beneficiary|Recorded\s+in)", re.IGNORECASE)

MORTGAGEE_A_RE = re.compile(r"([A-Z][A-Za-z0-9 ,.&'\-]{3,80}?)\s+is\s+the\s+current\s+mortgagee", re.IGNORECASE)
MORTGAGEE_B_RE = re.compile(r"Current\s+Beneficiary/Mortgagee:\s*\n?([A-Za-z][A-Za-z0-9 ,.&'\-\n]{3,90}?)\n\s*(?:Recorded\s+in|Mortgage\s+Servicer)", re.IGNORECASE)

NOTICE_START_RE = re.compile(r"NOTICE\s+OF\s+(?:\[?SUBSTITUTE\]?\s*)?TRUSTEE'?S?\s+SALE|NOTICE\s+OF\s+FORECLOSURE\s+SALE", re.IGNORECASE)
# The county clerk stamps every notice, on filing, with its own standalone
# 6-digit control number (e.g. "000525") at the very top of the notice's
# first page. Unlike NOTICE_START_RE's title text -- which some multi-page
# notices repeat as a running header on EVERY page, over-splitting a single
# 3-page notice into 3 spurious blocks (confirmed live 2026-09-18: a 19-way
# split on title text was actually only 12 real notices) -- this control
# number appears exactly once per real notice, making it the reliable
# boundary.
CONTROL_NUM_RE = re.compile(r"(?m)^\s*(\d{6})\s*$")


def upcoming_sale_dates(n=6):
    """First Tuesday of each of the next n months. Starts from the current
    month but skips it if that month's first Tuesday has already passed
    (e.g. run on the 18th, this month's sale already happened)."""
    dates = []
    y, m = TODAY_CT.year, TODAY_CT.month
    while len(dates) < n:
        d = datetime(y, m, 1)
        while d.weekday() != 1:  # Tuesday
            d += timedelta(days=1)
        if d.date() >= TODAY_CT.date():
            dates.append(d)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return dates


def download_pdf(sale_date):
    date_str = sale_date.strftime("%Y-%m-%d")
    for page_id in PAGE_IDS:
        url = f"{BASE}/{page_id}/0/{date_str}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if data[:4] == b"%PDF":
                log.info(f"  downloaded {date_str} via page {page_id} ({len(data)} bytes)")
                return data, url
        except Exception as e:
            log.debug(f"  {date_str} page {page_id}: {e}")
    return None, None


def extract_text(pdf_bytes):
    import io
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def split_notices(pages):
    """Join pages with a marker, then split into per-notice blocks on the
    county clerk's own standalone 6-digit control number -- see
    CONTROL_NUM_RE's comment for why this beats splitting on the "NOTICE
    OF ... SALE" title text (repeated as a running header on some
    multi-page notices).
    """
    full = "\n---PAGEBREAK---\n".join(pages)
    starts = [m.start() for m in CONTROL_NUM_RE.finditer(full)]
    if not starts:
        # fall back to title-text splitting for a PDF format with no
        # control-number stamps at all
        starts = [m.start() for m in NOTICE_START_RE.finditer(full)]
        if not starts:
            return []
    blocks = []
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(full)
        blocks.append(full[s:end])
    return blocks


def clean_city(raw):
    return re.sub(r"\s+", " ", raw).strip().upper()


def clean_street(raw):
    """Barcode/tracking noise (long digit runs, sometimes with an OCR'd
    stray letter breaking it up, e.g. '000000 I 0860716') sometimes rides
    along after the real street text on the header-line match path. Cut
    the string at the first token that's mostly digits and 5+ chars."""
    s = re.sub(r"\s+", " ", raw).strip().rstrip(".,")
    tokens = s.split(" ")
    out = []
    for t in tokens:
        digits = sum(ch.isdigit() for ch in t)
        if len(t) >= 5 and digits >= len(t) - 1:
            break
        out.append(t)
    return " ".join(out).strip().rstrip(".,") or s


def extract_address(block):
    m = ADDR_LABEL_RE.search(block)
    if m:
        return clean_street(m.group(1)), clean_city(m.group(2)), m.group(3), "label"

    m = HEADER_ADDR_RE.search(block)
    if m:
        return clean_street(m.group(1)), clean_city(m.group(2)), m.group(3), "header"

    m = ADDR_LABEL_NOCITY_RE.search(block)
    if m:
        return clean_street(m.group(1)), clean_city(m.group(2)), m.group(3), "label_nextline"

    return "", "", "", "none"


def extract_owner(block):
    m = GRANTOR_A_RE.search(block)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
    m = GRANTOR_B_RE.search(block)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
    return ""


def extract_lender(block):
    m = MORTGAGEE_B_RE.search(block)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
    m = MORTGAGEE_A_RE.search(block)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
    return ""


def extract_doc_number(block, sale_date, idx):
    m = INSTRUMENT_RE.search(block)
    if m:
        return m.group(1)
    m = FILE_NO_RE.search(block)
    if m:
        return m.group(1)
    return f"GDL-{sale_date.strftime('%Y%m%d')}-{idx:03d}"


def build_record(block, sale_date, idx):
    street, city, zip_, addr_source = extract_address(block)
    owner = extract_owner(block)
    lender = extract_lender(block)
    loan_m = LOAN_AMT_RE.search(block)
    loan_amount = loan_m.group(1).replace(",", "") if loan_m else ""
    doc_number = extract_doc_number(block, sale_date, idx)

    flags = ["NEW"]
    if not street:
        flags.append("NO ADDRESS - LEGAL DESC ONLY")
    if not owner:
        flags.append("NO OWNER - PARSE MISS")

    days_until = (sale_date.date() - TODAY_CT.date()).days

    return {
        "type": "NOF",
        "source": "guadalupe_pdf",
        "county": "guadalupe",
        "owner": owner.title() if owner else "",
        "address": street if street else "",
        "mail_addr": "",
        "city": city,
        "zip": zip_,
        "school_dist": "",
        "date_filed": "",
        "sale_date": sale_date.strftime("%m/%d/%Y"),
        "days_until_sale": days_until,
        "run_ts": RUN_TS,
        "is_new": True,
        "duplicate": False,
        "absentee": False,
        "flags": flags,
        "doc_number": doc_number,
        "lender": lender,
        "loan_amount": loan_amount,
        "loan_date": "",
        "trustee": "",
        "sale_date_arcgis": "",
        "tenure_years": None,
        "tenure_score_bonus": 0,
        "prop_id": "",
        "deed_date": "",
        "last_sale_amt": "",
        "appr_history": [],
        "appr_trend": "",
        "appraised_value": "",
        "annual_taxes": "",
        "land_value": "",
        "stacked": False,
        "score": 8 if street else 4,
        "_addr_source": addr_source,  # diagnostic only, stripped before commit
    }


def norm_addr(rec):
    a = (rec.get("address") or "").strip().upper()
    sd = (rec.get("sale_date") or "").strip()
    return (a, sd) if a else None


def load_known_docs():
    if not RECORDS_PATH.exists():
        return set(), set(), []
    prev = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
    known_docs = {r.get("doc_number") for r in prev}
    # The pre-existing manually-seeded records (source=guadalupe_pdf_manual)
    # use their own doc-number scheme (transcribed TS-/case IDs) that never
    # matches this script's own doc numbers, so doc_number alone missed 21
    # of 37 as true duplicates on a live test run (2026-09-18) -- same
    # address+sale_date already covered by a manual entry, re-added as a
    # "new" auto entry with none of that record's existing ARV/ghl_pushed
    # enrichment. Guard on (address, sale_date) too.
    known_addrs = {norm_addr(r) for r in prev if norm_addr(r)}
    return known_docs, known_addrs, prev


def main():
    known_docs, known_addrs, prev_records = load_known_docs()
    new_records = []
    skipped_dupe_addr = 0
    total_blocks = 0
    addr_hits = 0

    for sale_date in upcoming_sale_dates():
        pdf_bytes, url = download_pdf(sale_date)
        if not pdf_bytes:
            log.info(f"{sale_date.strftime('%Y-%m-%d')}: no PDF posted yet")
            continue
        pages = extract_text(pdf_bytes)
        blocks = split_notices(pages)
        log.info(f"{sale_date.strftime('%Y-%m-%d')}: {len(blocks)} notices in PDF ({url})")
        total_blocks += len(blocks)

        for idx, block in enumerate(blocks):
            rec = build_record(block, sale_date, idx)
            if rec["doc_number"] in known_docs:
                continue
            na = norm_addr(rec)
            if na and na in known_addrs:
                # covers both "already in records.json" and "already added
                # earlier in this same run" (e.g. one real notice that this
                # PDF's messier page-stamp layout split into two blocks,
                # confirmed live 2026-09-18 on the Oct 6 PDF: 309 Sunrose
                # Lane came back twice under two different doc numbers)
                skipped_dupe_addr += 1
                continue
            if rec["address"]:
                addr_hits += 1
            known_docs.add(rec["doc_number"])
            if na:
                known_addrs.add(na)
            new_records.append(rec)

    log.info(f"ADDRESS extraction: {addr_hits}/{len(new_records)} new notices yielded a street address "
              f"(out of {total_blocks} total notices seen across all sale-date PDFs)")
    log.info(f"Skipped {skipped_dupe_addr} notices already covered by an existing record (same address+sale_date)")

    for r in new_records:
        r.pop("_addr_source", None)

    # drop past-sale-date prev records the same way purge_past_leads.py would on next run;
    # just append new ones here, purge runs separately per repo convention.
    all_records = prev_records + new_records
    RECORDS_PATH.write_text(json.dumps(all_records, indent=2), encoding="utf-8")
    print(f"new={len(new_records)}")


if __name__ == "__main__":
    main()
