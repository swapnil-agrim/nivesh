"""CAS PDF ingestion via casparser (ST-2.5, ST-2.6): the PII boundary (NFR-3).

Only this module imports casparser (lazily). Everything leaving it is a `ParsedStatement`, which
cannot carry a name, PAN, e-mail, mobile, address, DP/client ID or folio: identifiers are
hashed into `holder_ref`, parser exceptions are replaced by fixed messages (they can echo
content), and warnings are redacted and truncated.
"""

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nivesh_core.errors import NiveshError
from nivesh_core.holder_ref import hash_ref
from nivesh_core.holdings import Holding, ParsedStatement, PriceBasis, Source, Txn
from nivesh_core.redact import redact_text
from nivesh_core.security_resolver import SecurityResolver

_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y")
_WARN_MAX = 200


class CasError(NiveshError):
    """A statement could not be read; messages are fixed and never echo parser content."""


def _read(path: Path, pdf_pass: str) -> Any:
    """The only call into casparser."""
    import casparser

    return casparser.read_cas_pdf(str(path), pdf_pass)


def _to_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(str(value).strip(), fmt).date()  # noqa: DTZ007 - date only
        except ValueError:
            continue
    raise CasError("could not read a date from the statement")


def _warnings(raw: Iterable[object]) -> list[str]:
    return [redact_text(str(w))[:_WARN_MAX] for w in raw]


def _wrong_password(e: BaseException) -> bool:
    return any(c.__name__ == "IncorrectPasswordError" for c in type(e).__mro__)


def read_statement(
    path: Path, pdf_pass: str, salt: str, resolver: SecurityResolver
) -> ParsedStatement:
    if not path.is_file():
        raise CasError("statement file not found")
    try:
        data = _read(path, pdf_pass)
    except Exception as e:  # noqa: BLE001 - parser errors can echo content; replaced by fixed text
        if _wrong_password(e):
            raise CasError("wrong CAS password") from None
        raise CasError("could not parse statement") from None
    from casparser.types import CASData, NSDLCASData

    if isinstance(data, NSDLCASData):
        return normalise_depository(data, salt, resolver)
    if isinstance(data, CASData):
        return normalise_rta(data, salt, resolver)
    raise CasError("unexpected result from the statement parser")


def _holding(
    *, source: Source, label: str, basis: PriceBasis, as_of: date, ref: str, isin: str | None,
    symbol: str, exchange: str, name: str | None, asset_class: str, quantity: Decimal,
    price: Decimal, value: Decimal, avg_cost: Decimal | None = None, plan: str | None = None,
    amfi_code: str | None = None, unresolved: bool = False,
) -> Holding:  # fmt: skip
    return Holding(
        isin=isin, symbol=symbol, exchange=exchange, name=name, asset_class=asset_class,
        quantity=quantity, avg_cost=avg_cost, price=price, price_basis=basis, value_inr=value,
        as_of=as_of, source=source, source_label=label, holder_ref=ref, plan=plan,
        amfi_code=amfi_code, unresolved=unresolved,
    )  # fmt: skip


def normalise_depository(data: Any, salt: str, resolver: SecurityResolver) -> ParsedStatement:
    """NSDL/CDSL statement: equities, ETFs, demat MFs and bonds. Cost is not on the statement."""
    as_of = _to_date(data.statement_period.to)
    holdings: list[Holding] = []
    refs: list[str] = []
    warnings = _warnings(data.parse_warnings)
    if data.nps is not None:
        warnings.append("NPS holdings are not ingested yet and were skipped")
    for acc in data.accounts:
        ref = hash_ref(f"{acc.dp_id or ''}/{acc.client_id or ''}", salt)
        refs.append(ref)
        common: dict[str, Any] = {
            "source": "cas_demat", "label": "CAS demat", "basis": "statement",
            "as_of": as_of, "ref": ref,
        }  # fmt: skip
        for eq in acc.equities:
            if eq.num_shares == 0:
                continue
            found = resolver.resolve(eq.isin)
            if found:
                ident = (found.symbol, found.exchange, found.name or eq.name, found.asset_class)
            elif eq.symbol:
                ident = (eq.symbol, eq.exchange or "NSE", eq.name, "equity")
            else:
                ident = (eq.isin, "ISIN", eq.name, "equity")
            holdings.append(
                _holding(
                    **common, isin=eq.isin, symbol=ident[0], exchange=ident[1], name=ident[2],
                    asset_class=ident[3], quantity=eq.num_shares, price=eq.price, value=eq.value,
                    unresolved=found is None and not eq.symbol,
                )
            )  # fmt: skip
        for mf in acc.mutual_funds:
            if mf.balance == 0:
                continue
            found = resolver.resolve(mf.isin)
            holdings.append(
                _holding(
                    **common, isin=mf.isin, symbol=mf.isin, exchange="AMFI",
                    name=(found.name if found else None) or mf.name, asset_class="mf",
                    quantity=mf.balance, price=mf.nav, value=mf.value, avg_cost=mf.avg_cost,
                    amfi_code=mf.amfi or (found.amfi_code if found else None),
                )
            )  # fmt: skip
        for bond in acc.bonds:
            if bond.num_bonds == 0:
                continue
            found = resolver.resolve(bond.isin)
            price = bond.market_price or bond.value / bond.num_bonds
            holdings.append(
                _holding(
                    **common, isin=bond.isin, symbol=found.symbol if found else bond.isin,
                    exchange=found.exchange if found else "ISIN",
                    name=(found.name if found else None) or bond.name, asset_class="bond",
                    quantity=bond.num_bonds, price=price, value=bond.value,
                    unresolved=found is None,
                )
            )  # fmt: skip
    return ParsedStatement(
        kind="cas_demat", as_of=as_of, holdings=holdings, holder_refs=refs, warnings=warnings
    )


def _plan(scheme_name: str) -> str | None:
    low = scheme_name.lower()
    return "direct" if "direct" in low else "regular" if "regular" in low else None


def normalise_rta(data: Any, salt: str, resolver: SecurityResolver) -> ParsedStatement:
    """CAMS/KFintech statement: schemes with units become holdings, every transaction is kept."""
    as_of = _to_date(data.statement_period.to)
    holdings: list[Holding] = []
    txns: list[Txn] = []
    refs: list[str] = []
    warnings = _warnings(data.parse_warnings)
    for folio in data.folios:
        ref = hash_ref(folio.folio, salt)
        refs.append(ref)
        for sc in folio.schemes:
            found = resolver.resolve(sc.isin) if sc.isin else None
            ident: dict[str, Any] = {
                "isin": sc.isin,
                "symbol": sc.isin or sc.amfi or sc.rta_code,
                "exchange": "AMFI",
                "name": (found.name if found else None) or sc.scheme,
                "asset_class": "mf",
                "amfi_code": sc.amfi or (found.amfi_code if found else None),
            }
            if sc.close != sc.close_calculated:
                warnings.append(
                    f"scheme {sc.isin or sc.rta_code}: closing units differ from the calculated "
                    "balance; check the statement"
                )
            if sc.close > 0:
                cost = sc.valuation.cost
                holdings.append(
                    _holding(
                        **ident, source="cas_rta", label="CAS RTA", basis="nav", as_of=as_of,
                        ref=ref, quantity=sc.close, price=sc.valuation.nav,
                        value=sc.valuation.value,
                        avg_cost=cost / sc.close if cost else None, plan=_plan(sc.scheme),
                    )
                )  # fmt: skip
            for t in sc.transactions:
                txns.append(
                    Txn(
                        **ident,
                        holder_ref=ref,
                        txn_date=_to_date(t.date),
                        txn_type=str(getattr(t.type, "value", t.type)).lower(),
                        quantity=t.units,
                        price=t.nav,
                        amount=t.amount,
                    )
                )
    return ParsedStatement(
        kind="cas_rta", as_of=as_of, holdings=holdings, txns=txns, holder_refs=refs,
        warnings=warnings,
    )  # fmt: skip
