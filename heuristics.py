"""
Smart Heuristics Engine for Corporate Email Classification, Importance & Usefulness Scoring.

Computes three distinct scores (0-100) for each email:
  - importance_score: Urgency, stakes, direct human/career/security/financial/legal action required
  - usefulness_score: Practical reference & tool utility (meeting notes, recordings, shipping/receipts,
                      latest CI/infra alerts on your own projects, attachments, magic links/keys)
  - combined_score:   Weighted blend emphasizing the stronger of Importance or Usefulness

Also performs chronological duplicate-alert decay so when an automated alert (such as a daily
GitHub Actions 'Run failed' email) repeats dozens of times, ONLY the newest instance stays high-scoring
while older stale copies automatically drop into the Auto-Delete bucket.
"""

import re
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set


# ---------------------------------------------------------------------------
# Fiscal & Financial Transactions Engine (Top Priority Signals & Patterns)
# ---------------------------------------------------------------------------
FISCAL_TRANSACTION_PATTERNS = [
    # 1. Receipts, E-Receipts & Order Confirmations
    (
        r"(?:\breceipt\b|\be-receipt\b|\bpayment receipt\b|\bpayment confirmed\b|\bpayment confirmation\b|\border confirmation\b|\border #\d+|\bthanks for your order\b|\bpurchase confirmation\b|\byour order (?:with|from|has been received)\b|\bstore receipt\b|\bdigital receipt\b|\bcheckout receipt\b|\byour receipt for\b)",
        38,
        38,
        "Fiscal: Purchase / Payment Receipt",
    ),
    # 2. Banking, Cards, Wire & Cash Transfers
    (
        r"(?:\btransaction alert\b|\bcard transaction\b|\bdebit card alert\b|\bcredit card alert\b|\bcard ending in \d{3,4}\b|\bcharge of \$\d+|\bcharged \$\d+|\bbilled \$\d+|\bpayment of \$\d+|\bpayment received\b|\bwe received your payment\b|\bpayment processed\b|\bdirect deposit\b|\bwire transfer\b|\bach transfer\b|\bach debit\b|\bach credit\b|\bfunds transferred\b|\bfunds available\b|\bmoney sent\b|\byou sent \$\d+|\byou received \$\d+|\btransfer confirmation\b|\btransfer completed\b|\bautopay scheduled\b|\bautopay confirmation\b|\bautopay processed\b|\bwithdrawal alert\b)",
        40,
        36,
        "Fiscal: Banking / Card / Transfer Alert",
    ),
    # 3. Invoices, Billing Statements & Accounts
    (
        r"(?:\binvoice #\d*|\binvoice\b|\bbilling statement\b|\baccount statement\b|\bmonthly statement\b|\bquarterly statement\b|\bannual statement\b|\bpayment due\b|\bpast due\b|\boverdue\b|\bamount due:\s*\$\d+|\bbalance due:\s*\$\d+|\bsubscription renewed\b|\brenewal notice\b|\bannual renewal\b|\bautopay payment\b|\butility bill\b|\belectric bill\b|\bstatement is ready\b)",
        38,
        36,
        "Fiscal: Invoice / Billing Statement",
    ),
    # 4. Taxes & Government Financial Documents
    (
        r"(?:\btax document\b|\btax documents are ready\b|\bform 1099\b|\b1099-(?:nec|misc|k|int|div|b|r|s|g)\b|\bform w-2\b|\bw-2\b|\bform 1040\b|\btax return\b|\btax statement\b|\btax refund\b|\birs\b|\bturbotax\b|\bh&r block\b|\btax filing confirmation\b|\bproperty tax\b|\bestimated tax\b)",
        42,
        38,
        "Fiscal: Tax / Legal Financial Document",
    ),
    # 5. Trades, Brokerage & Investments
    (
        r"(?:\btrade confirmation\b|\bexecuted:\s*(?:buy|sell)\b|\border executed\b|\bshares of\b|\btrade execution\b|\bdividend received\b|\bportfolio statement\b|\bbuy order filled\b|\bsell order filled\b|\bcrypto purchase\b|\bcoinbase receipt\b|\brobinhood trade\b|\bfidelity confirmation\b|\bschwab trade\b|\bvanguard transaction\b)",
        38,
        36,
        "Fiscal: Trade / Investment Confirmation",
    ),
    # 6. Refunds & Account Credits
    (
        r"(?:\brefund confirmation\b|\brefund processed\b|\brefund of \$\d+|\bcredit of \$\d+|\bcredited to your account\b|\bchargeback\b|\bpayment adjustment\b|\bcredit memo\b)",
        36,
        34,
        "Fiscal: Refund / Account Credit",
    ),
]

CURRENCY_AMOUNT_PATTERN = re.compile(
    r"(?:\$\s*\d+(?:,\d{3})*(?:\.\d{2})?|\b\d+(?:,\d{3})*(?:\.\d{2})?\s*(?:usd|eur|gbp|cad)\b)",
    re.IGNORECASE,
)

TRANSACTION_AMOUNT_CONTEXT = re.compile(
    r"\b(paid|total|amount|charge|charged|billed|payment|subtotal|debited|credited|refund|due|price|fee|balance)\b",
    re.IGNORECASE,
)

FINANCIAL_DOMAINS = {
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "citibank.com",
    "capitalone.com", "americanexpress.com", "amex.com", "discover.com", "usbank.com",
    "fidelity.com", "schwab.com", "vanguard.com", "etrade.com", "merrill.com",
    "paypal.com", "venmo.com", "squareup.com", "square.com", "stripe.com",
    "intuit.com", "turbotax.com", "gusto.com", "adp.com", "paychex.com",
    "robinhood.com", "coinbase.com", "mercury.com", "brex.com", "ramp.com",
    "affirm.com", "klarna.com", "afterpay.com", "apple.com"
}


def is_fiscal_transaction(item) -> Tuple[bool, str, int]:
    """
    Determine if an email represents a genuine fiscal or financial transaction:
    receipts, invoices, banking alerts, credit card charges, tax documents,
    trade confirmations, refunds, or payment notices.
    Returns (is_fiscal, matched_description, matched_weight).
    """
    subj = (getattr(item, "subject", "") or "").lower()
    sender_domain = (getattr(item, "sender_domain", "") or "").lower()
    snippet = (getattr(item, "snippet", "") or "").lower()
    body_prev = (getattr(item, "body_preview", "") or "").lower()
    blob = f"{subj} {snippet} {body_prev}"

    # 1. Subject match on explicit fiscal patterns (highest confidence)
    for pattern, imp_w, use_w, desc in FISCAL_TRANSACTION_PATTERNS:
        if re.search(pattern, subj):
            return (True, desc, max(imp_w, use_w))

    # 2. Known financial institutions with transaction indicators in subject or preview
    domain_match = any(sender_domain.endswith(dom) for dom in FINANCIAL_DOMAINS)
    if domain_match:
        if re.search(r"\b(statement|alert|transfer|deposit|withdrawal|charge|payment|tax|confirm|receipt|account|balance)\b", subj):
            return (True, "Fiscal: Financial Institution Alert", 38)
        if re.search(r"\b(statement|transfer|deposit|withdrawal|charge|payment|tax|confirm|receipt)\b", blob):
            return (True, "Fiscal: Financial Institution Notice", 32)

    # 3. Currency amount with transactional context in subject or snippet
    has_amount = bool(CURRENCY_AMOUNT_PATTERN.search(subj) or CURRENCY_AMOUNT_PATTERN.search(blob))
    has_context = bool(TRANSACTION_AMOUNT_CONTEXT.search(subj) or TRANSACTION_AMOUNT_CONTEXT.search(blob))
    if has_amount and has_context:
        # Avoid marketing discounts like "$10 off" or "save $50"
        if not re.search(r"\b(\$\d+\s*off|save\s*\$\d+|up to\s*\$\d+)\b", subj):
            if re.search(r"\b(order|receipt|invoice|bill|charge|charged|paid|payment|autopay|statement|due)\b", subj):
                return (True, "Fiscal: Monetary Order / Billing Notice", 36)
            if re.search(r"\b(order #\d+|receipt #\d+|invoice #\d+|total:\s*\$\d+|amount paid|payment of \$\d+)\b", blob):
                return (True, "Fiscal: Monetary Transaction Record", 32)

    # 4. Body match on fiscal patterns
    for pattern, imp_w, use_w, desc in FISCAL_TRANSACTION_PATTERNS:
        if re.search(pattern, blob):
            return (True, desc, int(max(imp_w, use_w) * 0.75))

    return (False, "", 0)


# ---------------------------------------------------------------------------
# High-Importance Signals (Urgency, Stakes, Career, Security, Legal, Finance)
# ---------------------------------------------------------------------------
IMPORTANCE_HIGH_PATTERNS = [
    (
        r"\b(urgent|immediate action|action required|response required|time[- ]sensitive|due:\s*rsvp|please rsvp)\b",
        28,
        "Urgent / Action Required",
    ),
    (
        r"\b(security alert|suspicious activity|fraud alert|password reset|reset your .* password|verification code|is your .* code|2fa|two-factor|new sign-in|require 2fa|oauth application has been added|requesting updated permissions)\b",
        28,
        "Security / Auth Alert",
    ),
    (
        r"\b(invoice|billing statement|payment due|past due|overdue|tax document|form 1099|1099-(?:nec|misc|k|int|div|b)|w-2|proxyvote|executed:\s*(?:buy|sell)|trade confirmation|annual report is due|important updates for election)\b",
        26,
        "Financial / Trade / Legal Obligation",
    ),
    (
        r"\b(contract|agreement|docusign|signature required|legal notice|updated our terms|terms of service|updated our privacy policy)\b",
        22,
        "Legal / Policy Notice",
    ),
    (
        r"\b(interview invitation|schedule an interview|phone screen|job offer|offer letter|application status|your application to|indeed application|hiring team|candidacy|onboarding|payroll|new applicant for|now accepting applications|inmail from)\b",
        30,
        "Career / Recruiting Critical",
    ),
    (
        r"\b(run failed|build failed|deployment failed|production incident|service outage|quota exceeded|codespaces that are about to be deleted)\b",
        24,
        "CI / Production / Infra Alert",
    ),
    (
        r"\b(flight confirmation|boarding pass|check-in for your|reservation confirmed|travel itinerary|upcoming appointment|appointment confirmation|appointment reminder)\b",
        24,
        "Schedule / Appointment Confirmation",
    ),
]

# ---------------------------------------------------------------------------
# High-Usefulness Signals (Practical Reference, Notes, Receipts, Shipping, Tools)
# ---------------------------------------------------------------------------
USEFULNESS_HIGH_PATTERNS = [
    (
        r"(?:\bnotes:|\bmeeting notes\b|\bmeeting records:|\bmeeting summary\b|\btranscript\b|\bunable to record\b|\bshared .* in otter\b|\baction items\b)",
        32,
        "Meeting Notes / Recording Utility",
    ),
    (
        r"(?:\breceipt\b|\bpayment receipt\b|\border confirmation\b|\border #\d+|\bthanks for your order\b|\bexecuted:\s*(?:buy|sell)\b|\btransaction material\b|\btransaction history\b|\bpurchase confirmation\b|\binvoice #\d*)",
        30,
        "Order / Receipt / Trade Record",
    ),
    (
        r"\b(tracking number|shipment from order|your item has sold|sold item has been delivered|has shipped|out for delivery|package is on the way|shipping options|(?:fedex|ups|usps|dhl)\s+(?:tracking|shipment|delivery|package))\b",
        30,
        "Shipping / Marketplace Logistics",
    ),
    (
        r"(?:\brun failed\b|\bworkflow run\b|\bpull request\b|\bissue #\d+|\bdeployment\b|\bdatabase\b|\bsupabase\b|\bnew applicant for\b|\bnow accepting applications\b|\bindeed application:|\bapplication status:)",
        24,
        "Developer / Hiring / Tool Utility",
    ),
    (
        r"(?:\bcalendar\b|\binvitation:|\bconfirmed:.*meeting\b|\bvoted in poll for\b|\bcheck-in\b|\bcohort meeting\b|\bzoom meeting\b|\bgoogle meet\b|\bmicrosoft teams\b)",
        24,
        "Calendar / Meeting Coordination",
    ),
    (
        r"\b(secure link|magic link|login link|verification code|api key|license key|activation link|shared a document|invited you to edit)\b",
        24,
        "Access Link / Collaboration Utility",
    ),
    (
        r"(?:\bquarterly statement\b|\bmonthly statement\b|\baccount statement\b|\bannual report\b|\bsupport ticket\b|\bticket #\d+|\bcase #\d+)",
        24,
        "Statement / Support Reference",
    ),
]

# ---------------------------------------------------------------------------
# Corporate Clutter, Bot Churn, Job-Board Spam & Marketing Noise Penalties
# ---------------------------------------------------------------------------
CORPORATE_NOISE_PATTERNS = [
    (
        r"(?:\bbuild\(deps\)|\bbuild\(deps-dev\)|\bdependabot\b|\brenovate\[bot\]|\bbump\s+\S+\s+from\s+\S+\s+to\b)",
        -40,
        "Automated Dependency Bot Churn",
    ),
    (
        r"(?:\b\d+%\s*off\b|\bup to \d+% off\b|\bon sale\b|\bwishlist\b|\bflash sale\b|\blimited time\b|\bfree shipping\b|\bpromo code\b|\bcoupon\b|\bclearance\b|\bgrail sale\b|\bmarkdowns\b|\bsave on fees\b|\b100% off seller fees\b|\bstack savings\b|\bweekend savings\b|\bprice drop alert\b)",
        -30,
        "Promotional Sale / Discount Spam",
    ),
    (
        r"(?:\bfast track to\b|\block in \d+(\.\d+)?%\s*apy\b|\bvalued member\b|\bwe miss you\b|\bwhere have you been\b|\bpoints expire\b|\bcome back\b|\bdon't miss out\b|\blast chance\b|\bfinal hours\b|\bfinal days\b|\balmost gone\b)",
        -30,
        "Marketing / Re-engagement Urgency Bloat",
    ),
    (
        r"(?:\bjob might be right for you\b|\bjob i found for you\b|\band others are hiring in\b|\bnew jobs in .* apply now\b|\bappointment setter\b|\bcareer & internship fair\b|\bcareer fair\b|\bearly bird rates\b|\bmarketing consultation\b|\bdonate today\b|\bgiving tuesday\b)",
        -28,
        "Bulk Job-Alert / Fair / Marketing Pitch",
    ),
    (
        r"\b(unsubscribe|opt[- ]out|manage preferences|email preferences|view in browser)\b",
        -12,
        "Bulk Marketing Footer",
    ),
    (
        r"(?:\bnewsletter\b|\bweekly digest\b|\bdaily digest\b|\bweekly download:|\broundup\b|\bwhat's new\b|\bnew blogpost\b)",
        -18,
        "Newsletter / Digest",
    ),
    (
        r"\b(webinar|register now|join us live|virtual event|sponsored|partner offer)\b",
        -22,
        "Marketing Event / Webinar",
    ),
    (
        r"\b(survey|rate your experience|tell us what you think|how'd we do|how’d we do|feedback request|year in review)\b",
        -20,
        "Automated Survey / Recap",
    ),
]

# Known marketing mailer subdomain prefixes (e.g. em.linkedin.com, e.tommy.com, mail.grailed.com)
MARKETING_SUBDOMAIN_PREFIXES = ("em.", "e.", "promo.", "marketing.", "news.", "offers.", "deals.", "campaigns.", "engage.", "learn.mail.")

# Automated / corporate sender local-part prefixes
AUTOMATED_PREFIXES = {
    "no-reply", "noreply", "do-not-reply", "donotreply",
    "marketing", "news", "newsletter", "newsletters",
    "promotions", "promo", "deals", "offers", "sales",
    "info", "updates", "notifications", "notify", "alerts",
    "hello", "team", "digest", "mailer", "bounce", "campaigns", "bot"
}

FREE_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com",
    "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "aol.com", "proton.me", "protonmail.com", "fastmail.com"
}


@dataclass(slots=True)
class EmailItem:
    msg_id: str
    thread_id: str
    subject: str
    sender: str
    sender_name: str
    sender_email: str
    sender_domain: str
    recipient: str
    date_str: str
    timestamp: int
    snippet: str
    body_preview: str
    labels: List[str] = field(default_factory=list)
    has_unsubscribe: bool = False
    has_attachment: bool = False
    is_unread: bool = False
    is_Corporate: bool = False
    category: str = "General Corporate"
    importance_score: int = 50
    usefulness_score: int = 50
    combined_score: int = 50
    tier: str = "Moderate Utility"
    reasons: List[str] = field(default_factory=list)
    status: str = "In Inbox"  # "In Inbox", "Quarantined", "Trashed", "Restored"
    company_name: str = ""
    logo_domain: str = ""
    unsubscribe_header: str = ""
    unsubscribe_post: str = ""


def parse_sender_parts(raw_sender: str):
    """Extract (display_name, email_address, domain) from a raw From header."""
    if not raw_sender:
        return ("Unknown", "", "")
    match = re.search(r"<([^>]+)>", raw_sender)
    if match:
        email_addr = match.group(1).strip().lower()
        name = raw_sender[:match.start()].strip().strip('"').strip("'")
    else:
        email_addr = raw_sender.strip().lower()
        name = email_addr.split("@")[0] if "@" in email_addr else email_addr

    raw_domain = email_addr.split("@")[-1] if "@" in email_addr else ""
    domain = sys.intern(raw_domain) if raw_domain else ""
    if not name:
        name = email_addr
    return (name, email_addr, domain)


def classify_corporate_category(item: EmailItem) -> str:
    """Assign a human-friendly corporate category to an EmailItem."""
    is_fiscal, _, _ = is_fiscal_transaction(item)
    if is_fiscal:
        return sys.intern("Fiscal Transactions")
    blob = f"{item.sender_name} {item.sender_email} {item.subject} {item.snippet}".lower()
    if re.search(r"\b(dependabot|renovate|build\(deps|github-actions\[bot\]|run failed)\b", blob):
        return sys.intern("Bot / CI Churn")
    if re.search(r"\b(notes:|meeting|calendar|otter\.ai|granola|check-in|cohort|rsvp|invitation)\b", blob):
        return sys.intern("Meetings & Notes")
    if re.search(r"\b(ycombinator|recruiter|hiring|job|career|handshake|workday|interview|application|candidacy|harvard|applicant)\b", blob):
        return sys.intern("Career & Recruiting")
    if re.search(r"\b(security alert|verification|password|sign-in|terms of service|privacy policy|proxyvote|supabase|claude|2fa|suspicious activity)\b", blob):
        return sys.intern("Security & Account")
    if re.search(r"\b(shipping|tracking|support|customer|delivery|logistics|ticket)\b", blob):
        return sys.intern("Logistics & Support")
    if item.has_unsubscribe or "CATEGORY_PROMOTIONS" in item.labels or re.search(r"\b(sale|wishlist|% off|deal|promo|apy|member|newsletter)\b", blob):
        return sys.intern("Marketing & Promo")
    return sys.intern("General Corporate")


def is_corporate_email(item: EmailItem, target_filters: Optional[List[str]] = None) -> bool:
    """
    Determine whether an email is from a corporate/bulk sender or matches user-specified filters.
    Protects personal emails from free email domains (gmail.com, icloud.com, etc.) unless they
    have a List-Unsubscribe header or automated local-part prefix.
    """
    if target_filters:
        haystack = f"{item.sender_name} {item.sender_email} {item.sender_domain} {item.subject}".lower()
        for f in target_filters:
            token = f.strip().lower()
            if token and token in haystack:
                return True
        return False

    local_part = item.sender_email.split("@")[0] if "@" in item.sender_email else ""
    is_auto_prefix = any(
        local_part == p or local_part.startswith(f"{p}+") or local_part.startswith(f"{p}.") or local_part.startswith(f"{p}-") or local_part.startswith(f"{p}_")
        for p in AUTOMATED_PREFIXES
    )

    # Protect real human emails on personal domains (e.g. @gmail.com)
    if item.sender_domain in FREE_PERSONAL_DOMAINS:
        if item.has_unsubscribe or is_auto_prefix or "CATEGORY_PROMOTIONS" in item.labels:
            return True
        return False

    if item.has_unsubscribe or is_auto_prefix:
        return True
    if any(lbl in item.labels for lbl in ("CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "CATEGORY_SOCIAL")):
        return True
    if item.sender_domain and item.sender_domain not in FREE_PERSONAL_DOMAINS:
        return True
    return False


def _normalize_subject_for_dedup(subject: str) -> str:
    """Normalize a subject line to detect repetitive automated alerts while keeping distinct order numbers separate."""
    s = (subject or "").strip().lower()
    s = re.sub(r"^(re|fwd|fw):\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def score_emails(emails: List[EmailItem], user_email: str = "", custom_boost_keywords: Optional[List[str]] = None) -> None:
    """
    Score a batch of EmailItem objects in-place for:
      - importance_score (0-100): Urgency, career/personal/security/financial stakes
      - usefulness_score (0-100): Reference value, meeting notes, shipping/receipts, actionable tools
      - combined_score   (0-100): Overall priority metric
    """
    from company_branding import resolve_company_identity

    if hasattr(score_emails, "_recall_snap"):
        delattr(score_emails, "_recall_snap")

    sender_counts: Dict[str, int] = {}
    for item in emails:
        item.company_name, item.logo_domain = resolve_company_identity(
            item.sender_domain, item.sender_name, item.sender_email
        )
        key = item.sender_email or item.sender_domain
        sender_counts[key] = sender_counts.get(key, 0) + 1

    # Track chronological rank of identical subjects from the same company (0 = newest copy, 1 = 2nd newest, etc.)
    by_comp_subj: Dict[tuple, List[EmailItem]] = {}
    for item in emails:
        norm_subj = _normalize_subject_for_dedup(item.subject)
        key = (item.company_name, norm_subj)
        by_comp_subj.setdefault(key, []).append(item)

    dup_rank_by_msg_id: Dict[str, Tuple[int, int]] = {}
    for key, grp in by_comp_subj.items():
        if len(grp) > 1:
            grp_sorted = sorted(grp, key=lambda x: x.timestamp, reverse=True)
            total_copies = len(grp_sorted)
            for idx, it in enumerate(grp_sorted):
                dup_rank_by_msg_id[it.msg_id] = (idx, total_copies)

    user_email_lower = (user_email or "").lower()
    custom_keywords = [k.strip().lower() for k in (custom_boost_keywords or []) if k.strip()]

    for item in emails:
        imp = 46
        use = 46
        reasons: List[str] = []

        text_blob = f"{item.subject} {item.snippet} {item.body_preview}".lower()
        subj_lower = item.subject.lower()
        sender_lower = f"{item.sender_name} {item.sender_email}".lower()
        local_part = item.sender_email.split("@")[0] if "@" in item.sender_email else ""

        is_fiscal, fiscal_desc, fiscal_w = is_fiscal_transaction(item)
        item.category = classify_corporate_category(item)

        # 1. Gmail Label signals
        if "STARRED" in item.labels:
            imp += 25
            use += 22
            reasons.append("Starred in Gmail (+25 Imp, +22 Use)")
        if "IMPORTANT" in item.labels and "CATEGORY_PROMOTIONS" not in item.labels:
            imp += 6
            reasons.append("Gmail Important marker (+6 Imp)")
        if "CATEGORY_PERSONAL" in item.labels:
            imp += 14
            use += 10
            reasons.append("Personal category (+14 Imp, +10 Use)")
        if "CATEGORY_PROMOTIONS" in item.labels:
            if is_fiscal:
                reasons.append("Fiscal Priority: Neutralized Gmail Promotions category penalty (+22 Imp, +18 Use retained)")
            else:
                imp -= 22
                use -= 18
                reasons.append("Promotions category (-22 Imp, -18 Use)")
        elif "CATEGORY_SOCIAL" in item.labels:
            imp -= 12
            use -= 10
            reasons.append("Social category (-12 Imp, -10 Use)")

        # 2. Marketing subdomain & automated sender checks
        if any(item.sender_domain.startswith(pref) for pref in MARKETING_SUBDOMAIN_PREFIXES) or "mailchimp" in item.sender_domain:
            if not is_fiscal:
                imp -= 16
                use -= 14
                reasons.append(f"Marketing subdomain '{item.sender_domain}' (-16 Imp)")

        if item.has_unsubscribe:
            if is_fiscal:
                reasons.append("Fiscal Exemption: Transactional email with unsubscribe link preserved (+10 Imp retained)")
            else:
                imp -= 10
                use -= 8
                reasons.append("Bulk List-Unsubscribe header (-10 Imp, -8 Use)")

        if any(local_part == p or local_part.startswith(f"{p}-") or local_part.startswith(f"{p}_")
               for p in ("noreply", "no-reply", "donotreply", "do-not-reply", "marketing", "newsletter", "promotions", "deals")):
            if is_fiscal:
                reasons.append(f"Fiscal Exemption: Transactional automated sender '{local_part}@' preserved (+8 Imp retained)")
            else:
                imp -= 8
                reasons.append(f"Automated sender '{local_part}@' (-8 Imp)")

        # 3. Direct human reply or personal name mention
        if subj_lower.startswith("re:") and not re.search(r"\[[^\]]+/[^\]]+\]", subj_lower) and not item.has_unsubscribe:
            imp += 18
            use += 16
            reasons.append("Direct human thread reply 'Re:' (+18 Imp, +16 Use)")

        # 4. Top-Tier Fiscal Transaction Priority Boost
        if is_fiscal:
            fiscal_in_subj = any(re.search(pat, subj_lower) for pat, _, _, _ in FISCAL_TRANSACTION_PATTERNS)
            if fiscal_in_subj:
                imp += 38
                use += 38
                reasons.append(f"{fiscal_desc} in subject (+38 Imp, +38 Use)")
            else:
                imp += 28
                use += 28
                reasons.append(f"{fiscal_desc} in body (+28 Imp, +28 Use)")

            has_currency = bool(CURRENCY_AMOUNT_PATTERN.search(subj_lower) or CURRENCY_AMOUNT_PATTERN.search(text_blob))
            has_trans_ctx = bool(TRANSACTION_AMOUNT_CONTEXT.search(subj_lower) or TRANSACTION_AMOUNT_CONTEXT.search(text_blob))
            if has_currency and has_trans_ctx:
                imp += 8
                use += 8
                reasons.append("Fiscal Confirmation: Monetary transaction value verified (+8 Imp, +8 Use)")

        # 5. High-Importance pattern matching (primarily boosts Importance)
        for pattern, weight, label in IMPORTANCE_HIGH_PATTERNS:
            if re.search(pattern, subj_lower):
                imp += weight
                use += int(weight * 0.3)
                reasons.append(f"{label} in subject (+{weight} Imp)")
            elif re.search(pattern, text_blob):
                boost = int(weight * 0.55)
                imp += boost
                use += int(boost * 0.2)
                reasons.append(f"{label} in body (+{boost} Imp)")

        # 6. High-Usefulness pattern matching (primarily boosts Usefulness)
        for pattern, weight, label in USEFULNESS_HIGH_PATTERNS:
            if re.search(pattern, subj_lower):
                use += weight
                imp += int(weight * 0.25)
                reasons.append(f"{label} in subject (+{weight} Use)")
            elif re.search(pattern, text_blob):
                boost = int(weight * 0.6)
                use += boost
                imp += int(boost * 0.15)
                reasons.append(f"{label} in body (+{boost} Use)")

        # 7. Corporate Noise / Bot Churn / Sale Penalties
        for pattern, penalty, label in CORPORATE_NOISE_PATTERNS:
            if re.search(pattern, text_blob) or re.search(pattern, sender_lower):
                if is_fiscal and penalty in (-30, -28):
                    # Exempt verified fiscal transactions from promotional sale/coupon penalties
                    reasons.append(f"Fiscal Exemption: Preserved against '{label}' penalty (+{abs(penalty)} retained)")
                    continue
                imp += penalty
                use += penalty
                reasons.append(f"{label} ({penalty} Imp/Use)")

        # 8. Real attachment bonus
        if item.has_attachment:
            use += 18
            imp += 8
            reasons.append("Has file attachment (+18 Use)")

        if user_email_lower and user_email_lower in (item.recipient or "").lower():
            imp += 5
            use += 5

        # 9. Sender volume fatigue penalty
        freq = sender_counts.get(item.sender_email or item.sender_domain, 1)
        if freq >= 8:
            if not is_fiscal:
                pen = min(20, (freq - 5) * 2)
                imp -= pen
                use -= int(pen * 0.75)
                reasons.append(f"High-frequency sender ({freq} msgs, -{pen} Imp)")
        elif freq == 1:
            imp += 4
            use += 4

        # 10. Stale Duplicate Alert Decay
        if item.msg_id in dup_rank_by_msg_id:
            if is_fiscal:
                reasons.append("Fiscal Preservation: Recurring transaction receipts exempted from duplicate alert decay")
            else:
                dup_idx, total_copies = dup_rank_by_msg_id[item.msg_id]
                if dup_idx == 0 and total_copies >= 3:
                    reasons.append(f"Latest copy of recurring alert (1 of {total_copies})")
                elif dup_idx == 1 and total_copies >= 3:
                    imp -= 18
                    use -= 16
                    reasons.append(f"Older duplicate alert (copy 2 of {total_copies}, -18 Imp, -16 Use)")
                elif dup_idx >= 2:
                    imp -= 36
                    use -= 34
                    reasons.append(f"Stale duplicate alert (copy {dup_idx + 1} of {total_copies}, -36 Imp, -34 Use)")

        # 11. Custom user boost keywords
        for kw in custom_keywords:
            if re.search(rf"\b{re.escape(kw)}\b", subj_lower):
                imp += 16
                use += 16
                reasons.append(f"Matched priority keyword '{kw}' in subject (+16)")
            elif re.search(rf"\b{re.escape(kw)}\b", text_blob):
                imp += 9
                use += 9
                reasons.append(f"Matched priority keyword '{kw}' in body (+9)")

        # 12. Recall Log & Preference Memory Integration
        try:
            from recall_log import RecallLogManager, extract_subject_pattern
            if not hasattr(score_emails, "_recall_snap"):
                score_emails._recall_snap = RecallLogManager().get_snapshot()
            snap = score_emails._recall_snap

            if item.msg_id in snap.needed_msg_ids:
                imp += 35
                use += 35
                reasons.append("Recall Log: Explicitly marked Needed (+35)")
            elif item.msg_id in snap.unwanted_msg_ids:
                imp -= 45
                use -= 45
                reasons.append("Recall Log: Explicitly marked Unwanted (-45)")
            else:
                pat = extract_subject_pattern(item.subject)
                p_stat = snap.pattern_stats.get((item.company_name, pat))
                if p_stat:
                    n_cnt, u_cnt, _ = p_stat
                    if n_cnt > u_cnt:
                        imp += 26
                        use += 26
                        reasons.append(f"Recall Log: Matches needed template (+26)")
                    elif u_cnt > n_cnt:
                        imp -= 32
                        use -= 32
                        reasons.append(f"Recall Log: Matches unwanted template (-32)")

                c_stat = snap.company_stats.get(item.company_name)
                if c_stat and c_stat[2]:  # skip_on_scan is True
                    imp -= 24
                    use -= 24
                    reasons.append(f"Recall Log: Company '{item.company_name}' blocked (-24)")

            # Explicit Algorithm Demotion: Lessen Importance on Company or Email
            is_comp_demoted = (
                item.company_name in getattr(snap, "demoted_companies", set())
                or item.sender_domain in getattr(snap, "demoted_companies", set())
            )
            if is_comp_demoted:
                imp -= 40
                use -= 25
                reasons.append(f"Algorithm Demoted: Lessened importance for '{item.company_name}' (-40 Imp, -25 Use)")

            if item.msg_id in getattr(snap, "demoted_msg_ids", set()):
                imp -= 45
                use -= 30
                reasons.append("Algorithm Demoted: Explicitly lessened importance for this email (-45 Imp, -30 Use)")
        except Exception:
            pass

        # 13. Priority Floor for Fiscal Transactions
        # Unless explicitly marked unwanted or demoted in Recall Log, guaranteed high priority
        if is_fiscal:
            snap_obj = getattr(score_emails, "_recall_snap", None)
            is_unwanted = snap_obj and item.msg_id in getattr(snap_obj, "unwanted_msg_ids", set())
            is_demoted = snap_obj and (
                item.msg_id in getattr(snap_obj, "demoted_msg_ids", set())
                or item.company_name in getattr(snap_obj, "demoted_companies", set())
            )
            if not is_unwanted and not is_demoted:
                imp = max(imp, 74)
                use = max(use, 76)

        # Clamp scores to [0, 100]
        item.importance_score = max(0, min(100, int(round(imp))))
        item.usefulness_score = max(0, min(100, int(round(use))))
        item.combined_score = max(
            0,
            min(100, int(round(0.55 * max(item.importance_score, item.usefulness_score) +
                               0.45 * ((item.importance_score + item.usefulness_score) / 2.0))))
        )

        if item.combined_score >= 68:
            item.tier = sys.intern("High Priority / Keep")
        elif item.combined_score >= 45:
            item.tier = sys.intern("Moderate Utility")
        else:
            item.tier = sys.intern("Low Value / Corporate Noise")

        if not reasons:
            reasons.append("Standard corporate communication")
        item.reasons = reasons
