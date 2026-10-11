"""Run the site's original check-in form and Cap widget in a fresh browser."""
import threading
import time
import os
from contextlib import ExitStack
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

CHECKIN_URL = "https://sb.sb/checkin/"
# The forum declares its hosted Cap endpoint on this exact public service origin.
CAP_ORIGIN = "https://capjs.net"
_LOCK = threading.Lock()
_SIGNED_JS = """() => [...document.querySelectorAll('button.btn-post')].some(
    b => b.disabled && b.textContent.trim() === '今日已签到')"""
_SAFE_CAP_ERRORS = {
    "automated_browser_detected", "instr_automated_browser", "instrumentation_timeout",
    "expired", "invalid_token", "invalid_solution", "invalid_solutions",
    "rate_limited", "already_redeemed", "scope_mismatch",
}
_SAFE_CAP_CLIENT_ERRORS = _SAFE_CAP_ERRORS | {
    "instr_blocked", "instr_timeout", "aborted", "challenge_parse_error",
    "challenge_unsupported", "missing_endpoint", "network_error",
    "redeem_failed", "solve_failed", "invalid_expires",
}


def cap_client_error(code):
    """Copy only known widget codes, never its message or arbitrary event data."""
    return "cap_client_" + code if isinstance(code, str) and code in _SAFE_CAP_CLIENT_ERRORS else "cap_client_error"


def cap_post_urls(endpoint):
    """Accept only the forum or its pinned hosted Cap origin, from the form."""
    if not endpoint:
        return set()
    base = urljoin(CHECKIN_URL, endpoint)
    parts = urlsplit(base)
    if (parts.scheme != "https" or parts.netloc not in ("sb.sb", "capjs.net")
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path in ("", "/")):
        raise ValueError("invalid_cap_endpoint")
    base = base.rstrip("/") + "/"
    return {base + "challenge", base + "redeem"}


def cookie_records(cookie):
    if "=" not in cookie:
        cookie = "__Host-bbs_session=" + cookie
    parsed = SimpleCookie()
    parsed.load(cookie)
    if not parsed:
        raise ValueError("invalid_cookie")
    return [{"name": name, "value": value.value, "url": "https://sb.sb/",
             "secure": True, "httpOnly": True, "sameSite": "Lax"}
            for name, value in parsed.items()]


def request_allowed(url, method, post_urls, allow_submit):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if parts.netloc == "sb.sb":
        return method == "GET" or (allow_submit and method == "POST" and url in post_urls)
    if parts.netloc == "capjs.net":
        return allow_submit and method in ("POST", "OPTIONS") and url in post_urls
    # The original widget preloads this public dependency. Never forward cookies.
    return (method == "GET" and parts.netloc == "cdn.jsdelivr.net"
            and parts.path.startswith("/npm/@cap.js/") and parts.path.endswith(".wasm")
            and not parts.query)


def failure(code, state=None):
    if code in ("automated_browser_detected", "instr_automated_browser"):
        message = "站方验证明确拒绝自动化浏览器；当天停止自动提交，请核对浏览器运行方式"
    else:
        message = "浏览器签到未完成（" + code + "）；当天停止自动提交，请人工核对"
    result = {"success": False, "needs_human": True, "browser_stage": code,
              "msg": message}
    # This evidence permits only a fresh official GET, never success by itself.
    if (state is not None and state["checkin_posts"] == 1
            and state["checkin_response"] in (200, 302, 303)
            and state["error"] in (None, "post_transport_error")):
        result["submission_accepted"] = True
    return result


def post_response_allowed(url, status, location):
    if 300 <= status < 400:
        return (url == CHECKIN_URL and status in (302, 303)
                and bool(location) and urljoin(CHECKIN_URL, location) == CHECKIN_URL)
    return True


def _navigation_context_changed(error):
    # Match known driver diagnostics in memory; never log the exception text.
    message = str(error)
    return ("Execution context was destroyed" in message
            or "Cannot find context with specified id" in message)


def run_checkin(cookie, allow_submit=True, cap_endpoint=None, *, playwright_factory=None,
                browser_mode=None):
    """Use original verification and confirm the business result, without logging secrets."""
    if not _LOCK.acquire(blocking=False):
        return failure("browser_busy")
    browser = None
    state = None
    phase = "inputs"
    try:
        try:
            records = cookie_records(cookie)
        except ValueError:
            return failure("invalid_cookie")
        try:
            allowed_posts = cap_post_urls(cap_endpoint) if allow_submit else set()
        except ValueError:
            return failure("invalid_cap_endpoint")
        if allow_submit:
            allowed_posts.add(CHECKIN_URL)
        mode = browser_mode or os.environ.get("SBSB_BROWSER_MODE", "headless")
        if mode not in ("headless", "desktop"):
            return failure("invalid_browser_mode")
        if playwright_factory is None:
            if mode == "desktop":
                from app.browser_runtime import desktop_playwright
                playwright_factory = desktop_playwright
            else:
                from playwright.sync_api import sync_playwright
                playwright_factory = sync_playwright
        with ExitStack() as resources:
            phase = "launch"
            runtime = resources.enter_context(playwright_factory())
            browser = runtime.chromium.launch(headless=mode == "headless", timeout=20000)
            resources.callback(browser.close)
            phase = "context"
            context_options = {"no_viewport": True} if mode == "desktop" else {}
            context = browser.new_context(service_workers="block", **context_options)
            context.set_default_timeout(15000)
            context.add_cookies(records)
            state = {"checkin_posts": 0, "cap_posts": {}, "error": None,
                     "checkin_response": None, "navigated": False}

            def guard(route):
                request = route.request
                if not request_allowed(request.url, request.method, allowed_posts, allow_submit):
                    route.abort()
                    return
                if request.method == "POST":
                    if request.url == CHECKIN_URL:
                        state["checkin_posts"] += 1
                        if state["checkin_posts"] > 1:
                            state["error"] = "duplicate_submission_blocked"
                            route.abort()
                            return
                    else:
                        count = state["cap_posts"].get(request.url, 0) + 1
                        state["cap_posts"][request.url] = count
                        if count > 3:
                            state["error"] = "verification_retry_limit"
                            route.abort()
                            return
                    # Browser routing can skip redirected requests. Inspect POST
                    # redirects before exposing them to the browser, with no retry.
                    response = None
                    try:
                        options = {"max_redirects": 0, "max_retries": 0, "timeout": 15000}
                        if urlsplit(request.url).netloc == "capjs.net":
                            # APIRequestContext shares a cookie jar with the page;
                            # explicitly suppress credentials on the hosted service.
                            headers = {k: v for k, v in request.all_headers().items()
                                       if k.lower() not in ("cookie", "authorization")}
                            headers["cookie"] = ""
                            options["headers"] = headers
                        response = route.fetch(**options)
                        if not post_response_allowed(request.url, response.status,
                                                     response.headers.get("location", "")):
                            state["error"] = "unexpected_post_redirect"
                            route.fulfill(status=409, content_type="text/plain", body="Redirect blocked")
                        else:
                            if request.url == CHECKIN_URL:
                                state["checkin_response"] = response.status
                            route.fulfill(response=response)
                    except Exception:
                        state["error"] = "post_transport_error"
                        route.abort()
                    finally:
                        if response is not None:
                            response.dispose()
                    return
                if request.method == "OPTIONS":
                    headers = {k: v for k, v in request.all_headers().items()
                               if k.lower() not in ("cookie", "authorization")}
                    headers["cookie"] = ""
                    route.continue_(headers=headers)
                else:
                    route.continue_()

            context.route("**/*", guard)
            page = context.new_page()

            def response_received(response):
                if response.request.method != "POST":
                    return
                if response.url == CHECKIN_URL:
                    state["checkin_response"] = response.status
                elif response.url in allowed_posts:
                    if response.status >= 400:
                        state["error"] = "cap_http_" + str(response.status)
                    try:
                        body = response.json()
                        if isinstance(body, dict) and (body.get("success") is False or body.get("error")):
                            code = body.get("reason") or body.get("error")
                            state["error"] = code if isinstance(code, str) and code in _SAFE_CAP_ERRORS else "cap_rejected"
                    except Exception:
                        # The form navigation may dispose a completed Cap body.
                        # Once a business POST exists, its own response/page is
                        # authoritative; a late body-read error is not rejection.
                        if response.status < 400 and not state["checkin_posts"]:
                            state["error"] = "cap_response_invalid"

            page.on("response", response_received)
            page.on("framenavigated", lambda frame: state.update(navigated=True)
                    if frame == page.main_frame and state["checkin_posts"] else None)
            phase = "page_load"
            response = page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=30000)
            if page.url != CHECKIN_URL:
                return failure("login_or_redirect")
            if not response or response.status != 200:
                return failure("checkin_page_http_error")
            if page.evaluate(_SIGNED_JS):
                return {"success": True, "already": True, "browser_stage": "already_signed"}
            if not allow_submit:
                return {"success": False, "needs_human": True, "browser_stage": "query_only_unsigned",
                        "msg": "浏览器只查询：今天尚未签到"}

            phase = "form"
            form = page.locator("form.signin-form")
            if form.count() != 1:
                return failure("missing_form")
            details = form.evaluate("""f => ({action: f.action, method: f.method,
                csrf: !!f.querySelector('input[name="_csrf"]')?.value,
                cap: !!f.querySelector('cap-widget'),
                endpoint: f.querySelector('cap-widget')?.getAttribute('data-cap-api-endpoint')})""")
            if details["action"] != CHECKIN_URL or details["method"].lower() != "post" or not details["csrf"]:
                return failure("invalid_form")
            if details["cap"]:
                try:
                    actual_posts = cap_post_urls(details["endpoint"])
                except ValueError:
                    return failure("invalid_cap_endpoint")
                if not actual_posts or (cap_endpoint and actual_posts != allowed_posts - {CHECKIN_URL}):
                    return failure("invalid_cap_endpoint")
                allowed_posts.update(actual_posts)
                phase = "widget_ready"
                page.wait_for_function("() => !!customElements.get('cap-widget')", timeout=15000)
            # Use the original submit handler, which opens Cap if a token is missing.
            phase = "form_submit"
            form.locator("button[type=submit]").click()
            if details["cap"]:
                phase = "cap_solve"
                widget = form.locator("cap-widget")
                widget.evaluate("""w => {
                    w.addEventListener('error', e => {
                        window.__monitorCapFailed = true;
                        window.__monitorCapErrorCode = e.detail?.code;
                    }, {once: true});
                    Promise.resolve(w.solve()).catch(() => {
                        window.__monitorCapFailed = true;
                    });
                }""")

            phase = "confirmation"
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    page.wait_for_timeout(250)
                    if state["error"]:
                        return failure(state["error"], state)
                    if state["checkin_response"] is not None and state["navigated"]:
                        if state["checkin_response"] not in (200, 302, 303):
                            return failure("checkin_http_" + str(state["checkin_response"]), state)
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                        if page.url == CHECKIN_URL and page.evaluate(_SIGNED_JS):
                            return {"success": True, "already": False, "browser_stage": "submitted_confirmed"}
                        return failure("submitted_unconfirmed", state)
                    # Once the original form POST starts, its navigation owns the
                    # result. Do not read the outgoing widget's destroyed context.
                    if state["checkin_posts"]:
                        continue
                    if page.evaluate("() => window.__monitorCapFailed === true"):
                        return failure(cap_client_error(page.evaluate("() => window.__monitorCapErrorCode")))
                except Exception as exc:
                    # The form can start posting while a widget evaluation is in
                    # flight. Only this known navigation race is retried, without
                    # another POST; accepted status and signed page are still required.
                    if state["checkin_posts"] and _navigation_context_changed(exc):
                        continue
                    raise
            return failure("verification_timeout", state)
    except Exception as exc:
        # Exception strings can contain URLs, form fields or tokens. Use only type.
        return failure("browser_" + phase + "_" + type(exc).__name__, state)
    finally:
        _LOCK.release()
