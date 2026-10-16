"""
Unsubscribe Manager for Automelinium Aero.

Supports:
  1. RFC 8058 One-Click HTTP POST Unsubscribe (List-Unsubscribe=One-Click).
  2. RFC 2369 mailto: Unsubscribe via Gmail API messages.send.
  3. RFC 2369 http(s): Unsubscribe via browser / web request.
  4. Integration with RecallLogManager to automatically record unsubscribed senders as unwanted.
"""

import base64
import email.message
import re
import ssl
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class UnsubscribeInfo:
    msg_id: str
    company_name: str
    sender: str
    http_url: Optional[str] = None
    mailto_address: Optional[str] = None
    mailto_subject: str = "unsubscribe"
    is_one_click: bool = False
    raw_header: str = ""


def parse_list_unsubscribe_header(header_val: str, post_header_val: str = "") -> Tuple[Optional[str], Optional[str], str, bool]:
    """
    Parse RFC 2369 / RFC 8058 List-Unsubscribe header.
    Returns (http_url, mailto_address, mailto_subject, is_one_click).
    """
    if not header_val:
        return (None, None, "unsubscribe", False)

    http_url: Optional[str] = None
    mailto_addr: Optional[str] = None
    mailto_subj = "unsubscribe"

    # Extract all <...> delimited targets
    urls = re.findall(r"<([^>]+)>", header_val)
    if not urls:
        # Fallback if unbracketed
        urls = [header_val.strip()]

    for target in urls:
        t = target.strip()
        if t.lower().startswith("mailto:"):
            parsed = urllib.parse.urlparse(t)
            mailto_addr = parsed.path.strip()
            queries = urllib.parse.parse_qs(parsed.query)
            if "subject" in queries and queries["subject"]:
                mailto_subj = queries["subject"][0]
        elif t.lower().startswith("http://") or t.lower().startswith("https://"):
            http_url = t

    is_one_click = bool(
        http_url and post_header_val and "list-unsubscribe=one-click" in post_header_val.lower()
    )
    return (http_url, mailto_addr, mailto_subj, is_one_click)


def execute_unsubscribe(
    client,
    unsub_info: UnsubscribeInfo,
    ssl_ctx: Optional[ssl.SSLContext] = None,
) -> Tuple[bool, str]:
    """
    Execute the unsubscribe action using the cleanest available method:
      1. RFC 8058 One-Click HTTP POST if supported.
      2. Gmail API messages.send to the mailto: address.
      3. Open the HTTPS unsubscribe link in the default browser.
    """
    # 1. Try RFC 8058 One-Click POST
    if unsub_info.is_one_click and unsub_info.http_url:
        try:
            req = urllib.request.Request(
                unsub_info.http_url,
                data=b"List-Unsubscribe=One-Click",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Automelinium/1.0 (Macintosh; RFC8058)",
                },
                method="POST",
            )
            ctx = ssl_ctx or getattr(client, "ssl_ctx", None)
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                status = resp.status
                if status in (200, 201, 202, 204):
                    return (True, f"Unsubscribed from {unsub_info.company_name} via RFC 8058 One-Click POST.")
        except Exception as e:
            # Fall through to mailto or browser
            pass

    # 2. Try RFC 2369 mailto via Gmail API
    if unsub_info.mailto_address and hasattr(client, "send_email"):
        try:
            client.send_email(
                to_addr=unsub_info.mailto_address,
                subject=unsub_info.mailto_subject or "unsubscribe",
                body="unsubscribe",
            )
            return (True, f"Unsubscribe request sent to {unsub_info.mailto_address} for {unsub_info.company_name}.")
        except Exception:
            # Fall through to browser
            pass

    # 3. Open Web Link in Browser
    if unsub_info.http_url:
        try:
            webbrowser.open(unsub_info.http_url)
            return (True, f"Opened unsubscribe page in browser for {unsub_info.company_name}.")
        except Exception as e:
            return (False, f"Failed to open unsubscribe link: {e}")

    return (False, f"No valid unsubscribe URL or email found for {unsub_info.company_name}.")
