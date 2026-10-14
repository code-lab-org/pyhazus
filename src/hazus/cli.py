"""Command line: ``hazus flood …``, ``hazus region create`` and ``hazus inventory export[-state]``."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ANALYSES = ("gbs", "essential_facilities", "utilities", "debris", "shelter", "aal")


def _grid(hazard: str):
    def parse(text: str):
        from hazus.flood.hazard import DepthGrid

        path, _, rp = text.rpartition("@") if "@" in text else (text, "", "-1")
        try:
            period = int(rp)
        except ValueError:
            raise argparse.ArgumentTypeError(f"return period must be an integer: {text!r}") from None
        return DepthGrid(Path(path), hazard, period)

    return parse


def _run(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from hazus.flood.hazard import create_scenario
    from hazus.flood.scenario import AnalysisOptions, run_scenario
    from hazus.io import read_region, write_results

    grids = [replace(g, units=args.units) for g in (args.riverine or []) + (args.coastal or [])]
    if not grids:
        print("error: give at least one --riverine or --coastal depth grid", file=sys.stderr)
        return 2
    skip = set(args.skip or [])
    unknown = skip - set(ANALYSES)
    if unknown:
        print(f"error: unknown analyses to skip: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    options = AnalysisOptions(**{a: a not in skip for a in ANALYSES})

    region = read_region(args.region)
    with tempfile.TemporaryDirectory(prefix="hazus-grids-") as tmp:
        work = Path(args.work) if args.work else Path(tmp)
        rasters = create_scenario(grids, work)
        results = run_scenario(region, rasters, options=options)
    written = write_results(results, args.out, args.format)
    for path in written:
        print(path)
    return 0


def _prepare(args: argparse.Namespace) -> int:
    from hazus.flood.hazard import prepare_depth_grid

    print(prepare_depth_grid(args.src, args.dst, args.units))
    return 0


def _export(args: argparse.Namespace) -> int:
    from hazus.inventory.sqlserver import connect, export_region

    with connect(args.database, server=args.server, port=args.port, user=args.user) as conn:
        written = export_region(conn, args.out)
    for path in written.values():
        print(path)
    return 0


def _export_state(args: argparse.Namespace) -> int:
    from hazus.inventory.sqlserver import connect, export_state

    with connect(args.database, server=args.server, port=args.port, user=args.user) as conn:
        written = export_state(conn, args.out)
    print(f"{len(written)} tables -> {args.out}")
    return 0


def _ids(text: str | None) -> list[str] | None:
    return [s.strip() for s in text.split(",") if s.strip()] if text else None


def _create_region(args: argparse.Namespace) -> int:
    from hazus.inventory.region import create_region
    from hazus.io import read_region, write_region

    states = [read_region(s) for s in args.state]
    region = create_region(states, counties=_ids(args.counties), tracts=_ids(args.tracts), blocks=_ids(args.blocks),
                           year=args.year)
    write_region(region, args.out)
    print(f"{len(region['hzCensusBlock'])} census blocks, {len(region)} tables -> {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hazus", description="FEMA Hazus loss estimation (flood).")
    sub = parser.add_subparsers(dest="model", required=True)
    flood = sub.add_parser("flood", help="flood model").add_subparsers(dest="command", required=True)

    run = flood.add_parser("run", help="run a flood scenario on a study region",
                           description="Run a flood scenario. Grids are PATH for a single event or PATH@RP for a "
                                       "return period; give three or more return periods for AAL.")
    run.add_argument("region", help="study-region directory (one Parquet file per Hazus table)")
    run.add_argument("out", help="output directory for the result tables")
    run.add_argument("--riverine", action="append", type=_grid("riverine"), metavar="PATH[@RP]")
    run.add_argument("--coastal", action="append", type=_grid("coastal"), metavar="PATH[@RP]")
    run.add_argument("--units", choices=["feet", "meters"], default="feet", help="depth units of the grids")
    run.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    run.add_argument("--skip", action="append", metavar="ANALYSIS", help=f"skip one of: {', '.join(ANALYSES)}")
    run.add_argument("--work", help="keep the prepared depth grids in this directory")
    run.set_defaults(func=_run)

    prep = flood.add_parser("prepare-grid", help="reproject a depth grid to EPSG:4326 in feet")
    prep.add_argument("src")
    prep.add_argument("dst")
    prep.add_argument("--units", choices=["feet", "meters"], default="feet")
    prep.set_defaults(func=_prepare)

    inventory = sub.add_parser("inventory", help="inventory data").add_subparsers(dest="command", required=True)
    export = inventory.add_parser(
        "export", help="export a Hazus study-region database from SQL Server to a study-region directory",
        description="Export the tables the flood model needs. The password is read from HAZUS_SA_PASSWORD; "
                    "server, port and user default to HAZUS_SQL_SERVER, HAZUS_SQL_PORT and HAZUS_SQL_USER.")
    export.add_argument("database", help="name of the attached study-region database")
    export.add_argument("out", help="output study-region directory")
    export.add_argument("--server")
    export.add_argument("--port", type=int)
    export.add_argument("--user")
    export.set_defaults(func=_export)

    state = inventory.add_parser(
        "export-state", help="export a FEMA state inventory database from SQL Server (once per state)",
        description="Export the tables study-region creation needs from a state database attached under its "
                    "abbreviation (e.g. DC). Connection settings as for 'export'.")
    state.add_argument("database", help="name of the attached state database, e.g. DC")
    state.add_argument("out", help="output state directory")
    state.add_argument("--server")
    state.add_argument("--port", type=int)
    state.add_argument("--user")
    state.set_defaults(func=_export_state)

    region = sub.add_parser("region", help="study regions").add_subparsers(dest="command", required=True)
    create = region.add_parser(
        "create", help="create a study region from exported state inventories",
        description="Create a study region as Hazus's aggregation does. Select counties, tracts or census "
                    "blocks (comma-separated ids); without a selection the whole state is used.")
    create.add_argument("state", nargs="+", help="state directories from 'hazus inventory export-state'")
    create.add_argument("out", help="output study-region directory")
    create.add_argument("--counties", help="county FIPS codes, e.g. 11001")
    create.add_argument("--tracts", help="tract ids")
    create.add_argument("--blocks", help="census block ids")
    create.add_argument("--year", type=int, help="creation year for depreciation (default: this year)")
    create.set_defaults(func=_create_region)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
