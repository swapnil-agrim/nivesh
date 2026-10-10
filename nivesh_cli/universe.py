"""`nivesh universe load | show` (ST-9.1): index membership from an owner-supplied constituent
file, and what each market's universe looks like after the liquidity floor and the exclusions.

`load` replaces the stored snapshot of one index from a CSV (nothing is fetched); `show` opens
the stores read-only. Index names and floors come from `universe:` in nivesh.yaml.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

import nivesh_cli.engine as engine_cli
from nivesh_adapters.analysis_service import plain
from nivesh_adapters.universe_service import enabled_indices, resolve_universe
from nivesh_cli.common import settings_of, user_errors
from nivesh_cli.engine import AsJson, AsOf, reader
from nivesh_core.db import init_stores
from nivesh_core.db.sqlite import open_sqlite
from nivesh_core.errors import NiveshError
from nivesh_core.membership import latest_as_of, load_members, members_of, read_constituent_file
from nivesh_core.security_master import SecurityMaster

universe_app = typer.Typer(no_args_is_help=True, help="Investable universes by index.")


@universe_app.command("load")
def load_cmd(
    ctx: typer.Context,
    index: Annotated[str, typer.Option("--index", help="Index name from `universe:` config.")],
    file: Annotated[Path, typer.Option("--file", help="CSV with symbol, isin, sector columns.")],
    as_of: AsOf = None,
) -> None:
    """Load the constituents of one index from a CSV file, replacing its stored snapshot."""
    settings = settings_of(ctx)
    with user_errors():
        spec = settings.universe.indices.get(index)
        if spec is None:
            known = ", ".join(sorted(settings.universe.indices))
            raise NiveshError(f"unknown index {index!r}; configured: {known}")
        day = engine_cli._as_of(as_of)
        rows = read_constituent_file(file)
        data_dir = Path(settings.data_dir)
        init_stores(data_dir)
        sql = open_sqlite(data_dir / "nivesh.sqlite")
        try:
            rep = load_members(sql, SecurityMaster(sql), index, spec.market, rows, day)
        finally:
            sql.close()
    typer.echo(f"loaded {rep.loaded} of {rep.total} rows for {index} as of {day}")
    if rep.unresolved:
        typer.echo(f"unresolved {len(rep.unresolved)}: {', '.join(rep.unresolved)}")


@universe_app.command("show")
def show_cmd(ctx: typer.Context, as_of: AsOf = None, as_json: AsJson = False) -> None:
    """Per market: members per index, snapshot dates and what the floor and exclusions remove."""
    settings = settings_of(ctx)
    out: dict[str, object] = {}
    lines: list[str] = []
    with user_errors(), reader(ctx) as r:
        day = engine_cli._as_of(as_of)
        for market in ("IN", "US"):
            info: dict[str, object] = {}
            names = enabled_indices(settings, market)
            info["indices"] = {
                n: {"members": len(members_of(r.sql, n)), "as_of": latest_as_of(r.sql, n)}
                for n in names
            }
            try:
                res = resolve_universe(r.duck, r.sql, settings, r.profile, market, day)
            except NiveshError as e:
                info["error"] = str(e)
                lines.append(f"{market}: {e}")
            else:
                info |= {
                    "size": len(res.ids), "considered": res.result.considered,
                    "removed": res.result.counts(), "warnings": res.warnings,
                    "excluded_names": {x.symbol: x.reason for x in res.result.removed},
                }  # fmt: skip
                lines.append(f"{market}: {res.basis}")
                lines += [f"  warning: {w}" for w in res.warnings]
                lines += [f"  removed {x.symbol}: {x.reason}" for x in res.result.removed]
            out[market] = info
    if as_json:
        typer.echo(json.dumps({"command": "universe show", "as_of": day.isoformat(),
                               "markets": plain(out)}, sort_keys=True))  # fmt: skip
        return
    typer.echo(f"universe as of {day}")
    for line in lines:
        typer.echo(line)
