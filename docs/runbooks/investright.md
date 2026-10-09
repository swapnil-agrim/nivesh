# InvestRight (HDFC Securities) API runbook

One-time setup, then a short login each trading day. Nivesh is read-only: it only reads
holdings, positions, margins and LTP. Parts of the wire format are unverified against the live API
(login URL, request-token parameter, response field names); they come from the product spec and
are listed in the last section.

## One-time registration
1. Log in at developer.hdfcsec.com with your HDFC Securities credentials.
2. Open My Apps, then Create.
3. Redirect URL: `http://127.0.0.1:8765/callback` (exactly; the login helper listens on loopback only).
4. Static IP: enter the machine's public egress IP as the primary static IP. Enter a second
   address as the secondary static IP if you have one (for example a standby VM).
5. Select the Trading API product (read access is all Nivesh uses).
6. Submit and activate the app. Note the API key and API secret shown once.
7. Store them in the OS keychain (hidden prompt; never on the command line):

       nivesh secrets set INVESTRIGHT_API_KEY
       nivesh secrets set INVESTRIGHT_API_SECRET

8. Also set the salt used to hash folio and demat identifiers, and the CAS PDF password:

       nivesh secrets set FOLIO_SALT
       nivesh secrets set CAS_PASSWORD

   Keep `FOLIO_SALT` stable. `nivesh ingest` records a fingerprint of the salt and refuses to run
   with a different one, because changing it would change every stored `holder_ref`.

## Finding the machine's public IP
Run `nivesh egress-check` (set `registered_ip` in `config/nivesh.yaml` first). It prints the public
IP seen from this machine and exits 1 when it differs from the registered one. On a VM, use the
reserved static IP (see `docs/deploy/vm.md`).

## What a mismatch looks like
- HTTP 401 or 403 from the API raises `SessionExpired`. Its message says the session may have
  expired or the static IP may not match the registered IP. Log in again first; if it persists,
  compare `nivesh egress-check` with the IP registered on the developer portal.
- A response with status other than success raises `InvestRightError` carrying HDFC's own message
  and numeric code (for example 60014). The code and message are shown as returned.

## Daily login
Run `nivesh login`. It opens the HDFC login page; complete OTP and consent. The redirect lands on
`http://127.0.0.1:8765/callback`, the request token is captured, and exchanged for an access token.
If the browser is on another machine, run `nivesh login --paste` and paste the full redirect URL
(or just the request token) at the hidden prompt.

The token is stored in `<data_dir>/investright_session.json` (mode 0600) with the IST issue date and
is treated as invalid once the IST date changes. A failed exchange prints HDFC's message and stores
nothing. Check state with the `session_status` tool of the holdings server.

On a VM, forward the callback port over SSH, as in `docs/deploy/vm.md`:
`ssh -L 8765:localhost:8765 <vm>`. A real-socket test of the listener over the tunnel is still to do.

## Using the data
- `nivesh sync [--ltp]` stores InvestRight holdings (previous close, or LTP with `--ltp`).
- `nivesh ingest` reads CDSL/NSDL and CAMS/KFintech CAS PDFs from `<data_dir>/inbox`.
- `nivesh import-csv PATH [--preset zerodha|groww|upstox --label NAME]` loads other accounts.

## Unverified against the live API
The following were written from the spec and are unverified against the live API: base URL, login
URL and request-token parameter name, access-token response shape, field names of holdings,
positions, margins and LTP rows, the exact 401/403 versus error-code wording, any User-Agent
requirement, and the body of the LTP lookup. A wrong guess fails loudly (data-quality or
`InvestRightError`), never silently. Correct the client, fixtures and this page after the first
live run.
