# Guadalupe County Motivated Seller Leads

## Status: manually seeded, not yet automated

Guadalupe County (Seguin / New Braunfels / Cibolo / Schertz) does not run the
same Tyler/publicsearch.us portal as Bexar and Nueces. Foreclosure notices are
instead published as monthly compiled PDFs at
`https://www.guadalupetx.gov/page/open/5894/0/<sale-date>` — one URL per
upcoming sale date (first Tuesday of each month), which the county appends to
as new notices get posted in the weeks before that sale.

`dashboard/records.json` (22 records as of 2026-08-10) was built by manually
pulling and parsing the September 1, 2026 and October 6, 2026 sale-date PDFs.
Of ~35 total notices across both, 22 had a real resolvable street address
("Commonly known as" or "Property Address" line) and were included; the
remaining ~13 only gave a legal description (lot/block/subdivision) with no
street address, and were excluded rather than guessed at — same category of
gap as Nueces's missing-address issue.

## Live dashboard
https://e4property.github.io/guadalupe-leads/

## Not yet built
A real automated scraper — download each month's PDF on a schedule, parse
with `pdfplumber` (or similar), split into individual notices, extract
structured fields, and commit to `records.json` automatically, matching the
Bexar/Nueces pattern (`.github/workflows/scrape.yml`). The county's own list
of upcoming sale dates is visible at
`https://www.guadalupetx.gov/page/open/5894` and currently runs through
2026-10-06+ several more months.

See `scratchpad/parse_guadalupe.py` (used to build the current seed data) as
a starting point — it handles most template variants but needs refinement
for full automation (some notice templates split address across table cells
in a way that trips up plain-text extraction).
