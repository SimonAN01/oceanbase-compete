"""Stable JSON CLI. Credentials are never command-line values."""

import argparse
import csv
import io
import math
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .errors import ToolError
from .platform import Platform, TRACKS
from .storage import Store, read_json
from .transport import Client, Redactor, dumps, load_cookie
from .workflow import Workflow, snapshot_diff


def number(text):
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a finite number") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("Expected a finite number")
    return value


def common(parser):
    # SUPPRESS lets a flag work before or after any nested subcommand.
    parser.add_argument("--cookie-file", default=argparse.SUPPRESS, help="Local raw-header, Netscape or JSON Cookie file")
    parser.add_argument("--cookie-stdin", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--state-dir", default=argparse.SUPPRESS)
    parser.add_argument("--http-timeout", type=number, default=argparse.SUPPRESS)
    parser.add_argument("--retries", type=int, default=argparse.SUPPRESS)


def parser():
    root = argparse.ArgumentParser(prog="obcomp", description="OceanBase competition submission and leaderboard tools")
    root.add_argument("--version", action="version", version=__version__)
    root.set_defaults(cookie_file=None, cookie_stdin=False, state_dir=None, http_timeout=15, retries=2)
    common(root)
    commands = root.add_subparsers(dest="command", required=True)
    def command(name, help_text, parent=commands):
        p = parent.add_parser(name, help=help_text)
        common(p)
        return p
    auth = command("auth", "Validate the authenticated identity")
    auth_sub = auth.add_subparsers(dest="auth_action", required=True)
    command("check", "Check login via the identity API", auth_sub)
    command("doctor", "Check runtime, Git and login")
    command("camps", "List camps and competitions")
    questions = command("questions", "List all questions in a camp")
    questions.add_argument("--camp-id", type=int, required=True)
    options = command("options", "Read the current submission requirements")
    options.add_argument("--question-id", type=int, required=True)
    profile = command("profile", "Manage account-bound competition profiles")
    profiles = profile.add_subparsers(dest="profile_action", required=True)
    create = command("create", "Create a verified target profile", profiles)
    create.add_argument("name")
    create.add_argument("--track", choices=list(TRACKS))
    create.add_argument("--camp-id", type=int)
    create.add_argument("--question-id", type=int)
    create.add_argument("--repository")
    create.add_argument("--branch")
    create.add_argument("--commit")
    create.add_argument("--direction", choices=["max", "min"], default="max")
    command("list", "List locally configured profiles", profiles)
    show = command("show", "Show a local profile", profiles)
    show.add_argument("name")
    def target(p):
        p.add_argument("--profile", required=True)
    def version(p):
        target(p)
        p.add_argument("--branch")
        p.add_argument("--commit")
        p.add_argument("--extra", action="append", default=[], metavar="FIELD=VALUE")
        p.add_argument("--extra-file", help="JSON object of extra material fields")
        p.add_argument("--repo-path", help="Local checkout for ancestry verification")
        p.add_argument("--subquestion")
    preflight = command("preflight", "Validate a target and immutable version without submitting")
    version(preflight)
    submit = command("submit", "Submit once, preserving a receipt before the request")
    version(submit)
    submit.add_argument("--dry-run", action="store_true")
    submit.add_argument("--resubmit", action="store_true", help="Explicitly request another evaluation of an identical version")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--snapshot", action="store_true", help="Capture the leaderboard after --wait reaches a terminal result")
    submit.add_argument("--interval", type=number, default=15)
    submit.add_argument("--timeout", type=number, default=600)
    adopt = command("adopt", "Create a receipt for an existing authenticated task")
    target(adopt)
    adopt.add_argument("--task-id", required=True, type=int)
    score = command("score", "Query the exact task in a receipt")
    score.add_argument("--receipt", required=True)
    score.add_argument("--wait", action="store_true")
    score.add_argument("--interval", type=number, default=15)
    score.add_argument("--timeout", type=number, default=600)
    reconcile = command("reconcile", "Resolve an accepted or uncertain submission without writing again")
    reconcile.add_argument("--receipt", required=True)
    reconcile.add_argument("--task-id", type=int)
    board = command("leaderboard", "Capture all accessible leaderboard pages")
    target(board)
    board.add_argument("--page-size", type=int, default=100)
    board.add_argument("--max-pages", type=int, default=200)
    board.add_argument("--format", choices=["json", "csv"], default="json")
    board.add_argument("--output")
    diff = command("diff", "Compare two complete snapshots of the same target")
    diff.add_argument("--before", required=True)
    diff.add_argument("--after", required=True)
    experiments = command("experiments", "Show immutable-version results and the best successful score")
    target(experiments)
    experiments.add_argument("--refresh", action="store_true")
    experiments.add_argument("--format", choices=["json", "csv"], default="json")
    experiments.add_argument("--output")
    compare = command("compare", "Compare the exact scores of two immutable-version receipts")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    watch = command("watch", "Bounded leaderboard monitoring or task waiting")
    mode = watch.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profile")
    mode.add_argument("--receipt")
    watch.add_argument("--interval", type=number, default=60)
    watch.add_argument("--timeout", type=number, default=1800)
    watch.add_argument("--max-cycles", type=int, default=1000)
    queue = command("queue", "Run an explicit, bounded, resumable version queue")
    queues = queue.add_subparsers(dest="queue_action", required=True)
    run = command("run", "Run or resume a JSON plan", queues)
    run.add_argument("plan")
    run.add_argument("--max-submissions", type=int)
    run.add_argument("--interval", type=number, default=60)
    run.add_argument("--timeout", type=number, default=600)
    run.add_argument("--continue-on-failure", action="store_true")
    status = command("status", "Read a queue checkpoint without network access", queues)
    status.add_argument("checkpoint")
    practice = command("practice", "seekdb practice material helpers")
    practice_sub = practice.add_subparsers(dest="practice_action", required=True)
    verification = command("verification", "Get the team's stable practice verification line", practice_sub)
    target(verification)
    return root


def version_args(args):
    extra = read_json(args.extra_file) if args.extra_file else {}
    if not isinstance(extra, dict):
        raise ToolError("Extra file must contain a JSON object")
    for item in args.extra:
        if "=" not in item:
            raise ToolError("--extra must use FIELD=VALUE")
        key, value = item.split("=", 1)
        if key in extra:
            raise ToolError("An extra field was supplied more than once")
        extra[key] = value
    return {"branch": args.branch, "commit": args.commit, "extra": extra, "repo_path": args.repo_path, "subquestion": args.subquestion}


def run(args, workflow, emit):
    api, store = workflow.api, workflow.store
    if args.command == "auth":
        return dict(workflow.identity(), outcome="authenticated")
    if args.command == "doctor":
        return {"version": __version__, "python": sys.version.split()[0], "git_available": shutil.which("git") is not None, "state_dir": str(store.root), "authentication": workflow.identity(), "outcome": "doctor_passed"}
    if args.command == "camps":
        keys = {"id", "campName", "activityCode", "commitStartTime", "commitEndTime", "isCompetition", "enrolmentStatus"}
        return {"camps": [{k: v for k, v in row.items() if k in keys} for row in api.camps()]}
    if args.command == "questions":
        keys = {"id", "questionName", "scoreType", "pushStartTime", "pushEndTime", "commitTimeInterval", "dailyCommitNumber", "commitNumber", "introduce"}
        return {"camp_id": args.camp_id, "questions": [{k: v for k, v in row.items() if k in keys} for row in api.questions(args.camp_id)]}
    if args.command == "options":
        return api.options(args.question_id)
    if args.command == "profile":
        if args.profile_action == "list":
            return {"profiles": store.list("profiles")}
        if args.profile_action == "show":
            return workflow.profile(args.name, authenticate=False)
        return workflow.create_profile(args.name, args.track, args.camp_id, args.question_id, args.repository, args.branch, args.commit, args.direction)
    if args.command == "preflight":
        result = workflow.preflight(args.profile, **version_args(args))
        result["existing_task_count"] = len(result.pop("tasks"))
        return result
    if args.command == "submit":
        if args.snapshot and not args.wait:
            raise ToolError("--snapshot requires --wait so the capture follows this task's terminal result")
        if args.interval <= 0 or args.timeout <= 0:
            raise ToolError("Wait interval and timeout must be positive")
        result = workflow.submit(args.profile, dry_run=args.dry_run, resubmit=args.resubmit, **version_args(args))
        if args.wait and not args.dry_run:
            result["result"] = workflow.wait_score(result["receipt"], args.interval, args.timeout, emit=emit)
            if args.snapshot and result["result"]["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                try:
                    result["leaderboard"] = workflow.snapshot(args.profile, receipt_path=result["receipt"])
                except ToolError as exc:
                    # A separate read must not hide the completed submission or its receipt.
                    result["leaderboard"] = {"complete": False, "outcome": exc.outcome, "message": str(exc), **exc.details}
        return result
    if args.command == "adopt":
        return workflow.adopt(args.profile, args.task_id)
    if args.command == "score":
        return workflow.wait_score(args.receipt, args.interval, args.timeout, emit=emit) if args.wait else workflow.score(args.receipt)
    if args.command == "reconcile":
        return workflow.reconcile(args.receipt, args.task_id)
    if args.command == "leaderboard":
        return workflow.snapshot(args.profile, args.page_size, args.max_pages)
    if args.command == "diff":
        return snapshot_diff(read_json(args.before), read_json(args.after))
    if args.command == "experiments":
        if args.refresh:
            profile = workflow.profile(args.profile)
            for receipt in store.list("receipts"):
                if receipt.get("account") == profile["account"] and receipt.get("payload", {}).get("questionId") == profile["question_id"] and receipt.get("status") == "CONFIRMED":
                    workflow.score(store.path("receipts", receipt["receipt_id"]))
        return workflow.experiments(args.profile)
    if args.command == "compare":
        return workflow.compare(args.before, args.after)
    if args.command == "watch":
        if args.receipt:
            return workflow.wait_score(args.receipt, args.interval, args.timeout, emit=emit)
        return workflow.watch(args.profile, args.interval, args.timeout, args.max_cycles, emit=emit)
    if args.command == "queue":
        if args.queue_action == "status":
            return read_json(args.checkpoint)
        return workflow.run_queue(args.plan, args.max_submissions, args.interval, args.timeout, args.continue_on_failure)
    if args.command == "practice":
        profile = workflow.profile(args.profile)
        return api.verification(profile["question_id"])
    raise ToolError("Unsupported command")


def csv_text(rows):
    if not rows:
        return ""
    fields = list(dict.fromkeys(key for row in rows for key in row))
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        cells = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = dumps(value)
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) and not re_decimal(value):
                value = "'" + value
            cells[key] = value
        writer.writerow(cells)
    return out.getvalue()


def re_decimal(value):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def exit_code(result):
    state = result.get("state") or result.get("result", {}).get("state")
    if state in ("FAILED", "CANCELLED") or result.get("outcome") in ("queue_stopped_on_failure", "queue_completed_with_failures"):
        return 2
    if state in ("QUEUED", "RUNNING") or result.get("outcome") in ("queue_waiting", "queue_submission_cap_reached", "watch_deadline_reached"):
        return 3
    if result.get("leaderboard", {}).get("complete") is False:
        return 1
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    redactor = Redactor()
    try:
        cookie = load_cookie(args.cookie_file, args.cookie_stdin)
        redactor = Redactor(cookie)
        client = Client(cookie, args.http_timeout, args.retries)
        workflow = Workflow(Platform(client), Store(args.state_dir))
        def emit(value):
            print(dumps(redactor.clean(value)), file=sys.stderr, flush=True)
        result = redactor.clean(run(args, workflow, emit))
        if getattr(args, "format", "json") == "csv":
            text = csv_text(result.get("rows", result.get("records", [])))
            if args.output:
                _write_export(args.output, text, args.cookie_file)
                print(dumps({"outcome": "exported", "output": str(Path(args.output).expanduser().resolve()), "row_count": len(result.get("rows", result.get("records", []))), "snapshot": result.get("snapshot")}))
            else:
                sys.stdout.write(text)
        elif getattr(args, "output", None):
            _write_export(args.output, dumps(result, indent=2) + "\n", args.cookie_file)
            print(dumps({"outcome": "exported", "output": str(Path(args.output).expanduser().resolve()), "snapshot": result.get("snapshot")}))
        else:
            print(dumps(result, indent=2))
        return exit_code(result)
    except ToolError as exc:
        print(dumps(redactor.clean({"outcome": exc.outcome, "message": str(exc), **exc.details})), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print(dumps({"outcome": "interrupted", "message": "Local receipts and checkpoints are retained; reconcile before another write"}), file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(dumps({"outcome": "invalid_input_or_state", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


def _write_export(path, text, cookie_file=None):
    import tempfile

    target = Path(path).expanduser().resolve()
    secret_path = cookie_file or os.environ.get("OBCOMP_COOKIE_FILE")
    if secret_path and target == Path(secret_path).expanduser().resolve():
        raise ToolError("Output path would overwrite the input Cookie file")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".obcomp-export-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    raise SystemExit(main())
