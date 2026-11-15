"""Command-line interface for the offline Silver scaffolder.

    python migration/scaffold_silver.py --edition bigquery --workspace <client-workspace> \
        --source-id <alias> --mapping-file <mapping.json> --inventory-file <dry-run.json> \
        --run-manifest <run_id>.json --source-binding-id <alias> --max-evidence-age-hours 72 \
        [--dry-run]

Output is one deterministic JSON object on stdout with fixed reason codes, the edition,
canonical table names, and project-relative planned paths. It never prints absolute paths,
input values, Bronze names, fingerprints, file contents, or exception text.

`--dry-run` never writes and reports `plan_only` or `refused`. Without `--dry-run`, the same
read-only preflight runs and then `scaffold.publisher` creates the output directory exactly
once: private staging, exclusive file creation, fsync and hash verification, then an atomic
no-replace directory rename. It reports `published` or `refused`. An existing output or stage
path, a symlink, or a platform without an atomic no-replace rename refuses; nothing is ever
overwritten, merged, or removed. There is no update, force, merge, or cleanup mode.
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
from .publisher import PublishResult, publish
from .reasons import Refusal
from .workspace import EDITION_PROJECTS, check_collisions, check_workspace


PROG = "scaffold_silver.py"
EXIT_PLAN_ONLY = 0
EXIT_PUBLISHED = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3
# The complete output was published, but restoring editable modes or syncing did not finish.
EXIT_PUBLISHED_INCOMPLETE = 4
SOURCE_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_EVIDENCE_AGE_HOURS = 24 * 366
GATES: tuple[str, ...] = ()  # no remaining core gate; the key is kept for report stability

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
        description=("Offline Silver starter-model scaffolder. Creates the output directory once; "
                     "never overwrites, merges, or removes. --dry-run writes nothing."),
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
                        help="Plan only; writes nothing. Without it the output directory is created once.")
    return parser


def plan_from_args(
    args: argparse.Namespace, *, now: datetime | None = None
) -> tuple[ScaffoldPlan, str, dict[str, str] | None]:
    """Run every read-only check in order.

    Returns (plan, absolute project directory, rendered files or None when the edition
    renderer is not installed) or raises Refusal. The project directory is never printed.
    """
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
    # In memory only: renderer contract + manifest.
    files = render_outputs(plan, renderer) if renderer is not None else None
    return plan, project, files


def _report(
    edition: str, mode: str, code: str | None, plan: ScaffoldPlan | None, renderer: bool,
    outcome: PublishResult | None = None,
) -> dict:
    published = outcome is not None and outcome.published
    if published:
        status = "published"
    elif code is not None:
        status = "refused"
    else:
        status = "plan_only"
    show_plan = plan is not None and status != "refused"
    return {
        "edition": edition,
        "gates": list(GATES),
        "mode": mode,
        "output_dir": plan.output_dir if show_plan else None,
        "planned_files": list(plan.planned_files) if show_plan else [],
        "project_dir": EDITION_PROJECTS[edition][0],
        "reason_codes": [code] if code is not None else [],
        "renderer_available": renderer,
        # True when this run left `.scaffold-staging/<source_id>/` behind for inspection.
        "staging_retained": outcome is not None and outcome.staging_retained,
        "status": status,
        "tables": [table.entity for table in plan.tables] if show_plan else [],
        "writes_performed": outcome is not None and outcome.touched,
    }


def run(argv: list[str] | None = None, *, now: datetime | None = None) -> tuple[int, str, str]:
    """Return (exit_code, stdout, stderr). Writes only in default mode, via `publisher.publish`."""
    try:
        args = build_parser().parse_args(argv)
    except _UsageError as error:
        return EXIT_USAGE, "", f"{PROG}: usage error: {error.message}\n"
    plan, project, files, code, outcome = None, None, None, None, None
    try:
        plan, project, files = plan_from_args(args, now=now)
    except Refusal as refusal:
        code = refusal.code
    renderer = files is not None
    if args.dry_run:
        report = _report(args.edition, "plan_only", code, plan, renderer)
    else:
        # Default mode runs the same read-only preflight first, so both modes report
        # identical earlier reason codes; only a fully clean preflight reaches the publisher.
        if code is None and files is None:
            code = R.RENDERER_UNAVAILABLE
        if code is None:
            prefix = f"{plan.output_dir}/"
            outcome = publish(project, plan.source_id, {path[len(prefix):]: text for path, text in files.items()})
            code = outcome.code
        report = _report(args.edition, "generate", code, plan, renderer, outcome)
    stdout = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if report["status"] == "refused":
        return EXIT_REFUSED, stdout, ""
    if report["reason_codes"]:
        return EXIT_PUBLISHED_INCOMPLETE, stdout, ""
    return EXIT_PLAN_ONLY, stdout, ""


def main(argv: list[str] | None = None) -> int:
    try:
        exit_code, stdout, stderr = run(argv)
    except SystemExit as exit_request:  # --help
        return int(exit_request.code or 0)
    except Exception:
        sys.stderr.write(f"{PROG}: internal error; inspect the workspace before retrying.\n")
        return EXIT_INTERNAL
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code
