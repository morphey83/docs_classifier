"""Telegram account linking: create/confirm tokens + the minimal linking page (§8).

Two independent handshakes, one token table (`app/services/tglink.py`):

- Bot-initiated (`/start`, no payload) — the bot creates the token; the human
  opens ``GET /tg/link/{token}`` in a browser, logs in or registers, and hits
  "confirm" (``POST /tg/link/{token}/confirm``, authed).
- Web-initiated (profile page) — ``POST /api/auth/tg-link`` (authed, in
  ``app/api/auth.py``) creates the token and returns a bot deep-link; the bot
  consumes it directly (in-process) when it receives ``/start <token>``.

These ``/tg/link/*`` routes are mounted at the site root (not under ``/api``)
— the page is a small self-contained HTML/CSS/JS flow, no build step.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.schemas.tglink import TgLinkStatusOut
from app.security import get_current_user
from app.services import tglink as svc
from app.util.time import as_aware, utcnow

router = APIRouter(tags=["telegram-link"])


@router.get("/tg/link/{token}/status", response_model=TgLinkStatusOut)
async def link_status(token: str, db: AsyncSession = Depends(get_session)) -> TgLinkStatusOut:
    tok = await svc.peek(db, token)
    if tok is None:
        return TgLinkStatusOut(valid=False, reason="ссылка не найдена")
    if tok.consumed_at is not None:
        return TgLinkStatusOut(valid=False, reason="эта ссылка уже использована")
    if as_aware(tok.expires_at) <= utcnow():
        return TgLinkStatusOut(valid=False, reason="срок действия ссылки истёк")
    kind = "bot" if tok.tg_id is not None else "web"
    return TgLinkStatusOut(valid=True, kind=kind, tg_username=tok.tg_username)


@router.post("/tg/link/{token}/confirm")
async def confirm_link(
    token: str,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    try:
        tok = await svc.confirm_bot_initiated(db, token, user)
    except svc.TgLinkError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    if tok.tg_id is not None:
        background.add_task(svc.notify_account_linked, tok.tg_id, user.username)
    return {"linked": True}


@router.get("/tg/link/{token}", response_class=HTMLResponse, include_in_schema=False)
async def link_page(token: str) -> Response:
    return HTMLResponse(_PAGE_HTML)


_PAGE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Привязка Telegram — DocsClassifier</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 420px; margin: 10vh auto; padding: 0 16px; }
  h1 { font-size: 1.2rem; }
  .card { border: 1px solid #8884; border-radius: 10px; padding: 20px; }
  input { display:block; width:100%; box-sizing:border-box; padding:8px; margin:6px 0 14px;
          border-radius:6px; border:1px solid #8888; font-size:1em; }
  button { padding:9px 16px; border-radius:6px; border:0; background:#2563eb; color:#fff;
           cursor:pointer; font-size:1em; }
  .err { color:#dc2626; margin: 8px 0; min-height: 1.2em; }
  .msg { margin: 8px 0; }
  .tabs { display:flex; gap:8px; margin-bottom:12px; }
  .tabs button { flex:1; background:#8884; color:inherit; }
  .tabs button.active { background:#2563eb; color:#fff; }
</style>
</head>
<body>
<h1>Привязка Telegram</h1>
<div class="card" id="app">Загрузка…</div>
<script>
const TOKEN = window.location.pathname.split("/").filter(Boolean).pop();
const app = document.getElementById("app");

async function api(path, opts) {
  const r = await fetch(path, Object.assign(
    {credentials: "same-origin", headers: {"Content-Type": "application/json"}}, opts || {}
  ));
  let body = null;
  try { body = await r.json(); } catch (e) {}
  return {ok: r.ok, status: r.status, body: body};
}

async function whoAmI() {
  const r = await api("/api/auth/me");
  return r.ok ? r.body : null;
}

function errText(body, fallback) {
  return (body && typeof body.detail === "string") ? body.detail : fallback;
}

async function render() {
  const st = await api("/tg/link/" + TOKEN + "/status");
  if (!st.ok || !st.body.valid) {
    app.innerHTML = '<p class="err">' + ((st.body && st.body.reason) || "Ссылка недействительна.") + "</p>";
    return;
  }
  if (st.body.kind !== "bot") {
    app.innerHTML = "<p>Эту ссылку нужно открыть в Telegram — нажмите кнопку в чате с ботом.</p>";
    return;
  }
  const who = await whoAmI();
  if (who) {
    const uname = st.body.tg_username ? " @" + st.body.tg_username : "";
    app.innerHTML =
      '<p class="msg">Вы вошли как <b>' + who.username + "</b>.</p>" +
      "<p>Привязать Telegram" + uname + "?</p>" +
      '<button id="confirm">Привязать</button>' +
      '<div class="err" id="err"></div>';
    document.getElementById("confirm").onclick = async function () {
      const r = await api("/tg/link/" + TOKEN + "/confirm", {method: "POST"});
      if (r.ok) {
        app.innerHTML = "<p>Готово! Можно вернуться в Telegram.</p>";
      } else {
        document.getElementById("err").textContent = errText(r.body, "Не удалось привязать.");
      }
    };
    return;
  }
  renderAuthForm();
}

function renderAuthForm() {
  app.innerHTML =
    '<div class="tabs"><button id="tab-login" class="active">Вход</button>' +
    '<button id="tab-register">Регистрация</button></div>' +
    '<div id="form"></div><div class="err" id="err"></div>';
  const formBox = document.getElementById("form");
  const err = document.getElementById("err");
  const tabLogin = document.getElementById("tab-login");
  const tabRegister = document.getElementById("tab-register");

  function showLogin() {
    tabLogin.classList.add("active");
    tabRegister.classList.remove("active");
    formBox.innerHTML =
      '<input id="login" placeholder="логин или email">' +
      '<input id="password" type="password" placeholder="пароль">' +
      '<button id="submit">Войти</button>';
    document.getElementById("submit").onclick = async function () {
      err.textContent = "";
      const r = await api("/api/auth/login", {method: "POST", body: JSON.stringify({
        login: document.getElementById("login").value,
        password: document.getElementById("password").value
      })});
      if (r.ok) { render(); } else { err.textContent = errText(r.body, "Неверный логин или пароль."); }
    };
  }

  function showRegister() {
    tabLogin.classList.remove("active");
    tabRegister.classList.add("active");
    formBox.innerHTML =
      '<input id="username" placeholder="логин">' +
      '<input id="email" type="email" placeholder="email">' +
      '<input id="password" type="password" placeholder="пароль (от 8 символов)">' +
      '<button id="submit">Создать аккаунт</button>';
    document.getElementById("submit").onclick = async function () {
      err.textContent = "";
      const r = await api("/api/auth/register", {method: "POST", body: JSON.stringify({
        username: document.getElementById("username").value,
        email: document.getElementById("email").value,
        password: document.getElementById("password").value
      })});
      if (r.ok) { render(); } else { err.textContent = errText(r.body, "Не удалось зарегистрироваться."); }
    };
  }

  tabLogin.onclick = showLogin;
  tabRegister.onclick = showRegister;
  showLogin();
}

render();
</script>
</body>
</html>
"""
