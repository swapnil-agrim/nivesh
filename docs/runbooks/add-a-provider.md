# Runbook: adding a fundamentals provider (ADR-0016)

A statements, shareholding or actions vendor is one adapter, a few pure mappers, one provider class
and one registration line. A contract suite then holds every provider to the same rules. Nothing
here makes a network call: tests use `httpx.MockTransport` and hand-written fixtures, there is no
live call from tests, and a vendor shape you have not seen yourself stays marked UNVERIFIED. The
Upstox slot (`nivesh_adapters/fundamentals_upstox.py`, `tests/fixtures/market/upstox_*.json`) is
the worked example of a vendor added without an account; the Tapetide provider is the example of a
metered one.

1. **Decide what the vendor really offers.** What does it key a company on (the NSE symbol, the BSE
   scrip code or the ISIN), which of the capabilities (statements, shareholding, pledge, actions,
   estimates, bank_metrics, news) does it return, in which units, and does it give a filing date?
   If it gives none, every `filed_at` is an estimate and the provider must say so
   (`filing_dates="estimated"`).
2. **Write fixtures from the documentation.** Put them under `tests/fixtures/market/`. Give each a
   `_comment` that says it is UNVERIFIED: hand-written from the public API documentation, not from a
   live response. Money in crore (small numbers), no run of nine or more digits, no email or phone
   shape, no secret. Check with `nivesh pii-scan tests/fixtures/market/<file>` (exit 0).
3. **Write the adapter** in `nivesh_adapters/fundamentals_<name>.py`.
   - Subclass `Adapter`: `name`, `source`, `_fetch(**params)` returning JSON-native data and its
     `as_of`, and a bounded `validate` (finite numbers, a magnitude ceiling, iterative so a nested
     document cannot overflow the stack).
   - GET only where the vendor allows it, and no public method with a write verb in its name; the
     scan in `tests/safety/test_tool_safety.py` discovers every `Adapter` and fails on one.
   - Get the HTTP client from `make_client(self.name)` (`nivesh_adapters.recorder`), never from
     `httpx.Client` directly, so record/replay and the test network block apply.
   - Read the secret per call with `get_secret(NAME)` from `nivesh_core.secrets`. It must not live
     in an attribute, a URL, a query parameter, a message, a `repr` or a cache parameter; send it
     only in the header the vendor documents (build the scheme word by concatenation in code so
     the secret scanner stays quiet) and scrub any vendor text you echo.
   - Raise `NotConfigured` (zero requests) when the owner must set something up: no token, no
     identifier. Raise `SourceUnavailable` for a bad reply and `RateLimited` for a quota or 429, and
     halt the adapter for the rest of the run: a quota failure is never retried.
   - If the vendor meters calls, accept `use_budget(budget)` and call `budget.reserve(tool, symbol)`
     immediately before the one request that leaves the machine and `settle(call_id, outcome)` in a
     `finally`; refuse to send at all without a budget. Copy `_rpc_inner` in
     `fundamentals_tapetide.py`.
4. **Write the mappers** as pure functions from the decoded document to `StatementRow` and
   `ShareholdingRow`: absolute INR (crore times 10 million, lakh times 100 thousand), EPS per share,
   share counts as counts, percentages within 0 to 100, and `period_end <= filed_at <= today` (clamp
   a filing date that falls after today and count it). A figure the vendor does not give is left
   out with a skipped line that says why; it is never a zero.
5. **Write the provider**: subclass `FundamentalsProvider`, declare `info = ProviderInfo(...)` and
   implement `collect(ctx)`, which fetches through `ctx.get`, maps, and returns a `ProviderResult`;
   a provider never writes a store and never imports the ingest. State the truth in `ProviderInfo`:
   `capabilities` (the contract suite checks them against what the harness produces), `keyed_on`,
   `credential` (the default secret name), `metered`, and the three honest labels `revenue_basis`,
   `profit_basis` and `filing_dates`, which `nivesh fundamentals compare` prints and `fa` quotes.
6. **Register it with one `BUILTIN` line** in `nivesh_adapters/providers.py`:
   `"name": "nivesh_adapters.fundamentals_name:NameProvider"`. The class is imported on first use.
7. **Add a disabled config entry** under `fundamentals.providers` in `config/nivesh.yaml`, for
   example `- {name: NAME, enabled: false, role: primary, credential: ref:NAME_SECRET}`. Order in
   the list is priority. A secret is only ever a `ref:` name. A metered vendor also needs a `plan`
   (`daily` and/or `monthly`, `min_interval_s`, `month_start_day`, `reserve_pct`), set from what
   the owner knows of the vendor plan; a metered provider without one is a `ConfigError` and is
   never built. If you add the entry to the shipped example, add it to the defaults in
   `nivesh_core/fundamentals_config.py` too (a test asserts the two are equal).
8. **Set the secret**, as the owner, with `nivesh secrets set NAME` (hidden prompt, OS keychain);
   `nivesh secrets check NAME` says whether it is set and never prints it.
9. **Test it**, with no network and no real secret.
   - A module for the adapter and the mappers on `MockTransport` (model it on
     `tests/adapters/test_fundamentals_upstox.py`): no token raises `NotConfigured` with zero
     requests; the credential never appears in a URL, message, `repr` or cache payload; error
     replies map to the right exception; every mapper branch has a case.
   - A harness entry in `tests/provider_fx.py` (`Harness`: `serve`, `adapter`, `provider`, `reply`,
     `fetch_params`, `known`, `dropped`, `early`, `cred`, and a `probe` for capabilities that
     `ProviderResult` has no slot for) appended to `HARNESSES`. The parametrized contract suite in
     `tests/adapters/test_provider_contract.py` then runs for it: point in time, `as_of`, unit
     scaling, unavailable with a reason and never zero, no secret anywhere, no network without the
     mock. `test_every_registered_provider_has_a_harness` fails until the entry exists.
   - The adapter's class name added to `MARKET_ADAPTERS` in `tests/safety/test_tool_safety.py`.
   - Every test that resolves a credential requests the `fake_keyring` fixture, so the owner's
     keychain is never read; build dummy values by concatenation and give them neutral names.
   - `FakeProvider` in `tests/provider_fx.py` is the smallest provider; a test registers it with
     `monkeypatch.setitem(REGISTRY, "fake", FakeProvider)`, which is how to try an idea without
     touching production code.
10. **Run the checks**: `uv run pytest -p no:cacheprovider -q tests/adapters/test_provider_contract.py
    tests/safety/test_tool_safety.py`, then `make check`.

Turning it on is the owner's decision. Enable the entry (`enabled: true`), set the secret, ingest
one company, and run `nivesh fundamentals compare SYMBOL` to see how the new provider differs from
the one you read today; it never averages and never picks a winner. Only a response seen with your
own eyes lets you remove an UNVERIFIED marker, and only for what you saw. For a metered vendor,
`nivesh quota status --vendor NAME` shows the local count; calls other clients of the same plan spent
are entered with `nivesh quota set NAME --used-today N`, because the ledger only knows this
machine's attempts.
