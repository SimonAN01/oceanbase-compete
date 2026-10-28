"""Protocol adapter for OceanBase's 2026 training/competition platform."""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit
from .errors import AuthError, ProtocolError, ToolError
from .storage import fingerprint
from .transport import checked, dumps

BASE = "https://openwebapi.oceanbase.com/api/techcamp/testplatform/"
TRACKS = {
    "powercontext": {"camp_id": 21, "question_id": 600070},
    "seekdb-code": {"camp_id": 21, "question_id": 600069},
    "seekdb-practice": {"camp_id": 21, "question_id": 600071},
    "seekdb-performance": {"camp_id": 21, "question_id": 600080},
}
STATES = {0: "QUEUED", 1: "RUNNING", 2: "SUCCEEDED", 3: "FAILED", 4: "CANCELLED"}
EXTRA_FIELDS = {"SEEKDB_PRACTICE": {"deploymentUuid", "prUrl", "forkUrl"}, None: {"prUrl", "workUrl"}}


def positive_int(value, name="ID"):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value)):
        raise ToolError(f"{name} must be a positive integer")
    return int(value)


def integer(value, name):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ProtocolError(f"Invalid {name} in pagination")
    return int(value)


def repo_url(value):
    if not isinstance(value, str):
        raise ToolError("Repository URL is required")
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value) or value.rsplit("/", 1)[-1] in (".", ".."):
        raise ToolError("Use a GitHub HTTPS repository URL without credentials, query or fragment")
    return value


def same_repo(a, b):
    return registered_repo_url(a).lower() == registered_repo_url(b).lower()


def registered_repo_url(value):
    """Canonical identity for a clone URL returned by the official platform.

    Command-line repository arguments still use the credential-free repo_url.
    A private clone credential is kept only in the adapter's process memory.
    """
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ToolError("Invalid registered GitHub repository URL")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "github.com"
                or parsed.port is not None or "?" in value or "#" in value):
            raise ValueError("invalid clone destination")
        if parsed.username is not None or parsed.password is not None:
            if not parsed.username or not parsed.password or value.count("@") != 1:
                raise ValueError("incomplete clone credentials")
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127
                   for c in unquote(parsed.username + parsed.password)):
                raise ValueError("invalid clone credentials")
        return repo_url("https://github.com" + parsed.path)
    except ValueError:
        raise ToolError("Invalid registered GitHub repository URL") from None


def score_text(value):
    if value is None or value == "" or value == "-":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ProtocolError("Score is not a decimal number") from exc
    if not number.is_finite():
        raise ProtocolError("Score must be finite")
    if len(number.as_tuple().digits) > 4096 or abs(number.as_tuple().exponent) > 4096:
        raise ProtocolError("Score precision exceeds the supported bound")
    return str(value)


def pick_score(row, keys):
    for key in keys:
        if row.get(key) is not None and row.get(key) not in ("", "-"):
            return score_text(row[key]), key
    return None, None


def collect_pages(fetch, params=None, page_size=50, max_pages=200):
    """Strict pagination for endpoints whose totals and page numbers are reliable."""
    if page_size < 1 or page_size > 1000 or max_pages < 1:
        raise ToolError("Invalid pagination bounds")
    rows, seen, expected_total = [], set(), None
    for page in range(1, max_pages + 1):
        data = fetch(dict(params or {}, pageNo=page, pageSize=page_size))
        if not isinstance(data, dict):
            raise ProtocolError("Expected a pagination object")
        total = integer(data["total"], "total") if data.get("total") is not None else None
        if page == 1:
            expected_total = total
        elif total != expected_total:
            raise ProtocolError("Pagination total changed during the read; retry the complete read")
        batch = data.get("list")
        if batch is None and total == 0 and page == 1:
            batch = []  # The live empty task endpoint returns pageNum=0, list=null.
        if not isinstance(batch, list):
            raise ProtocolError("Pagination list is missing or invalid")
        reported = data.get("pageNum")
        if reported is not None and integer(reported, "page number") != page:
            if not (page == 1 and total == 0 and not batch and int(reported) == 0):
                raise ProtocolError("Server returned a different page")
        for row in batch:
            if not isinstance(row, dict) or row.get("id") is None:
                raise ProtocolError("Pagination row has no stable ID")
            identity = str(row["id"])
            if identity in seen:
                raise ProtocolError("Duplicate or repeated page; refusing to claim a complete result")
            seen.add(identity)
            rows.append(row)
        if total is not None:
            if len(rows) > total or (not batch and len(rows) < total):
                raise ProtocolError("Pagination rows do not match the reported total")
            if len(rows) == total:
                return rows
        elif not batch:
            return rows
    raise ProtocolError("Pagination exceeded its page limit; result is incomplete")


def task_result(row):
    if not isinstance(row, dict) or row.get("id") is None:
        raise ProtocolError("Task record has no ID")
    raw_status = row.get("taskStatus")
    state = STATES.get(int(raw_status)) if str(raw_status) in {str(k) for k in STATES} else None
    if state is None:
        raise ProtocolError("Unknown task status; no score has been inferred", task_status=raw_status)
    score, source = (None, None)
    if state == "SUCCEEDED":
        score, source = pick_score(row, ("strMachineScore", "machineScore"))
    return {
        "task_id": str(row["id"]), "question_id": row.get("questionId"),
        "state": state, "score_text": score, "score_source": source,
        "branch": row.get("branch"), "commit": row.get("commitId"),
        "repository": registered_repo_url(row["gitRepo"]) if row.get("gitRepo") else None, "created_at": row.get("gmtCreate"),
        "updated_at": row.get("gmtModified"), "execution_time": row.get("executeTime"),
        "error_message": row.get("errorMessage"), "failure_code": row.get("failMsg"),
        "result_type": row.get("resultType"), "deleted": row.get("deleteFlag") == 1,
    }


class Platform:
    def __init__(self, client):
        self.client = client
        self._clone_urls = {}

    def _clone_identity(self, value):
        identity = registered_repo_url(value)
        password = urlsplit(value).password
        if password:
            self.client.redactor.values.update((password, unquote(password)))
        return identity

    def get(self, path, params=None):
        return checked(self.client.request("GET", BASE + path, params=params))

    def user(self):
        envelope = self.client.request("GET", "https://webapi.oceanbase.com/api/user")
        user = envelope.get("data")
        if envelope.get("success") is not True or not isinstance(user, dict) or not user.get("uid"):
            raise AuthError("Cookie does not identify a logged-in OceanBase user")
        return {"account": fingerprint("oceanbase:" + str(user["uid"])), "display_name": user.get("nickName")}

    def camps(self):
        return collect_pages(lambda p: self.get("camp/queryOpenTestPlatformCampList", p))

    def questions(self, camp_id):
        return collect_pages(lambda p: self.get("question/webQueryTestPlatformQuestionList", p), {"campId": positive_int(camp_id)})

    def options(self, question_id):
        data = self.get("taskrecord/submissionOptions", {"questionId": positive_int(question_id)})
        if not isinstance(data, dict) or data.get("managed") is not True or not isinstance(data.get("available"), bool):
            raise ProtocolError("This tool requires explicit managed submission options for the target question")
        if str(data.get("questionId")) != str(question_id):
            raise ProtocolError("Submission options belong to a different question")
        if data.get("submissionType", "CODE") not in ("CODE", "ARTIFACT") or data.get("resultFormat", "CASES") not in ("CASES", "SCORE"):
            raise ProtocolError("Unsupported submission or result format")
        extra, required = data.get("extraFields"), data.get("requiredFields")
        if not isinstance(extra, list) or not isinstance(required, list) or not set(required).issubset(extra):
            raise ProtocolError("Incomplete submission field configuration")
        supported = EXTRA_FIELDS.get(data.get("verifier"))
        if supported is None or not set(extra).issubset(supported):
            raise ProtocolError("Target requires a submission verifier not supported by this adapter")
        return data

    def registration(self, question_id):
        data = checked(self.client.request("POST", BASE + "taskrecord/backFillCommitDetail", body={"questionId": positive_int(question_id)}))
        if not isinstance(data, dict) or not data.get("gitUrl"):
            raise ToolError("No registered team repository for this question")
        clone_url = data["gitUrl"]
        identity = self._clone_identity(clone_url)
        self._clone_urls[positive_int(question_id)] = (identity, clone_url)
        return dict(self.client.redactor.clean(data), gitUrl=identity,
                    repository_authenticated=bool(urlsplit(clone_url).password))

    def tasks(self, question_id):
        rows = collect_pages(lambda p: self.get("taskrecord/queryTestPlatformTaskRecordList", p), {"questionId": positive_int(question_id), "sortType": 1})
        for row in rows:
            if row.get("gitRepo"):
                row["gitRepo"] = self._clone_identity(row["gitRepo"])
        return self.client.redactor.clean(rows)

    def details(self, task_id):
        data = self.get("taskrecord/queryTaskRecordDetail", {"taskId": positive_int(task_id)})
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ProtocolError("Expected a task detail array")
        # Expose evaluation content, not unrelated account/contact fields.
        keys = {"test_case", "testCase", "questionScore", "score", "message", "status", "result"}
        return [{k: v for k, v in row.items() if k in keys} for row in data]

    def my_rank(self, question_id):
        data = self.get("score/queryUserScoreRank", {"questionId": positive_int(question_id)})
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("id") is None:
            raise ProtocolError("Personal leaderboard result has no stable ID")
        score, source = pick_score(data, ("webTotalScore", "totalScore"))
        rank = data.get("userRank")
        if rank is not None:
            rank = positive_int(rank, "Official rank")
        return {"entry_id": str(data["id"]), "team_id": str(data["groupId"]) if data.get("groupId") is not None else None, "rank": rank, "rank_source": "server" if rank is not None else "unavailable", "score_text": score, "score_source": source}

    def submit(self, payload):
        body = dict(payload)
        body["gitRepo"] = repo_url(payload["gitRepo"])
        registered = self._clone_urls.get(positive_int(payload["questionId"]))
        if registered:
            identity, clone_url = registered
            if not same_repo(identity, payload["gitRepo"]):
                raise ToolError("Submission repository differs from its authenticated registration")
            body["gitRepo"] = clone_url
        envelope = self.client.request("POST", BASE + "taskrecord/commitTestPlatformTaskRecord", body=body, uncertain=True)
        return checked(self.client.redactor.clean(envelope), uncertain=True)

    def verification(self, question_id):
        options = self.options(question_id)
        if options.get("verifier") != "SEEKDB_PRACTICE":
            raise ToolError("Verification text is only supported for seekdb practice")
        data = checked(self.client.request("POST", BASE + "taskrecord/seekdb-practice/verification", body={"questionId": positive_int(question_id)}))
        text = data.get("verificationText") if isinstance(data, dict) else None
        if not isinstance(text, str) or not re.fullmatch(r"OB-P:[A-Za-z0-9_-]{22}", text):
            raise ProtocolError("Invalid practice verification text")
        return {"verification_text": text}

    def leaderboard(self, question_id, page_size=100, max_pages=200):
        """Live score API reports per-page totals and pageNum=1 even on page 2.

        Continue to a short or empty page, check IDs, then re-read page one. Never use
        those misleading totals to declare a truncated first page complete.
        """
        if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 1000:
            raise ToolError("Invalid leaderboard pagination bounds")
        rows, seen, first = [], set(), None
        params = {"questionId": positive_int(question_id), "pageSize": page_size}
        for page in range(1, max_pages + 1):
            data = self.get("score/queryTestPlatformScoreList", dict(params, pageNo=page))
            if not isinstance(data, dict):
                raise ProtocolError("Leaderboard page is missing")
            batch = data.get("list")
            if batch is None and data.get("total") in (0, "0"):
                batch = []
            if not isinstance(batch, list):
                raise ProtocolError("Leaderboard list is invalid")
            if first is None:
                first = batch
            terminal = len(batch) < page_size
            for row in batch:
                if not isinstance(row, dict) or row.get("id") is None:
                    raise ProtocolError("Leaderboard row has no stable ID")
                identity = str(row["id"])
                if identity in seen:
                    raise ProtocolError("Leaderboard pagination repeated a row; capture is incomplete")
                if row.get("questionId") is not None and str(row["questionId"]) != str(question_id):
                    raise ProtocolError("Leaderboard returned a different question")
                seen.add(identity)
                score, source = pick_score(row, ("webTotalScore", "totalScore"))
                official_rank = row.get("userRank", row.get("rank"))
                rows.append({
                    "entry_id": identity, "team_id": str(row["groupId"]) if row.get("groupId") is not None else None,
                    "team_name": row.get("groupName"), "question_id": question_id,
                    "rank": official_rank, "rank_source": "server" if official_rank is not None else "unavailable",
                    "position": len(rows) + 1, "score_text": score, "score_source": source,
                    "updated_at": row.get("newestTime", row.get("gmtModified")),
                })
            if terminal:
                verify = self.get("score/queryTestPlatformScoreList", dict(params, pageNo=1))
                again = verify.get("list") if isinstance(verify, dict) else None
                if again is None and isinstance(verify, dict) and verify.get("total") in (0, "0"):
                    again = []
                if dumps(again, sort_keys=True) != dumps(first, sort_keys=True):
                    raise ProtocolError("Leaderboard changed during capture; retry to avoid mixing snapshots")
                if batch and page > 1:
                    last_again = self.get("score/queryTestPlatformScoreList", dict(params, pageNo=page))
                    if not isinstance(last_again, dict) or dumps(last_again.get("list"), sort_keys=True) != dumps(batch, sort_keys=True):
                        raise ProtocolError("Leaderboard final page changed during capture")
                return {"rows": rows, "complete": True, "coverage": "all_rows_exposed_by_question_endpoint", "atomic": False, "pages": page, "count": len(rows), "terminal_reason": "short_page" if batch else "empty_page"}
        raise ProtocolError("Leaderboard page limit reached before a terminal page")
