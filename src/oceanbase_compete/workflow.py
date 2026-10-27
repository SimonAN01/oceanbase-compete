"""Submission receipts, bounded monitoring, snapshots and resumable queues."""

import hashlib
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from .errors import DeadlineReached, OutcomeUnknown, Pending, ProtocolError, ToolError
from .platform import TRACKS, positive_int, repo_url, same_repo, score_text, task_result
from .storage import now, read_json, slug
from .transport import dumps


def validate_version(branch, commit):
    if not isinstance(branch, str) or not 1 <= len(branch) <= 64 or branch.startswith(("-", "/")) or branch.endswith(("/", ".", ".lock")) or any(c.isspace() or ord(c) < 32 for c in branch) or any(x in branch for x in ("..", "@{", "//", "\\", "~", "^", ":", "?", "*", "[")):
        raise ToolError("An explicit valid branch of 1–64 characters is required")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ToolError("An explicit full 40-character commit SHA is required")
    return branch, commit.lower()


def verify_remote(repository, branch, commit, repo_path=None):
    """Verify the branch tip, or prove an older commit is an ancestor locally."""
    try:
        result = subprocess.run(["git", "-c", "credential.interactive=never", "ls-remote", "--exit-code", "--heads", repo_url(repository), "refs/heads/" + branch], capture_output=True, text=True, timeout=30, env=_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError("Cannot verify the remote branch with git") from exc
    if result.returncode:
        raise ToolError("Remote branch is unavailable; configure Git authentication for private repositories")
    heads = [line.split()[0] for line in result.stdout.splitlines() if len(line.split()) == 2 and line.split()[1] == "refs/heads/" + branch]
    if len(heads) != 1:
        raise ToolError("The remote branch did not resolve uniquely")
    head = heads[0]
    if head != commit:
        if not repo_path:
            raise ToolError("Commit differs from the remote branch tip; provide --repo-path to prove ancestry", remote_head=head)
        try:
            path = str(Path(repo_path).expanduser().resolve())
            origin = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10)
            origin_url = origin.stdout.strip()
            if origin_url.startswith("git@github.com:"):
                origin_url = "https://github.com/" + origin_url[len("git@github.com:") :]
            if origin.returncode or not same_repo(origin_url, repository):
                raise ToolError("Local origin differs from the registered repository")
            ancestry = subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", commit, head], capture_output=True, timeout=10)
            if ancestry.returncode:
                raise ToolError("Local objects do not prove that this commit is reachable from the remote branch")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError("Cannot verify local commit ancestry") from exc
    return {"verified": True, "remote_head": head, "verified_at": now()}


def _git_env():
    import os

    return dict({k: v for k, v in os.environ.items() if k not in ("OBCOMP_COOKIE", "OBCOMP_COOKIE_FILE")}, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")


def extra_params(options, supplied):
    if not isinstance(supplied, dict):
        raise ToolError("Extra submission fields must be an object")
    permitted = options["extraFields"]
    if set(supplied) - set(permitted):
        raise ToolError("Extra fields are not allowed for this question", allowed_fields=permitted)
    result = {}
    for key, value in supplied.items():
        if not isinstance(value, str):
            raise ToolError("Extra submission field values must be strings")
        value = value.strip()
        if not value:
            continue
        if key == "deploymentUuid":
            if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
                raise ToolError("deploymentUuid must be a complete UUID")
            value = value.lower()
        elif key == "forkUrl":
            value = repo_url(value).lower()
            if value == "https://github.com/oceanbase/seekdb":
                raise ToolError("forkUrl must identify a fork, not the official repository")
        elif options.get("verifier") == "SEEKDB_PRACTICE" and key == "prUrl":
            if not re.fullmatch(r"https://github\.com/oceanbase/seekdb/pull/[1-9]\d*/?", value, re.I):
                raise ToolError("prUrl must be an official oceanbase/seekdb pull request URL")
            value = value.rstrip("/")
        else:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if len(value) > 2048 or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in value):
                raise ToolError("Submission links must be HTTPS without embedded credentials")
        result[key] = value
    if not set(options["requiredFields"]).issubset(result):
        raise ToolError("Required extra submission fields are missing", required_fields=options["requiredFields"])
    if options.get("submissionType") == "ARTIFACT" and not result:
        raise ToolError("An artifact submission needs at least one material")
    return result


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError("Server time is not a supported ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ProtocolError("Server time has no timezone")
    return parsed


def decimal_delta(after, before):
    a, b = Decimal(score_text(after)), Decimal(score_text(before))
    with localcontext() as context:
        context.prec = max(28, max(a.adjusted(), b.adjusted()) - min(a.as_tuple().exponent, b.as_tuple().exponent) + 3)
        return str(a - b)


def payload_matches(row, payload):
    if str(row.get("questionId")) != str(payload["questionId"]):
        return False
    try:
        if not same_repo(row.get("gitRepo"), payload["gitRepo"]):
            return False
    except ToolError:
        return False
    if "commitId" in payload and (str(row.get("commitId", "")).lower() != payload["commitId"] or row.get("branch") != payload["branch"]):
        return False
    if payload.get("subQuestionName") is not None and row.get("subQuestionName") is not None and row["subQuestionName"] != payload["subQuestionName"]:
        return False
    return True


def snapshot_diff(before, after):
    if before.get("scope") != after.get("scope"):
        raise ToolError("Snapshots belong to different questions or stages")
    if not before.get("complete") or not after.get("complete"):
        raise ToolError("Cannot compare incomplete snapshots")
    def index(snapshot):
        out = {}
        for row in snapshot["rows"]:
            identity = "team:" + str(row["team_id"]) if row.get("team_id") is not None else "entry:" + str(row["entry_id"])
            if identity in out:
                raise ProtocolError("Snapshot contains duplicate team identities")
            out[identity] = row
        return out
    old, new = index(before), index(after)
    changed = []
    for identity in sorted(old.keys() & new.keys()):
        a, b = old[identity], new[identity]
        if any(a.get(k) != b.get(k) for k in ("score_text", "rank", "position", "team_name")):
            score_delta = decimal_delta(b["score_text"], a["score_text"]) if a.get("score_text") is not None and b.get("score_text") is not None and a.get("score_source") == b.get("score_source") else None
            changed.append({"identity": identity, "team_name": b.get("team_name"), "score_before": a.get("score_text"), "score_after": b.get("score_text"), "score_delta": score_delta, "rank_before": a.get("rank"), "rank_after": b.get("rank"), "position_before": a.get("position"), "position_after": b.get("position")})
    added = [new[k] for k in sorted(new.keys() - old.keys())]
    removed = [old[k] for k in sorted(old.keys() - new.keys())]
    personal_change = None
    if before.get("account") == after.get("account"):
        pa, pb = before.get("personal"), after.get("personal")
        if pa != pb:
            personal_change = {"before": pa, "after": pb}
    return {"outcome": "leaderboard_changed" if changed or added or removed or personal_change else "leaderboard_unchanged", "changed": changed, "added": added, "removed": removed, "personal_change": personal_change, "scope": after["scope"]}


class Workflow:
    def __init__(self, platform, store, remote_check=verify_remote, sleep=time.sleep, clock=time.monotonic):
        self.api, self.store, self.remote_check = platform, store, remote_check
        self.sleep, self.clock = sleep, clock
        self._identity = None

    def identity(self):
        if self._identity is None:
            self._identity = self.api.user()
        return self._identity

    def create_profile(self, name, track=None, camp_id=None, question_id=None, repository=None, branch=None, commit=None, direction="max"):
        slug(name)
        if direction not in ("max", "min"):
            raise ToolError("Score direction must be max or min")
        if track:
            if track not in TRACKS:
                raise ToolError("Unknown track", tracks=list(TRACKS))
            if camp_id is not None or question_id is not None:
                raise ToolError("Choose a track or explicit camp/question IDs")
            camp_id, question_id = TRACKS[track]["camp_id"], TRACKS[track]["question_id"]
        camp_id, question_id = positive_int(camp_id), positive_int(question_id)
        identity = self.identity()
        questions = self.api.questions(camp_id)
        question = next((q for q in questions if str(q["id"]) == str(question_id)), None)
        if question is None:
            raise ToolError("Question is not listed in this camp")
        options = self.api.options(question_id)
        if str(options.get("campId")) != str(camp_id):
            raise ProtocolError("Question submission options identify a different camp")
        registered = self.api.registration(question_id)
        actual_repo = repo_url(registered["gitUrl"])
        if repository and not same_repo(repository, actual_repo):
            raise ToolError("Repository differs from the team's registered repository")
        if branch is not None or commit is not None:
            branch, commit = validate_version(branch, commit)
        profile = {"schema": 1, "name": name, "account": identity["account"], "track": track, "camp_id": camp_id, "question_id": question_id, "question_name": question.get("questionName"), "repository": actual_repo, "branch": branch, "commit": commit, "direction": direction, "created_at": now()}
        path = self.store.path("profiles", name)
        if path.exists():
            raise ToolError("Profile already exists; choose a new profile name")
        self.store.save("profiles", name, profile)
        return profile

    def profile(self, name, authenticate=True):
        profile = self.store.load("profiles", slug(name))
        if not isinstance(profile, dict) or profile.get("schema") != 1:
            raise ToolError("Unsupported profile schema")
        if authenticate and profile.get("account") != self.identity()["account"]:
            raise ToolError("Profile belongs to a different authenticated account")
        return profile

    def preflight(self, name, branch=None, commit=None, extra=None, repo_path=None, subquestion=None):
        profile = self.profile(name)
        qid, cid = profile["question_id"], profile["camp_id"]
        camp = next((x for x in self.api.camps() if str(x["id"]) == str(cid)), None)
        question = next((x for x in self.api.questions(cid) if str(x["id"]) == str(qid)), None)
        if camp is None or question is None:
            raise ToolError("Profile target is no longer listed")
        current = datetime.now(timezone.utc)
        for source, start_key, end_key in [(camp, "commitStartTime", "commitEndTime"), (question, "pushStartTime", "pushEndTime")]:
            start, end = parse_time(source.get(start_key)), parse_time(source.get(end_key))
            if start and current < start:
                raise ToolError("Submission window has not opened")
            if end and current > end:
                raise ToolError("Submission window has closed")
        options = self.api.options(qid)
        if str(options.get("campId")) != str(cid):
            raise ProtocolError("Submission options changed the target camp")
        if not options["available"]:
            raise ToolError(str(options.get("message") or "Question is not available for submission"))
        registered = self.api.registration(qid)
        if not same_repo(registered["gitUrl"], profile["repository"]):
            raise ToolError("Registered repository changed; create a new profile")
        payload = {"questionId": qid, "gitRepo": profile["repository"]}
        if subquestion:
            if not isinstance(subquestion, str) or len(subquestion) > 256:
                raise ToolError("Invalid subquestion name")
            payload["subQuestionName"] = subquestion
        if options.get("submissionType", "CODE") == "CODE":
            branch, commit = validate_version(profile.get("branch") if branch is None else branch, profile.get("commit") if commit is None else commit)
            payload.update(branch=branch, commitId=commit)
            remote = self.remote_check(profile["repository"], branch, commit, repo_path)
        else:
            if branch or commit:
                raise ToolError("Artifact questions do not accept branch or commit fields")
            remote = {"verified": None, "reason": "artifact_submission"}
        extras = extra_params(options, extra or {})
        if extras:
            payload["extraParams"] = extras
        tasks = self.api.tasks(qid)
        return {"outcome": "preflight_passed", "profile": profile, "payload": payload, "remote_version": remote, "submission_options": options, "limits": {k: question.get(k) for k in ("dailyCommitNumber", "commitTimeInterval", "commitNumber", "reserveOneTaskRecord")}, "limits_note": "Null values and undocumented units are unknown; server enforcement remains authoritative", "tasks": tasks}

    def _receipt_save(self, receipt):
        receipt["updated_at"] = now()
        path = self.store.save("receipts", receipt["receipt_id"], self.api.client.redactor.clean(receipt))
        return path

    def _load_receipt(self, path):
        receipt = read_json(path)
        if not isinstance(receipt, dict) or receipt.get("schema") != 1 or not receipt.get("receipt_id") or not isinstance(receipt.get("payload"), dict):
            raise ToolError("Invalid submission receipt")
        if receipt.get("account") != self.identity()["account"]:
            raise ToolError("Receipt belongs to a different authenticated account")
        return receipt

    def _bind(self, receipt, row):
        if not payload_matches(row, receipt["payload"]):
            raise ProtocolError("Task does not match the receipt's question, repository or version")
        receipt["task_id"] = str(row["id"])
        receipt["status"] = "CONFIRMED"
        receipt["result"] = task_result(row)
        return self._receipt_save(receipt)

    def adopt(self, name, task_id):
        profile = self.profile(name)
        row = next((r for r in self.api.tasks(profile["question_id"]) if str(r["id"]) == str(task_id)), None)
        if row is None or str(row.get("questionId")) != str(profile["question_id"]) or not same_repo(row.get("gitRepo"), profile["repository"]):
            raise ToolError("Task is not in this profile's authenticated task list")
        existing = [r for r in self.store.list("receipts") if r.get("account") == profile["account"] and str(r.get("task_id")) == str(task_id) and r.get("payload", {}).get("questionId") == profile["question_id"]]
        if existing:
            receipt = max(existing, key=lambda r: r["created_at"])
            path = self._bind(receipt, row)
            return {"outcome": "task_adopted", "receipt": path, "result": receipt["result"], "submitted": False, "reused_receipt": True}
        payload = {"questionId": profile["question_id"], "gitRepo": profile["repository"]}
        if row.get("commitId"):
            branch, commit = validate_version(row.get("branch"), row.get("commitId"))
            payload.update(branch=branch, commitId=commit)
        receipt = {"schema": 1, "receipt_id": uuid.uuid4().hex, "account": profile["account"], "profile_name": name, "payload": payload, "created_at": now(), "write_attempted": False, "source": "adopted_existing_task", "fingerprint": hashlib.sha256(dumps(payload, sort_keys=True).encode()).hexdigest()}
        path = self._bind(receipt, row)
        return {"outcome": "task_adopted", "receipt": path, "result": receipt["result"], "submitted": False}

    def submit(self, name, dry_run=False, resubmit=False, queue_id=None, job_index=None, **version):
        profile = self.profile(name)
        with self.store.lock(f"submit-{profile['account']}-{profile['question_id']}"):
            plan = self.preflight(name, **version)
            payload, tasks = plan["payload"], plan.pop("tasks")
            if dry_run:
                return dict(plan, outcome="dry_run", submitted=False)
            digest = hashlib.sha256(dumps(payload, sort_keys=True).encode()).hexdigest()
            previous = [r for r in self.store.list("receipts") if r.get("account") == profile["account"] and str(r.get("payload", {}).get("questionId")) == str(profile["question_id"])]
            unresolved = [r for r in previous if r.get("status") in ("PREPARED", "UNCONFIRMED", "ACCEPTED") and not r.get("task_id")]
            for old in unresolved:
                path = str(self.store.path("receipts", old["receipt_id"]))
                try:
                    self.reconcile(path)
                except OutcomeUnknown:
                    raise OutcomeUnknown("An earlier submission is unresolved; reconcile it before any new write", receipt=path)
            if unresolved:
                previous = [r for r in self.store.list("receipts") if r.get("account") == profile["account"] and str(r.get("payload", {}).get("questionId")) == str(profile["question_id"])]
            if not resubmit:
                matches = [r for r in previous if r.get("fingerprint") == digest and r.get("task_id") and r.get("status") != "REJECTED"]
                if matches:
                    old = max(matches, key=lambda r: r["created_at"])
                    return {"outcome": "already_submitted", "submitted": False, "receipt": str(self.store.path("receipts", old["receipt_id"])), "task_id": old["task_id"]}
                if "commitId" in payload and not payload.get("subQuestionName"):
                    matching = [r for r in tasks if payload_matches(r, payload) and not r.get("deleteFlag")]
                    if matching:
                        latest = max(matching, key=lambda r: int(r["id"]))
                        result = self.adopt(name, latest["id"])
                        result["outcome"] = "already_submitted"
                        return result
            receipt = {"schema": 1, "receipt_id": uuid.uuid4().hex, "account": profile["account"], "profile_name": name, "payload": payload, "fingerprint": digest, "created_at": now(), "status": "PREPARED", "task_id": None, "before_task_ids": [str(t["id"]) for t in tasks], "remote_version": plan["remote_version"], "write_attempted": True, "queue_id": queue_id, "job_index": job_index}
            path = self._receipt_save(receipt)
            try:
                data = self.api.submit(payload)
            except OutcomeUnknown as exc:
                receipt["status"] = "UNCONFIRMED"
                self._receipt_save(receipt)
                raise OutcomeUnknown(str(exc), receipt=path) from exc
            except ToolError as exc:
                receipt["status"] = "REJECTED"
                receipt["error"] = self.api.client.redactor.clean(str(exc))
                self._receipt_save(receipt)
                raise
            receipt["status"] = "ACCEPTED"
            if isinstance(data, dict):
                ids = [str(data[k]) for k in ("taskId", "taskRecordId", "id") if data.get(k) is not None and not isinstance(data[k], bool)]
                if len(set(ids)) == 1 and re.fullmatch(r"[1-9]\d*", ids[0]):
                    receipt["task_id"] = ids[0]
            elif not isinstance(data, bool) and isinstance(data, (int, str)) and re.fullmatch(r"[1-9]\d*", str(data)):
                receipt["task_id"] = str(data)
            self._receipt_save(receipt)
            try:
                return dict(self.reconcile(path), submitted=True)
            except OutcomeUnknown as exc:
                raise OutcomeUnknown("Server accepted the request; task association is not confirmed yet", receipt=path) from exc

    def reconcile(self, path, task_id=None):
        receipt = self._load_receipt(path)
        if receipt.get("status") == "REJECTED":
            raise ToolError("Server rejected this submission; it cannot be reconciled as accepted")
        tasks = self.api.tasks(receipt["payload"]["questionId"])
        selected = task_id or receipt.get("task_id")
        if selected:
            if receipt.get("write_attempted") and str(selected) in set(receipt.get("before_task_ids", [])):
                raise ProtocolError("Task predates this submission; refusing to return its old score")
            matches = [r for r in tasks if str(r["id"]) == str(positive_int(selected))]
        else:
            before = set(receipt.get("before_task_ids", []))
            matches = [r for r in tasks if str(r["id"]) not in before and payload_matches(r, receipt["payload"])]
        if len(matches) != 1:
            raise OutcomeUnknown("No unique matching task is visible; no resubmission was made", receipt=str(path), candidate_count=len(matches))
        saved = self._bind(receipt, matches[0])
        return {"outcome": "submission_confirmed", "receipt": saved, "task_id": receipt["task_id"], "result": receipt["result"]}

    def score(self, path, include_details=True):
        bound = self.reconcile(path)
        receipt = self._load_receipt(bound["receipt"])
        result = receipt["result"]
        state = result["state"]
        if state == "SUCCEEDED" and result["score_text"] is None:
            raise ProtocolError("Task succeeded but its score is missing; no historical score was substituted")
        result["outcome"] = "score_completed" if state == "SUCCEEDED" else "evaluation_failed" if state in ("FAILED", "CANCELLED") else "evaluation_pending"
        if include_details and state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            result["details"] = self.api.details(result["task_id"])
        result["receipt"] = bound["receipt"]
        receipt["result"] = result
        self._receipt_save(receipt)
        return result

    def wait_score(self, path, interval=15, timeout=600, emit=None):
        if interval <= 0 or timeout <= 0:
            raise ToolError("Wait interval and timeout must be positive")
        end, last = self.clock() + timeout, None
        client = self.api.client
        previous_deadline = client.deadline
        client.deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    result = self.score(path)
                except DeadlineReached as exc:
                    raise Pending("Evaluation wait timed out during a query; resume with the same receipt", receipt=str(path)) from exc
                state = result["state"]
                if emit and state != last:
                    emit(result)
                last = state
                if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    return result
                remaining = end - self.clock()
                if remaining <= 0:
                    return dict(result, outcome="evaluation_pending", timed_out=True)
                self.sleep(min(interval, remaining))
                if self.clock() >= end:
                    return dict(result, outcome="evaluation_pending", timed_out=True)
        finally:
            client.deadline = previous_deadline

    def snapshot(self, name, page_size=100, max_pages=200, receipt_path=None):
        has_auth = bool(getattr(self.api.client, "cookie", ""))
        profile = self.profile(name, authenticate=has_auth)
        started = now()
        result = self.api.leaderboard(profile["question_id"], page_size, max_pages)
        snapshot_id = uuid.uuid4().hex
        personal = self.api.my_rank(profile["question_id"]) if has_auth else None
        snapshot = dict(result, schema=1, snapshot_id=snapshot_id, profile_name=name, account=profile["account"], scope={"camp_id": profile["camp_id"], "question_id": profile["question_id"], "stage": "camp_question"}, started_at=started, captured_at=now(), personal=personal, personal_authenticated=has_auth)
        path = self.store.save("snapshots", snapshot_id, self.api.client.redactor.clean(snapshot))
        if receipt_path:
            receipt = self._load_receipt(receipt_path)
            if receipt["payload"].get("questionId") != profile["question_id"] or receipt.get("result", {}).get("state") not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                raise ToolError("Snapshot can only attach to a terminal task in the same profile target")
            receipt["leaderboard_snapshot"] = {"snapshot_id": snapshot_id, "path": path, "captured_at": snapshot["captured_at"], "personal": personal}
            self._receipt_save(receipt)
        return dict(snapshot, snapshot=path)

    def experiments(self, name):
        profile = self.profile(name, authenticate=False)
        receipts = [r for r in self.store.list("receipts") if r.get("account") == profile["account"] and r.get("payload", {}).get("questionId") == profile["question_id"]]
        records = [{"receipt_id": r["receipt_id"], "task_id": r.get("task_id"), "commit": r["payload"].get("commitId"), "branch": r["payload"].get("branch"), "created_at": r["created_at"], "receipt_status": r.get("status"), "state": r.get("result", {}).get("state"), "score_text": r.get("result", {}).get("score_text"), "score_source": r.get("result", {}).get("score_source"), "execution_time": r.get("result", {}).get("execution_time"), "leaderboard_snapshot": r.get("leaderboard_snapshot")} for r in receipts]
        records.sort(key=lambda r: (r["created_at"], r["receipt_id"]))
        candidates = [r for r in records if r["state"] == "SUCCEEDED" and r["score_text"] is not None]
        comparable = len({r["score_source"] for r in candidates}) <= 1
        best = (max if profile["direction"] == "max" else min)(candidates, key=lambda r: Decimal(r["score_text"])) if candidates and comparable else None
        return {"profile": name, "direction": profile["direction"], "records": records, "best": best, "comparable": comparable, "scores_are_cached": True}

    def compare(self, before_path, after_path):
        before, after = self._load_receipt(before_path), self._load_receipt(after_path)
        a, b = before["payload"], after["payload"]
        if str(a.get("questionId")) != str(b.get("questionId")) or not same_repo(a.get("gitRepo"), b.get("gitRepo")) or a.get("subQuestionName") != b.get("subQuestionName"):
            raise ToolError("Receipts do not describe comparable targets")
        old, new = self.score(before_path), self.score(after_path)
        comparable = old["state"] == new["state"] == "SUCCEEDED" and old.get("score_source") == new.get("score_source")
        delta = decimal_delta(new["score_text"], old["score_text"]) if comparable else None
        return {"outcome": "versions_compared", "before": old, "after": new, "comparable": comparable, "score_delta": delta}

    def watch(self, name, interval=60, timeout=1800, max_cycles=1000, emit=None):
        if interval <= 0 or timeout <= 0 or max_cycles < 1:
            raise ToolError("Watch bounds must be positive")
        end, previous, events, cycles, deadline_hit = self.clock() + timeout, None, [], 0, False
        client, old_deadline = self.api.client, self.api.client.deadline
        client.deadline = time.monotonic() + timeout
        try:
            while cycles < max_cycles and self.clock() < end:
                try:
                    snapshot = self.snapshot(name)
                except DeadlineReached:
                    deadline_hit = True
                    break
                cycles += 1
                event = {"outcome": "watch_started", "count": snapshot["count"], "snapshot": snapshot["snapshot"]} if previous is None else dict(snapshot_diff(previous, snapshot), snapshot=snapshot["snapshot"])
                if event["outcome"] != "leaderboard_unchanged":
                    events.append(event)
                    if emit:
                        emit(event)
                previous = snapshot
                remaining = end - self.clock()
                if remaining <= 0 or cycles >= max_cycles:
                    break
                self.sleep(min(interval, remaining))
            return {"outcome": "watch_deadline_reached" if deadline_hit else "watch_finished", "cycles": cycles, "change_events": len(events) - (1 if events else 0), "last_snapshot": previous.get("snapshot") if previous else None}
        finally:
            client.deadline = old_deadline

    def run_queue(self, plan_path, max_submissions=None, interval=60, timeout=600, continue_on_failure=False):
        plan = read_json(plan_path)
        if not isinstance(plan, dict) or not isinstance(plan.get("jobs"), list) or not plan["jobs"]:
            raise ToolError("Queue plan requires a profile and nonempty jobs array")
        if set(plan) - {"profile", "jobs"}:
            raise ToolError("Unknown queue plan fields")
        name = plan.get("profile")
        profile = self.profile(name)
        jobs = plan["jobs"]
        allowed = {"branch", "commit", "extra", "repo_path", "subquestion"}
        if any(not isinstance(job, dict) or set(job) - allowed for job in jobs):
            raise ToolError("Queue job has unsupported fields")
        frozen_jobs = [dict(job, branch=job.get("branch", profile.get("branch")), commit=job.get("commit", profile.get("commit"))) for job in jobs]
        signatures = [dumps(job, sort_keys=True) for job in frozen_jobs]
        if len(set(signatures)) != len(signatures):
            raise ToolError("Queue plan contains duplicate jobs")
        # Validate all requested versions before the first network mutation.
        options = self.api.options(profile["question_id"])
        for job in jobs:
            extra_params(options, job.get("extra", {}))
            if job.get("subquestion") is not None and (not isinstance(job["subquestion"], str) or not 1 <= len(job["subquestion"]) <= 256):
                raise ToolError("Invalid subquestion in queue plan")
            if job.get("repo_path") is not None and (not isinstance(job["repo_path"], str) or not job["repo_path"]):
                raise ToolError("Invalid checkout path in queue plan")
            if options.get("submissionType", "CODE") == "CODE":
                validate_version(job.get("branch", profile.get("branch")), job.get("commit", profile.get("commit")))
            elif job.get("branch") is not None or job.get("commit") is not None:
                raise ToolError("Artifact queue jobs must not specify code versions")
        cap = len(jobs) if max_submissions is None else positive_int(max_submissions, "Submission cap")
        if interval < 0 or timeout <= 0:
            raise ToolError("Invalid queue interval or wait timeout")
        # Defaults are frozen in the key; changing a profile cannot redirect a resumed queue.
        key = hashlib.sha256(dumps({"plan": plan, "target": profile}, sort_keys=True).encode()).hexdigest()
        with self.store.lock("queue-" + key):
            file = self.store.path("queues", key)
            checkpoint = read_json(file) if file.exists() else {"schema": 1, "queue_id": key, "account": profile["account"], "profile": name, "jobs": [{"status": "PENDING"} for _ in jobs], "created_at": now()}
            def save():
                checkpoint["updated_at"] = now()
                self.store.save("queues", key, checkpoint)
            save()
            for index, job in enumerate(jobs):
                state = checkpoint["jobs"][index]
                if state["status"] == "DONE":
                    continue
                if state["status"] == "FAILED" and not continue_on_failure:
                    return dict(checkpoint, outcome="queue_stopped_on_failure", checkpoint=str(file))
                if state["status"] == "FAILED":
                    continue
                attempted = [r for r in self.store.list("receipts") if r.get("queue_id") == key and r.get("write_attempted")]
                if len(attempted) >= cap and not state.get("receipt") and state["status"] != "RUNNING":
                    return dict(checkpoint, outcome="queue_submission_cap_reached", checkpoint=str(file))
                if attempted and state["status"] == "PENDING":
                    elapsed = (datetime.now(timezone.utc) - parse_time(max(r["created_at"] for r in attempted))).total_seconds()
                    self.sleep(max(0, interval - elapsed))
                state["status"] = "RUNNING"
                save()
                try:
                    if not state.get("receipt"):
                        own_attempts = [r for r in attempted if r.get("job_index") == index]
                        if own_attempts:
                            own = max(own_attempts, key=lambda r: r["created_at"])
                            state["receipt"] = str(self.store.path("receipts", own["receipt_id"]))
                            save()
                    if not state.get("receipt"):
                        if len(attempted) >= cap:
                            return dict(checkpoint, outcome="queue_submission_cap_reached", checkpoint=str(file))
                        # On crash/resume a matching unresolved receipt prevents a second write.
                        submitted = self.submit(name, queue_id=key, job_index=index, **job)
                        state["receipt"] = submitted["receipt"]
                        save()
                    result = self.wait_score(state["receipt"], timeout=timeout)
                    state["result"] = result
                    save()
                    if result["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
                        capture = self.snapshot(name, receipt_path=state["receipt"])
                        state["snapshot"] = capture["snapshot"]
                    if result["state"] in ("FAILED", "CANCELLED"):
                        state["status"] = "FAILED"
                        save()
                        if not continue_on_failure:
                            return dict(checkpoint, outcome="queue_stopped_on_failure", checkpoint=str(file))
                    elif result["state"] == "SUCCEEDED":
                        state["status"] = "DONE"
                        save()
                    else:
                        save()
                        return dict(checkpoint, outcome="queue_waiting", checkpoint=str(file))
                except OutcomeUnknown as exc:
                    state["receipt"] = exc.details.get("receipt", state.get("receipt"))
                    state["error"] = str(exc)
                    save()
                    raise OutcomeUnknown("Queue stopped on an unconfirmed submission", receipt=state.get("receipt"), checkpoint=str(file)) from exc
                except ToolError as exc:
                    state["error"] = self.api.client.redactor.clean(str(exc))
                    save()
                    raise
            outcome = "queue_completed_with_failures" if any(j["status"] == "FAILED" for j in checkpoint["jobs"]) else "queue_completed"
            return dict(checkpoint, outcome=outcome, checkpoint=str(file))
