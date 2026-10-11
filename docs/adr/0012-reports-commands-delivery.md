# ADR-0012: Reports, commands and delivery (E10)

Status: accepted. E10 adds one report model with Markdown and HTML serialisers, purpose-built
templates, a citation check on every number, the `/research`, `/ta`, `/fa`, `/brief`, `/ideas`,
`/watch`, `/status` and `/ingest` commands, and opt-in delivery to the owner's own channels
(ST-10.1 to ST-10.7; ST-10.8 is deferred). There is **no new MCP server**, **no new agent**, no
new prompt or schema, no new allow-list entry, no migration and **no new dependency** (stdlib
`html`, `email`, `smtplib`, and the existing httpx). Agents see only read-only tools and are tested
against the scripted fake SDK; no test uses the network, a model, personal data or a stored value.
Every report is the owner's own research and not investment advice.

## Owner decisions (safest option chosen; owner sign-off is X10)
- **OD-1 Run directory.** A run lives in `runs/<date>/<run_id>/` with the IST date of the start
  (`run.run_dir` stores that relative path). `find_run_dir(data_dir, run_id)` resolves it from the
  integer alone (no database read, no path built from text): the dated directory first, then the
  legacy `runs/<run_id>`. Old stores stay readable; nothing is moved, and backup and restore still
  copy `runs/` wholesale. `replay` and `review_flags` use the resolver. The legacy shape is kept
  working by a test that builds an old tree by hand.
- **OD-2 HTML.** Standard library only: `html.escape`, control characters removed, one inline
  `<style>`, no script, no remote asset, no link built from model or news text. Markdown and HTML
  are two serialisers of one `Report` model. Templates are Python builders; `templates/` stays
  CSV-only.
- **OD-3 Slash commands** are `.claude/commands/<name>.md` files (description, argument hint, an
  `allowed-tools` list of read-only `mcp__` names) that name the `nivesh <command>` handler.
  Interactive sessions deny the shell, so the file documents the contract and tells the owner what
  to run. A test pins the set and the tool lists; `.claude/settings.json` is unchanged.
- **OD-4 Delivery fails closed and is opt-in.** With no `delivery.channels` nothing is sent. The
  subject, the chat text and the HTML page are scanned with the PII scanner before every send; a
  finding blocks that channel and the log gets kinds and positions only, never the matched text.
  The other channels are not blocked by it. A draft report is delivered with its banner, not
  suppressed.
- **OD-5 Citation check.** Every number in a checked block must match evidence from the run: tool
  results in the trace, the snapshot, and `report_input.json` (facts the code produced, never model
  text or a rendered block). The window is a half unit of the last displayed digit, bounded by
  `report.max_tolerance_digits` and never widened to hide a miss. Numbers the renderer produces
  itself are registered through `Num` (clamped weight, coverage, dates, derived rupee amounts) and
  match by value. **Entry-zone numbers a model proposes are not exempt**: they are held to the
  evidence like any other, so a zone with an invented level yields a DRAFT stamp. A miss stamps the
  `DRAFT - UNVERIFIED NUMBERS` banner with each unmatched figure, lowers an "ok" run to
  `needs_review` (a free-text status; an error stays an error) and exits 0. Each evidence number
  is tagged with its security, so another security's tool result never matches.
- **OD-6 Brief is a deterministic brief.** `nivesh brief` makes no model call and costs nothing;
  what the stores cannot supply (sector leaders and laggards, breadth without a loaded universe)
  is listed as "unavailable (reason)". It is at most 400 words of prose plus one table, footer and
  sources excluded.
- **OD-7 `/ta` and `/fa` are engine-only by default.** `--note` saves an engine-result report at
  zero cost; `--analyst` adds one quick analyst call under the budget gate. The prefixes `NSE:` and
  `US:` resolve an ambiguous ticker (the market, then the exchange), after the `id:<n>` form.
- **OD-8 `/review-portfolio` is not built here.** The portfolio-review and fund-doctor templates
  ship and are tested; the command that assembles them from the xray, a saved review run, the fund
  doctor, rebalance and tax services, and the committee fan-out, stay on #83 (X5). Because no
  command yet produces them, `deliver` excludes runs of `review-portfolio` and `fund-doctor` (which
  list holdings) unless `--include-holdings` is passed.
- **OD-9 Report files and kinds.** Reports are saved with the owner-only writer directly, not
  through `RunStore`, so the committee file kinds are unchanged. The trace gets a name and digest
  per file (`report_saved`), never content. Report facts are never traced as engine calls
  (`replay` fails on unregistered names). Four files per run: `report.md`, `report.html`,
  `report_input.json` and `report.json` (the report itself, with its banner, which `deliver`
  loads and which is not evidence).

## Decisions
1. **Money and lakh/crore.** `nivesh_engine/money.py` (Decimal only) formats rupees in lakh and
   crore (above 1,000 crore the count stays in crore) and dollars with the rate and its date. A
   `Num` keeps the value in displayed units, so no 9-18 digit run reaches a saved file (the PII
   scanner would flag it as an account-like number).
2. **Delivery wire formats are unverified.** Telegram uses the Bot API `sendMessage` then
   `sendDocument` (plain text, no parse mode); Slack posts JSON to an incoming webhook with `&`,
   `<` and `>` escaped; email is a text body with the HTML page attached, over SMTP with TLS from
   the start. These follow the public documentation and were never run against a live service (X7).
3. **Delivery config.** `delivery:` holds `channels`, `telegram_bot`, `telegram_chat`,
   `slack_hook`, `smtp_host`, `smtp_user`, `smtp_auth`, `mail_to`, `max_attempts` (1 to 5, default
   3) and `timeout_s`. Every connection detail is a `ref:NAME` reference; a literal value is
   rejected without being echoed, and recipients are references too. Field names avoid every
   sensitive word, so the redaction helper never matches them. References are resolved at send
   time only and never traced, printed or put in an error text.
4. **Retry.** A timeout, a connection error, HTTP 429 or 5xx, or a mail connection error is
   retried with doubling waits (1, 2, 4 seconds) up to `max_attempts`; any other 4xx or a refused
   login is not. A failure is logged to the trace as the attempt number and a short reason (a
   status code or an exception type), never an address or payload, and returns an outcome object;
   it never raises, never changes the run status and never changes the exit code (a warning is
   printed).
5. **Injected clients; the recorder exemption.** The HTTP client and the mail connection are
   injected, so tests use `MockTransport` and a fake mail class and send nothing. `delivery.py`
   never calls `recorder.make_client`: the replay recorder persists request URLs in record mode,
   and the Telegram and Slack addresses carry the bot or hook value. `raise_for_status` is not
   used either (its message embeds the URL). This is an exemption from the adapter convention
   (every other adapter goes through the recorder) and a test pins it.
6. **`nivesh deliver RUN [--channel NAME] [--include-holdings]`** loads `report.json` and
   `report.html` through `find_run_dir`, records its own plain run (so failures land in that trace)
   and prints one line per channel. `--deliver` on `research`, `brief` and `ideas` sends right
   after the report is checked and saved. The message names the saved page by its path under the
   data directory, not an absolute path.
7. **Where logic lives.** Pure logic is in `nivesh_engine` (`money`, `citations`, both in the
   deterministic-engine scan: no float, random or clock), services and transports in
   `nivesh_adapters`, thin commands in `nivesh_cli`, config models in `nivesh_core`. The new
   modules pass the write-word scan; the senders are named `send_message` and `deliver_run`.

## Known limits
- **Gap lines are not citation-checked.** `Report.gaps` mixes lines the code wrote with lines
  copied from analyst views (`data_gaps`), and the two are not separable in the report model, so
  `checked_texts` leaves gaps out rather than flag code-written lines. A number in a model-written
  gap line is therefore unchecked.
- **Holdings gating is narrow.** `HOLDINGS_COMMANDS` protects only `review-portfolio` and
  `fund-doctor`, which are not implemented yet (S6 deferred, X5). Research and ideas reports are
  not gated on holdings content.
- **The brief, `ta` and `fa` citation check is tautological for code-registered numbers.** Numbers
  the renderer registers always match as written, so the check cannot catch a wrong registered
  value there; it only catches numbers the code did not produce.
- **Registered-number matching is not scoped per block.** A registered number matches anywhere in
  the report, so a number correct in one block is accepted in another.
- Continuing a failed `ideas` report step keeps the ledger rows and marks the run `needs_review`.

## Contract changes
- `report:` and `delivery:` config blocks are additive and optional (the examples equal the
  defaults and contain references only). The run directory shape is dated (see OD-1); `replay`,
  `review_flags` and backup keep the old shape readable. `run.status` may be `needs_review`. The
  validator contract for the later scoring epic: a report's checked text may only cite numbers in
  `report_input.json`, the snapshot or tool results of the same run.
- New commands only: `research`, `brief`, `deliver`, `ta|fa --note`, plus extended `status`
  health lines and `ingest` reconciliation. No agent spec, prompt, schema, MCP file or
  `.claude/settings.json` entry changed.

## Deferred
- X1: the `/scorecard` command and scorecard data (ST-12.2 scoring job, ST-12.3); the template and its empty state ship.
- X2: `/decide` (#84) and `/review-funds` (#59); no ST-10.x criterion needs them.
- X3: ST-10.8 report archive, search and verdict history (needs `calls_for_security` on the ledger).
- X4: a model-written brief narrative; sector leaders and laggards (sector-index mapping).
- X5: `/review-portfolio` (#83): the deterministic assembly, the macro-once plus per-holding quick committee fan-out and the NFR-10 timing.
- X6: the `--analyst` extras beyond one quick analyst call, if the owner wants more depth.
- X7: live verification of the Telegram, Slack and SMTP wire formats, and hosting or linking the HTML beyond the local file.
- X8: a live SDK smoke of `/research`, the citation false-positive rate on real runs, and R2 watch alerts (#97 remainder, E11).
- X9: replay registration of the `money` and `citations` engines (#79).
- X10: owner sign-off of the defaults: IST-dated run directory, tolerance cap, retry count and timeout, DRAFT banner wording, entry-zone numbers held to the evidence, holdings reports excluded from delivery.
