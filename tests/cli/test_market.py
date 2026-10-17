import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from click.testing import Result
from typer.testing import CliRunner

import nivesh_cli.market as cm
from nivesh_adapters.master_sources import URLS, MasterSources
from nivesh_adapters.prices_in import IndiaPrices
from nivesh_adapters.prices_us import UsPrices
from nivesh_cli.main import app
from tests import pii_values as pv
from tests import tapetide_fx as tx
from tests.conftest import MemoryKeyring
from tests.market_fx import Feed
from tests.scan_fx import scan_output

FX = Path(__file__).resolve().parents[1] / "fixtures" / "market"
runner = CliRunner()
Env = tuple[list[str], Path]
FILES = {
    URLS["nse"]: "nse_equity_l.csv",
    URLS["amfi"]: "amfi_navall.txt",
    URLS["sec"]: "sec_tickers_exchange.json",
}
BSE_HOST_PATH = "BhavCopy_BSE_CM_0_0_0_"  # the BSE master is the dated bhavcopy (any day)
AMFI_MOVED = "https://portal.amfiindia.com/spages/NAVAll-moved.txt"


class Net:
    """MockTransport serving the master fixtures; counts requests, can fail one source."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls = 0
        self.fail = fail
        self.amfi_redirect = False  # answer the AMFI URL with a 302 to AMFI_MOVED
        self.bse_status = 200  # status of every dated BSE bhavcopy request

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        url = str(req.url)
        source = "bse" if BSE_HOST_PATH in url else next(k for k, v in URLS.items() if v == url)
        if self.fail == source:
            return httpx.Response(503)
        if source == "bse":
            if self.bse_status != 200:
                return httpx.Response(self.bse_status)
            return httpx.Response(200, text=(FX / "bse_scrips.csv").read_text())
        if source == "amfi" and self.amfi_redirect:
            return httpx.Response(302, headers={"Location": AMFI_MOVED})
        return httpx.Response(200, text=(FX / FILES[url]).read_text())


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    n = Net()
    monkeypatch.setenv("EDGAR_CONTACT", pv.edgar_contact())
    monkeypatch.setattr(
        cm, "_master_sources", lambda: MasterSources(httpx.Client(transport=httpx.MockTransport(n)))
    )
    return n


def securities(data: Path) -> int:
    c = sqlite3.connect(data / "nivesh.sqlite")
    try:
        return int(c.execute("select count(*) from security").fetchone()[0])
    finally:
        c.close()


def test_master_build_prints_counts_and_exits_0(cli_env: Env, net: Net) -> None:
    args, data = cli_env
    r = runner.invoke(app, [*args, "master", "build"])
    assert r.exit_code == 0, r.output
    assert "inserted" in r.output and "nse: 5 rows" in r.output
    assert securities(data) > 10


def test_master_build_from_dir_reads_local_files_without_network(cli_env: Env, net: Net) -> None:
    args, data = cli_env
    r = runner.invoke(app, [*args, "master", "build", "--from-dir", str(FX)])
    assert r.exit_code == 0, r.output
    assert net.calls == 0 and securities(data) > 10


def test_master_build_survives_one_failed_source_and_reports_it(cli_env: Env, net: Net) -> None:
    net.fail = "bse"
    args, _ = cli_env
    r = runner.invoke(app, [*args, "master", "build"])
    assert r.exit_code == 0, r.output
    assert "failed source bse" in r.output and "503" in r.output


def test_master_build_bse_404_and_amfi_302_and_no_edgar_contact(
    cli_env: Env, net: Net, monkeypatch: pytest.MonkeyPatch, fake_keyring: object
) -> None:
    monkeypatch.delenv("EDGAR_CONTACT", raising=False)
    net.bse_status = 404
    net.amfi_redirect = True
    monkeypatch.setattr(  # the redirect target serves the NAVAll fixture
        cm,
        "_master_sources",
        lambda: MasterSources(
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda req: (
                        httpx.Response(200, text=(FX / "amfi_navall.txt").read_text())
                        if str(req.url) == AMFI_MOVED
                        else net(req)
                    )
                )
            )
        ),
    )
    r = runner.invoke(app, [*cli_env[0], "master", "build"])
    assert r.exit_code == 0, r.output
    assert "sources ok: nse, amfi" in r.stdout
    assert "sources failed: bse (no BSE bhavcopy in the last 10 days)" in r.stdout
    assert "sec (EDGAR_CONTACT not set; US securities skipped" in r.stdout
    assert "nivesh secrets set EDGAR_CONTACT" in r.stdout
    assert "failed source sec" in r.output and "failed source bse" in r.output


def test_master_build_with_all_sources_ok_prints_no_failed_line(cli_env: Env, net: Net) -> None:
    r = runner.invoke(app, [*cli_env[0], "master", "build"])
    assert "sources ok: nse, bse, amfi, sec" in r.stdout and "sources failed" not in r.stdout
    assert securities(cli_env[1]) > 10


def test_master_build_fails_when_every_source_fails(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    monkeypatch.setattr(cm, "_master_sources", lambda: MasterSources(down))
    monkeypatch.setenv("EDGAR_CONTACT", pv.edgar_contact())
    r = runner.invoke(app, [*cli_env[0], "master", "build"])
    assert r.exit_code == 1 and "no master source" in r.output


def test_master_build_from_cache_twice_gives_identical_output(cli_env: Env, net: Net) -> None:
    args, _ = cli_env
    first = runner.invoke(app, [*args, "master", "build"])
    calls = net.calls
    second = runner.invoke(app, [*args, "master", "build"])
    assert net.calls == calls  # second run served from the unredacted cache
    assert first.output.split("master:")[0] == second.output.split("master:")[0]
    assert "inserted 0, updated 0" in second.output


def test_master_build_output_contains_no_pii(cli_env: Env, net: Net, tmp_path: Path) -> None:
    r = runner.invoke(app, [*cli_env[0], "master", "build"])
    assert scan_output(r.output, tmp_path) == [] and pv.edgar_contact() not in r.output


def test_master_build_prints_conflicts_and_rows_dropped(cli_env: Env, net: Net) -> None:
    args, data = cli_env
    runner.invoke(app, [*args, "init"])
    c = sqlite3.connect(data / "nivesh.sqlite")
    c.execute(
        "insert into security (symbol, exchange, isin, currency) "
        "values ('TCS', 'NSE', 'INE000Q01010', 'INR')"
    )
    c.commit()
    c.close()
    r = runner.invoke(app, [*args, "master", "build"])
    assert r.exit_code == 0 and "conflict:" in r.output and "rows_dropped 0" in r.output


IN_DAYS = [date(2026, 1, 22), date(2026, 1, 23), date(2026, 1, 27), date(2026, 1, 28)]


@pytest.fixture
def prices(cli_env: Env, net: Net, monkeypatch: pytest.MonkeyPatch) -> Feed:
    assert runner.invoke(app, [*cli_env[0], "master", "build"]).exit_code == 0
    feed = Feed(closes=dict(zip(IN_DAYS, [100.0, 101.0, 102.0, 103.0], strict=True)))
    c = httpx.Client(transport=httpx.MockTransport(feed))
    monkeypatch.setattr(cm, "_india_prices", lambda: IndiaPrices(c))
    monkeypatch.setattr(cm, "_us_prices", lambda: UsPrices(c))
    return feed


def prices_cmd(args: list[str], *extra: str) -> Result:
    return runner.invoke(app, [*args, "market", "prices", *extra])


def test_market_prices_india_prints_counts_flags_and_gaps(cli_env: Env, prices: Feed) -> None:
    del prices.closes[IN_DAYS[3]]  # missing from both sources: a real gap
    prices.yahoo = {**prices.closes, IN_DAYS[0]: 150.0}
    r = prices_cmd(cli_env[0], "RELIANCE", "--start", "2026-01-22", "--end", "2026-01-28")
    assert r.exit_code == 0, r.output
    assert "RELIANCE (NSE)" in r.output and "mismatch 1" in r.output and "ok 2" in r.output
    assert "gaps: 2026-01-28" in r.output  # 26 Jan holiday and the weekend are not gaps


def test_market_prices_us_requires_known_security_with_candidates_hint(
    cli_env: Env, prices: Feed
) -> None:
    r = prices_cmd(cli_env[0], "ZZZZQQ", "--start", "2024-04-01", "--end", "2024-04-02")
    assert r.exit_code == 1 and "no security matches" in r.output


def test_market_prices_ambiguous_security_lists_candidates(cli_env: Env, prices: Feed) -> None:
    r = prices_cmd(cli_env[0], "Tata", "--start", "2026-01-22", "--end", "2026-01-23")
    assert r.exit_code == 1 and ("candidates" in r.output or "no security" in r.output)


def test_market_prices_start_after_end_exits_1(cli_env: Env, prices: Feed) -> None:
    r = prices_cmd(cli_env[0], "RELIANCE", "--start", "2026-01-28", "--end", "2026-01-22")
    assert r.exit_code == 1 and "after end" in r.output
    r = prices_cmd(cli_env[0], "RELIANCE", "--start", "28-01-2026", "--end", "2026-01-22")
    assert r.exit_code == 1 and "--start must be an ISO date" in r.output


def test_market_prices_missing_nse_holiday_year_exits_1_with_config_hint(
    cli_env: Env, prices: Feed
) -> None:
    r = prices_cmd(cli_env[0], "RELIANCE", "--start", "2027-01-04", "--end", "2027-01-05")
    assert r.exit_code == 1 and "market.nse_holidays" in r.output


def _without_configured_holidays(args: list[str]) -> None:
    import re

    cfg = Path(args[1]) / "nivesh.yaml"
    block = re.compile(r"  nse_holidays:\n(?:    .*\n)+")
    cfg.write_text(block.sub("  nse_holidays: {}\n", cfg.read_text()))


def test_market_prices_2025_range_succeeds_without_config_entry(cli_env: Env, prices: Feed) -> None:
    _without_configured_holidays(cli_env[0])
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    prices.closes = dict(zip(days, [100.0, 101.0], strict=True))
    r = prices_cmd(cli_env[0], "RELIANCE", "--start", "2025-01-06", "--end", "2025-01-07")
    assert r.exit_code == 0, r.output
    assert "bars 4" in r.output and "gaps: none" in r.output


def test_market_prices_output_has_no_pii(cli_env: Env, prices: Feed, tmp_path: Path) -> None:
    prices.fail = {"yahoo"}
    r = prices_cmd(cli_env[0], "RELIANCE", "--start", "2026-01-22", "--end", "2026-01-28")
    assert r.exit_code == 0 and "failed source yahoo" in r.output
    assert scan_output(r.output, tmp_path) == [] and pv.edgar_contact() not in r.output


# fundamentals ---------------------------------------------------------------------------------


@pytest.fixture
def tape(monkeypatch: pytest.MonkeyPatch) -> tx.Tapetide:
    """Tapetide served from the recorded fixtures, with a (test) token set."""
    srv = tx.Tapetide()
    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token())
    monkeypatch.setattr(cm, "_tapetide", lambda: tx.adapter(srv))
    return srv


@pytest.fixture
def funds(cli_env: Env, net: Net, tape: tx.Tapetide, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from nivesh_adapters.edgar import Edgar, RateLimiter
    from nivesh_adapters.fundamentals_in import IndiaFundamentals
    from tests.market_fx import submissions

    assert runner.invoke(app, [*cli_env[0], "master", "build"]).exit_code == 0

    def edge(req: httpx.Request) -> httpx.Response:
        if "companyfacts" in req.url.path:
            return httpx.Response(200, text=(FX / "edgar_companyfacts.json").read_text())
        if "submissions" in req.url.path:
            return httpx.Response(200, json=submissions("320193"))
        return httpx.Response(200, text=(FX / "edgar_10k.html").read_text())

    def bse(req: httpx.Request) -> httpx.Response:
        if "ShareHolding" in req.url.path:
            return httpx.Response(
                200, json=json.loads((FX / "india_shareholding.json").read_text())
            )
        if "ResultsXbrl" in req.url.path:
            one = {"period_end": "2025-12-31", "filed_at": "2026-01-20",
                   "xbrl_url": "https://example.invalid/r.xml"}  # fmt: skip
            return httpx.Response(200, json=[one])
        return httpx.Response(200, text=(FX / "india_results.xbrl").read_text())

    ec = httpx.Client(transport=httpx.MockTransport(edge))
    bc = httpx.Client(transport=httpx.MockTransport(bse))
    monkeypatch.setattr(
        cm, "_edgar", lambda s: Edgar(ec, limiter=RateLimiter(10, lambda: 0.0, lambda x: None))
    )
    monkeypatch.setattr(cm, "_india_fundamentals", lambda: IndiaFundamentals(bc))


def test_market_fundamentals_cli_prints_counts_and_depth_and_exits_0(
    cli_env: Env, funds: None, tmp_path: Path
) -> None:
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "AAPL"])
    assert r.exit_code == 0, r.output
    assert "AAPL" in r.output and "annual periods 2" in r.output and "filings: 4 listed" in r.output
    assert scan_output(r.output, tmp_path) == [] and pv.edgar_contact() not in r.output


def test_market_fundamentals_shares_one_edgar_limiter_per_run(
    cli_env: Env, funds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = []
    real = cm._edgar
    monkeypatch.setattr(cm, "_edgar", lambda s: made.append(1) or real(s))
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "AAPL"])
    assert r.exit_code == 0, r.output
    assert len(made) == 1  # fundamentals and filings share one Edgar, hence one RateLimiter


def test_market_fundamentals_india_prints_shareholding(cli_env: Env, funds: None) -> None:
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "RELIANCE"])
    assert r.exit_code == 0, r.output
    assert "shareholding quarters 8" in r.output and "filings:" not in r.output


def test_market_fundamentals_india_prints_tapetide_counts_notes_and_quota(
    cli_env: Env, funds: None, tape: tx.Tapetide, tmp_path: Path
) -> None:
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "RELIANCE"])
    assert r.exit_code == 0, r.output
    assert "annual periods 12, quarterly periods 13" in r.output
    assert "estimated filing dates:" in r.output and "minority interest" in r.output
    assert "Tapetide calls this run: 6 (free plan 50/day, resets 00:00 IST)" in r.output
    assert tape.calls == 6 and tx.token() not in r.output and scan_output(r.output, tmp_path) == []
    again = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "RELIANCE"])
    assert "Tapetide calls this run: 0" in again.output and tape.calls == 6  # cached


def test_market_fundamentals_india_without_token_degrades_with_the_setup_hint(
    cli_env: Env,
    funds: None,
    tape: tx.Tapetide,
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: MemoryKeyring,
) -> None:
    monkeypatch.delenv("TAPETIDE_TOKEN")
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "RELIANCE"])
    assert r.exit_code == 0, r.output  # degrades like a missing EDGAR_CONTACT
    assert "nivesh secrets set TAPETIDE_TOKEN" in r.output and tape.calls == 0
    assert "shareholding quarters 3" in r.output  # the BSE fallback still ran


def test_market_fundamentals_india_scrubs_a_token_echoed_by_the_vendor(
    cli_env: Env, funds: None, tape: tx.Tapetide
) -> None:
    echo = f"Error: bad credential {tx.token()} supplied"
    tape.fail_all = httpx.Response(200, json=tx.envelope(echo, is_error=True))
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "RELIANCE"])
    assert r.exit_code == 0, r.output
    assert "Tapetide error" in r.output and tx.token() not in r.output


def test_cli_unknown_security_lists_candidates_exit_1(cli_env: Env, funds: None) -> None:
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "ZZZZQQ"])
    assert r.exit_code == 1 and "no security matches" in r.output
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "NIFTY 50"])
    assert r.exit_code == 1 and "index" in r.output


def test_market_fundamentals_failed_source_reported(
    cli_env: Env, funds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nivesh_adapters.edgar import Edgar, RateLimiter

    down = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    monkeypatch.setattr(
        cm, "_edgar", lambda s: Edgar(down, limiter=RateLimiter(10, lambda: 0.0, lambda x: None))
    )
    r = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "AAPL"])
    assert r.exit_code == 0 and "failed source" in r.output


# macro ----------------------------------------------------------------------------------------


@pytest.fixture
def macro(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    from nivesh_adapters.macro import MacroFetch

    seen: list[httpx.Request] = []

    def serve(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if "stlouisfed" in req.url.host:
            if req.url.params["series_id"] == "DGS10":
                return httpx.Response(503)
            return httpx.Response(200, text=(FX / "fred_obs.json").read_text())
        return httpx.Response(200, text=(FX / "nse_fiidii.json").read_text())

    c = httpx.Client(transport=httpx.MockTransport(serve))
    monkeypatch.setenv("FRED_API_KEY", pv.fred_key())
    monkeypatch.setattr(cm, "_macro_fetch", lambda s: MacroFetch(c))
    return seen


def test_market_macro_cli_exit_codes_and_no_secret_in_output(
    cli_env: Env, macro: list[httpx.Request], tmp_path: Path
) -> None:
    r = runner.invoke(
        app, [*cli_env[0], "market", "macro", "--role", "usdinr", "--role", "fii_net"]
    )
    assert r.exit_code == 0, r.output
    assert "usdinr: 3 new points" in r.output and "fii_net: 1 new points" in r.output
    assert pv.fred_key() not in r.output and scan_output(r.output, tmp_path) == []
    bad = runner.invoke(app, [*cli_env[0], "market", "macro", "--role", "bogus"])
    assert bad.exit_code == 1 and "unknown role" in bad.output


def test_market_macro_all_roles_reports_failed_source(
    cli_env: Env, macro: list[httpx.Request]
) -> None:
    r = runner.invoke(app, [*cli_env[0], "market", "macro"])
    assert r.exit_code == 0, r.output
    assert (
        "y10_us: api.stlouisfed.org returned HTTP 503" in r.output
        and "policy_us: 3 new points" in r.output
    )


# estimates ------------------------------------------------------------------------------------


def test_market_estimates_cli_us_and_india(
    cli_env: Env, net: Net, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nivesh_adapters.estimates import Estimates
    from tests.market_fx import fmp_calendar, fmp_estimates

    assert runner.invoke(app, [*cli_env[0], "master", "build"]).exit_code == 0

    def serve(req: httpx.Request) -> httpx.Response:
        body = fmp_calendar() if "earning_calendar" in req.url.path else fmp_estimates()
        return httpx.Response(200, json=body)

    c = httpx.Client(transport=httpx.MockTransport(serve))
    monkeypatch.setenv("FMP_API_KEY", pv.fmp_key())
    monkeypatch.setattr(cm, "_estimates", lambda s: Estimates(c))
    r = runner.invoke(app, [*cli_env[0], "market", "estimates", "AAPL"])
    assert r.exit_code == 0, r.output
    assert "AAPL (NASDAQ): " in r.output and "estimate values" in r.output  # clock is real
    assert pv.fmp_key() not in r.output and scan_output(r.output, tmp_path) == []
    r = runner.invoke(app, [*cli_env[0], "market", "estimates", "RELIANCE"])
    assert r.exit_code == 0 and "unavailable: no free estimates source for India" in r.output


# news -----------------------------------------------------------------------------------------


@pytest.fixture
def newsnet(cli_env: Env, net: Net, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from nivesh_adapters.news import Feeds

    assert runner.invoke(app, [*cli_env[0], "master", "build"]).exit_code == 0

    def serve(req: httpx.Request) -> httpx.Response:
        rss = (FX / "rss_a.xml").read_text().replace("TCS bags", "Reliance Industries bags")
        if req.url.path.endswith("feed.xml"):
            return httpx.Response(200, text=rss)
        return httpx.Response(200, json=json.loads((FX / "bse_announcements.json").read_text()))

    c = httpx.Client(transport=httpx.MockTransport(serve))
    monkeypatch.setattr(cm, "_feeds", lambda: Feeds(c))


def test_market_news_cli_counts_and_security_filter(
    cli_env: Env, newsnet: None, tmp_path: Path
) -> None:
    cfg = Path(cli_env[0][1]) / "nivesh.yaml"
    cfg.write_text(
        cfg.read_text().replace(
            "https://example.invalid/feed.xml", "https://news.example.invalid/feed.xml"
        )
    )
    r = runner.invoke(app, [*cli_env[0], "market", "news"])
    assert r.exit_code == 0, r.output
    assert "new 3" in r.output and "untagged" in r.output
    r = runner.invoke(app, [*cli_env[0], "market", "news", "--security", "RELIANCE"])
    assert r.exit_code == 0 and "RELIANCE (NSE): 2 stored items" in r.output
    assert "results" in r.output and scan_output(r.output, tmp_path) == []
    r = runner.invoke(app, [*cli_env[0], "market", "news", "--security", "ZZZZQQ"])
    assert r.exit_code == 1 and "no security matches" in r.output


def test_market_prices_prints_bad_rows_line_only_when_nonzero(cli_env: Env, prices: Feed) -> None:
    args = ["RELIANCE", "--start", "2026-01-22", "--end", "2026-01-23"]
    clean = prices_cmd(cli_env[0], *args)
    assert clean.exit_code == 0 and "bad rows skipped" not in clean.output
    prices.extra_rows = ("ZZBAD,T0,6.65,6.65,6.65,6.61",)
    r = prices_cmd(cli_env[0], *args, "--refresh")
    assert r.exit_code == 0, r.output
    assert "bad rows skipped: 2 (T0: outside the day's range x2)" in r.output


def test_market_prices_bad_rows_reasons_capped_at_five_with_remainder(
    cli_env: Env, prices: Feed
) -> None:
    prices.extra_rows = tuple(f"ZZ{i},T{i},6.65,6.65,6.65,6.61" for i in range(7))
    r = prices_cmd(cli_env[0], "RELIANCE", "--start", "2026-01-22", "--end", "2026-01-22")
    assert r.exit_code == 0, r.output
    line = next(x for x in r.output.splitlines() if x.startswith("bad rows skipped: 7"))
    assert line.count(" x1") == 5 and line.endswith("and 2 more)")


def test_market_fundamentals_india_token_with_newline_and_transport_error_never_printed(
    cli_env: Env, funds: None, tape: tx.Tapetide, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {tx.token()}\\x07'")

    good = cm._tapetide
    monkeypatch.setattr(cm, "_tapetide", lambda: tx.adapter(boom))
    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token() + "\x07")
    bad = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "RELIANCE"])
    assert bad.exit_code == 0, bad.output
    assert "failed source" in bad.output and tx.token() not in bad.output

    monkeypatch.setattr(cm, "_tapetide", good)
    monkeypatch.setenv("TAPETIDE_TOKEN", tx.token() + "\n")
    ok = runner.invoke(app, [*cli_env[0], "market", "fundamentals", "RELIANCE"])
    assert ok.exit_code == 0, ok.output
    assert tx.token() not in ok.output and "failed source fundamentals" not in ok.output
