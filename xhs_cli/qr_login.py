"""QR code login for Xiaohongshu.

Generates a QR code in the terminal using half-block Unicode characters,
polls for scan completion, and extracts cookies via the login/activate API.

Flow discovered through reverse engineering:
1. Generate temporary a1 / webId cookies.
2. Call ``/api/sns/web/v1/login/activate`` to obtain a *guest* session.
3. Call ``/api/sns/web/v1/login/qrcode/create`` to create a QR code.
4. Render the QR URL in the terminal.
5. Poll ``/api/qrcode/userinfo`` until ``codeStatus == 2``.
6. After confirmation, verify that the current session has switched to the
   confirmed user.
7. Save the upgraded session cookies.
"""

from __future__ import annotations

import logging
import random
import time

from .client import XhsClient
from .cookies import save_cookies

logger = logging.getLogger(__name__)

# QR code status values
QR_WAITING = 0      # Waiting for scan
QR_SCANNED = 1      # Scanned, awaiting confirmation
QR_CONFIRMED = 2    # Login confirmed

# Poll config
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 240  # 4 minutes


# ── Helpers ────────────────────────────────────────────────────────────────


def _apply_session_cookies(client: XhsClient, payload: dict[str, str]) -> None:
    """Persist any session cookies returned by the QR login endpoints."""
    session = payload.get("session", "")
    secure_session = payload.get("secure_session", "")
    if session:
        client.cookies["web_session"] = session
    if secure_session:
        client.cookies["web_session_sec"] = secure_session


def _build_saved_cookies(a1: str, webid: str, payload: dict[str, str]) -> dict[str, str]:
    """Build the cookie payload persisted after QR login succeeds."""
    session = payload.get("session") or payload.get("web_session", "")
    secure_session = payload.get("secure_session") or payload.get("web_session_sec", "")
    cookies = {
        "a1": a1,
        "webId": webid,
    }
    if session:
        cookies["web_session"] = session
    if secure_session:
        cookies["web_session_sec"] = secure_session
    return cookies


def _resolved_user_id(info: dict[str, object]) -> str:
    """Extract the current user ID from either flat or nested profile payloads."""
    if not isinstance(info, dict):
        return ""
    basic = info.get("basic_info", info)
    if isinstance(basic, dict) and basic.get("user_id"):
        return str(basic["user_id"])
    if info.get("user_id"):
        return str(info["user_id"])
    if info.get("userid"):
        return str(info["userid"])
    return ""


def _wait_for_confirmed_session(
    client: XhsClient,
    confirmed_user_id: str,
    *,
    retries: int = 5,
    wait_s: float = 1.0,
) -> dict[str, object]:
    """Wait for the current session to resolve to the QR-confirmed user."""
    from .exceptions import XhsApiError

    last_info: dict[str, object] = {}
    for attempt in range(retries):
        info = client.get_self_info()
        if isinstance(info, dict):
            last_info = info
        current_user_id = _resolved_user_id(info)
        logger.debug(
            "QR verify self info attempt=%d confirmed_user_id=%s current_user_id=%s info=%s",
            attempt + 1,
            confirmed_user_id,
            current_user_id,
            info,
        )
        if current_user_id and current_user_id == confirmed_user_id:
            return info
        if attempt + 1 < retries:
            time.sleep(wait_s)

    raise XhsApiError(
        "QR login confirmed, but the current session never switched to the confirmed user. "
        f"expected={confirmed_user_id} got={_resolved_user_id(last_info) or 'unknown'}"
    )


def _generate_a1() -> str:
    """Generate a fresh a1 cookie value (52 hex chars with embedded timestamp)."""
    prefix = "".join(random.choices("0123456789abcdef", k=24))
    ts = str(int(time.time() * 1000))
    suffix = "".join(random.choices("0123456789abcdef", k=15))
    return prefix + ts + suffix


def _generate_webid() -> str:
    """Generate a webId cookie value (32 hex chars)."""
    return "".join(random.choices("0123456789abcdef", k=32))


def _render_qr_half_blocks(matrix: list[list[bool]]) -> str:
    """Render QR matrix using half-block characters (▀▄█ and space).

    Two rows of the QR matrix merge into one terminal line, halving
    the vertical footprint while keeping cells square.
    """
    if not matrix:
        return ""

    size = len(matrix)
    lines: list[str] = []

    for row_idx in range(0, size, 2):
        line = ""
        for col_idx in range(size):
            top = matrix[row_idx][col_idx]
            bot = matrix[row_idx + 1][col_idx] if row_idx + 1 < size else False

            if top and bot:
                line += "█"
            elif top and not bot:
                line += "▀"
            elif not top and bot:
                line += "▄"
            else:
                line += " "

        lines.append(line)

    return "\n".join(lines)


def _display_qr_in_terminal(data: str) -> bool:
    """Display *data* as a QR code in the terminal.  Returns True on success."""
    try:
        import qrcode  # type: ignore[import-untyped]
    except ImportError:
        return False

    qr = qrcode.QRCode(border=4)
    qr.add_data(data)
    qr.make(fit=True)

    modules = qr.get_matrix()
    print(_render_qr_half_blocks(modules))
    return True


# ── Main flow ──────────────────────────────────────────────────────────────

def qrcode_login(
    *,
    on_status: callable[[str], None] | None = None,
    timeout_s: int = POLL_TIMEOUT_S,
) -> dict[str, str]:
    """Run the QR code login flow.

    Returns:
        Cookie dict with ``a1``, ``webId``, ``web_session``.

    Raises:
        XhsApiError on timeout or failure.
    """
    from .exceptions import XhsApiError

    def _print(msg: str) -> None:
        if on_status:
            on_status(msg)
        else:
            print(msg)

    # 1. Generate temporary cookies
    a1 = _generate_a1()
    webid = _generate_webid()
    tmp_cookies = {"a1": a1, "webId": webid}

    _print("🔑 Starting QR code login...")

    with XhsClient(tmp_cookies, request_delay=0) as client:

        # 2. Activate guest session (this gives us an initial web_session)
        try:
            activate_data = client.login_activate()
            _apply_session_cookies(client, activate_data)
            guest_session = activate_data.get("session", "")
            logger.debug(
                "Initial activate: session=%s user_id=%s",
                guest_session, activate_data.get("user_id"),
            )
        except Exception as exc:
            logger.debug("Initial activate failed (non-fatal): %s", exc)
            guest_session = ""

        # 3. Create QR code
        qr_data = client.create_qr_login()

        qr_id = qr_data["qr_id"]
        code = qr_data["code"]
        qr_url = qr_data["url"]

        logger.debug("QR created: qr_id=%s, code=%s", qr_id, code)

        # 4. Display QR in terminal
        _print("\n📱 Scan the QR code below with the Xiaohongshu app:\n")
        if not _display_qr_in_terminal(qr_url):
            _print("⚠️  Install 'qrcode' for terminal rendering: pip install qrcode")
            _print(f"QR URL: {qr_url}")
        _print("\n⏳ Waiting for QR code scan...")

        # 5. Poll for confirmation
        start = time.time()
        last_status = -1

        while (time.time() - start) < timeout_s:
            time.sleep(POLL_INTERVAL_S)

            try:
                status_data = client.check_qr_status(qr_id, code)
            except Exception as exc:
                logger.debug("QR status check error: %s", exc)
                continue

            code_status = status_data.get("codeStatus", -1)
            logger.debug("QR poll: codeStatus=%s data=%s", code_status, status_data)

            if code_status != last_status:
                last_status = code_status
                if code_status == QR_SCANNED:
                    _print("📲 Scanned! Waiting for confirmation...")
                elif code_status == QR_CONFIRMED:
                    _print("✅ Login confirmed!")

            if code_status == QR_CONFIRMED:
                confirmed_user_id = status_data.get("userId", "")
                if not confirmed_user_id:
                    raise XhsApiError("QR login confirmed but no confirmed userId was returned.")

                info = _wait_for_confirmed_session(client, confirmed_user_id)
                user_id = _resolved_user_id(info)

                # 7. Save cookies
                cookies = _build_saved_cookies(a1, webid, client.cookies)
                save_cookies(cookies)
                _print(f"👤 User ID: {user_id}")

                return cookies

            elapsed = time.time() - start
            if int(elapsed) % 30 == 0 and int(elapsed) > 0:
                _print("  Still waiting...")

    raise XhsApiError("QR code login timed out after 4 minutes")
