"""Browser pages: device sign-in (`highhx login`) and billing return pages.

Forms are parsed from the urlencoded body directly (no multipart dependency).
Pages carry strict security headers (see app.py) and no third-party assets.
"""

from __future__ import annotations

import html
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from highhx_platform import accounts, security
from highhx_platform.accounts import AccountProblem
from highhx_platform.config import Settings
from highhx_platform.deps import get_db, get_settings
from highhx_platform.models import DeviceAuthorization
from highhx_platform.routers.auth import approve_device, client_ip, limits

router = APIRouter(include_in_schema=False)

STYLE = """
:root{color-scheme:light dark;--fg:#1c1b22;--bg:#fafafa;--muted:#6b6875;--accent:#8b3dff;--card:#fff;--line:#e4e2ea}
@media (prefers-color-scheme:dark){:root{--fg:#ecebf0;--bg:#121118;--muted:#9b98a6;--card:#1b1a22;--line:#2c2a35}}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--fg);background:var(--bg)}
main{max-width:420px;margin:8vh auto;padding:0 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
h1{font-size:20px;margin:0 0 4px}.brand{letter-spacing:.14em;font-size:12px;color:var(--accent);font-weight:700}
p{color:var(--muted);margin:6px 0 18px}label{display:block;font-size:14px;margin:14px 0 6px}
input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid var(--line);background:transparent;color:inherit;font:inherit}
input.code{font:600 20px ui-monospace,monospace;letter-spacing:.2em;text-transform:uppercase;text-align:center}
.row{display:flex;gap:10px;margin-top:22px}button{flex:1;padding:11px;border-radius:9px;border:0;font:600 15px system-ui;cursor:pointer}
.primary{background:var(--accent);color:#fff}.secondary{background:transparent;color:var(--muted);border:1px solid var(--line)}
.check{display:flex;gap:8px;align-items:center;font-size:14px;color:var(--muted);margin-top:14px}.check input{width:auto}
.error{color:#d93f3f;font-size:14px}.ok{font-size:40px}
"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    document = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} · HighhX</title><style>{STYLE}</style></head>"
        f"<body><main><div class=card><div class=brand>HIGHHX</div>{body}</div></main></body></html>"
    )
    return HTMLResponse(document, status_code=status)


def device_form(code: str = "", email: str = "", error: str = "", host: str = "") -> str:
    err = f"<p class=error>{html.escape(error)}</p>" if error else ""
    where = f" on <b>{html.escape(host)}</b>" if host else ""
    return f"""
<h1>Sign in to the HighhX CLI</h1>
<p>Confirm the code shown in your terminal{where}. Only continue if you started <code>highhx login</code> yourself.</p>
{err}
<form method=post action=/device autocomplete=on>
<label for=code>Code</label><input class=code id=code name=code value="{html.escape(code)}" maxlength=9 required>
<label for=email>Email</label><input id=email name=email type=email value="{html.escape(email)}" autocomplete=email required>
<label for=password>Password</label><input id=password name=password type=password autocomplete=current-password minlength=10 required>
<label class=check><input type=checkbox name=create value=1> I'm new — create my HighhX account</label>
<label for=name>Name (new accounts)</label><input id=name name=name autocomplete=name>
<div class=row><button class=secondary name=action value=deny>Deny</button><button class=primary name=action value=approve>Approve</button></div>
</form>"""


@router.get("/device")
def device_page(code: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    host = ""
    if code:
        auth = db.scalar(
            select(DeviceAuthorization).where(DeviceAuthorization.user_code == security.normalize_user_code(code))
        )
        host = auth.hostname if auth is not None else ""
    return page("Sign in", device_form(code=code, host=host))


@router.post("/device")
async def device_submit(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> HTMLResponse:
    raw = await request.body()
    if len(raw) > 10_000:
        return page("Sign in", device_form(error="Request too large."), 413)
    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace")).items()}
    code, email, password = form.get("code", ""), form.get("email", ""), form.get("password", "")
    if form.get("action") == "deny":
        auth = db.scalar(
            select(DeviceAuthorization).where(DeviceAuthorization.user_code == security.normalize_user_code(code))
        )
        if auth is not None and auth.status == "pending":
            auth.status = "denied"
        return page(
            "Denied", "<h1>Sign-in denied</h1><p>The terminal will not be signed in. You can close this tab.</p>"
        )
    key = f"{client_ip(request)}|{email.strip().lower()}"
    try:
        if form.get("create"):
            if not settings.signup_enabled:
                raise AccountProblem(403, "signup_disabled", "Sign-ups are closed on this HighhX platform.")
            if not limits(request).signup.allow(client_ip(request)):
                raise AccountProblem(429, "rate_limited", "Too many sign-ups. Try again later.")
            user = accounts.create_user(db, email, password, form.get("name", ""))
        else:
            if not limits(request).login.allow(key):
                raise AccountProblem(429, "rate_limited", "Too many attempts. Try again in 15 minutes.")
            user = accounts.authenticate(db, email, password)
        if user.suspended_at is not None:
            raise AccountProblem(403, "account_suspended", "This HighhX account is suspended.")
        approve_device(db, code, user)
    except AccountProblem as exc:
        db.rollback()
        return page(
            "Sign in", device_form(code=code, email=email, error=exc.message), 400 if exc.status < 500 else exc.status
        )
    db.commit()  # before touching the shared limiter (its own transaction)
    limits(request).login.reset(key)
    plan = accounts.effective_plan(user)
    return page(
        "Signed in",
        f"<div class=ok>✓</div><h1>You're signed in</h1><p>Return to your terminal — HighhX is now connected to "
        f"<b>{html.escape(user.email)}</b> ({html.escape(plan.name)}).</p>",
    )


@router.get("/billing/success")
def billing_success() -> HTMLResponse:
    return page(
        "Welcome to Pro",
        "<div class=ok>✓</div><h1>Welcome to HighhX Pro</h1><p>Your plan is active. Back in your terminal, run "
        "<code>highhx agent</code>.</p>",
    )


@router.get("/billing/cancel")
def billing_cancel() -> HTMLResponse:
    return page(
        "Checkout cancelled", "<h1>Checkout cancelled</h1><p>Nothing was charged. HighhX Free keeps working.</p>"
    )
