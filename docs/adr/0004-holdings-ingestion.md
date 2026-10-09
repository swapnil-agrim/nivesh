# ADR-0004: India holdings ingestion (E2)

Status: accepted

## Schema (migration 0003)
- `security` gains `amfi_code`, `market` (default `IN`) and `unresolved`. The PID `asset_type` is the
  existing `asset_class` column. `account.name` is the PID `label`; `account(kind, name)` is unique.
- An unresolved ISIN is stored as `symbol=<ISIN>, exchange='ISIN', unresolved=1`, which fits the
  existing `UNIQUE(symbol, exchange)` without relaxing it.
- New tables `ingest`, `ingest_holder`, `holding_snapshot`, `txn` (not `transaction`, a reserved
  word) and `meta`. Money and quantities are TEXT holding exact `Decimal` strings; sums happen in
  Python. Snapshots are append-only per ingest.
- "Latest view" per `(account_id, holder_ref)` is the newest `ingest.as_of` (then highest id) that
  lists that pair in `ingest_holder`, so a fully redeemed folio disappears after a newer statement
  and an older statement ingested late never replaces a newer one.
- `txn` is deduplicated across overlapping statements by a unique index over account, security,
  holder, date, type, quantity, amount and an `occurrence` ordinal (the nth identical row inside one
  statement), so two identical same-day SIP debits are both kept.
- File dedup uses `ingest.digest` (SHA-256 of the file). `source_label` is letters only or at most
  8 hex characters, because a 9+ digit run would be masked by redaction.

## Redaction-safe field names
`redact_json` masks keys containing `token`, `secret`, `password`, `folio`, `aadhaar`, `apikey`,
`authorization`, `cookie`, or an `_`-part in `pan, account, acct, key, email, phone, mobile,
address, dp, client`, and any string with a 9-18 digit run. Payload fields therefore use
`holder_ref`, `source_label`, `isin`, `quantity`, `avg_cost`, `price`, `price_basis`, `value_inr`,
`weight`, `as_of`, `source`, `txn_type`, `amfi_code`, `plan`. Numbers are JSON numbers.
`redact.py` is not changed.

## Dependency: casparser (NFR-6)
- `casparser==1.4.1`, MIT licence, a maintained parser for CDSL, NSDL, CAMS and KFintech CAS PDFs;
  writing and maintaining our own PDF parsers is out of proportion for a single-owner tool.
- Pinned exactly. Transitives: `casparser-isin` (offline ISIN database, about 47 MB), `pypdfium2`
  (native wheel), `rapidfuzz`, `python-dateutil`, `colorama`, `six`. Docker image growth is about 60 MB.
- `pip-audit` was run after adding it and reported no known vulnerabilities; CI runs it
  and a future advisory on casparser or its transitives blocks the build.
- It ships no `py.typed`, so mypy has an `ignore_missing_imports` override for `casparser.*`.
- Only `nivesh_adapters/cas.py` imports it, lazily inside the parse call (so `nivesh --help` and
  the MCP servers do not load pypdfium2 or the ISIN database).

## casparser model facts (read from the installed 1.4.1 source)
- `read_cas_pdf(filename, password, output="dict", sort_transactions=True)` returns `CASData`
  (CAMS/KFintech) or `NSDLCASData` (NSDL/CDSL). `IncorrectPasswordError` subclasses `CASParseError`.
- `CASData`: `statement_period.from_` / `.to` (strings as printed), `folios[].schemes[]` with
  `scheme`, `isin`, `amfi`, `open`, `close`, `close_calculated`, `valuation{date, nav, cost, value}`,
  `transactions[]{date, description, amount, units, nav, balance, type}`, `parse_warnings`,
  `investor_info{name, email, address, mobile}`. `Folio.folio`, `.PAN` and `.name` are PII.
- `NSDLCASData`: `accounts[]{dp_id, client_id, equities[], mutual_funds[], bonds[]}`, `nps`,
  `parse_warnings`, `investor_info`. Equities carry `isin`, `num_shares`, `price`, `value` and a
  `symbol`/`exchange` backfilled from the bundled ISIN database. Demat MFs carry `balance`, `nav`,
  `value`, optional `avg_cost`, `amfi`. Bonds carry `num_bonds`, `value`, `market_price`.
- Everything that identifies a person (investor info, owners, PAN, DP ID, client ID, folio) is
  dropped or hashed at the boundary; see "PII boundary".

## PII boundary (NFR-3)
Only `nivesh_adapters/cas.py` imports casparser. Its return type `ParsedStatement` holds only
`Holding`, `Txn`, a date, a kind, warnings and `holder_ref` values: no field can carry a name, PAN,
e-mail, mobile, address, DP/client ID or folio. `holder_ref = hash_ref(value, FOLIO_SALT)`: 12
lowercase letters from HMAC-SHA256 (letters only, because a hex digest has about a 4 percent
chance of containing a 9+ digit run that redaction would mask). Demat ref hashes `dp_id/client_id`;
RTA ref hashes the folio. casparser exceptions are replaced by fixed messages (they can echo
content); the password never appears in a message. A fingerprint of the salt is stored in `meta`
at first ingest and `nivesh ingest` refuses a different salt, since changing it would change every
`holder_ref`.

## Consolidation precedence (ST-2.8)
Key is the ISIN, or `symbol:exchange` when a row has none. Rows of one source and key are summed
(cost quantity-weighted over rows that have cost). Across sources the order InvestRight >
depository CAS > RTA CAS > CSV decides quantity, price and value; `avg_cost` comes from the
highest-precedence source that has one. `investright.demat_ref` (a `holder_ref` printed by
`nivesh ingest`) scopes the InvestRight-versus-depository overlap to one demat: depository rows of
other demats then add to the holding instead of being treated as duplicates, and reconciliation
compares only that demat. Unset, the match is by ISIN across all demats (the literal spec) and a note
says so. Ceiling: several InvestRight-linked demats are not modelled (D9).

## Security resolver (ST-2.4)
`SecurityResolver.resolve(isin) -> Resolved | None` is a Protocol. `TableResolver` answers from the
`security` table (ignoring unresolved placeholders); the real master is ST-4.8 and replaces the
wiring later (D1). Depository CAS rows fall back to the symbol that casparser backfills from its
bundled ISIN database before using the ISIN placeholder.

## InvestRight wire format: unverified (D2)
The seed `investright-mcp` is absent, so the client is written from the spec. Endpoints, header and
query rules, the `data.net` unwrap and error code 60014 are from the spec. Base URL, login URL, the
request-token query parameter, the access-token response shape, the row field names (`isin`,
`security_id`, `name`, `quantity`, `average_price`, `close_price`, `last_price`, `ltp`) and the LTP
request body are this repository's assumptions, marked "per spec, unverified against live API" in
code docstrings, the runbook and here. A wrong guess fails loudly (data-quality error or
`InvestRightError`), never silently. Live verification is deferred.

## Session token
`<data_dir>/investright_session.json`, mode 0600, replaced atomically (`replace_private`), holds the
access token and the IST issue date; it is invalid once the IST date changes. The callback listener
binds the literal loopback address only. API key and secret come from the keychain, never argv.

## Contract note
New surface: CLI `login`, `sync`, `ingest`, `import-csv`; MCP server `holdings` with seven read-only
tools (explicit allow-list entries in `.claude/settings.json`); config block `investright`; secrets
`INVESTRIGHT_API_KEY`, `INVESTRIGHT_API_SECRET`, `CAS_PASSWORD`, `FOLIO_SALT`.
