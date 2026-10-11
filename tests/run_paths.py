"""Where a run's files live, whatever the shape (dated `runs/<date>/<id>` or legacy)."""

import re
from pathlib import Path

from nivesh_core.paths import find_run_dir

RUN_COL = re.compile(r"runs/\d{4}-\d\d-\d\d/\d+")


def run_path(data: Path, run_id: int) -> Path:
    found = find_run_dir(data, run_id)
    assert found is not None, f"no run {run_id} under {data}"
    return found


def run_col(run_id: int) -> str:
    """Regex text for the stored `run.run_dir` column of run `run_id`."""
    return rf"runs/\d{{4}}-\d\d-\d\d/{run_id}"
