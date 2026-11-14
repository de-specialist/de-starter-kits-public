"""Command-line interface for the offline, plan-only Silver scaffolder.

    python migration/scaffold_silver.py --edition bigquery --workspace <client-workspace> \
        --source-id <alias> --mapping-file <mapping.json> --inventory-file <dry-run.json> \
        --run-manifest <run_id>.json --source-binding-id <alias> --max-evidence-age-hours 72 \
        --dry-run

Output is one deterministic JSON object on stdout with fixed reason codes, the edition,
canonical table names, and project-relative planned paths. It never prints absolute paths,
input values, Bronze names, fingerprints, file contents, or exception text.

This CLI never writes. `--dry-run` reports `plan_only` (never `ready`) or `refused`. Without
`--dry-run`, the same read-only preflight runs and then the run refuses with
`final_generation_gated`: staging, sealing, and atomic no-replace publication stay disabled
until atomic workspace publication lands. There is no update, force, or merge mode.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime

from . import reasons as R
from .inputs import SOURCE_BINDING_ID, validate_inputs
from .plan import EDITIONS, ScaffoldPlan, build_plan, render_outputs, resolve_renderer
from .reasons import Refusal
from .workspace import EDITION_PROJECTS, check_collisions, check_workspace


PROG = "scaffold_silver.py"
EXIT_PLAN_ONLY = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3
SOURCE_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_EVIDENCE_AGE_HOURS = 24 * 366
GATES = (R.FINAL_GENERATION_GATED,)

_KNOWN_FLAGS = frozenset({
    "--edition", "--workspace", "--source-id", "--mapping-file", "--inventory-file",
    "--run-manifest", "--source-binding-id", "--max-evidence-age-hours", "--dry-run",
})
_EDITION_ERROR = "must be bigquery or snowflake."
_PATH_ERROR = "must be a non-empty path."
_AGE_ERROR = f"must be a whole number of hours from 1 to {MAX_EVIDENCE_AGE_HOURS}."
_FIXED_TYPE_ERRORS = frozenset({_EDITION_ERROR, _PATH_ERROR, _AGE_ERROR})


class _UsageError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _edition(value: str) -> str:
    if value not in EDITIONS:
        raise argparse.ArgumentTypeError(_EDITION_ERROR)
    return value


def _path_text(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise argparse.ArgumentTypeError(_PATH_ERROR)
    return value


def _age_hours(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", value or ""):
        raise argparse.ArgumentTypeError(_AGE_ERROR)
    hours = int(value)
    if not 1 <= hours <= MAX_EVIDENCE_AGE_HOURS:
        raise argparse.ArgumentTypeError(_AGE_ERROR)
    return hours


def _safe_usage_message(message: str) -> str:
    """Pass through only argparse messages that cannot echo argument values."""
    required = re.fullmatch(r"the following arguments are required: (--[a-z-]+(?:, --[a-z-]+)*)", message)
    if required and all(flag in _KNOWN_FLAGS for flag in required.group(1).split(", ")):
        return message
    argument = re.fullmatch(r"argument (--[a-z-]+): (.*)", message, re.DOTALL)
    if argument and argument.group(1) in _KNOWN_FLAGS:
        if argument.group(2) == "expected one argument" or argument.group(2) in _FIXED_TYPE_ERRORS:
            return message
    if message.startswith("unrecognized arguments"):
        return "unrecognized or unsupported arguments; this scaffolder has no update, force, or merge mode."
    return "invalid arguments."


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse hook; never exits or echoes values
        raise _UsageError(_safe_usage_message(message))


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description="Offline Silver starter-model scaffolder. Plan-only; writes nothing.",
        allow_abbrev=False,
    )
    parser.add_argument("--edition", type=_edition, required=True, help="bigquery or snowflake.")
    parser.add_argument("--workspace", type=_path_text, required=True,
                        help="Client workspace root created from one edition (outside the starter repo).")
    parser.add_argument("--source-id", required=True,
                        help="Generic non-sensitive alias, [a-z][a-z0-9_]{0,39}; names the output.")
    parser.add_argument("--mapping-file", type=_path_text, help="Reviewed version 1 mapping file.")
    parser.add_argument("--inventory-file", type=_path_text,
                        help="Saved owner-only migration --dry-run JSON (the dry_run input role).")
    parser.add_argument("--run-manifest", type=_path_text,
                        help="Completed owner-only run manifest, <run_id>.json.")
    parser.add_argument("--source-binding-id",
                        help="Operator alias that must equal both artifacts'. An assertion, never authentication.")
    parser.add_argument("--max-evidence-age-hours", type=_age_hours,
                        help="Maximum age of the run manifest's finished_at_utc. No default.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only. Without it the run still refuses: final generation is gated.")
    return parser


def plan_from_args(args: argparse.Namespace, *, now: datetime | None = None) -> tuple[ScaffoldPlan, bool]:
    """Run every read-only check in order. Returns (plan, renderer_available) or raises Refusal."""
    if not isinstance(args.source_id, str) or SOURCE_ID.fullmatch(args.source_id) is None:
        raise Refusal(R.INVALID_SOURCE_ID)
    if args.source_binding_id is not None and SOURCE_BINDING_ID.fullmatch(args.source_binding_id) is None:
        raise Refusal(R.INVALID_SOURCE_BINDING_ID)
    project = check_workspace(args.workspace, args.edition)
    evidence = validate_inputs(
        edition=args.edition,
        mapping_path=args.mapping_file,
        dry_run_path=args.inventory_file,
        run_manifest_path=args.run_manifest,
        source_binding_id=args.source_binding_id,
        max_age_hours=args.max_evidence_age_hours,
        now=now,
    )
    plan = build_plan(evidence, args.source_id)
    check_collisions(project, args.source_id)
    renderer = resolve_renderer(args.edition)
    if renderer is not None:
        render_outputs(plan, renderer)  # in memory only: renderer contract + manifest
    return plan, renderer is not None


def _report(edition: str, mode: str, code: str | None, plan: ScaffoldPlan | None, renderer: bool) -> dict:
    refused = code is not None
    return {
        "edition": edition,
        "gates": list(GATES),
        "mode": mode,
        "output_dir": plan.output_dir if plan is not None and not refused else None,
        "planned_files": list(plan.planned_files) if plan is not None and not refused else [],
        "project_dir": EDITION_PROJECTS[edition][0],
        "reason_codes": [code] if refused else [],
        "renderer_available": renderer,
        "status": "refused" if refused else "plan_only",
        "tables": [table.entity for table in plan.tables] if plan is not None and not refused else [],
        "writes_performed": False,
    }


def run(argv: list[str] | None = None, *, now: datetime | None = None) -> tuple[int, str, str]:
    """Return (exit_code, stdout, stderr). Never writes to the filesystem."""
    try:
        args = build_parser().parse_args(argv)
    except _UsageError as error:
        return EXIT_USAGE, "", f"{PROG}: usage error: {error.message}\n"
    plan, renderer, code = None, False, None
    try:
        plan, renderer = plan_from_args(args, now=now)
    except Refusal as refusal:
        code = refusal.code
    if args.dry_run:
        report = _report(args.edition, "plan_only", code, plan, renderer)
    else:
        # Final generation is not implemented. It refuses after the same read-only preflight,
        # so dry-run and default mode report identical earlier reason codes.
        report = _report(args.edition, "generate", code or R.FINAL_GENERATION_GATED, plan, renderer)
    stdout = json.dumps(report, indent=2, sort_keys=True) + "\n"
    return (EXIT_REFUSED if report["status"] == "refused" else EXIT_PLAN_ONLY), stdout, ""


def main(argv: list[str] | None = None) -> int:
    try:
        exit_code, stdout, stderr = run(argv)
    except SystemExit as exit_request:  # --help
        return int(exit_request.code or 0)
    except Exception:
        sys.stderr.write(f"{PROG}: internal error; nothing was written.\n")
        return EXIT_INTERNAL
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code
