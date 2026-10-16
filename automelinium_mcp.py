#!/usr/bin/env python3
"""
Automelinium Model Context Protocol (MCP) Server.

Exposes Automelinium's corporate email intelligence, scoring engine,
batch trashing, immediate re-population, automated unsubscribing, and
backend recall memory log as standard MCP tools, resources, and prompts.

Zero custom Gmail labels are accessed or created; all operations use native
Gmail system folders (INBOX and TRASH). Zero emojis in all outputs and logs.
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

# Import core Automelinium modules
from company_branding import resolve_company_identity
from gmail_client import AutomatischGmailClient
from heuristics import EmailItem, score_emails
from recall_log import RecallLogManager
from unsubscribe_manager import UnsubscribeInfo, execute_unsubscribe as do_unsubscribe, parse_list_unsubscribe_header

# Initialize MCP Server instance
app = MCPServer(
    name="automelinium",
    title="Automelinium Corporate Email Intelligence",
    description="Corporate email organization, importance/usefulness scoring, batch trashing, and smart re-populator.",
    version="1.0.0",
)

# Shared client cache for MCP server session
_CLIENT: Optional[AutomatischGmailClient] = None
_RECALL_LOG: Optional[RecallLogManager] = None


def get_client() -> AutomatischGmailClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = AutomatischGmailClient()
    return _CLIENT


def get_recall_log() -> RecallLogManager:
    global _RECALL_LOG
    if _RECALL_LOG is None:
        _RECALL_LOG = RecallLogManager()
    return _RECALL_LOG


# ============================================================================
# MCP Tools
# ============================================================================


@app.tool()
def check_connection() -> Dict[str, Any]:
    """Verify connection to the Automatisch Gmail integration and active OAuth scopes.

    Returns user email, granted scopes, and local SQLite cache path.
    """
    client = get_client()
    email_addr, scope = client.connect_and_verify()
    rlog = get_recall_log()
    counts = rlog.get_summary_counts()
    return {
        "connected": True,
        "user_email": email_addr,
        "active_scopes": scope,
        "cache_db": client.db_path,
        "recall_memory_stats": counts,
        "labeling_capabilities": "Disabled (Native INBOX and TRASH system folders only)",
    }


@app.tool()
def scan_corporate_emails(
    max_results: int = 100,
    skip_unwanted_recall: bool = True,
) -> List[Dict[str, Any]]:
    """Scan and score corporate emails from the Gmail inbox.

    Filters out personal conversations, extracts company identities, and
    calculates Level of Importance (0-100) and Level of Usefulness (0-100).

    Args:
        max_results: Maximum number of emails to scan (1 to 10000).
        skip_unwanted_recall: If True, automatically filters out companies and
          subject patterns previously logged as unwanted.
    """
    client = get_client()
    capped_results = max(1, min(10000, max_results))
    items = client.fetch_emails(
        source_scope="inbox",
        corporate_only=True,
        max_results=capped_results,
        skip_unwanted_recall=skip_unwanted_recall,
    )

    results = []
    for it in items:
        results.append({
            "msg_id": it.msg_id,
            "thread_id": it.thread_id,
            "company_name": it.company_name or it.sender_domain,
            "sender_domain": it.sender_domain,
            "sender": it.sender,
            "subject": it.subject,
            "date": it.date_str,
            "importance_score": it.importance_score,
            "usefulness_score": it.usefulness_score,
            "combined_score": it.combined_score,
            "category": it.category,
            "status": it.status,
            "has_unsubscribe": it.has_unsubscribe,
            "has_attachment": it.has_attachment,
            "snippet": it.snippet,
        })
    return results


@app.tool()
def list_companies(max_results: int = 150) -> List[Dict[str, Any]]:
    """Aggregate recent inbox corporate emails by company.

    Returns company display names, root domains, total email count,
    average importance and usefulness scores, and sample subjects.

    Args:
        max_results: Number of inbox emails to scan and aggregate.
    """
    client = get_client()
    capped_results = max(1, min(10000, max_results))
    items = client.fetch_emails(
        source_scope="inbox",
        corporate_only=True,
        max_results=capped_results,
        skip_unwanted_recall=False,
    )

    groups: Dict[str, List[EmailItem]] = {}
    for it in items:
        dom = it.sender_domain or "Unknown"
        groups.setdefault(dom, []).append(it)

    company_list = []
    for dom, g_items in groups.items():
        comp_name, root_dom = resolve_company_identity(
            dom, g_items[0].sender_name, g_items[0].sender_email
        )
        total = len(g_items)
        avg_imp = sum(it.importance_score for it in g_items) // total
        avg_use = sum(it.usefulness_score for it in g_items) // total
        avg_comb = sum(it.combined_score for it in g_items) // total

        status_counts: Dict[str, int] = {}
        for it in g_items:
            status_counts[it.status] = status_counts.get(it.status, 0) + 1

        company_list.append({
            "company_name": comp_name,
            "domain": dom,
            "root_domain": root_dom,
            "email_count": total,
            "avg_importance": avg_imp,
            "avg_usefulness": avg_use,
            "avg_combined": avg_comb,
            "status_distribution": status_counts,
            "has_unsubscribe": any(it.has_unsubscribe for it in g_items),
            "sample_subjects": [it.subject for it in g_items[:3]],
        })

    # Sort companies by email count descending
    company_list.sort(key=lambda c: c["email_count"], reverse=True)
    return company_list


@app.tool()
def get_email_details(msg_id: str) -> Dict[str, Any]:
    """Retrieve full details, scoring breakdown, and unsubscribe info for an email.

    Args:
        msg_id: Gmail message ID.
    """
    client = get_client()
    it = client.get_email(msg_id)
    if not it:
        return {"error": f"Email with ID '{msg_id}' not found."}

    unsub_info = client.get_unsubscribe_info(msg_id, it)
    has_one_click = bool(getattr(unsub_info, "is_one_click", False))
    mailto_addr = getattr(unsub_info, "mailto_address", None)
    http_url = getattr(unsub_info, "http_url", None)

    return {
        "msg_id": it.msg_id,
        "thread_id": it.thread_id,
        "company_name": it.company_name or it.sender_domain,
        "sender": it.sender,
        "sender_name": it.sender_name,
        "sender_email": it.sender_email,
        "sender_domain": it.sender_domain,
        "recipient": it.recipient,
        "subject": it.subject,
        "date": it.date_str,
        "status": it.status,
        "is_unread": it.is_unread,
        "importance_score": it.importance_score,
        "importance_breakdown": it.importance_breakdown,
        "usefulness_score": it.usefulness_score,
        "usefulness_breakdown": it.usefulness_breakdown,
        "combined_score": it.combined_score,
        "category": it.category,
        "has_attachment": it.has_attachment,
        "has_unsubscribe": it.has_unsubscribe,
        "unsubscribe_details": {
            "is_one_click": has_one_click,
            "mailto": mailto_addr,
            "url": http_url,
        },
        "snippet": it.snippet,
        "body_preview": it.body_preview,
    }


@app.tool()
def trash_emails(msg_ids: List[str]) -> Dict[str, Any]:
    """Move one or more emails to Gmail Trash without using custom labels.

    Records the action in the backend recall memory log as unwanted.

    Args:
        msg_ids: List of Gmail message IDs to trash.
    """
    if not msg_ids:
        return {"trashed_count": 0, "status": "No message IDs provided"}

    client = get_client()
    count = client.trash_emails(msg_ids, record_undo=True)

    # Record in recall log
    rlog = get_recall_log()
    items_to_log = []
    for mid in msg_ids:
        it = client.get_email(mid)
        if it:
            items_to_log.append(it)
    if items_to_log:
        rlog.record_decisions(
            items=items_to_log,
            decision="unwanted",
            action_name="mcp_trash_emails",
        )

    return {
        "trashed_count": count,
        "message_ids": msg_ids,
        "folder": "TRASH",
        "labels_used": "None (Native system folder)",
        "recall_logged": len(items_to_log),
    }


@app.tool()
def repopulate_emails(msg_ids: List[str]) -> Dict[str, Any]:
    """Restore one or more emails from Gmail Trash back to the Inbox.

    Zero custom labels are attached; emails are restored purely to INBOX.
    Learns positive preference for these messages in the recall log.

    Args:
        msg_ids: List of Gmail message IDs to restore to Inbox.
    """
    if not msg_ids:
        return {"repopulated_count": 0, "status": "No message IDs provided"}

    client = get_client()
    count = client.repopulate_emails(msg_ids, record_undo=True)

    # Record in recall log
    rlog = get_recall_log()
    items_to_log = []
    for mid in msg_ids:
        it = client.get_email(mid)
        if it:
            items_to_log.append(it)
    if items_to_log:
        rlog.record_repopulated(
            items=items_to_log,
            action_source="mcp_repopulate_emails",
        )

    return {
        "repopulated_count": count,
        "message_ids": msg_ids,
        "folder": "INBOX",
        "labels_used": "None (Native system folder)",
        "recall_logged": len(items_to_log),
    }


@app.tool()
def auto_trash_company(
    sender_domain: str,
    max_emails: int = 100,
    preserve_fiscal: bool = True,
) -> Dict[str, Any]:
    """Move emails from a specific company domain to Gmail Trash.

    Marks the company domain to skip on future scans in the recall log.
    By default, genuine fiscal transactions (receipts, invoices, orders) are preserved.

    Args:
        sender_domain: Domain of the company to trash (e.g. 'e.etradefinancial.com').
        max_emails: Maximum emails to fetch and trash for this domain.
        preserve_fiscal: If True (default), protects receipts and fiscal transactions from being trashed.
    """
    clean_domain = sender_domain.strip().lower()
    if not clean_domain:
        return {"error": "sender_domain must not be empty"}

    client = get_client()
    items = client.fetch_emails(
        source_scope="inbox",
        target_senders=[clean_domain],
        corporate_only=False,
        max_results=max_emails,
        skip_unwanted_recall=False,
    )

    if preserve_fiscal:
        candidates = [it for it in items if it.status != "Trashed" and it.category != "Fiscal Transactions"]
        preserved_count = len(items) - len(candidates)
    else:
        candidates = [it for it in items if it.status != "Trashed"]
        preserved_count = 0

    msg_ids = [it.msg_id for it in candidates]
    trashed = 0
    if msg_ids:
        trashed = client.trash_emails(msg_ids, record_undo=True)

    # Mark company to skip on scan
    rlog = get_recall_log()
    comp_name = clean_domain
    if items and items[0].company_name:
        comp_name = items[0].company_name
    rlog.set_company_skip_on_scan(comp_name, skip=True)
    if candidates:
        rlog.record_decisions(
            items=candidates,
            decision="unwanted",
            action_name=f"mcp_auto_trash_company:{comp_name}",
        )

    return {
        "company_name": comp_name,
        "sender_domain": clean_domain,
        "emails_scanned": len(items),
        "trashed_count": trashed,
        "fiscal_preserved_count": preserved_count,
        "marked_skip_on_scan": True,
        "folder": "TRASH",
    }


@app.tool()
def clean_and_repopulate_by_threshold(
    sender_domain: str = "",
    threshold: int = 50,
    metric: str = "importance",
    max_results: int = 150,
    preserve_fiscal: bool = True,
) -> Dict[str, Any]:
    """Automate corporate email triage based on a score threshold.

    Emails scoring below the cutoff are trashed (Red Bucket).
    Emails scoring at or above the cutoff are re-populated / kept in Inbox (Green Bucket).
    Zero custom labels are used. Genuine fiscal transactions are safely preserved by default.

    Args:
        sender_domain: Optional company domain. If empty, evaluates all corporate emails.
        threshold: Score threshold between 0 and 100 (default 50).
        metric: Scoring metric to use ('importance', 'usefulness', or 'combined').
        max_results: Max number of emails to evaluate.
        preserve_fiscal: If True (default), protects receipts and fiscal transactions from being trashed.
    """
    clean_domain = sender_domain.strip().lower()
    metric_key = metric.strip().lower()
    if metric_key not in ("importance", "usefulness", "combined"):
        metric_key = "importance"

    client = get_client()
    targets = [clean_domain] if clean_domain else None
    items = client.fetch_emails(
        source_scope="inbox",
        target_senders=targets,
        corporate_only=True,
        max_results=max_results,
        skip_unwanted_recall=False,
    )

    del_ids = []
    rest_ids = []
    del_items = []
    rest_items = []

    for it in items:
        if metric_key == "importance":
            sc = it.importance_score
        elif metric_key == "usefulness":
            sc = it.usefulness_score
        else:
            sc = it.combined_score

        if sc < threshold and not (preserve_fiscal and it.category == "Fiscal Transactions"):
            del_ids.append(it.msg_id)
            del_items.append(it)
        else:
            rest_ids.append(it.msg_id)
            rest_items.append(it)

    res = client.clean_and_repopulate_by_threshold(del_ids, rest_ids)

    # Record in recall log
    rlog = get_recall_log()
    if del_items:
        rlog.record_decisions(
            items=del_items,
            decision="unwanted",
            action_name=f"mcp_threshold_delete_{metric_key}<{threshold}",
        )
    if rest_items:
        rlog.record_repopulated(
            items=rest_items,
            action_source=f"mcp_threshold_repopulate_{metric_key}>={threshold}",
        )

    return {
        "sender_domain": clean_domain or "All Corporate Companies",
        "threshold": threshold,
        "metric": metric_key,
        "total_evaluated": len(items),
        "trashed_count": res.get("deleted_count", 0),
        "repopulated_count": res.get("repopulated_count", 0),
    }


@app.tool()
def execute_unsubscribe(msg_id: str) -> Dict[str, Any]:
    """Execute automated unsubscribe for an email.

    Attempts RFC 8058 One-Click HTTP POST or RFC 2369 mailto.
    Records the company domain as unwanted in the recall memory log.

    Args:
        msg_id: Gmail message ID to unsubscribe from.
    """
    client = get_client()
    it = client.get_email(msg_id)
    if not it:
        return {"success": False, "message": f"Email '{msg_id}' not found."}

    unsub_info = client.get_unsubscribe_info(msg_id, it)
    if not unsub_info:
        return {"success": False, "message": f"No unsubscribe metadata found for '{msg_id}'."}

    success, msg = do_unsubscribe(client, unsub_info)

    # Record sender domain in recall log
    if success:
        rlog = get_recall_log()
        comp = unsub_info.company_name or it.sender_domain
        rlog.set_company_skip_on_scan(comp, skip=True)
        rlog.record_decisions(
            items=[it],
            decision="unwanted",
            action_name=f"mcp_unsubscribe:{comp}",
        )

    return {
        "success": success,
        "message": msg,
        "company_name": unsub_info.company_name,
        "sender": it.sender,
        "method": "RFC 8058 One-Click POST" if unsub_info.is_one_click else ("mailto" if unsub_info.mailto_address else "web"),
    }


@app.tool()
def get_recall_memory_stats() -> Dict[str, Any]:
    """Retrieve statistics and learned rules from the backend SQLite recall memory log.

    Shows counts of needed emails, unwanted emails, blocked companies,
    and generalized structural subject patterns.
    """
    rlog = get_recall_log()
    counts = rlog.get_summary_counts()
    snap = rlog.get_snapshot()

    sample_rules = []
    for (comp, pattern), (needed_c, unwanted_c, sample_subj) in list(snap.pattern_stats.items())[:20]:
        sample_rules.append({
            "company": comp,
            "pattern": pattern,
            "needed_count": needed_c,
            "unwanted_count": unwanted_c,
            "sample_subject": sample_subj,
        })

    return {
        "summary_counts": counts,
        "blocked_companies": sorted(list(snap.blocked_companies)),
        "active_pattern_rules_sample": sample_rules,
        "storage": "Local SQLite (~/.config/automelinium/email_cache.db)",
    }


@app.tool()
def lessen_importance(
    company_or_domain: str = "",
    msg_ids: Optional[List[str]] = None,
    lessen: bool = True,
) -> Dict[str, Any]:
    """Influence algorithms to lessen or restore importance of emails.

    Applies an explicit -40 Importance algorithm penalty in Automelinium's persistent
    Recall Log, and strips Gmail's native IMPORTANT and STARRED markers to train
    Google's machine learning importance model. Zero custom labels are used.

    Args:
        company_or_domain: Company name or domain to lessen importance for.
        msg_ids: Optional list of individual email message IDs to lessen importance for.
        lessen: True to lessen importance (demote), False to restore baseline importance.
    """
    client = get_client()
    rlog = get_recall_log()
    clean_target = company_or_domain.strip()

    affected_messages = 0
    if clean_target:
        rlog.set_company_lessen_importance(clean_target, lessen=lessen)
        if lessen:
            items = client.fetch_emails(
                source_scope="inbox",
                target_senders=[clean_target],
                corporate_only=False,
                max_results=200,
                skip_unwanted_recall=False,
            )
            target_ids = [it.msg_id for it in items]
            if target_ids:
                affected_messages = client.demote_email_importance_in_gmail(target_ids)

    if msg_ids:
        for mid in msg_ids:
            it = client.get_email(mid)
            if it:
                rlog.demote_message_importance(it, lessen=lessen)
        if lessen:
            affected_messages += client.demote_email_importance_in_gmail(msg_ids)

    return {
        "success": True,
        "company_or_domain": clean_target,
        "lessen_importance": lessen,
        "algorithm_penalty": "-40 Importance (-25 Usefulness)" if lessen else "Baseline Restored",
        "gmail_importance_stripped_count": affected_messages if lessen else 0,
        "status": f"Algorithms influenced for '{clean_target or msg_ids}': {'Demoted' if lessen else 'Restored'}",
    }


@app.tool()
def block_sender_in_gmail(
    company_or_domain: str,
    rule_set: str = "spam",
    msg_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Block a corporate sender or company in Gmail using one of the 3 main rule sets.

    Rule Sets:
      - 'spam': Gmail Native Block. Routes all messages directly to Spam and removes from Inbox.
      - 'trash': Direct to Trash. Moves all messages immediately to Trash.
      - 'archive': Skip Inbox & Mute. Removes from Inbox, marks as read, strips importance markers.

    Also generates a native Gmail Filter Atom XML file for optional cloud filter import,
    and records the rule in the persistent SQLite Recall Log so future scans automatically filter it.
    Zero custom labels are used.

    Args:
        company_or_domain: Company name or domain to block (e.g. 'e.etradefinancial.com' or 'LinkedIn').
        rule_set: One of 'spam' (default), 'trash', or 'archive'.
        msg_ids: Optional explicit message IDs to block. If omitted, scans and blocks matching inbox emails.
    """
    clean_target = company_or_domain.strip()
    rule_key = rule_set.strip().lower()
    if rule_key not in ("spam", "trash", "archive"):
        rule_key = "spam"

    client = get_client()
    rlog = get_recall_log()

    # 1. Save rule in recall log
    rlog.add_block_rule(clean_target, rule_key, target_type="company")

    # 2. Collect target message IDs if not provided
    target_ids = list(msg_ids or [])
    if not target_ids and clean_target:
        items = client.fetch_emails(
            source_scope="inbox",
            target_senders=[clean_target],
            corporate_only=False,
            max_results=200,
            skip_unwanted_recall=False,
        )
        target_ids = [it.msg_id for it in items]

    # 3. Apply Gmail rule
    res = client.block_sender_with_rule(target_ids, rule=rule_key, record_undo=True)

    # 4. Generate native Gmail Filter XML
    xml_path = client.generate_gmail_filter_xml(clean_target, rule=rule_key)

    return {
        "success": True,
        "company_or_domain": clean_target,
        "rule_set_applied": rule_key.upper(),
        "rule_description": {
            "spam": "Moved to Gmail Spam folder and removed from Inbox",
            "trash": "Moved to Gmail Trash folder and removed from Inbox",
            "archive": "Removed from Inbox, marked as read, stripped of importance",
        }.get(rule_key, ""),
        "affected_messages_count": len(target_ids),
        "gmail_filter_xml": xml_path,
        "labels_used": "None (Native system folders: SPAM, TRASH, INBOX)",
    }


@app.tool()
def undo_last_action() -> Dict[str, Any]:
    """Revert the most recent batch trash or re-population action in Gmail."""
    client = get_client()
    success, msg = client.undo_last_action()
    return {
        "success": success,
        "message": msg,
    }


# ============================================================================
# MCP Resources
# ============================================================================


@app.resource("automelinium://inbox/summary", name="inbox_summary", description="Summary of current corporate inbox volume and company distribution")
def resource_inbox_summary() -> str:
    """Return JSON string summarizing corporate emails and top senders."""
    client = get_client()
    items = client.fetch_emails(source_scope="inbox", corporate_only=True, max_results=100)
    groups: Dict[str, int] = {}
    for it in items:
        groups[it.sender_domain] = groups.get(it.sender_domain, 0) + 1

    top_senders = sorted(groups.items(), key=lambda x: x[1], reverse=True)[:15]
    summary_data = {
        "user_email": client.user_email,
        "total_scanned": len(items),
        "unique_corporate_domains": len(groups),
        "top_corporate_senders": [{"domain": dom, "count": cnt} for dom, cnt in top_senders],
    }
    return json.dumps(summary_data, indent=2)


@app.resource("automelinium://recall/preferences", name="recall_preferences", description="Learned user email preferences and domain rules")
def resource_recall_preferences() -> str:
    """Return JSON string of active recall memory rules and preferences."""
    rlog = get_recall_log()
    counts = rlog.get_summary_counts()
    snap = rlog.get_snapshot()
    data = {
        "counts": counts,
        "blocked_companies": sorted(list(snap.blocked_companies)),
        "total_learned_patterns": len(snap.pattern_stats),
    }
    return json.dumps(data, indent=2)


# ============================================================================
# MCP Prompts
# ============================================================================


@app.prompt(
    name="triage_inbox",
    title="Corporate Inbox Triage & Declutter",
    description="Guided workflow for an AI assistant to evaluate top email senders, identify clutter, and suggest threshold actions.",
)
def prompt_triage_inbox(company_focus: str = "") -> str:
    focus_str = f" focusing on '{company_focus}'" if company_focus else ""
    return (
        f"You are the Automelinium Corporate Email Triage Assistant{focus_str}.\n\n"
        "Please follow these steps:\n"
        "1. Call `list_companies` to identify the highest volume corporate senders in the user's Gmail inbox.\n"
        "2. For high-volume marketing senders with low usefulness and importance scores (e.g. promotional mailers, automated bots), "
        "recommend auto-trashing them or setting an auto-delete cutoff via `clean_and_repopulate_by_threshold`.\n"
        "3. Highlight high-importance emails (e.g. security alerts, recruiters, critical orders) and ensure they are kept in the Inbox.\n"
        "4. Summarize your recommendations clearly with zero emojis.\n"
    )


@app.prompt(
    name="audit_company_senders",
    title="Corporate Sender Audit",
    description="Analyze a specific company's email stream and recommend threshold cutoffs.",
)
def prompt_audit_company_senders(domain: str) -> str:
    return (
        f"Please perform a deep-dive audit of emails received from company domain '{domain}'.\n\n"
        "1. Call `scan_corporate_emails` or `get_email_details` to inspect recent messages from this domain.\n"
        "2. Break down their Level of Importance vs Level of Usefulness scores.\n"
        "3. Recommend whether to:\n"
        "   - Keep all emails in Inbox.\n"
        "   - Auto-trash all emails via `auto_trash_company`.\n"
        "   - Apply a threshold partition (e.g. delete < 50, re-populate >= 50).\n"
        "   - Unsubscribe via `execute_unsubscribe`.\n"
    )


# ============================================================================
# Main Entry Point
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Automelinium MCP Server -- Model Context Protocol for Corporate Email Triage"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Test connection to Automatisch Gmail API and print server status",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print list of registered MCP tools and exit",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    args = parser.parse_args()

    if args.check:
        print("[+] Connecting to Automatisch Gmail API...")
        client = get_client()
        email_addr, scope = client.connect_and_verify()
        print(f"[+] Connected successfully as: {email_addr}")
        print(f"[+] Active OAuth Scopes: {scope}")
        rlog = get_recall_log()
        counts = rlog.get_summary_counts()
        print(f"[+] Recall Memory Log: {counts}")
        print("[+] MCP Server ready.")
        return

    if args.list_tools:
        async def _print_tools():
            tools = await app.list_tools()
            print(f"[+] Registered MCP Tools ({len(tools)}):")
            for t in tools:
                print(f"  - {t.name}: {t.description.splitlines()[0] if t.description else ''}")
            res = await app.list_resources()
            print(f"[+] Registered MCP Resources ({len(res)}):")
            for r in res:
                print(f"  - {r.uri}: {r.name}")
            prompts = await app.list_prompts()
            print(f"[+] Registered MCP Prompts ({len(prompts)}):")
            for p in prompts:
                print(f"  - {p.name}: {p.title}")

        asyncio.run(_print_tools())
        return

    # Run the MCP server over stdio
    app.run(transport=args.transport)


if __name__ == "__main__":
    main()
