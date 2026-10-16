"""
Persistent Recall & Preference Learning Log for Automelinium Aero.

Maintains a durable SQLite + JSONL audit log of:
  1. Emails & subject patterns the user NEEDS (re-populated to Inbox, manually moved to Green Bucket, or Starred).
  2. Emails, subject patterns, and companies the user DOES NOT NEED (deleted to Trash, manually moved to Red Bucket,
     or purged via 'Auto-Delete ALL From This Company').

Used in two places:
  - Scan-Time Recall Filter (`gmail_client.py`): Automatically skips/excludes previously deleted message IDs,
    blocked companies, and recurring unwanted subject patterns during a scan so clutter never re-appears.
  - Heuristic Scoring Recall (`heuristics.py`): Boosts new emails matching logged 'needed' patterns (+28 to +35)
    and penalizes new emails matching logged 'unwanted' patterns (-36 to -45).
"""

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

CONFIG_DIR = os.path.expanduser("~/.config/automelinium")
SQLITE_CACHE_PATH = os.path.join(CONFIG_DIR, "email_cache.db")
AUDIT_JSONL_PATH = os.path.join(CONFIG_DIR, "recall_audit.jsonl")


def extract_subject_pattern(subject: str) -> str:
    """
    Convert a specific email subject into a generalized structural pattern so future emails
    of the same template match the Recall Log even when dates, codes, or numbers change.
    """
    s = (subject or "").strip().lower()
    s = re.sub(r"^(re|fwd|fw):\s*", "", s)
    # Replace dates like Oct 15, 2026 or 04/13/2026 or 2026-10-15
    s = re.sub(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?\b",
        "<date>",
        s,
    )
    s = re.sub(r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b", "<date>", s)
    # Replace dollar amounts and percentages
    s = re.sub(r"\$\d+(?:,\d{3})*(?:\.\d+)?", "$<amt>", s)
    s = re.sub(r"\b\d+(?:\.\d+)?%", "<pct>%", s)
    # Replace standalone numbers / IDs / order numbers
    s = re.sub(r"#\d+", "#<id>", s)
    s = re.sub(r"\b\d{2,}\b", "<num>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:140]


@dataclass
class RecallSnapshot:
    needed_msg_ids: Set[str]
    unwanted_msg_ids: Set[str]
    demoted_msg_ids: Set[str]
    repopulated_msg_ids: Set[str]  # message IDs that have been re-populated to Gmail Inbox
    blocked_companies: Set[str]
    demoted_companies: Set[str]
    block_rules: Dict[str, str]  # target -> rule_type ('spam', 'trash', 'archive')
    # (company_name, subject_pattern) -> (needed_count, unwanted_count, sample_subject)
    pattern_stats: Dict[Tuple[str, str], Tuple[int, int, str]]
    # company_name -> (needed_count, unwanted_count, skip_on_scan, lessen_importance)
    company_stats: Dict[str, Tuple[int, int, bool, bool]]


class RecallLogManager:
    def __init__(self, db_path: str = SQLITE_CACHE_PATH):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_email_log (
                        msg_id TEXT PRIMARY KEY,
                        company_name TEXT,
                        sender_email TEXT,
                        sender_domain TEXT,
                        subject TEXT,
                        subject_pattern TEXT,
                        decision TEXT,
                        action_source TEXT,
                        updated_at INTEGER
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_decision ON recall_email_log(decision)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_company ON recall_email_log(company_name)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_repopulated_emails (
                        msg_id TEXT PRIMARY KEY,
                        company_name TEXT,
                        subject TEXT,
                        repopulated_at INTEGER
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_repop_at ON recall_repopulated_emails(repopulated_at)")
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO recall_repopulated_emails (msg_id, company_name, subject, repopulated_at)
                        SELECT msg_id, company_name, subject, updated_at
                        FROM recall_email_log
                        WHERE decision = 'needed' AND action_source NOT IN ('gmail_starred_seed')
                        """
                    )
                except Exception:
                    pass
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_pattern_memory (
                        company_name TEXT,
                        subject_pattern TEXT,
                        sample_subject TEXT,
                        needed_count INTEGER DEFAULT 0,
                        unwanted_count INTEGER DEFAULT 0,
                        updated_at INTEGER,
                        PRIMARY KEY (company_name, subject_pattern)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_company_memory (
                        company_name TEXT PRIMARY KEY,
                        needed_count INTEGER DEFAULT 0,
                        unwanted_count INTEGER DEFAULT 0,
                        skip_on_scan INTEGER DEFAULT 0,
                        lessen_importance INTEGER DEFAULT 0,
                        updated_at INTEGER
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recall_block_rules (
                        target TEXT PRIMARY KEY,
                        rule_type TEXT,
                        target_type TEXT,
                        action_count INTEGER DEFAULT 0,
                        created_at INTEGER,
                        updated_at INTEGER
                    )
                    """
                )
                try:
                    conn.execute("ALTER TABLE recall_company_memory ADD COLUMN lessen_importance INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass
                conn.commit()
            finally:
                conn.close()

    def _append_audit_jsonl(self, event_type: str, decision: str, count: int, companies: List[str], sample_subjects: List[str]):
        try:
            entry = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "event_type": event_type,
                "decision": decision,
                "email_count": count,
                "companies": companies[:10],
                "sample_subjects": sample_subjects[:5],
            }
            with open(AUDIT_JSONL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except Exception:
            pass

    def record_decisions(
        self,
        items: list,
        decision: str,
        action_source: str,
        mark_company_skip_on_scan: bool = False,
    ) -> int:
        """
        Log a batch of EmailItem objects as either:
          - decision='needed'   (user wants to find/keep these emails)
          - decision='unwanted' (user does NOT want to find these emails)
        Rebuilds pattern and company aggregates atomically.
        """
        if not items or decision not in ("needed", "unwanted"):
            return 0

        now_ts = int(time.time())
        rows = []
        companies_seen: Set[str] = set()
        sample_subjects: List[str] = []

        for it in items:
            comp = getattr(it, "company_name", "") or getattr(it, "sender_domain", "") or "Unknown"
            subj = getattr(it, "subject", "") or ""
            pat = extract_subject_pattern(subj)
            companies_seen.add(comp)
            if len(sample_subjects) < 5 and subj:
                sample_subjects.append(subj[:80])
            rows.append(
                (
                    it.msg_id,
                    comp,
                    getattr(it, "sender_email", ""),
                    getattr(it, "sender_domain", ""),
                    subj,
                    pat,
                    decision,
                    action_source,
                    now_ts,
                )
            )

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO recall_email_log (
                        msg_id, company_name, sender_email, sender_domain,
                        subject, subject_pattern, decision, action_source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                self._rebuild_aggregates_unlocked(
                    conn,
                    now_ts,
                    force_skip_companies=companies_seen if (mark_company_skip_on_scan and decision == "unwanted") else None,
                    unblock_companies=companies_seen if decision == "needed" else None,
                )
                conn.commit()
            finally:
                conn.close()

    def record_repopulated(self, items: list, action_source: str = "repopulated") -> int:
        """
        Record message IDs as permanently re-populated into the user's Gmail Inbox
        so they are dismissed from the service and never return upon future rescans.
        Also learns the subject patterns and company preferences as 'needed'.
        """
        if not items:
            return 0
        now_ts = int(time.time())
        rows = []
        for it in items:
            comp = getattr(it, "company_name", "") or getattr(it, "sender_domain", "") or "Unknown"
            subj = getattr(it, "subject", "") or ""
            mid = getattr(it, "msg_id", str(it))
            rows.append((mid, comp, subj, now_ts))

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO recall_repopulated_emails (msg_id, company_name, subject, repopulated_at) VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

        # Learn positive pattern weights in recall memory log
        self.record_decisions(items, decision="needed", action_source=action_source)
        return len(rows)

    def remove_decisions_for_msg_ids(self, msg_ids: List[str]):
        """Remove logged decisions when the user clicks Undo."""
        if not msg_ids:
            return
        now_ts = int(time.time())
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                for i in range(0, len(msg_ids), 400):
                    chunk = msg_ids[i:i + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(f"DELETE FROM recall_email_log WHERE msg_id IN ({placeholders})", chunk)
                    try:
                        conn.execute(f"DELETE FROM recall_repopulated_emails WHERE msg_id IN ({placeholders})", chunk)
                    except Exception:
                        pass
                self._rebuild_aggregates_unlocked(conn, now_ts)
                conn.commit()
            finally:
                conn.close()

    def set_company_skip_on_scan(self, company_name: str, skip: bool):
        """Explicitly set whether a company should be skipped on future scans."""
        now_ts = int(time.time())
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.execute(
                    "SELECT needed_count, unwanted_count FROM recall_company_memory WHERE company_name = ?",
                    (company_name,),
                ).fetchone()
                needed_c = cur[0] if cur else 0
                unwanted_c = cur[1] if cur else 0
                conn.execute(
                    """
                    INSERT OR REPLACE INTO recall_company_memory (
                        company_name, needed_count, unwanted_count, skip_on_scan, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (company_name, needed_c, unwanted_c, 1 if skip else 0, now_ts),
                )
                conn.commit()
            finally:
                conn.close()

    def set_company_lessen_importance(self, company_name: str, lessen: bool = True):
        """
        Influence algorithms to lessen or restore importance for all emails from a company.
        When lessen=True, Automelinium applies an explicit -40 Importance algorithm penalty.
        """
        now_ts = int(time.time())
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.execute(
                    "SELECT needed_count, unwanted_count, skip_on_scan FROM recall_company_memory WHERE company_name = ?",
                    (company_name,),
                ).fetchone()
                needed_c = cur[0] if cur else 0
                unwanted_c = cur[1] if cur else 0
                skip_flag = cur[2] if cur else 0
                conn.execute(
                    """
                    INSERT INTO recall_company_memory (
                        company_name, needed_count, unwanted_count, skip_on_scan, lessen_importance, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(company_name) DO UPDATE SET
                        lessen_importance = excluded.lessen_importance,
                        updated_at = excluded.updated_at
                    """,
                    (company_name, needed_c, unwanted_c, skip_flag, 1 if lessen else 0, now_ts),
                )
                conn.commit()
            finally:
                conn.close()

        self._append_audit_jsonl(
            event_type="company_lessen_importance" if lessen else "company_restore_importance",
            decision="demoted" if lessen else "normal",
            count=1,
            companies=[company_name],
            sample_subjects=[],
        )

    def is_company_importance_lessened(self, company_name: str) -> bool:
        """Check whether algorithms are set to lessen importance for this company."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT COALESCE(lessen_importance, 0) FROM recall_company_memory WHERE company_name = ?",
                    (company_name,),
                ).fetchone()
                return bool(row and row[0])
            finally:
                conn.close()

    def demote_message_importance(self, item, lessen: bool = True):
        """
        Mark or unmark an individual email message ID as demoted in the recall log.
        """
        now_ts = int(time.time())
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                if lessen:
                    pat = extract_subject_pattern(getattr(item, "subject", ""))
                    conn.execute(
                        """
                        INSERT INTO recall_email_log (
                            msg_id, company_name, sender_email, sender_domain, subject,
                            subject_pattern, decision, action_source, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'demoted', 'manual_demote', ?)
                        ON CONFLICT(msg_id) DO UPDATE SET
                            decision = 'demoted',
                            action_source = 'manual_demote',
                            updated_at = excluded.updated_at
                        """,
                        (item.msg_id, getattr(item, "company_name", ""), getattr(item, "sender_email", ""),
                         getattr(item, "sender_domain", ""), getattr(item, "subject", ""), pat, now_ts),
                    )
                else:
                    conn.execute("DELETE FROM recall_email_log WHERE msg_id = ? AND decision = 'demoted'", (item.msg_id,))
                conn.commit()
            finally:
                conn.close()

    def add_block_rule(self, target: str, rule_type: str, target_type: str = "company") -> None:
        """
        Record a persistent Gmail blocking rule ('spam', 'trash', or 'archive')
        for a company, domain, or email address.
        """
        now_ts = int(time.time())
        clean_target = (target or "").strip()
        clean_rule = rule_type.strip().lower()
        if clean_rule not in ("spam", "trash", "archive"):
            clean_rule = "spam"

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO recall_block_rules (target, rule_type, target_type, action_count, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(target) DO UPDATE SET
                        rule_type = excluded.rule_type,
                        target_type = excluded.target_type,
                        action_count = action_count + 1,
                        updated_at = excluded.updated_at
                    """,
                    (clean_target, clean_rule, target_type, now_ts, now_ts),
                )
                conn.commit()
            finally:
                conn.close()

        self._append_audit_jsonl(
            event_type="gmail_block_rule_added",
            decision=clean_rule,
            count=1,
            companies=[clean_target],
            sample_subjects=[],
        )

    def remove_block_rule(self, target: str) -> None:
        """Remove a blocking rule for a target."""
        clean_target = (target or "").strip()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("DELETE FROM recall_block_rules WHERE target = ?", (clean_target,))
                conn.commit()
            finally:
                conn.close()

    def get_block_rules(self) -> Dict[str, str]:
        """Return dict of {target: rule_type} for all active Gmail block rules."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                rows = conn.execute("SELECT target, rule_type FROM recall_block_rules").fetchall()
                return {r[0]: r[1] for r in rows}
            finally:
                conn.close()

    def get_rule_for_target(self, target: str) -> Optional[str]:
        """Get the active block rule type for a target if one exists."""
        clean_target = (target or "").strip()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute("SELECT rule_type FROM recall_block_rules WHERE target = ?", (clean_target,)).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    def delete_pattern_rule(self, company_name: str, subject_pattern: str):
        """Remove a specific pattern rule and its underlying email logs."""
        now_ts = int(time.time())
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "DELETE FROM recall_email_log WHERE company_name = ? AND subject_pattern = ?",
                    (company_name, subject_pattern),
                )
                self._rebuild_aggregates_unlocked(conn, now_ts)
                conn.commit()
            finally:
                conn.close()

    def clear_all_logs(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("DELETE FROM recall_email_log")
                conn.execute("DELETE FROM recall_pattern_memory")
                conn.execute("DELETE FROM recall_company_memory")
                conn.execute("DELETE FROM recall_block_rules")
                conn.commit()
            finally:
                conn.close()

    def _rebuild_aggregates_unlocked(
        self,
        conn: sqlite3.Connection,
        now_ts: int,
        force_skip_companies: Optional[Set[str]] = None,
        unblock_companies: Optional[Set[str]] = None,
    ):
        existing_skips: Dict[str, int] = {}
        for comp, sk in conn.execute("SELECT company_name, skip_on_scan FROM recall_company_memory").fetchall():
            existing_skips[comp] = int(sk)

        conn.execute("DELETE FROM recall_pattern_memory")
        conn.execute("DELETE FROM recall_company_memory")

        pat_rows = conn.execute(
            """
            SELECT company_name,
                   subject_pattern,
                   MAX(subject) AS sample_subject,
                   SUM(CASE WHEN decision = 'needed' THEN 1 ELSE 0 END) AS needed_cnt,
                   SUM(CASE WHEN decision = 'unwanted' THEN 1 ELSE 0 END) AS unwanted_cnt
            FROM recall_email_log
            GROUP BY company_name, subject_pattern
            """
        ).fetchall()

        conn.executemany(
            """
            INSERT INTO recall_pattern_memory (
                company_name, subject_pattern, sample_subject, needed_count, unwanted_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(r[0], r[1], r[2], r[3], r[4], now_ts) for r in pat_rows],
        )

        comp_rows = conn.execute(
            """
            SELECT company_name,
                   SUM(CASE WHEN decision = 'needed' THEN 1 ELSE 0 END) AS needed_cnt,
                   SUM(CASE WHEN decision = 'unwanted' THEN 1 ELSE 0 END) AS unwanted_cnt
            FROM recall_email_log
            GROUP BY company_name
            """
        ).fetchall()

        comp_inserts = []
        for comp, needed_c, unwanted_c in comp_rows:
            skip_val = existing_skips.get(comp, 0)
            if force_skip_companies and comp in force_skip_companies and needed_c == 0:
                skip_val = 1
            if unblock_companies and comp in unblock_companies:
                skip_val = 0
            comp_inserts.append((comp, needed_c, unwanted_c, skip_val, now_ts))

        conn.executemany(
            """
            INSERT INTO recall_company_memory (
                company_name, needed_count, unwanted_count, skip_on_scan, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            comp_inserts,
        )

    def seed_from_gmail_signals_if_empty(self, items: list):
        """
        Seed the Recall Log automatically from clear Gmail signals (Starred = needed,
        Trashed / Quarantined = unwanted) for any message IDs not yet logged.
        """
        if not items:
            return
        snap = self.get_snapshot()
        already_logged = snap.needed_msg_ids | snap.unwanted_msg_ids

        to_seed_needed = []
        to_seed_unwanted = []
        for it in items:
            if it.msg_id in already_logged:
                continue
            if "STARRED" in getattr(it, "labels", []):
                to_seed_needed.append(it)
            elif getattr(it, "status", "") in ("Trashed", "Quarantined"):
                to_seed_unwanted.append(it)

        if to_seed_needed:
            self.record_decisions(to_seed_needed, decision="needed", action_source="gmail_starred_seed")
        if to_seed_unwanted:
            self.record_decisions(to_seed_unwanted, decision="unwanted", action_source="gmail_trashed_seed")

    def get_snapshot(self) -> RecallSnapshot:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                needed_ids: Set[str] = set()
                unwanted_ids: Set[str] = set()
                demoted_ids: Set[str] = set()
                for mid, dec in conn.execute("SELECT msg_id, decision FROM recall_email_log").fetchall():
                    if dec == "needed":
                        needed_ids.add(mid)
                    elif dec == "unwanted":
                        unwanted_ids.add(mid)
                    elif dec == "demoted":
                        demoted_ids.add(mid)

                repop_ids: Set[str] = set()
                try:
                    for (rmid,) in conn.execute("SELECT msg_id FROM recall_repopulated_emails").fetchall():
                        repop_ids.add(rmid)
                except Exception:
                    pass

                pat_stats: Dict[Tuple[str, str], Tuple[int, int, str]] = {}
                for comp, pat, sample, n_cnt, u_cnt in conn.execute(
                    "SELECT company_name, subject_pattern, sample_subject, needed_count, unwanted_count FROM recall_pattern_memory"
                ).fetchall():
                    pat_stats[(comp, pat)] = (int(n_cnt), int(u_cnt), sample or pat)

                comp_stats: Dict[str, Tuple[int, int, bool, bool]] = {}
                blocked_comps: Set[str] = set()
                demoted_comps: Set[str] = set()
                for comp, n_cnt, u_cnt, skip_flag, lessen_flag in conn.execute(
                    "SELECT company_name, needed_count, unwanted_count, skip_on_scan, COALESCE(lessen_importance, 0) FROM recall_company_memory"
                ).fetchall():
                    is_skip = bool(skip_flag)
                    is_lessen = bool(lessen_flag)
                    comp_stats[comp] = (int(n_cnt), int(u_cnt), is_skip, is_lessen)
                    if is_skip:
                        blocked_comps.add(comp)
                    if is_lessen:
                        demoted_comps.add(comp)

                block_rules: Dict[str, str] = {}
                for target, r_type in conn.execute("SELECT target, rule_type FROM recall_block_rules").fetchall():
                    block_rules[target] = r_type

                return RecallSnapshot(
                    needed_msg_ids=needed_ids,
                    unwanted_msg_ids=unwanted_ids,
                    demoted_msg_ids=demoted_ids,
                    repopulated_msg_ids=repop_ids,
                    blocked_companies=blocked_comps,
                    demoted_companies=demoted_comps,
                    block_rules=block_rules,
                    pattern_stats=pat_stats,
                    company_stats=comp_stats,
                )
            finally:
                conn.close()

    def should_skip_item_on_scan(self, item, snap: RecallSnapshot) -> Tuple[bool, str]:
        """
        Determine if an EmailItem should be skipped during a scan based on the Recall Log:
          - Skip if its exact msg_id was already re-populated to Gmail Inbox (never return upon rescan).
          - Never skip an email matching an active 'needed' pattern.
          - Skip if its exact msg_id was logged as 'unwanted'.
          - Skip if its company, domain, or email is blocked via a Gmail Block Rule.
          - Skip if its company is marked skip_on_scan=1 (unless it matches a needed pattern).
          - Skip if its (company_name, subject_pattern) has >= 2 unwanted logs and 0 needed logs.
        """
        # 1. Skip if already re-populated to Gmail Inbox (never return upon rescan)
        if item.msg_id in getattr(snap, "repopulated_msg_ids", set()):
            return (True, "Already re-populated to Gmail Inbox")

        if item.msg_id in snap.needed_msg_ids:
            return (False, "")

        comp = getattr(item, "company_name", "") or ""
        s_dom = getattr(item, "sender_domain", "") or ""
        s_email = getattr(item, "sender_email", "") or ""

        # Check Gmail Block Rules
        if comp in snap.block_rules:
            return (True, f"Blocked in Gmail via Rule Set: {snap.block_rules[comp].upper()}")
        if s_dom in snap.block_rules:
            return (True, f"Blocked in Gmail via Rule Set: {snap.block_rules[s_dom].upper()}")
        if s_email in snap.block_rules:
            return (True, f"Blocked in Gmail via Rule Set: {snap.block_rules[s_email].upper()}")

        pat = extract_subject_pattern(getattr(item, "subject", ""))
        p_stat = snap.pattern_stats.get((comp, pat))
        if p_stat and p_stat[0] > 0:
            # Matches a pattern the user needed in the past
            return (False, "")

        if item.msg_id in snap.unwanted_msg_ids:
            return (True, "Previously deleted/marked unwanted in Recall Log")

        # Protect Fiscal Transactions: Never skip genuine receipts/invoices on company or pattern recall
        try:
            from heuristics import is_fiscal_transaction
            is_fiscal, _, _ = is_fiscal_transaction(item)
            if is_fiscal:
                return (False, "")
        except Exception:
            pass

        if comp in snap.blocked_companies:
            return (True, f"Company '{comp}' is set to Skip on Scan in Recall Log")

        if p_stat and p_stat[1] >= 2 and p_stat[0] == 0:
            return (True, f"Subject pattern '{pat[:45]}' recalled as unwanted ({p_stat[1]}x)")

        return (False, "")

    def get_summary_counts(self) -> Dict[str, int]:
        snap = self.get_snapshot()
        needed_patterns = sum(1 for (n, u, _) in snap.pattern_stats.values() if n > u)
        unwanted_patterns = sum(1 for (n, u, _) in snap.pattern_stats.values() if u > n)
        return {
            "needed_emails": len(snap.needed_msg_ids),
            "unwanted_emails": len(snap.unwanted_msg_ids),
            "demoted_emails": len(snap.demoted_msg_ids),
            "repopulated_emails": len(getattr(snap, "repopulated_msg_ids", set())),
            "needed_patterns": needed_patterns,
            "unwanted_patterns": unwanted_patterns,
            "blocked_companies": len(snap.blocked_companies),
            "lessened_importance_companies": len(snap.demoted_companies),
            "active_block_rules": len(snap.block_rules),
        }
