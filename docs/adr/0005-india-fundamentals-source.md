# ADR-0005: India fundamentals source (ST-4.1, closes Q-2)

Status: accepted, **provisional**. This choice comes from a desk comparison only. The live
30-company sampling (5 banks/NBFCs among them), the 10-company manual check against annual
reports, and the paid trial have not been run (no network or paid account in the build loop).
They are deferred to the follow-up issue "E4/ST-4.1: run the 30-company India fundamentals
sampling and confirm or revise ADR-0005" (deferred item D1). If that sampling picks a different
winner, only the mapper in `nivesh_adapters/fundamentals_in.py` changes. The tables
(`fundamental`, `shareholding`) and the MCP tools stay the same.

## Context
PID section 12 leaves the Indian fundamentals source open (Q-2: "pick after data-quality spike",
free first). ST-4.4 needs at least 5 years of annual and 12 quarters of statements with
`period_end` and `filed_at` (point in time), bank/NBFC fields (GNPA, NNPA, NIM, CASA, CAR) and
quarterly promoter pledge.

## Desk comparison
Every cell below is from public documentation and general knowledge. None of it has been
checked against live data. Entries marked (unverified) need the D1 sampling.

| Candidate | Coverage | History depth | Restatement handling | Accuracy method | Licence |
| --- | --- | --- | --- | --- | --- |
| Tapetide MCP | Listed NSE/BSE companies (unverified) | Several years claimed (unverified) | Unknown: may overwrite older values (unverified) | 10-company manual check vs annual report | Vendor terms unknown (unverified) |
| Screener-style source (scraped web pages) | Broad, about 5,000 companies | About 10 years of annual data, 12+ quarters | Overwrites in place, so there is no point-in-time history | 10-company manual check vs annual report | Site ToS forbid scraping and redistribution; **rejected as primary** |
| BSE XBRL (and NSE) financial-results filings | Every listed company files under SEBI LODR | XBRL filing mandatory from about 2011 for results; depth varies by company (unverified) | Each filing has its own `filed_at`, so revised results become new rows (append-only) | Tag values cross-checked against the PDF results in the same filing, plus the 10-company manual check | Regulator disclosures; the facts are public. Bulk access terms of the exchange sites need confirming (unverified) |
| Paid trial (vendor API) | Full (vendor claim) | 10+ years (vendor claim) | Vendor-specific; often point in time on premium tiers | 10-company manual check vs annual report | Paid, with redistribution limits; last resort under the free-first rule |

**Accuracy method (for the D1 check):** for 10 of the 30 sample companies (including 3
banks/NBFCs), compare revenue, operating profit, net profit, EPS, total debt, equity and, for
banks, GNPA/NNPA/CAR for the last 2 annual and 2 quarterly periods against the audited annual
report or the quarterly results PDF. A field is accurate if it is within 0.5 percent, or exact for
ratios given to two decimals. Report the mismatch rate per candidate and per field.

## Decision
- **Primary: exchange XBRL financial-results filings (BSE, with NSE as an alternate host).** These
  are the regulator source. They carry a native filing date, so point-in-time is exact and
  restatements stay as history. They are free, and the facts carry no redistribution
  restriction. `fundamentals_in.py` maps the Ind-AS XBRL tags to the standard items. The tag
  mapping is best effort and unverified (deferred D3).
- **Fallback: Tapetide MCP**, behind the same `StatementRow`/`ShareholdingRow` boundary, if D1
  shows XBRL coverage or depth gaps. Not implemented until D1 confirms availability, coverage
  and licence.
- Screener-style scraping is rejected as primary on licence/ToS grounds. A paid trial is the last
  resort.
- Q-2 is closed with this provisional choice. D1 confirms or revises it.

## Dependencies (NFR-6): none added
- `yfinance`: rejected. It pulls in pandas, numpy, lxml and more, and it is an unofficial scraper
  that breaks often. Prices call the public Yahoo chart JSON endpoint directly through `httpx`
  instead. That endpoint is also unofficial (ToS and breakage risk), and it is the fallback and
  cross-check for India and the primary for US.
- `feedparser`: rejected. RSS 2.0 and Atom are parsed with `xml.etree.ElementTree`. Any payload
  that contains a DOCTYPE or ENTITY declaration is rejected first, as an entity-expansion defence.
- `exchange_calendars`: rejected (it pulls in pandas and numpy). NYSE holidays are computed from
  rules. NSE holidays are owner-maintained config, and a year that is missing from config raises
  instead of guessing.
- `rapidfuzz`: not promoted to a direct dependency. Fuzzy name matching uses
  `difflib.SequenceMatcher` over an SQL-prefiltered candidate set of at most 300 names. It is
  already in the lock file as a transitive dependency, so swapping it in later needs no new
  install.
- pandas/numpy and OpenBB: not needed. Models are frozen pydantic rows with `Decimal` values.

## Wire-format caveats
The NSE/BSE XBRL tag names, the shareholding-pattern JSON shape and the exchange endpoints are
per spec and unverified against live sources (deferred D2/D3). The parsers fail loudly with
`DataQualityError` on shapes they do not expect, so a wrong guess never becomes silent bad data.
