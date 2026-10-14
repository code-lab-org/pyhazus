"""Print a T-SQL module with its repetitive per-occupancy CASE branches collapsed.

Hazus procedures repeat lines such as ``when 12 then flGBSXResultsEOB.COM1I*(...)`` once per occupancy.
This keeps the first and last line of each such run and replaces the middle with a count.

    python tools/sqlcondense.py work/sql/HazusModel/sql_stored_procedure/dbo.absp_GBSEconLoss.sql
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WHEN = re.compile(r"^\s*when\s+('?[\w.]+'?)\s+then\b", re.I)
UNION_LINE = re.compile(r"^\s*(union all|select .* from \w+ where \w+ is not null)\s*$", re.I)


def shape(line: str) -> str:
    """Line with numbers and occupancy names normalised, used to detect runs of similar lines."""
    s = re.sub(r"\b(RES\d[A-F]?|COM\d+|IND\d|AGR1|REL1|GOV\d|EDU\d)", "OCC", line)
    s = re.sub(r"\d+", "N", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def condense(lines: list[str], min_run: int = 4) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i + 1
        while j < len(lines) and lines[j].strip() and shape(lines[j]) == shape(lines[i]):
            j += 1
        if j - i >= min_run and (WHEN.match(lines[i]) or UNION_LINE.match(lines[i]) or "SUM(CASE" in lines[i].upper()
                                  or re.search(r"\bOCC\b", shape(lines[i]))):
            out.append(lines[i])
            out.append(f"    ... {j - i - 2} similar lines ...")
            out.append(lines[j - 1])
        else:
            out.extend(lines[i:j])
        i = j
    return out


def main(argv: list[str]) -> int:
    for path in argv[1:]:
        text = Path(path).read_text(encoding="utf-8").splitlines()
        body = condense([ln.rstrip() for ln in text])
        body = [ln for k, ln in enumerate(body) if ln.strip() or (k and body[k - 1].strip())]
        print(f"===== {Path(path).name} ({len(text)} -> {len(body)} lines)")
        print("\n".join(body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
