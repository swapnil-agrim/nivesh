"""Guard: no test runs the raw PII scan over CLI output (#113). Best effort: it sees a
`scan_text(...)` call that reads `.output`, `.stdout` or `.stderr` in its own arguments; a scan of
a variable that was assigned from CLI output is invisible to it (the grep audit in the PR and the
labelled-blob tests in test_pii_e2e.py cover those)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP = frozenset({Path(__file__).resolve(), ROOT / "tests" / "security" / "test_scan_output.py"})
FIX = "use tests.scan_fx.scan_output(text, tmp_path) (or clean_output / request_text) instead"
OUTPUT_ATTRS = {"output", "stdout", "stderr"}
CLEANERS = {"clean_output", "scan_output", "request_text"}


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _reads_output(node: ast.AST) -> bool:
    """True if `node` reads `.output` / `.stdout` / `.stderr` outside a cleaning call."""
    if isinstance(node, ast.Call) and _name(node.func) in CLEANERS:
        return False
    if isinstance(node, ast.Attribute) and node.attr in OUTPUT_ATTRS:
        return True
    return any(_reads_output(child) for child in ast.iter_child_nodes(node))


def raw_cli_scans(source: str) -> list[int]:
    """Line numbers of `scan_text(...)` calls (name or attribute) that read CLI output."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and _name(node.func) == "scan_text"
        and any(_reads_output(arg) for arg in (*node.args, *(k.value for k in node.keywords)))
    ]


def raw_scans_under(
    base: Path, root: Path = ROOT, skip: frozenset[Path] = frozenset()
) -> list[str]:
    """`path:line` of every raw CLI-output scan in the `*.py` files under `base`, except `skip`."""
    hits: list[str] = []
    for path in sorted(base.rglob("*.py")):
        if path.resolve() in skip:
            continue
        try:
            lines = raw_cli_scans(path.read_text())
        except SyntaxError:  # a deliberately broken fixture is not a test file
            continue
        hits += [f"{path.relative_to(root)}:{line}" for line in lines]
    return hits


def test_guard_flags_raw_scans_of_cli_output() -> None:
    source = (
        "scan_text(r.output)\n"
        "scan_text(x.stdout)\n"
        "pii_scan.scan_text(a + r.stderr)\n"
        'scan_text(f"{r.output}")\n'
    )
    assert raw_cli_scans(source) == [1, 2, 3, 4]


def test_guard_accepts_cleaned_and_non_output_scans() -> None:
    source = (
        "scan_output(r.output, tmp_path)\n"
        "scan_text(clean_output(r.output))\n"
        "scan_text(request_text(calls.requests[1]))\n"
        "scan_text(p.read_text())\n"
        "scan_text(text)\n"
    )
    assert raw_cli_scans(source) == []


def test_a_file_that_does_not_parse_is_skipped_and_the_rest_are_still_checked(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken.py").write_text("def (:\n")
    (tmp_path / "dirty.py").write_text("x = 1\nscan_text(r.output)\n")
    assert raw_scans_under(tmp_path, tmp_path) == ["dirty.py:2"]


def test_no_test_scans_raw_cli_output() -> None:
    hits = raw_scans_under(ROOT / "tests", skip=SKIP)
    assert hits == [], f"{FIX} of scan_text on CLI output: {hits}"
