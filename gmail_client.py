"""
Gmail API Client powered by Automatisch's existing Gmail OAuth2 connection.

Features:
  - Automatic extraction & decryption of Gmail OAuth credentials from Automatisch Docker DB
  - Automatic OAuth token refresh & Selenium headless scope recovery
  - Local SQLite metadata cache (~/.config/automelinium/email_cache.db) for near-instant loads
  - Exponential backoff retry on Gmail API rate limits (HTTP 429 / 403 rateLimitExceeded / 5xx)
  - Batch Quarantine, Trash, Re-Populate (by Importance or Usefulness), and Undo Stack
"""

import concurrent.futures
import json
import os
import random
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import List, Dict, Optional, Tuple, Callable, Any

from heuristics import EmailItem, parse_sender_parts, is_corporate_email, score_emails

AUTOMATISCH_GMAIL_CONN_ID = os.environ.get("AUTOMATISCH_GMAIL_CONN_ID", "")

def _resolve_reconnect_script() -> str:
    env_script = os.environ.get("AUTOMATISCH_RECONNECT_SCRIPT")
    if env_script and os.path.exists(env_script):
        return env_script
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Applications", "Utillities", "automatisch", "scripts", "reconnect_google_connections.py"),
        os.path.join(home, "Applications", "automatisch", "scripts", "reconnect_google_connections.py"),
        os.path.join(home, ".automatisch", "scripts", "reconnect_google_connections.py"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return env_script or candidates[0]

AUTOMATISCH_RECONNECT_SCRIPT = _resolve_reconnect_script()
CONFIG_DIR = os.path.expanduser("~/.config/automelinium")
TOKEN_CACHE_PATH = os.path.join(CONFIG_DIR, "gmail_credentials.json")
SQLITE_CACHE_PATH = os.path.join(CONFIG_DIR, "email_cache.db")
WATCHLIST_PATH = os.path.join(CONFIG_DIR, "target_senders.json")

def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl._create_unverified_context()


class AutomatischGmailClient:
    def __init__(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self.ssl_ctx = _build_ssl_context()
        self.creds: Dict[str, str] = {}
        self.access_token: str = ""
        self.token_expiry: float = 0.0
        self.user_email: str = ""
        self._token_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self.undo_stack: List[Dict] = []
        self.db_path: str = SQLITE_CACHE_PATH
        self._sender_id_cache: Dict[str, int] = {}
        self._label_id_cache: Dict[str, int] = {}
        self._id_to_label_cache: Dict[int, str] = {}
        self._init_sqlite_cache()

    # -------------------------------------------------------------------------
    # Local SQLite Metadata Cache (High-Efficiency Normalized Engine)
    # -------------------------------------------------------------------------
    def _init_sqlite_cache(self):
        with self._db_lock:
            conn = sqlite3.connect(SQLITE_CACHE_PATH)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS senders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT UNIQUE NOT NULL,
                        name TEXT,
                        domain TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_senders_domain ON senders(domain)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS labels_dict (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS emails (
                        msg_id TEXT PRIMARY KEY,
                        thread_id TEXT,
                        subject TEXT,
                        sender_id INTEGER,
                        recipient TEXT,
                        date_str TEXT,
                        timestamp INTEGER,
                        has_unsubscribe INTEGER,
                        has_attachment INTEGER,
                        updated_at INTEGER,
                        label_ids TEXT,
                        snippet_comp BLOB,
                        unsub_comp BLOB,
                        FOREIGN KEY (sender_id) REFERENCES senders(id)
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_sender_id ON emails(sender_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_timestamp ON emails(timestamp DESC)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS company_logos (
                        domain TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        logo_png BLOB NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (domain, size)
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_company_logos_domain ON company_logos(domain)")
                conn.commit()

                # Seamless auto-migration of legacy unnormalized table if present
                has_old = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='email_metadata'"
                ).fetchone()
                if has_old:
                    self._migrate_legacy_cache(conn)

                # Warm in-memory caches
                cur = conn.execute("SELECT id, email FROM senders")
                for sid, semail in cur.fetchall():
                    self._sender_id_cache[semail] = sid

                cur = conn.execute("SELECT id, name FROM labels_dict")
                for lid, lname in cur.fetchall():
                    self._label_id_cache[lname] = lid
                    self._id_to_label_cache[lid] = lname
            finally:
                conn.close()

    def _migrate_legacy_cache(self, conn: sqlite3.Connection):
        """Seamlessly migrate unnormalized email_metadata table to high-efficiency schema."""
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT DISTINCT sender_email, sender_name, sender_domain FROM email_metadata"
        ).fetchall()
        for s_email, s_name, s_dom in rows:
            clean_email = (s_email or "").strip().lower()
            if clean_email:
                conn.execute(
                    "INSERT OR IGNORE INTO senders (email, name, domain) VALUES (?, ?, ?)",
                    (clean_email, s_name or "", s_dom or ""),
                )
        conn.commit()

        all_labels = set()
        for (lj,) in cur.execute("SELECT DISTINCT labels_json FROM email_metadata").fetchall():
            try:
                for lbl in json.loads(lj or "[]"):
                    all_labels.add(lbl)
            except Exception:
                pass
        for lbl in sorted(all_labels):
            conn.execute("INSERT OR IGNORE INTO labels_dict (name) VALUES (?)", (lbl,))
        conn.commit()

        s_map = {r[1]: r[0] for r in cur.execute("SELECT id, email FROM senders").fetchall()}
        l_map = {r[1]: r[0] for r in cur.execute("SELECT id, name FROM labels_dict").fetchall()}

        cur.execute(
            """
            SELECT msg_id, thread_id, subject, sender_email, recipient, date_str, timestamp,
                   snippet, labels_json, has_unsubscribe, has_attachment, updated_at,
                   unsubscribe_header, unsubscribe_post
            FROM email_metadata
            """
        )
        migrated = []
        for row in cur.fetchall():
            (mid, tid, subj, semail, recip, dstr, ts, snip, lj, hunsub, hattach, up_at, uh, up) = row
            clean_email = (semail or "").strip().lower()
            sid = s_map.get(clean_email)
            snip_blob = zlib.compress(snip.encode("utf-8"), 9) if snip else None
            unsub_blob = None
            if uh or up:
                unsub_blob = zlib.compress(f"{uh or ''}\t{up or ''}".encode("utf-8"), 9)

            l_ids = []
            try:
                for lbl in json.loads(lj or "[]"):
                    lid = l_map.get(lbl)
                    if lid:
                        l_ids.append(str(lid))
            except Exception:
                pass

            migrated.append((
                mid, tid, subj, sid, recip, dstr, ts,
                hunsub, hattach, up_at, ",".join(l_ids), snip_blob, unsub_blob
            ))

        conn.executemany(
            """
            INSERT OR REPLACE INTO emails (
                msg_id, thread_id, subject, sender_id, recipient, date_str, timestamp,
                has_unsubscribe, has_attachment, updated_at, label_ids, snippet_comp, unsub_comp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            migrated,
        )
        conn.execute("DROP TABLE email_metadata")
        conn.commit()
        try:
            conn.execute("VACUUM")
        except Exception:
            pass

    def _get_cached_items(self, msg_ids: List[str]) -> Dict[str, Dict]:
        if not msg_ids:
            return {}
        results: Dict[str, Dict] = {}
        with self._db_lock:
            conn = sqlite3.connect(SQLITE_CACHE_PATH)
            try:
                if not self._id_to_label_cache:
                    cur = conn.execute("SELECT id, name FROM labels_dict")
                    for lid, lname in cur.fetchall():
                        self._label_id_cache[lname] = lid
                        self._id_to_label_cache[lid] = lname

                for i in range(0, len(msg_ids), 400):
                    chunk = msg_ids[i:i + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    cur = conn.execute(
                        f"""
                        SELECT e.msg_id, e.thread_id, e.subject,
                               s.name, s.email, s.domain,
                               e.recipient, e.date_str, e.timestamp,
                               e.snippet_comp, e.label_ids,
                               e.has_unsubscribe, e.has_attachment,
                               e.unsub_comp
                        FROM emails e
                        LEFT JOIN senders s ON e.sender_id = s.id
                        WHERE e.msg_id IN ({placeholders})
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        s_name = row[3] or ""
                        s_email = row[4] or ""
                        s_domain = sys.intern(row[5]) if row[5] else ""
                        if s_name and s_name != s_email:
                            full_sender = f"{s_name} <{s_email}>"
                        else:
                            full_sender = s_email

                        snip_raw = row[9]
                        if snip_raw:
                            try:
                                snip_text = zlib.decompress(snip_raw).decode("utf-8")
                            except Exception:
                                snip_text = ""
                        else:
                            snip_text = ""

                        l_ids_str = row[10] or ""
                        labels = []
                        if l_ids_str:
                            for lid_s in l_ids_str.split(","):
                                if lid_s:
                                    try:
                                        lid_int = int(lid_s)
                                        if lid_int in self._id_to_label_cache:
                                            labels.append(self._id_to_label_cache[lid_int])
                                    except ValueError:
                                        pass

                        unsub_raw = row[13]
                        unsub_h, unsub_p = "", ""
                        if unsub_raw:
                            try:
                                unsub_text = zlib.decompress(unsub_raw).decode("utf-8")
                                parts = unsub_text.split("\t", 1)
                                unsub_h = parts[0]
                                unsub_p = parts[1] if len(parts) > 1 else ""
                            except Exception:
                                pass

                        results[row[0]] = {
                            "msg_id": row[0],
                            "thread_id": row[1],
                            "subject": row[2] or "",
                            "sender": full_sender,
                            "sender_name": s_name,
                            "sender_email": s_email,
                            "sender_domain": s_domain,
                            "recipient": row[6] or "",
                            "date_str": row[7] or "",
                            "timestamp": row[8] or 0,
                            "snippet": snip_text,
                            "labels": labels,
                            "has_unsubscribe": bool(row[11]),
                            "has_attachment": bool(row[12]),
                            "unsubscribe_header": unsub_h,
                            "unsubscribe_post": unsub_p,
                        }
            finally:
                conn.close()
        return results

    def _save_cached_items(self, items: List[EmailItem]):
        if not items:
            return
        now_ts = int(time.time())
        with self._db_lock:
            conn = sqlite3.connect(SQLITE_CACHE_PATH)
            try:
                for it in items:
                    clean_email = (it.sender_email or "").strip().lower()
                    if clean_email and clean_email not in self._sender_id_cache:
                        conn.execute(
                            "INSERT OR IGNORE INTO senders (email, name, domain) VALUES (?, ?, ?)",
                            (clean_email, it.sender_name or "", it.sender_domain or ""),
                        )
                for it in items:
                    for lbl in it.labels:
                        if lbl not in self._label_id_cache:
                            conn.execute("INSERT OR IGNORE INTO labels_dict (name) VALUES (?)", (lbl,))
                conn.commit()

                cur = conn.execute("SELECT id, email FROM senders")
                for sid, semail in cur.fetchall():
                    self._sender_id_cache[semail] = sid

                cur = conn.execute("SELECT id, name FROM labels_dict")
                for lid, lname in cur.fetchall():
                    self._label_id_cache[lname] = lid
                    self._id_to_label_cache[lid] = lname

                rows = []
                for it in items:
                    clean_email = (it.sender_email or "").strip().lower()
                    sid = self._sender_id_cache.get(clean_email)
                    l_ids = [str(self._label_id_cache[l]) for l in it.labels if l in self._label_id_cache]
                    l_ids_str = ",".join(sorted(l_ids))

                    snip_blob = zlib.compress(it.snippet.encode("utf-8"), 9) if it.snippet else None
                    unsub_blob = None
                    if it.unsubscribe_header or it.unsubscribe_post:
                        unsub_blob = zlib.compress(f"{it.unsubscribe_header}\t{it.unsubscribe_post}".encode("utf-8"), 9)

                    rows.append((
                        it.msg_id,
                        it.thread_id,
                        it.subject,
                        sid,
                        it.recipient,
                        it.date_str,
                        it.timestamp,
                        1 if it.has_unsubscribe else 0,
                        1 if it.has_attachment else 0,
                        now_ts,
                        l_ids_str,
                        snip_blob,
                        unsub_blob,
                    ))

                conn.executemany(
                    """
                    INSERT OR REPLACE INTO emails (
                        msg_id, thread_id, subject, sender_id, recipient, date_str, timestamp,
                        has_unsubscribe, has_attachment, updated_at, label_ids, snippet_comp, unsub_comp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

    def _update_cached_labels(self, msg_ids: List[str], add_labels: List[str], remove_labels: List[str]):
        if not msg_ids:
            return
        cached = self._get_cached_items(msg_ids)
        if not cached:
            return
        with self._db_lock:
            conn = sqlite3.connect(SQLITE_CACHE_PATH)
            try:
                for lbl in add_labels:
                    if lbl not in self._label_id_cache:
                        conn.execute("INSERT OR IGNORE INTO labels_dict (name) VALUES (?)", (lbl,))
                conn.commit()

                cur = conn.execute("SELECT id, name FROM labels_dict")
                for lid, lname in cur.fetchall():
                    self._label_id_cache[lname] = lid
                    self._id_to_label_cache[lid] = lname

                now_ts = int(time.time())
                updates = []
                for mid, data in cached.items():
                    lbls = set(data["labels"])
                    for r in remove_labels:
                        lbls.discard(r)
                    for a in add_labels:
                        lbls.add(a)
                    l_ids = sorted([str(self._label_id_cache[l]) for l in lbls if l in self._label_id_cache])
                    updates.append((",".join(l_ids), now_ts, mid))

                conn.executemany(
                    "UPDATE emails SET label_ids = ?, updated_at = ? WHERE msg_id = ?",
                    updates,
                )
                conn.commit()
            finally:
                conn.close()

    # -------------------------------------------------------------------------
    # Automatisch OAuth Credential Extraction & Token Management
    # -------------------------------------------------------------------------
    def load_credentials_from_automatisch(self, force_db: bool = False) -> Dict[str, str]:
        # 1. Environment variables (standalone / headless / CI)
        env_cid = os.environ.get("GMAIL_CLIENT_ID")
        env_secret = os.environ.get("GMAIL_CLIENT_SECRET")
        env_token = os.environ.get("GMAIL_REFRESH_TOKEN")
        if env_cid and env_secret and env_token:
            self.creds = {
                "clientId": env_cid,
                "clientSecret": env_secret,
                "refreshToken": env_token,
                "accessToken": "",
                "scope": os.environ.get("GMAIL_SCOPE", ""),
                "screenName": os.environ.get("GMAIL_SCREEN_NAME", ""),
            }
            return self.creds

        # 2. Local token cache (~/.config/automelinium/gmail_credentials.json)
        if not force_db and os.path.exists(TOKEN_CACHE_PATH):
            try:
                with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if cached.get("refreshToken") and cached.get("clientId"):
                    self.creds = cached
                    return self.creds
            except Exception:
                pass

        try:
            where_filter = f"WHERE id = '{AUTOMATISCH_GMAIL_CONN_ID}' OR (key = 'gmail' AND verified = true)" if AUTOMATISCH_GMAIL_CONN_ID else "WHERE key = 'gmail' AND verified = true"
            sql = (
                f"SELECT data FROM connections "
                f"{where_filter} "
                f"ORDER BY updated_at DESC LIMIT 1;"
            )
            pg_res = subprocess.run(
                [
                    "docker", "exec", "automatisch-postgres-1",
                    "psql", "-U", "automatisch_user", "-d", "automatisch",
                    "-t", "-A", "-c", sql
                ],
                capture_output=True, text=True, check=True, timeout=8
            )
            cipher_b64 = pg_res.stdout.strip().splitlines()[0].strip()

            key_res = subprocess.run(
                [
                    "docker", "exec", "automatisch-main-1",
                    "sh", "-c", "grep '^ENCRYPTION_KEY=' /automatisch/storage/.env | cut -d= -f2-"
                ],
                capture_output=True, text=True, check=True, timeout=8
            )
            enc_key = key_res.stdout.strip()

            dec_res = subprocess.run(
                [
                    "openssl", "enc", "-d", "-aes-256-cbc", "-md", "md5",
                    "-base64", "-A", "-pass", f"pass:{enc_key}"
                ],
                input=cipher_b64, capture_output=True, text=True, check=True, timeout=5
            )
            data = json.loads(dec_res.stdout)
            self.creds = {
                "clientId": data.get("clientId", ""),
                "clientSecret": data.get("clientSecret", ""),
                "refreshToken": data.get("refreshToken", ""),
                "accessToken": data.get("accessToken", ""),
                "scope": data.get("scope", ""),
                "screenName": data.get("screenName", ""),
            }
            with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.creds, f, indent=2)
            return self.creds
        except Exception as e:
            if self.creds:
                return self.creds
            raise RuntimeError(
                f"Failed to load Gmail credentials. Either:\n"
                f"  1. Ensure Automatisch containers ('automatisch-postgres-1' & 'automatisch-main-1') are running in Docker.\n"
                f"  2. Or set environment variables: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN.\n"
                f"  3. Or place valid credentials in {TOKEN_CACHE_PATH}.\n"
                f"Original error: {e}"
            )

    def refresh_access_token(self) -> str:
        with self._token_lock:
            if not self.creds:
                self.load_credentials_from_automatisch()

            payload = urllib.parse.urlencode({
                "client_id": self.creds["clientId"],
                "client_secret": self.creds["clientSecret"],
                "refresh_token": self.creds["refreshToken"],
                "grant_type": "refresh_token",
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST"
            )
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=12) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            self.access_token = body["access_token"]
            self.token_expiry = time.time() + int(body.get("expires_in", 3500)) - 60
            if "scope" in body:
                self.creds["scope"] = body["scope"]
            return self.access_token

    def get_valid_token(self) -> str:
        if not self.access_token or time.time() >= self.token_expiry:
            return self.refresh_access_token()
        return self.access_token

    def _api_request(self, method: str, path: str, params: Optional[Dict] = None, body: Optional[Dict] = None) -> Dict:
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path.lstrip('/')}"
        if params:
            query_str = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query_str}"

        data_bytes = json.dumps(body).encode("utf-8") if body is not None else None

        for attempt in range(4):
            token = self.get_valid_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if data_bytes is not None:
                headers["Content-Type"] = "application/json"

            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=15) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="replace")
                if e.code == 401 and attempt == 0:
                    self.refresh_access_token()
                    continue
                if e.code in (429, 500, 502, 503, 504) or (e.code == 403 and "rateLimitExceeded" in err_text):
                    time.sleep((0.4 * (2 ** attempt)) + random.uniform(0.05, 0.25))
                    continue
                raise RuntimeError(f"Gmail API HTTP {e.code}: {err_text}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 3:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Network error contacting Gmail API: {e}")

        raise RuntimeError("Gmail API request failed after retries.")

    def reconnect_via_automatisch_selenium(self, headless: bool = True) -> bool:
        if not AUTOMATISCH_RECONNECT_SCRIPT or not os.path.exists(AUTOMATISCH_RECONNECT_SCRIPT):
            return False
        cmd = ["python3", AUTOMATISCH_RECONNECT_SCRIPT, "--app", "gmail"]
        if headless:
            cmd.append("--headless")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.load_credentials_from_automatisch(force_db=True)
        self.refresh_access_token()
        return res.returncode == 0

    def connect_and_verify(self) -> Tuple[str, str]:
        self.load_credentials_from_automatisch(force_db=True)
        self.refresh_access_token()
        try:
            profile = self._api_request("GET", "profile")
            self.user_email = profile.get("emailAddress", "")
            return (self.user_email, self.creds.get("scope", ""))
        except RuntimeError as err:
            if "403" in str(err) or "insufficientPermissions" in str(err):
                self.reconnect_via_automatisch_selenium(headless=True)
                profile = self._api_request("GET", "profile")
                self.user_email = profile.get("emailAddress", "")
                return (self.user_email, self.creds.get("scope", ""))
            raise

    # -------------------------------------------------------------------------
    # Fetching & Parsing Emails
    # -------------------------------------------------------------------------
    @staticmethod
    def _compute_status(labels: List[str]) -> str:
        if "TRASH" in labels:
            return "Trashed"
        if "INBOX" in labels:
            return "In Inbox"
        return "Archived"

    def _fetch_single_message(self, msg_id: str, cached_entry: Optional[Dict] = None) -> Optional[EmailItem]:
        try:
            if cached_entry is not None:
                labels = cached_entry["labels"]
                status = self._compute_status(labels)
                return EmailItem(
                    msg_id=msg_id,
                    thread_id=cached_entry["thread_id"],
                    subject=cached_entry["subject"],
                    sender=cached_entry["sender"],
                    sender_name=cached_entry["sender_name"],
                    sender_email=cached_entry["sender_email"],
                    sender_domain=cached_entry["sender_domain"],
                    recipient=cached_entry["recipient"],
                    date_str=cached_entry["date_str"],
                    timestamp=cached_entry["timestamp"],
                    snippet=cached_entry["snippet"],
                    body_preview=cached_entry["snippet"],
                    labels=labels,
                    has_unsubscribe=cached_entry["has_unsubscribe"],
                    has_attachment=cached_entry["has_attachment"],
                    is_unread=("UNREAD" in labels),
                    status=status,
                    unsubscribe_header=cached_entry.get("unsubscribe_header", ""),
                    unsubscribe_post=cached_entry.get("unsubscribe_post", ""),
                )

            msg = self._api_request(
                "GET",
                f"messages/{msg_id}",
                params={
                    "format": "metadata",
                    "metadataHeaders": [
                        "From", "To", "Subject", "Date", "List-Unsubscribe",
                        "List-Unsubscribe-Post", "Content-Disposition", "X-Attachment-Id"
                    ]
                }
            )
            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            h_map: Dict[str, str] = {}
            for h in headers:
                name = h.get("name", "").lower()
                if name not in h_map:
                    h_map[name] = h.get("value", "")

            raw_from = h_map.get("from", "Unknown")
            sender_name, sender_email, sender_domain = parse_sender_parts(raw_from)
            subject = h_map.get("subject", "(No Subject)") or "(No Subject)"
            recipient = h_map.get("to", "")
            date_raw = h_map.get("date", "")
            unsub_h = h_map.get("list-unsubscribe", "")
            unsub_p = h_map.get("list-unsubscribe-post", "")
            has_unsub = bool(unsub_h)
            has_attachment = (
                "x-attachment-id" in h_map
                or "attachment" in h_map.get("content-disposition", "").lower()
            )

            internal_date = int(msg.get("internalDate", "0")) // 1000
            if internal_date > 0:
                date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(internal_date))
            else:
                date_str = date_raw[:22]

            labels = msg.get("labelIds", [])
            snippet = msg.get("snippet", "")
            status = self._compute_status(labels)

            return EmailItem(
                msg_id=msg["id"],
                thread_id=msg.get("threadId", ""),
                subject=subject,
                sender=raw_from,
                sender_name=sender_name,
                sender_email=sender_email,
                sender_domain=sender_domain,
                recipient=recipient,
                date_str=date_str,
                timestamp=internal_date,
                snippet=snippet,
                body_preview=snippet,
                labels=labels,
                has_unsubscribe=has_unsub,
                has_attachment=has_attachment,
                is_unread=("UNREAD" in labels),
                status=status,
                unsubscribe_header=unsub_h,
                unsubscribe_post=unsub_p,
            )
        except Exception:
            return None

    def get_email(self, msg_id: str) -> Optional[EmailItem]:
        """Fetch and score a single message by ID, resolving company branding."""
        it = self._fetch_single_message(msg_id)
        if not it:
            return None
        items = score_emails([it])
        if items:
            it = items[0]
            try:
                from company_branding import resolve_company_name
                it.company_name = resolve_company_name(it.sender_domain, it.sender_name)
            except Exception:
                pass
            return it
        return it

    def send_email(self, to_addr: str, subject: str, body: str) -> Dict:
        """Send an RFC 822 email message via Gmail API messages.send."""
        import email.message
        import base64
        msg = email.message.EmailMessage()
        msg["To"] = to_addr
        msg["From"] = self.user_email
        msg["Subject"] = subject
        msg.set_content(body)
        raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return self._api_request("POST", "messages/send", body={"raw": raw_b64})

    def get_unsubscribe_info(self, msg_id: str, item: Optional[EmailItem] = None) -> Optional[object]:
        """Extract and parse List-Unsubscribe information for a given email."""
        from unsubscribe_manager import parse_list_unsubscribe_header, UnsubscribeInfo

        unsub_h = getattr(item, "unsubscribe_header", "") if item else ""
        unsub_p = getattr(item, "unsubscribe_post", "") if item else ""
        comp = getattr(item, "company_name", "") or getattr(item, "sender_domain", "Sender")
        sender = getattr(item, "sender", "Unknown")

        if not unsub_h:
            # Check SQLite cache
            cached = self._get_cached_items([msg_id]).get(msg_id)
            if cached and cached.get("unsubscribe_header"):
                unsub_h = cached["unsubscribe_header"]
                unsub_p = cached.get("unsubscribe_post", "")

        if not unsub_h:
            # Fetch on-demand from Gmail API
            try:
                res = self._api_request(
                    "GET",
                    f"messages/{msg_id}",
                    params={
                        "format": "metadata",
                        "metadataHeaders": ["List-Unsubscribe", "List-Unsubscribe-Post", "From"],
                    },
                )
                headers = {h.get("name", "").lower(): h.get("value", "") for h in res.get("payload", {}).get("headers", [])}
                unsub_h = headers.get("list-unsubscribe", "")
                unsub_p = headers.get("list-unsubscribe-post", "")
                if unsub_h:
                    unsub_blob = zlib.compress(f"{unsub_h}\t{unsub_p}".encode("utf-8"), 9)
                    with self._db_lock:
                        conn = sqlite3.connect(SQLITE_CACHE_PATH)
                        try:
                            conn.execute(
                                "UPDATE emails SET unsub_comp = ? WHERE msg_id = ?",
                                (unsub_blob, msg_id),
                            )
                            conn.commit()
                        finally:
                            conn.close()
            except Exception:
                pass

        if not unsub_h:
            return None

        http_url, mailto_addr, mailto_subj, is_one_click = parse_list_unsubscribe_header(unsub_h, unsub_p)
        return UnsubscribeInfo(
            msg_id=msg_id,
            company_name=comp,
            sender=sender,
            http_url=http_url,
            mailto_address=mailto_addr,
            mailto_subject=mailto_subj,
            is_one_click=is_one_click,
            raw_header=unsub_h,
        )

    def unsubscribe_from_message(self, msg_id: str, item: Optional[EmailItem] = None) -> Tuple[bool, str]:
        """Perform 1-click or direct unsubscription and log the sender as unwanted in Recall Log."""
        from unsubscribe_manager import execute_unsubscribe
        from recall_log import RecallLogManager

        unsub_info = self.get_unsubscribe_info(msg_id, item)
        if not unsub_info:
            comp = getattr(item, "company_name", "") or "This sender"
            return (False, f"{comp} does not supply a valid List-Unsubscribe header in this email.")

        success, msg = execute_unsubscribe(self, unsub_info, self.ssl_ctx)
        if success and item is not None:
            try:
                RecallLogManager().record_decisions([item], decision="unwanted", action_source="direct_gui_unsubscribe")
            except Exception:
                pass
        return (success, msg)

    def fetch_emails(
        self,
        source_scope: str = "inbox",
        target_senders: Optional[List[str]] = None,
        corporate_only: bool = True,
        max_results: int = 150,
        custom_boost_keywords: Optional[List[str]] = None,
        skip_unwanted_recall: bool = True,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> List[EmailItem]:
        query_parts = []
        if source_scope == "inbox":
            query_parts.append("in:inbox")
        elif source_scope == "trash":
            query_parts.append("in:trash")
        elif source_scope in ("inbox_and_trash", "inbox_and_quarantine"):
            query_parts.append("(in:inbox OR in:trash)")

        clean_senders = [s.strip() for s in (target_senders or []) if s.strip()]
        if clean_senders:
            sender_clauses = " OR ".join(f'from:"{s}"' for s in clean_senders)
            query_parts.append(f"({sender_clauses})")

        q_str = " ".join(query_parts).strip()
        if progress_cb:
            progress_cb(f"Searching Gmail ({q_str or 'all messages'})...")

        recall_snap = None
        recall_mgr = None
        if skip_unwanted_recall:
            try:
                from recall_log import RecallLogManager
                recall_mgr = RecallLogManager(self.db_path)
                recall_snap = recall_mgr.get_snapshot()
            except Exception:
                recall_snap = None

        repop_filter = recall_snap.repopulated_msg_ids if (recall_snap and recall_snap.repopulated_msg_ids) else set()
        unwanted_filter = recall_snap.unwanted_msg_ids if (recall_snap and recall_snap.unwanted_msg_ids) else set()

        msg_stubs = []
        page_token = None
        while len(msg_stubs) < max_results:
            batch_limit = min(500, max(50, max_results - len(msg_stubs)))
            params = {"maxResults": batch_limit, "includeSpamTrash": "true"}
            if q_str:
                params["q"] = q_str
            if page_token:
                params["pageToken"] = page_token

            resp = self._api_request("GET", "messages", params=params)
            batch = resp.get("messages", [])
            if not batch:
                break
            # Exclude already re-populated and unwanted emails so they never return upon rescan
            if repop_filter or unwanted_filter:
                batch = [m for m in batch if m.get("id") not in repop_filter and m.get("id") not in unwanted_filter]
            msg_stubs.extend(batch)
            if progress_cb and len(msg_stubs) >= 500:
                progress_cb(f"Indexed {len(msg_stubs)} message IDs from Gmail...")
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        msg_ids = [m["id"] for m in msg_stubs[:max_results]]
        if not msg_ids:
            return []

        cached_map = self._get_cached_items(msg_ids)
        items: List[EmailItem] = []
        uncached_ids: List[str] = []

        for mid in msg_ids:
            c_entry = cached_map.get(mid)
            if c_entry is not None:
                res = self._fetch_single_message(mid, c_entry)
                if res is not None:
                    items.append(res)
            else:
                uncached_ids.append(mid)

        if uncached_ids:
            total_new = len(uncached_ids)
            if progress_cb:
                progress_cb(
                    f"Loaded {len(items)} cached emails; downloading {total_new} new emails from Gmail..."
                )
            newly_downloaded: List[EmailItem] = []
            completed_new = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                futures = {
                    pool.submit(self._fetch_single_message, mid, None): mid
                    for mid in uncached_ids
                }
                for fut in concurrent.futures.as_completed(futures):
                    res = fut.result()
                    completed_new += 1
                    if res is not None:
                        items.append(res)
                        newly_downloaded.append(res)
                    if len(newly_downloaded) >= 200:
                        self._save_cached_items(newly_downloaded)
                        newly_downloaded.clear()
                    if progress_cb and (completed_new % 50 == 0 or completed_new == total_new):
                        progress_cb(
                            f"Downloading new emails: {completed_new}/{total_new} ({len(items)} total ready)..."
                        )

            if newly_downloaded:
                self._save_cached_items(newly_downloaded)
        recall_snap = None
        recall_mgr = None
        if skip_unwanted_recall:
            try:
                from recall_log import RecallLogManager
                recall_mgr = RecallLogManager()
                recall_snap = recall_mgr.get_snapshot()
            except Exception:
                recall_snap = None

        filtered_items: List[EmailItem] = []
        skipped_recall_count = 0
        for item in items:
            item.is_Corporate = is_corporate_email(item, clean_senders if clean_senders else None)
            if corporate_only and not item.is_Corporate:
                continue
            if skip_unwanted_recall and recall_snap and recall_mgr:
                should_skip, _ = recall_mgr.should_skip_item_on_scan(item, recall_snap)
                if should_skip:
                    skipped_recall_count += 1
                    continue
            filtered_items.append(item)

        if skipped_recall_count > 0 and progress_cb:
            progress_cb(f"Recall log filtered out {skipped_recall_count} previously purged or blocked emails.")

        score_emails(filtered_items, user_email=self.user_email, custom_boost_keywords=custom_boost_keywords)
        filtered_items.sort(key=lambda x: (x.combined_score, x.timestamp), reverse=True)
        return filtered_items

    # -------------------------------------------------------------------------
    # Batch Organization, Quarantine, Trash, Re-Populate & Undo
    # -------------------------------------------------------------------------
    def _batch_modify(self, msg_ids: List[str], add_labels: List[str], remove_labels: List[str]):
        if not msg_ids:
            return
        for i in range(0, len(msg_ids), 500):
            chunk = msg_ids[i:i + 500]
            body: Dict[str, List[str]] = {"ids": chunk}
            if add_labels:
                body["addLabelIds"] = add_labels
            if remove_labels:
                body["removeLabelIds"] = remove_labels
            self._api_request("POST", "messages/batchModify", body=body)
        self._update_cached_labels(msg_ids, add_labels, remove_labels)

    def quarantine_emails(self, msg_ids: List[str], record_undo: bool = True) -> int:
        """Move low-value emails out of inbox to Trash without using custom labels."""
        return self.trash_emails(msg_ids, record_undo=record_undo)

    def demote_email_importance_in_gmail(self, msg_ids: List[str]) -> int:
        """
        Influence Gmail's internal ML importance algorithm by removing
        the IMPORTANT and STARRED system labels from the specified messages.
        Does NOT touch or create any custom labels.
        """
        if not msg_ids:
            return 0
        self._batch_modify(msg_ids, add_labels=[], remove_labels=["IMPORTANT", "STARRED"])
        return len(msg_ids)

    def block_sender_with_rule(
        self,
        msg_ids: List[str],
        rule: str = "spam",
        record_undo: bool = True,
    ) -> Dict[str, Any]:
        """
        Apply one of Gmail's 3 main blocking rule sets to a batch of messages:
          1. 'spam': Native Gmail Block. Routes messages to SPAM and removes from INBOX, IMPORTANT, STARRED.
          2. 'trash': Direct to Trash. Moves messages to TRASH and removes from INBOX, IMPORTANT, STARRED.
          3. 'archive': Skip Inbox & Mute. Removes from INBOX, UNREAD, IMPORTANT, STARRED.
        Zero custom labels are used.
        """
        rule_key = rule.strip().lower()
        if rule_key not in ("spam", "trash", "archive"):
            rule_key = "spam"

        if not msg_ids:
            return {"count": 0, "rule": rule_key, "status": "No messages to block"}

        if rule_key == "spam":
            add_labels = ["SPAM"]
            remove_labels = ["INBOX", "IMPORTANT", "STARRED"]
            undo_ops = [(msg_ids, ["INBOX"], ["SPAM"])]
            target_status = "Spam"
        elif rule_key == "trash":
            add_labels = ["TRASH"]
            remove_labels = ["INBOX", "IMPORTANT", "STARRED"]
            undo_ops = [(msg_ids, ["INBOX"], ["TRASH"])]
            target_status = "Trashed"
        else:  # archive
            add_labels = []
            remove_labels = ["INBOX", "UNREAD", "IMPORTANT", "STARRED"]
            undo_ops = [(msg_ids, ["INBOX"], [])]
            target_status = "Archived"

        if record_undo:
            self.undo_stack.append({
                "description": f"Gmail Block ({rule_key.upper()}): {len(msg_ids)} messages",
                "ops": undo_ops,
                "target_status": "In Inbox",
            })

        self._batch_modify(msg_ids, add_labels=add_labels, remove_labels=remove_labels)

        return {
            "count": len(msg_ids),
            "rule": rule_key,
            "target_status": target_status,
            "add_labels": add_labels,
            "remove_labels": remove_labels,
            "status": "success",
        }

    def generate_gmail_filter_xml(
        self,
        sender_or_domain: str,
        rule: str = "spam",
        output_dir: Optional[str] = None,
    ) -> str:
        """
        Generate a standard Gmail Filter Atom XML file for the chosen rule set.
        Can be directly imported into Gmail: Settings -> Filters and Blocked Addresses -> Import filters.
        """
        clean_target = sender_or_domain.strip()
        rule_key = rule.strip().lower()
        out_dir = output_dir or os.path.join(CONFIG_DIR, "gmail_filters")
        os.makedirs(out_dir, exist_ok=True)

        iso_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        unix_ts = int(time.time())

        props = [f"<apps:property name='from' value='{clean_target}'/>"]
        if rule_key == "spam":
            props.append("<apps:property name='shouldSpam' value='true'/>")
            props.append("<apps:property name='shouldArchive' value='true'/>")
            props.append("<apps:property name='shouldNeverMarkAsImportant' value='true'/>")
        elif rule_key == "trash":
            props.append("<apps:property name='shouldTrash' value='true'/>")
            props.append("<apps:property name='shouldArchive' value='true'/>")
            props.append("<apps:property name='shouldNeverMarkAsImportant' value='true'/>")
        else:  # archive
            props.append("<apps:property name='shouldArchive' value='true'/>")
            props.append("<apps:property name='shouldMarkAsRead' value='true'/>")
            props.append("<apps:property name='shouldNeverMarkAsImportant' value='true'/>")

        props_str = "\n    ".join(props)
        xml_content = f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>
  <title>Mail Filters</title>
  <id>tag:mail.google.com,2008:filters:{unix_ts}</id>
  <updated>{iso_time}</updated>
  <entry>
    <category term='filter'></category>
    <title>Mail Filter</title>
    {props_str}
  </entry>
</feed>
"""
        safe_name = re.sub(r"[^a-zA-Z0-9_\.-]", "_", clean_target)
        file_path = os.path.join(out_dir, f"gmail_block_{safe_name}_{rule_key}.xml")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(xml_content)
        return file_path

    def trash_emails(self, msg_ids: List[str], record_undo: bool = True) -> int:
        if not msg_ids:
            return 0
        if record_undo:
            self.undo_stack.append({
                "description": f"Trash {len(msg_ids)} emails",
                "ops": [(msg_ids, ["INBOX"], ["TRASH"])],
                "target_status": "In Inbox",
            })
        self._batch_modify(msg_ids, add_labels=["TRASH"], remove_labels=["INBOX"])
        return len(msg_ids)

    def repopulate_emails(
        self,
        items: List[Any],
        metric_mode: str = "combined",
        mark_priority_above: int = 68,
        star_above: int = 82,
        record_undo: bool = True,
    ) -> int:
        """
        Restore ('re-populate') emails back to the INBOX, removing them from Trash if previously trashed.
        Does NOT touch or create any custom Gmail labels.
        Records re-populated message IDs in Recall Memory so they never return upon future rescans.
        """
        if not items:
            return 0

        all_ids = [getattr(it, "msg_id", str(it)) for it in items]
        was_trashed_ids = [
            getattr(it, "msg_id", str(it))
            for it in items
            if getattr(it, "status", "") == "Trashed"
        ]

        if record_undo and was_trashed_ids:
            self.undo_stack.append({
                "description": f"Re-populate {len(all_ids)} emails to Inbox",
                "ops": [(was_trashed_ids, ["TRASH"], ["INBOX"])],
                "target_status": "Trashed",
            })

        # Restore all target items to INBOX and remove from TRASH without custom labels
        self._batch_modify(all_ids, add_labels=["INBOX"], remove_labels=["TRASH"])

        # Automatically record in recall log as permanently re-populated so they never return on rescan
        try:
            from recall_log import RecallLogManager
            rlog = RecallLogManager(self.db_path)
            items_to_record = []
            for it in items:
                if hasattr(it, "msg_id"):
                    items_to_record.append(it)
                else:
                    item_obj = self.get_email(str(it))
                    if item_obj:
                        items_to_record.append(item_obj)
            if items_to_record:
                rlog.record_repopulated(items_to_record, action_source="repopulate_emails")
        except Exception:
            pass

        return len(all_ids)

    def clean_and_repopulate_by_threshold(
        self,
        keep_items: List[EmailItem],
        quarantine_items: List[EmailItem],
        metric_mode: str = "combined",
    ) -> Tuple[int, int]:
        """
        Single master operation:
          1. Trashes all emails below the cutoff out of INBOX
          2. Re-populates all emails >= the cutoff back into INBOX
          (Zero custom labels accessed or created)
        """
        t_ids = [it.msg_id for it in quarantine_items if it.status == "In Inbox"]
        k_ids = [it.msg_id for it in keep_items]

        undo_ops = []
        if t_ids:
            undo_ops.append((t_ids, ["INBOX"], ["TRASH"]))
        restored_from_trash = [it.msg_id for it in keep_items if it.status == "Trashed"]
        if restored_from_trash:
            undo_ops.append((restored_from_trash, ["TRASH"], ["INBOX"]))
        if undo_ops:
            self.undo_stack.append({
                "description": f"Clean & Re-populate ({len(t_ids)} trashed, {len(k_ids)} kept)",
                "ops": undo_ops,
                "target_status": "In Inbox",
            })

        if t_ids:
            self.trash_emails(t_ids, record_undo=False)
        if keep_items:
            self.repopulate_emails(keep_items, metric_mode=metric_mode, record_undo=False)

        return (len(t_ids), len(k_ids))

    def undo_last_action(self) -> Optional[Tuple[str, List[str], str]]:
        """Revert the most recent batch modify action. Returns (description, affected_ids, target_status)."""
        if not self.undo_stack:
            return None
        entry = self.undo_stack.pop()
        affected: List[str] = []
        for msg_ids, add_lbls, rem_lbls in entry["ops"]:
            self._batch_modify(msg_ids, add_labels=add_lbls, remove_labels=rem_lbls)
            affected.extend(msg_ids)
        return (entry["description"], affected, entry.get("target_status", "In Inbox"))

    # -------------------------------------------------------------------------
    # Saved Sender Watchlist Persistence
    # -------------------------------------------------------------------------
    def load_saved_target_senders(self) -> List[str]:
        if os.path.exists(WATCHLIST_PATH):
            try:
                with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data if str(x).strip()]
            except Exception:
                pass
        return []

    def save_target_senders(self, senders: List[str]):
        clean = [s.strip() for s in senders if s.strip()]
        with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
