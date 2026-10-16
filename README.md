# Automelinium -- Corporate Email Organizer & Smart Re-Populator GUI

Automelinium is a Python desktop GUI application in **Frutiger Aero** style that connects directly to your **Gmail Inbox** using your existing **Automatisch Gmail OAuth2 connection**, sorts corporate senders by volume and value, lets you automatically delete emails from a specific corporate sender, and lets you **immediately re-populate** emails based on their estimated **Level of Use (Usefulness)** or **Level of Importance**.

---

## Architecture & Modules

### 1. [`gmail_client.py`](gmail_client.py) -- Automatisch Gmail API Client & SQLite Cache (`AutomatischGmailClient`)
- **Zero-Config Automatisch OAuth**: Extracts and decrypts your Gmail OAuth credentials from `automatisch-postgres-1` and `automatisch-main-1`, refreshes tokens automatically, and includes automatic headless Selenium scope recovery via `reconnect_google_connections.py`.
- **Local SQLite Metadata Cache** (`~/.config/automelinium/email_cache.db`): Caches immutable message headers locally so fetching and re-populating 200+ corporate emails completes in under 2 seconds, with exponential backoff retry on Gmail rate limits.
- **Native Gmail System Folders (Zero Custom Labels)**:
  - Operates strictly with native `INBOX` and `TRASH` system folders.
  - Zero custom labels created, queried, or modified in Gmail.
- **1-Click Algorithm Influence**:
  - Influences both **Automelinium's Heuristic Scoring Engine** and **Gmail's ML Importance Classifier** on the click of a button.
  - Clicking **Lessen Importance in Algorithms** on a company or email applies an explicit -40 Importance penalty in the persistent SQLite Recall Log and strips Gmail's native `IMPORTANT` and `STARRED` system markers via Gmail API, training Google's algorithmic models.
- **1-Click Undo Stack (`undo_last_action`)**: Reverts any Delete, Trash, or Re-Populate batch operation back to its previous Gmail folder state.

### 2. [`heuristics.py`](heuristics.py) -- Distinct Importance & Usefulness Scoring Engine (`score_emails`)
- **Personal Email Protection**: Protects non-automated emails from personal domains (`gmail.com`, `icloud.com`, etc.) so human conversations are never swept into corporate deletion.
- **Fiscal Transaction Priority Engine**: Highest-priority classification and scoring tier for financial transactions, including purchase receipts (`orders@`, `receipt #`), order confirmations, banking/debit/credit card alerts (`alerts@chase.com`, card charges, wire/ACH transfers), invoices and billing statements (`invoice #`, `amount due`), tax filings and documents (`Form 1099-NEC`, `W-2`, `1040`), trade confirmations, and refunds.
  - **Pattern & Context Verification**: Multi-layered detection analyzing explicit fiscal patterns, verified financial institutions (`FINANCIAL_DOMAINS`), and currency amount extraction (`CURRENCY_AMOUNT_PATTERN`) with transactional context.
  - **Noise & Penalty Exemptions**: Genuine fiscal emails are exempted from promotional discount/coupon penalties (`-30` saved), automated sender penalties (`-8` saved), bulk unsubscribe header deductions, and Gmail `CATEGORY_PROMOTIONS` tab penalties.
  - **Duplicate Alert Protection**: Recurring monthly subscription and billing receipts are exempted from duplicate alert decay so legitimate transaction histories are never degraded.
  - **Guaranteed Priority Floor**: Verified fiscal transactions receive an automatic priority floor (minimum combined score of 75+, tier `High Priority / Keep`), ensuring they remain in the Re-Populated/Keep bucket by default.
  - **Dedicated Category**: Categorized as `Fiscal Transactions` in both GUI inspector tables and MCP agent tools.
- **Estimated Importance Score (`importance_score`, 0-100)**: Measures urgency, personal/career stakes, direct replies (`Re:`), recruiting/job actions (`ycombinator.com`, `harvard.edu`, `myworkday.com`), RSVP deadlines, security/auth alerts (`accounts.google.com`), legal/financial/proxy notices (`proxyvote.com`), and CI failures on your own repositories.
- **Estimated Usefulness Score (`usefulness_score`, 0-100)**: Measures practical reference and tool utility: meeting notes and recordings (`Notes:`, `otter.ai`), shipping/logistics/order records (`ezcomputers.com`), developer/infrastructure alerts (`supabase.com`), magic/access links, and real file attachments.
- **Corporate Clutter & Bot Penalties**: Heavily penalizes automated dependency bot churn (`dependabot[bot]`, `build(deps)`), promotional sales/wishlist alerts (`steampowered.com`, `e.etradefinancial.com`, `e.tommy.com`, `mail.grailed.com`), marketing mailer subdomains (`em.*`, `e.*`), and high-volume sender blasts.

### 3. [`aero_theme.py`](aero_theme.py) & [`automelinium_gui.py`](automelinium_gui.py) -- Frutiger Aero Sender-First Auto-Deleter GUI
- **Step 1 (Left Panel) -- Pick a Corporate Sender & Auto-Delete**:
  - Sorts all corporate senders in your inbox by **Most Emails**, **Lowest Value**, or **A-Z**.
  - **Lessen Importance in Algorithms**: 1-click button to demote a company's importance in both Automelinium's engine (-40 penalty) and Gmail's ML model.
  - **Block in Gmail (Choose Rule)**: 1-click dialog to block a company or sender using Gmail's 3 main rule sets:
    1. **1. Send to Spam (Gmail Official Block)**: Routes all matching messages directly to Spam, strips importance, and removes from Inbox.
    2. **2. Direct to Trash (Instant Auto-Delete)**: Immediately trashes all messages from this sender, removes from Inbox, and purges from workflow.
    3. **3. Archive & Mute (Skip the Inbox)**: Silently archives messages, marks as read, and preserves them in All Mail for searchable reference without inbox clutter.
  - Click **Auto-Delete ALL From This Sender in Gmail** to immediately delete all emails from that specific sender, or **Auto-Delete < Cutoff & Re-Populate >= Cutoff**.
  - **Unsubscribe from Active Company**: Triggers RFC 8058 One-Click HTTP POST or RFC 2369 mailto unsubscribe directly.
- **Step 2 (Top Control Card) -- Sort & Partition by Usefulness or Importance**:
  - Switch between **Estimated Level of USE (Usefulness)**, **Estimated Level of IMPORTANCE**, or **Combined Score**, and adjust the **Auto-Partition Cutoff Slider (`0-100`)**.
- **Step 3 (Side-by-Side Split Buckets)**:
  - **Left Bucket (Coral Glass -- `AUTO-DELETE BUCKET`)**: Emails from that sender slated for deletion or already deleted in Gmail.
  - **Right Bucket (Mint-Emerald Glass -- `RE-POPULATED BY USEFULNESS / IMPORTANCE`)**: Emails from that sender that meet your Usefulness or Importance cutoff, sorted highest-score-first.
- **Inspector Panel**: Shows detailed score breakdown, email snippet, **Unsubscribe from Sender**, **Lessen Email Importance**, and **Block Sender in Gmail** buttons.

### 4. [`automelinium_mcp.py`](automelinium_mcp.py) -- Model Context Protocol (MCP) Server
- **Open MCP Standard**: Exposes Automelinium corporate email intelligence, scoring, auto-deletion, re-population, and recall memory as an MCP server for AI agents (Antigravity, Claude Desktop, Cursor, etc.).
- **13 MCP Tools**:
  - `check_connection`: Verifies Automatisch Gmail API connection, active OAuth scopes, and local SQLite cache.
  - `scan_corporate_emails`: Scans and scores corporate emails from Gmail inbox with Importance & Usefulness scoring.
  - `list_companies`: Aggregates inbox corporate emails by company with volume, average scores, and status breakdown.
  - `get_email_details`: Detailed email metadata, scoring rationale breakdown, and unsubscribe info.
  - `trash_emails`: Batch trashes emails via native Gmail TRASH folder (zero custom labels) and logs feedback.
  - `repopulate_emails`: Restores emails back to Gmail INBOX (zero custom labels) and updates recall memory.
  - `auto_trash_company`: Trashes all emails from a specific company domain and marks it to skip on future scans.
  - `clean_and_repopulate_by_threshold`: Automates triage by score threshold (trashes below cutoff, restores >= cutoff).
  - `execute_unsubscribe`: Executes RFC 8058 1-click POST or mailto unsubscribe and marks domain as unwanted.
  - `get_recall_memory_stats`: Retrieves statistics, blocked companies, and structural subject pattern rules.
  - `lessen_importance`: Influences algorithms to lessen or restore importance of emails for a company or message IDs.
  - `block_sender_in_gmail`: Blocks a sender or company in Gmail using one of the 3 main rule sets (Spam, Trash, Archive).
  - `undo_last_action`: Reverts the most recent batch trash or re-population operation.
- **2 MCP Resources**:
  - `automelinium://inbox/summary`: Real-time JSON summary of inbox volume and top company senders.
  - `automelinium://recall/preferences`: Real-time JSON export of learned sender preferences and pattern rules.
- **2 MCP Prompts**:
  - `triage_inbox`: Interactive prompt guiding AI models to evaluate high-volume corporate senders and suggest threshold actions.
  - `audit_company_senders`: Deep-dive audit prompt for a specific company's email stream.

---

## Installation & Setup

### 1. Prerequisites
- **Python**: 3.10 or higher
- **Tkinter**: Included in standard Python installers on macOS & Windows. On Linux (Ubuntu/Debian): `sudo apt install python3-tk`

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Authentication & Gmail Connection
Automelinium supports three seamless ways to connect to Gmail:

1. **Automatisch Docker (Zero-Config Default)**:
   If you run [Automatisch](https://github.com/automatisch/automatisch) locally (`automatisch-postgres-1` and `automatisch-main-1`), Automelinium automatically extracts, decrypts, and refreshes your Gmail OAuth tokens without manual copy-pasting.

2. **Environment Variables**:
   Set standard Google OAuth2 credentials in your environment or `.env`:
   ```bash
   export GMAIL_CLIENT_ID="your_google_client_id"
   export GMAIL_CLIENT_SECRET="your_google_client_secret"
   export GMAIL_REFRESH_TOKEN="your_google_refresh_token"
   ```

3. **Direct Config File**:
   Save your OAuth credentials to `~/.config/automelinium/gmail_credentials.json`:
   ```json
   {
     "clientId": "your_google_client_id",
     "clientSecret": "your_google_client_secret",
     "refreshToken": "your_google_refresh_token"
   }
   ```

---

## Usage

### Launch the GUI
```bash
python3 automelinium_gui.py
```

### Run Headless Verification & Live Scoring Check
```bash
python3 automelinium_gui.py --check
```

### Run Model Context Protocol (MCP) Server
```bash
# Verify MCP connection and tool registrations
python3 automelinium_mcp.py --check
python3 automelinium_mcp.py --list-tools

# Run as standard Stdio MCP server (for AI agents & MCP clients)
python3 automelinium_mcp.py
```

---

## License

This project is licensed under the [MIT License](LICENSE).



