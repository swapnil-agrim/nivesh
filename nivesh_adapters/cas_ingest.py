"""Scan the CAS inbox and store each new statement (ST-2.5, ST-2.6, ST-2.7 report)."""

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

from nivesh_adapters.cas import CasError, read_statement
from nivesh_core.holder_ref import salt_fingerprint
from nivesh_core.holdings import InboxReport
from nivesh_core.holdings_store import check_salt_fingerprint, ingest_exists, save_ingest
from nivesh_core.security_resolver import SecurityResolver

KINDS = ("cas_demat", "cas_rta")


def ingest_inbox(
    conn: sqlite3.Connection,
    inbox: Path,
    pdf_pass: str,
    salt: str,
    resolver: SecurityResolver,
    now: datetime | None = None,
) -> InboxReport:
    """Regular `*.pdf` files directly in `inbox` only (no subdirectories, no symlinks)."""
    if not inbox.is_dir():
        raise CasError("inbox directory not found")
    check_salt_fingerprint(conn, salt_fingerprint(salt))
    reports, errors, skipped = [], [], []
    for f in sorted(inbox.iterdir()):
        if f.is_symlink() or not f.is_file() or f.suffix.lower() != ".pdf":
            continue
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        label = digest[:8]
        if any(ingest_exists(conn, k, digest) for k in KINDS):
            skipped.append(label)
            continue
        try:
            parsed = read_statement(f, pdf_pass, salt, resolver)
        except CasError as e:
            errors.append(f"{label}: {e}")
            continue
        reports.append(
            save_ingest(
                conn,
                kind=parsed.kind,
                source_label=label,
                digest=digest,
                as_of=parsed.as_of,
                holdings=parsed.holdings,
                txns=parsed.txns,
                holder_refs=parsed.holder_refs,
                warnings=parsed.warnings,
                now=now,
            )  # fmt: skip
        )
    return InboxReport(reports=reports, errors=errors, skipped=skipped)
