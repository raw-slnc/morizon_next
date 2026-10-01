# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
基盤地図情報ダウンロードサービスのログイン（Keycloak/OIDC）を、
requestsだけで再現するモジュール。

サイトの地図アプリ（SPA）自体はQGIS同梱のQtWebEngine（特にQt5系）では
正常に描画できないことが実機検証で判明した一方、認証自体はKeycloakの
標準的なサーバーレンダリングHTMLフォームで完結しており、ブラウザ描画は
不要と分かったため、この方式を採用している。
"""

import html
import re
from urllib.parse import urlparse

import requests

SERVICE_BASE = "https://service.gsi.go.jp/kiban/app"
LOGIN_URL = f"{SERVICE_BASE}/api/Login"
BLANK_URL = f"{SERVICE_BASE}/blank/"
AUTH_ERROR_PATHS = ("/kiban/app/api/Authentication/error", "/error/error")


def _extract_login_action(text: str) -> str | None:
    m = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', text)
    if m:
        return html.unescape(m.group(1))
    return None


def _extract_page_message(text: str) -> str | None:
    patterns = [
        r'id="input-error"[^>]*>\s*([^<]+?)\s*</span>',
        r'class="[^"]*(?:alert|error)[^"]*"[^>]*>\s*([^<]{1,300})\s*</',
        r'<(?:h1|h2|p)[^>]*>\s*([^<]{1,300})\s*</(?:h1|h2|p)>',
        r'<title[^>]*>\s*([^<]{1,300})\s*</title>',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            message = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
            if message:
                return message
    return None


def _is_auth_error_response(resp: requests.Response) -> bool:
    path = urlparse(resp.url).path
    return any(path.endswith(error_path) for error_path in AUTH_ERROR_PATHS)


def start_login(session: requests.Session) -> str:
    """サービス本体のログイン開始APIへアクセスし、Keycloakフォームのaction URLを返す。

    2026年時点の基盤地図情報ダウンロードサービスは、Web画面のログインボタンで
    /api/Login?next=... を呼び、サービス側セッションにstateやPKCE情報を保存してから
    Keycloakへリダイレクトする。Keycloakを直接開始するとAuthenticationで検証に失敗する。
    """
    resp = session.get(LOGIN_URL, params={"next": BLANK_URL}, timeout=30)
    resp.raise_for_status()
    action_url = _extract_login_action(resp.text)
    if not action_url:
        message = _extract_page_message(resp.text)
        detail = f": {message}" if message else ""
        raise RuntimeError(
            "ログインフォームが見つかりませんでした"
            f"{detail}（サイト側の仕様が変わった可能性があります）"
        )
    return action_url


def _force_https(url: str) -> str:
    """redirect_uriがhttp://service.gsi.go.jpのまま設定されており、実ブラウザはHSTSで
    自動的にhttps化しているのに対し、requestsはHSTSを解釈せずそのままポート80へ接続して
    タイムアウトするため、既知のホストに限定して強制的にhttps化する。"""
    if url.startswith("http://service.gsi.go.jp"):
        return "https://" + url[len("http://"):]
    return url


def submit_login(session: requests.Session, action_url: str, username: str, password: str):
    """ログインを試行する。戻り値は(成功したか, 失敗時のエラーメッセージ, リダイレクト経路のログ)。
    成功時はリダイレクトを手動で辿り(http://redirect_uriのhttps強制含む)、
    Cookieセッションが完成した状態にする
    （実際にログインできたかはfgd_fetcher.check_login()で最終確認すること）。"""
    chain = []
    resp = session.post(
        action_url,
        data={"username": username, "password": password, "credentialId": ""},
        timeout=30,
        allow_redirects=False,
    )
    chain.append(f"POST login -> {resp.status_code}")
    if not (300 <= resp.status_code < 400):
        message = _extract_page_message(resp.text)
        if message:
            return False, message, chain
        return False, f"ログインに失敗しました（予期しない応答: HTTP {resp.status_code}）。", chain

    location = resp.headers.get("Location")
    for _ in range(10):
        if not location:
            break
        was_http = location.startswith("http://service.gsi.go.jp")
        location = _force_https(location)
        # サーバー側がredirect_uri(元はhttp://)をリクエストの実スキームから再構築している場合に
        # 備え、プロキシ経由で来たように見せてhttpのままアクセスされたと伝える
        headers = {"X-Forwarded-Proto": "http"} if was_http else {}
        resp = session.get(location, timeout=30, allow_redirects=False, headers=headers)
        chain.append(f"{location} -> {resp.status_code}")
        location = resp.headers.get("Location")
        if not (300 <= resp.status_code < 400):
            break

    if 300 <= resp.status_code < 400:
        return False, "ログイン後のリダイレクトが完了しませんでした。", chain

    if _is_auth_error_response(resp) or "error" in resp.url.lower():
        message = _extract_page_message(resp.text)
        detail = f"（{message}）" if message else ""
        return (
            False,
            "ログイン後の認証確認に失敗しました"
            f"{detail}。時間をおいて再試行してください。",
            chain,
        )

    return True, None, chain
