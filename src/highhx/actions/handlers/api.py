"""``api.request``: one HTTP(S) request, as an action.

Risk follows what the request can do: a read of this computer (loopback) is LOW, a read of
another host is MEDIUM (data in the URL leaves the machine), and anything that changes state
(POST, PUT, PATCH, DELETE) or uses a credential is HIGH. Credentials come from environment
variables named in ``headers_from_env``. Their values are registered with the redactor and never
logged. Policy names are ``network:<host>`` (and ``credential:use``), so a policy can allow or
deny hosts.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlencode, urlparse

from highhx.actions.policy import Risk
from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.model.capabilities import is_loopback
from highhx.agent.tools.base import ToolError
from highhx.computer.network import sanitize_url

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
MAX_BODY = 64_000
SHOWN_HEADERS = ("content-type", "content-length", "location", "etag", "last-modified")


def method_of(inputs: Inputs) -> str:
    return str(inputs.get("method") or "GET").upper()


def risk_for(inputs: Inputs) -> Risk:
    url = str(inputs.get("url") or "")
    if inputs.get("headers_from_env") or method_of(inputs) not in SAFE_METHODS or inputs.get("allow_private"):
        return Risk.HIGH
    return Risk.LOW if is_loopback(url) else Risk.MEDIUM


def policy_name(inputs: Inputs) -> str:
    if inputs.get("headers_from_env"):
        return "credential:use"
    return f"network:{urlparse(str(inputs.get('url') or '')).hostname or 'unknown'}"


# ------------------------------------------------------------ SSRF protection
CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
MAX_REDIRECTS = 5
METADATA = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200"})


def classify_address(ip: str) -> str:
    """``loopback`` · ``private`` · ``link_local`` (incl. cloud metadata) · ``reserved`` · ``public``."""
    address = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if ip in METADATA or address.is_link_local:
        return "link_local"
    if address.is_loopback:
        return "loopback"
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return "reserved"
    if address.is_private:
        return "private"
    return "public"


def resolve(host: str, port: int) -> list[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


@dataclass
class AddressPolicy:
    """Which addresses a request may connect to. Decided when each connection is made (redirects
    included) on the address actually dialled, so DNS cannot change it between check and connect."""

    loopback: bool
    """The request was explicitly for this computer."""
    private: bool
    """``allow_private``: the person approved private-network access (high risk)."""
    hops: list[str] = field(default_factory=list)

    def allowed(self, host: str, port: int) -> str:
        try:
            addresses = resolve(host, port)
        except OSError as exc:
            raise urllib.error.URLError(f"cannot resolve {host}: {exc}") from None
        for ip in addresses:
            kind = classify_address(ip)
            if kind in ("link_local", "reserved"):
                raise urllib.error.URLError(f"refused: {host} is a {kind.replace('_', '-')} address ({ip})")
            if kind == "loopback" and not self.loopback:
                raise urllib.error.URLError(f"refused: {host} is this computer ({ip}), which the request did not ask for")
            if kind == "private" and not self.private:
                raise urllib.error.URLError(f"refused: {host} is on a private network ({ip}); allow_private is needed")
        return addresses[0]


def _pinned(policy: AddressPolicy, base: type) -> type:
    class Pinned(base):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)

            def create(address: tuple[str, int], timeout: Any = None, source: Any = None) -> socket.socket:
                ip = policy.allowed(address[0], address[1])
                return socket.create_connection((ip, address[1]), timeout, source)

            self._create_connection = create

    return Pinned


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: AddressPolicy) -> None:
        self.policy = policy

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if len(self.policy.hops) >= MAX_REDIRECTS:
            raise urllib.error.URLError(f"more than {MAX_REDIRECTS} redirects")
        target = urlparse(newurl)
        if target.scheme not in ("http", "https"):
            raise urllib.error.URLError(f"refused redirect to {target.scheme}:")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        self.policy.hops.append(sanitize_url(newurl))
        if (target.hostname or "") != (urlparse(req.full_url).hostname or ""):
            for name in list(new.headers):
                if name.lower() in CREDENTIAL_HEADERS:
                    del new.headers[name]  # never hand a credential to another host
            for name in list(new.unredirected_hdrs):
                if name.lower() in CREDENTIAL_HEADERS:
                    del new.unredirected_hdrs[name]
            self.policy.loopback = self.policy.loopback and is_loopback(newurl)
        return new


def opener(policy: AddressPolicy) -> urllib.request.OpenerDirector:
    import http.client

    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return self.do_open(cast(Any, _pinned(policy, http.client.HTTPConnection)), req)

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req: Any) -> Any:
            return self.do_open(cast(Any, _pinned(policy, http.client.HTTPSConnection)), req, context=getattr(self, "_context", None))

    return urllib.request.build_opener(HTTP(), HTTPS(), _Redirects(policy))


# ---------------------------------------------------------- response helpers
def json_path(value: Any, path: str) -> Any:
    """``data.items.0.id`` in parsed JSON (KeyError when it is not there)."""
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError(path)
    return value


def schema_problems(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """A small JSON Schema check: type, enum, properties, required, items, minimum/maximum."""
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    wanted = schema.get("type")
    problems: list[str] = []
    if wanted in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, int | float) or (wanted == "integer" and not float(value).is_integer()):
            return [f"{path}: expected {wanted}"]
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above {schema['maximum']}")
    elif wanted in kinds and not isinstance(value, kinds[wanted]):
        return [f"{path}: expected {wanted}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: not one of {schema['enum']}")
    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}.{name}: required")
        for name, sub in (schema.get("properties") or {}).items():
            if name in value and isinstance(sub, dict):
                problems += schema_problems(value[name], sub, f"{path}.{name}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, entry in enumerate(value[:1000]):
            problems += schema_problems(entry, schema["items"], f"{path}[{index}]")
    return problems


def request(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    url = str(inputs["url"])
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError(f"only http(s) URLs can be requested, not {url!r}")
    if inputs.get("query"):
        extra = urlencode([(str(k), str(v)) for k, v in dict(inputs["query"]).items()])
        url = parsed._replace(query=f"{parsed.query}&{extra}" if parsed.query else extra).geturl()
        parsed = urlparse(url)
    method = method_of(inputs)
    headers = {str(k): str(v) for k, v in (inputs.get("headers") or {}).items()}
    secrets: list[str] = []
    for header, variable in (inputs.get("headers_from_env") or {}).items():
        value = os.environ.get(str(variable))
        if value is None:
            raise ToolError(f"environment variable {variable} is not set")
        headers[str(header)] = value
        secrets.append(value)
    if secrets:
        ctx.app.engine.redactor.add(secrets)
    body: bytes | None = None
    if inputs.get("json") is not None:
        body = json.dumps(inputs["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    elif inputs.get("form") is not None:
        body = urlencode([(str(k), str(v)) for k, v in dict(inputs["form"]).items()]).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif inputs.get("body") is not None:
        body = str(inputs["body"]).encode()
    timeout = float(inputs.get("timeout") or 30)
    attempts = 1 + (int(inputs.get("retries") or 0) if method in SAFE_METHODS else 0)
    policy = AddressPolicy(loopback=is_loopback(url), private=bool(inputs.get("allow_private")))
    status, raw, response_headers, error = 0, b"", {}, ""
    for attempt in range(1, attempts + 1):
        policy.hops.clear()
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with opener(policy).open(req, timeout=timeout) as response:
                status = response.status
                raw = response.read(MAX_BODY + 1)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
            error = ""
        except urllib.error.HTTPError as exc:
            with exc:  # an error response is still a response: read it, then release the connection
                status = exc.code
                raw = exc.read(MAX_BODY + 1) if exc.fp is not None else b""
                response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            error = ""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = str(getattr(exc, "reason", exc))
            if "refused:" in error or "redirect" in error:
                break  # a policy refusal is final, never retried
        if not error and status < 500:
            break
        if attempt < attempts and ctx.cancel.wait(min(8.0, 0.5 * 2 ** (attempt - 1))):
            break
    if error:
        return ActionResult(False, error=f"{method} {sanitize_url(url)} failed: {error}", summary="request failed", output={"redirects": policy.hops})
    text = raw[:MAX_BODY].decode("utf-8", errors="replace")
    output: dict[str, Any] = {
        "status": status,
        "url": url,
        "method": method,
        "headers": {k: v for k, v in response_headers.items() if k in SHOWN_HEADERS},
        "body": text,
        "truncated": len(raw) > MAX_BODY,
        "redirects": policy.hops,
        "network": [{"url": sanitize_url(url), "method": method, "status": status}],
    }
    parsed_json: Any = None
    if "json" in response_headers.get("content-type", ""):
        try:
            parsed_json = output["json"] = json.loads(text)
        except ValueError:
            pass
    expected = inputs.get("expect_status")
    # a final 3xx is a redirect that was not followed (refused scheme, too many hops): not success
    ok = (status == int(expected)) if expected is not None else 200 <= status < 300 or status == 304
    problems: list[str] = []
    if expected is None and 300 <= status < 400 and status != 304:
        problems.append(f"redirect to {sanitize_url(response_headers.get('location', '')) or 'nowhere'} was not followed")
    if inputs.get("response_schema") is not None:
        problems = schema_problems(parsed_json, dict(inputs["response_schema"])) if parsed_json is not None else ["$: the response is not JSON"]
        output["schema_problems"] = problems
    if inputs.get("extract"):
        extracted: dict[str, Any] = {}
        for name, path in dict(inputs["extract"]).items():
            try:
                extracted[str(name)] = json_path(parsed_json, str(path))
            except (KeyError, IndexError, TypeError):
                problems.append(f"extract {name}: {path} is not in the response")
        output["extracted"] = extracted
    ok = ok and not problems
    detail = "; ".join(problems[:5]) if problems else f"{method} {sanitize_url(url)} returned {status}"
    return ActionResult(
        ok,
        output=output,
        summary=f"{method} {parsed.hostname}{parsed.path or '/'} → {status}",
        verified=ok,
        error="" if ok else detail,
        retryable=method in SAFE_METHODS,
    )


# ------------------------------------------------------------------- email
SMTP_ENV = {
    "host": "HIGHHX_SMTP_HOST",
    "port": "HIGHHX_SMTP_PORT",
    "user": "HIGHHX_SMTP_USER",
    "password": "HIGHHX_SMTP_PASSWORD",
    "sender": "HIGHHX_SMTP_FROM",
}
MAX_RECIPIENTS = 20
MAX_EMAIL_BODY = 200_000
_ADDRESS = re.compile(r"^[^@\s<>,;\"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$|^[^@\s<>,;\"]{1,64}@localhost$")


def recipients_of(inputs: Inputs) -> list[str]:
    return [str(a) for key in ("to", "cc") for a in (inputs.get(key) or [])]


def email_problems(inputs: Inputs) -> list[str]:
    """Addresses that are not single plain addresses, header injection, oversized mail."""
    problems = [f"not a single e-mail address: {a[:60]!r}" for a in recipients_of(inputs) if not _ADDRESS.match(a)]
    if len(recipients_of(inputs)) > MAX_RECIPIENTS:
        problems.append(f"at most {MAX_RECIPIENTS} recipients")
    if any(c in str(inputs.get("subject") or "") for c in "\r\n"):
        problems.append("the subject may not contain line breaks")
    if len(str(inputs.get("body") or "")) > MAX_EMAIL_BODY:
        problems.append("the body is too large")
    return problems


MAX_ATTACHMENTS_BYTES = 10_000_000
_MESSAGE_ID = re.compile(r"^<[^<>\s@]{1,200}@[^<>\s@]{1,200}>$")


def email_send(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Send an e-mail (text, optionally HTML and attachments, optionally a reply) through the SMTP
    server in ``HIGHHX_SMTP_*``. The password comes only from the environment (never an input, so
    never in history or audit). TLS is required — port 465 (implicit) or STARTTLS — unless the
    server is on this computer. Always asked first (high risk); the body is logged only as its
    length. Attachments are project files (secret files refused). ``retries`` repeats only failures
    *before* the message was handed over (connecting, TLS, login): after that a retry could deliver
    it twice."""
    import mimetypes
    import smtplib
    import ssl
    from email.message import EmailMessage
    from email.utils import make_msgid

    from highhx.actions.handlers.files import _confine

    problems = email_problems(inputs)
    reply_to = str(inputs.get("in_reply_to") or "")
    if reply_to and not _MESSAGE_ID.match(reply_to):
        problems.append("in_reply_to must be a Message-ID such as <abc@example.com>")
    if problems:
        raise ToolError("; ".join(problems[:3]))
    env = {k: os.environ.get(v, "") for k, v in SMTP_ENV.items()}
    if not env["host"]:
        raise ToolError(
            f"No SMTP server is configured: set {SMTP_ENV['host']}, {SMTP_ENV['port']}, {SMTP_ENV['user']},"
            f" {SMTP_ENV['password']} and {SMTP_ENV['sender']}."
        )
    sender = str(inputs.get("from") or env["sender"] or env["user"])
    if not _ADDRESS.match(sender):
        raise ToolError(f"Set {SMTP_ENV['sender']} to the sender's address.")
    port = int(env["port"] or 587)
    local = is_loopback(f"smtp://{env['host']}")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(str(a) for a in inputs["to"])
    if inputs.get("cc"):
        message["Cc"] = ", ".join(str(a) for a in inputs["cc"])
    message["Subject"] = str(inputs["subject"])
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    if reply_to:
        message["In-Reply-To"] = reply_to
        message["References"] = reply_to
    message.set_content(str(inputs.get("body") or ""))
    if inputs.get("html"):
        message.add_alternative(str(inputs["html"]), subtype="html")
    attached: list[dict[str, Any]] = []
    total = 0
    for raw_path in inputs.get("attachments") or []:
        path = _confine(ctx, str(raw_path), must_exist=True)
        if path.is_dir():
            raise ToolError(f"{raw_path} is a directory")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_ATTACHMENTS_BYTES:
            raise ToolError(f"attachments exceed {MAX_ATTACHMENTS_BYTES // 1_000_000} MB")
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        main, _, sub = kind.partition("/")
        message.add_attachment(data, maintype=main, subtype=sub, filename=path.name)
        attached.append({"name": path.name, "bytes": len(data), "type": kind})
    timeout = float(inputs.get("timeout") or 30)
    attempts = 1 + int(inputs.get("retries") or 0)
    context = ssl.create_default_context()
    refused: dict[str, Any] = {}
    handed_over = False
    error = ""
    for attempt in range(1, attempts + 1):
        try:
            if port == 465:
                client: smtplib.SMTP = smtplib.SMTP_SSL(env["host"], port, timeout=timeout, context=context)
            else:
                client = smtplib.SMTP(env["host"], port, timeout=timeout)
            with client:
                client.ehlo()
                if port != 465:
                    if client.has_extn("starttls"):
                        client.starttls(context=context)
                        client.ehlo()
                    elif not local:
                        raise ToolError(
                            f"{env['host']} does not offer TLS (STARTTLS); refusing to send the password and mail in clear text."
                        )
                if env["user"] and env["password"]:
                    client.login(env["user"], env["password"])
                handed_over = True
                refused = client.send_message(message, from_addr=sender, to_addrs=recipients_of(inputs))
            error = ""
            break
        except (smtplib.SMTPException, OSError) as exc:
            detail = str(exc).replace(env["password"], "…") if env["password"] else str(exc)
            error = f"the mail server refused or failed: {detail[:200]}"
            if handed_over or isinstance(exc, smtplib.SMTPAuthenticationError) or attempt == attempts:
                break  # never resend a message the server may have taken; a bad password stays bad
            if ctx.cancel.wait(min(8.0, attempt * 1.0)):
                break
    if error:
        return ActionResult(False, error=error, output={"attempts": attempt, "handed_over": handed_over})
    delivered = [a for a in recipients_of(inputs) if a not in refused]
    return ActionResult(
        bool(delivered) and not refused,
        output={"accepted": delivered, "refused": sorted(refused), "message_id": message["Message-ID"], "attachments": attached, "attempts": attempt},
        summary=f"sent to {len(delivered)} recipient(s)" + (f", {len(refused)} refused" if refused else ""),
        verified=not refused,
        error="" if not refused else f"refused: {', '.join(sorted(refused))}",
    )
