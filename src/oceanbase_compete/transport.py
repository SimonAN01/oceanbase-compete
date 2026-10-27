"""Small HTTPS client with bounded reads, read retries and no write retries."""

import json
import os
import re
import socket
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import AuthError, DeadlineReached, OutcomeUnknown, ProtocolError, ToolError

HOSTS = frozenset({"openwebapi.oceanbase.com", "webapi.oceanbase.com"})
SECRET_KEY = re.compile(r"cookie|authorization|token|password|secret", re.I)


def dumps(value, **kwargs):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False, **kwargs)


def loads(value):
    def invalid(text):
        raise ValueError("non-finite JSON number")

    return json.loads(value, parse_float=Decimal, parse_constant=invalid)


def load_cookie(path=None, stdin=False, stream=None):
    """Accept a raw header, a Netscape jar, or a browser-export JSON array."""
    if path and stdin:
        raise ToolError("Choose a cookie file or stdin, not both")
    if stdin:
        import sys

        raw = (stream or sys.stdin).read(1_000_001)
    elif path or os.environ.get("OBCOMP_COOKIE_FILE"):
        file = Path(path or os.environ["OBCOMP_COOKIE_FILE"]).expanduser()
        try:
            if file.stat().st_size > 1_000_000:
                raise ToolError("Cookie file is too large")
            raw = file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ToolError("Cannot read cookie file") from exc
    else:
        raw = os.environ.get("OBCOMP_COOKIE", "")
    if len(raw) > 1_000_000:
        raise ToolError("Cookie input is too large")
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("["):
        try:
            rows = loads(raw)
            parts = []
            for row in rows:
                domain = str(row.get("domain", "")).lstrip(".").lower()
                if domain and domain != "oceanbase.com" and not domain.endswith(".oceanbase.com"):
                    continue
                expiry = row.get("expirationDate", row.get("expires", 0))
                if expiry and float(expiry) > 0 and float(expiry) <= time.time():
                    continue
                parts.append(f"{row['name']}={row['value']}")
            raw = "; ".join(parts)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ToolError("Invalid browser cookie JSON") from exc
    elif "\t" in raw:
        parts = []
        for line in raw.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_") :]
            elif line.startswith("#") or not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                raise ToolError("Invalid Netscape cookie row")
            domain = fields[0].lstrip(".").lower()
            if domain != "oceanbase.com" and not domain.endswith(".oceanbase.com"):
                continue
            try:
                if int(fields[4]) > 0 and int(fields[4]) <= time.time():
                    continue
            except ValueError as exc:
                raise ToolError("Invalid cookie expiry") from exc
            parts.append(f"{fields[5]}={fields[6]}")
        raw = "; ".join(parts)
    raw = re.sub(r"^cookie\s*:\s*", "", raw, flags=re.I)
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ToolError("Cookie must contain a single header without control characters")
    if raw and any("=" not in part for part in raw.split(";") if part.strip()):
        raise ToolError("Invalid Cookie header")
    try:
        raw.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ToolError("Cookie contains characters that require a browser header export") from exc
    return raw


class Redactor:
    def __init__(self, cookie=""):
        self.values = set()
        if cookie:
            self.values.add(cookie)
        for part in cookie.split(";"):
            key, _, val = part.strip().partition("=")
            # Preference flags such as receive-cookie-deprecation=1 are not
            # credentials: replacing their short values would corrupt IDs.
            auth_key = key.lower() in {"authorization", "rds-token", "rds-biz-no", "auth", "session", "sessionid"} or re.search(r"(?:^|[_-])(?:token|password|secret)(?:$|[_-])", key, re.I)
            if auth_key and len(val) >= 6:
                self.values.update([val, unquote(val)])

    def clean(self, value):
        if isinstance(value, dict):
            return {k: "[redacted]" if SECRET_KEY.search(str(k)) else self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self.values, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[redacted]")
            value = re.sub(r"(?i)(authorization|rds-token|rds-biz-no|cookie)\s*[=:]\s*[^\s;,]+", r"\1=[redacted]", value)
        return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, cookie="", timeout=15, retries=2, opener=None, sleep=time.sleep):
        if timeout <= 0 or retries < 0 or retries > 5:
            raise ToolError("Invalid HTTP timeout or retry count")
        self.cookie = cookie
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep
        self.redactor = Redactor(cookie)
        self.deadline = None

    def request(self, method, url, params=None, body=None, uncertain=False):
        target = urlsplit(url)
        if target.scheme != "https" or target.hostname not in HOSTS or target.port not in (None, 443) or target.username or target.password or target.fragment:
            raise ToolError("Credentials may only be sent to the supported OceanBase HTTPS APIs")
        if not target.path.startswith("/api/"):
            raise ToolError("Unsupported API path")
        if method not in ("GET", "POST"):
            raise ToolError("Unsupported HTTP method")
        if method == "GET" and body is not None:
            raise ToolError("A GET request cannot contain a write payload")
        if params:
            url += ("&" if target.query else "?") + urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json", "Origin": "https://open.oceanbase.com", "Referer": "https://open.oceanbase.com/train", "User-Agent": "oceanbase-compete/0.1.0"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        encoded = None
        if body is not None:
            encoded = dumps(body).encode()
            headers["Content-Type"] = "application/json; charset=utf-8"
        attempts = self.retries + 1 if method == "GET" else 1
        for attempt in range(attempts):
            timeout = self.timeout
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise DeadlineReached("Request deadline reached")
                timeout = min(timeout, remaining)
            try:
                try:
                    response = self.opener.open(Request(url, data=encoded, headers=headers, method=method), timeout=timeout)
                except HTTPError as exc:
                    response = exc
                with response:
                    status = response.status
                    read = getattr(response, "read1", response.read)
                    chunks, size = [], 0
                    read_deadline = time.monotonic() + timeout
                    if self.deadline is not None:
                        read_deadline = min(read_deadline, self.deadline)
                    while True:
                        if time.monotonic() >= read_deadline:
                            raise TimeoutError("Response read deadline reached")
                        chunk = read(min(65536, 16_000_001 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > 16_000_000:
                            break
                    raw = b"".join(chunks)
                    retry_after = response.headers.get("Retry-After", "")
                if status in (401, 403):
                    raise AuthError("Login expired or access denied", http_status=status)
                if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                    delay = min(float(retry_after), 30) if retry_after.isdigit() else min(2 ** attempt, 8)
                    if self.deadline is not None:
                        delay = min(delay, max(0, self.deadline - time.monotonic()))
                    self.sleep(delay)
                    continue
                if status < 200 or status >= 300:
                    cls = OutcomeUnknown if uncertain and (status >= 500 or 300 <= status < 400) else ProtocolError
                    raise cls("API returned an unsuccessful HTTP status", http_status=status)
                if len(raw) > 16_000_000:
                    cls = OutcomeUnknown if uncertain else ProtocolError
                    raise cls("API response exceeds the size limit")
                try:
                    result = loads(raw)
                except (ValueError, UnicodeError) as exc:
                    cls = OutcomeUnknown if uncertain else ProtocolError
                    raise cls("API did not return valid JSON") from exc
                if not isinstance(result, dict):
                    cls = OutcomeUnknown if uncertain else ProtocolError
                    raise cls("API JSON envelope must be an object")
                return result
            except (URLError, TimeoutError, OSError, socket.timeout) as exc:
                if uncertain:
                    raise OutcomeUnknown("Submission response was not confirmed; reconcile its receipt before submitting again") from exc
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    raise DeadlineReached("Request deadline reached") from exc
                if attempt + 1 < attempts:
                    delay = min(2 ** attempt, 8)
                    if self.deadline is not None:
                        delay = min(delay, max(0, self.deadline - time.monotonic()))
                    self.sleep(delay)
                    continue
                raise ToolError("Network request failed", error_type=type(exc).__name__) from exc
        raise ToolError("Request attempts exhausted")


def checked(envelope, uncertain=False):
    code = envelope.get("code")
    if code in (401, 403, "401", "403"):
        raise AuthError("Login expired or access denied", business_code=code)
    if code not in (200, "200"):
        if uncertain and (code is None or not re.fullmatch(r"\d+", str(code)) or int(code) >= 500):
            raise OutcomeUnknown("Submission response has no definite business outcome")
        raise ProtocolError(str(envelope.get("message") or "API business request failed"), business_code=code)
    return envelope.get("data")
