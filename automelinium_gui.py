#!/usr/bin/env python3
"""
Automelinium Aero -- Sender-Specific Corporate Email Auto-Deleter & Smart Re-Populator.

Designed as a Sender-First Sorting & Auto-Deletion Console (NOT a generic email inbox)
in authentic 2008 Frutiger Aero art style:
  1. Left Column: Pick ONE specific corporate company (sorted by clutter volume or lowest value).
     Click "Auto-Delete ALL From This Company in Gmail" to immediately purge that company from your inbox.
  2. Center & Right Split Glass Buckets (for that specific company):
     - Left Bucket (Coral Glass):  AUTO-DELETE / SWEPT FROM SENDER (Low Use / Importance)
     - Right Bucket (Mint Glass):  RE-POPULATED / RESTORED BY USEFULNESS OR IMPORTANCE
  3. Dynamic Text & Logo Scaling:
     - Choose from 75% to 200% scale factor dynamically.
     - Fonts scale with Trebuchet MS 10pt (headings) and Tahoma 8pt (body) as baseline.
     - Company logos and glass orbs scale proportionally with pixel-crisp rendering.
  4. One-Click RFC 8058 / Mailto Unsubscribe directly from the GUI.
  5. Persistent Recall Memory & Learning Log tracking what you need vs what not to find on scan.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from collections import defaultdict
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Set

from aero_theme import (
    AERO_BG_CARD,
    AERO_BG_GLASS,
    AERO_BG_OUTER,
    AERO_BORDER_OUTER,
    AERO_TEXT_DARK,
    AERO_TEXT_MUTED,
    STATE_COLORS,
    AeroGelButton,
    make_glass_orb_image,
)
from company_branding import CompanyLogoManager, resolve_company_identity, get_automelinium_logo
from gmail_client import AutomatischGmailClient
from heuristics import EmailItem, score_emails
from recall_log import RecallLogManager

SETTINGS_DIR = os.path.expanduser("~/.config/automelinium")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")


def load_settings() -> dict:
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(new_data: dict):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    try:
        cur = load_settings()
        cur.update(new_data)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2)
    except Exception:
        pass


SENDER_SORT_MAP = {
    "Most Emails": "most_clutter",
    "Least Emails": "least_clutter",
    "Lowest Value": "lowest_score",
    "Highest Value": "highest_score",
    "Highest Usefulness": "highest_use",
    "Highest Importance": "highest_imp",
    "Company (A-Z)": "az",
    "Company (Z-A)": "za",
}
SENDER_SORT_REVERSE_MAP = {v: k for k, v in SENDER_SORT_MAP.items()}


class AutomeliniumGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Automelinium Aero -- Company-First Corporate Auto-Deleter & Smart Re-Populator")
        try:
            self._app_icon = get_automelinium_logo(64)
            self.root.iconphoto(True, self._app_icon)
        except Exception:
            pass

        if sys.platform == "darwin":
            try:
                import AppKit
                base_dir = os.path.dirname(os.path.abspath(__file__))
                for icon_name in ("Automelinium.icns", "automelinium_logo_centered.png", "automelinium_logo_256.png"):
                    icns_p = os.path.join(base_dir, "assets", icon_name)
                    if os.path.exists(icns_p):
                        ns_img = AppKit.NSImage.alloc().initWithContentsOfFile_(icns_p)
                        if ns_img:
                            AppKit.NSApplication.sharedApplication().setApplicationIconImage_(ns_img)
                            break
            except Exception:
                pass
        self.root.geometry("1460x900")
        self.root.minsize(1180, 720)
        self.root.configure(bg=AERO_BG_OUTER)

        self.settings = load_settings()
        self.client = AutomatischGmailClient()
        self.logo_mgr = CompanyLogoManager(self.client.ssl_ctx)
        self.recall_mgr = RecallLogManager()

        self.emails: List[EmailItem] = []
        self.item_by_id: Dict[str, EmailItem] = {}
        self.sender_groups: Dict[str, List[EmailItem]] = defaultdict(list)
        self.company_logo_domains: Dict[str, str] = {}
        self.company_raw_domains: Dict[str, Set[str]] = defaultdict(set)

        # Active specific company currently being sorted/purged
        self.active_sender: str = ""
        # Currently inspected email item in bottom inspector
        self.active_inspected_item: Optional[EmailItem] = None
        # Manual overrides per message: "restore" or "delete"
        self.manual_bucket_override: Dict[str, str] = {}
        # Track which messages have been actively synced to Gmail Trash vs Restored in this session
        self.gmail_deleted_ids: Set[str] = set()
        self.gmail_restored_ids: Set[str] = set()

        self.delete_bucket_items: List[EmailItem] = []
        self.restore_bucket_items: List[EmailItem] = []
        self.is_busy = False
        self.registered_buttons: List[AeroGelButton] = []

        # Scaling and persistent preferences
        saved_scale = self.settings.get("scale", "100%")
        self.scale_var = tk.StringVar(value=saved_scale)
        self.scale_factor = 1.0
        self._compute_fonts(self.get_scale_factor())

        # Tk Variables
        self.conn_status_var = tk.StringVar(value="Connecting to Automatisch Gmail API...")
        self.sender_search_var = tk.StringVar(value="")
        saved_sender_sort = self.settings.get("sender_sort", "most_clutter")
        self.sender_sort_var = tk.StringVar(value=saved_sender_sort)
        self.sender_sort_display_var = tk.StringVar(
            value=SENDER_SORT_REVERSE_MAP.get(saved_sender_sort, "Most Emails")
        )

        saved_bucket_sort = self.settings.get("bucket_sort", "Score (Highest First)")
        self.bucket_sort_var = tk.StringVar(value=saved_bucket_sort)
        self.max_results_var = tk.IntVar(value=10000)
        self.active_sender_banner_var = tk.StringVar(value="Select a Company on the Left")

        self.metric_mode_var = tk.StringVar(value="usefulness")  # "usefulness", "importance", "combined"
        self.threshold_var = tk.IntVar(value=50)
        self.threshold_label_var = tk.StringVar(value="Auto-Delete < 50  |  Re-Populate >= 50")

        self.delete_bucket_hdr_var = tk.StringVar(value="AUTO-DELETE BUCKET (0 Emails)")
        self.restore_bucket_hdr_var = tk.StringVar(value="RE-POPULATED / RESTORED BUCKET (0 Emails)")
        self.status_bar_var = tk.StringVar(
            value="Welcome. Pick a company on the left to sort, auto-delete, and re-populate by Usefulness or Importance."
        )

        # Automated Inbox Sending preference
        saved_auto_send = self.settings.get("auto_send_inbox", True)
        self.auto_send_inbox_var = tk.BooleanVar(value=saved_auto_send)

        # 3D Glossy Frutiger Aero Glass Orbs (pure pixel graphics, no emojis)
        self._rebuild_orbs(self.scale_factor)

        self._configure_styles()
        self._build_layout()
        self._setup_keyboard_shortcuts()
        self._schedule_auto_scan()

        # Bring window to front on macOS
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(400, lambda: self.root.attributes("-topmost", False))
        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'tell application "System Events" to set frontmost of the first process whose unix id is {os.getpid()} to true',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        self.root.after(150, self._initial_connect_and_fetch)

    # -------------------------------------------------------------------------
    # Typography & Logo Scaling Engine
    # -------------------------------------------------------------------------
    def get_scale_factor(self) -> float:
        val_str = self.scale_var.get().replace("%", "").strip()
        try:
            val = float(val_str)
            return max(0.65, min(2.5, val / 100.0))
        except Exception:
            return 1.0

    def _compute_fonts(self, scale: float):
        self.scale_factor = scale
        self.font_title = ("Trebuchet MS", max(8, int(round(12 * scale))), "bold")
        self.font_heading = ("Trebuchet MS", max(7, int(round(10 * scale))), "bold")
        self.font_body = ("Tahoma", max(6, int(round(8 * scale))))
        self.font_body_bold = ("Tahoma", max(6, int(round(8 * scale))), "bold")
        self.font_body_strike = ("Tahoma", max(6, int(round(8 * scale))), "overstrike")

    def _rebuild_orbs(self, scale: float):
        r16 = max(12, int(round(16 * scale)))
        r14 = max(10, int(round(14 * scale)))
        logo_sz = max(24, int(round(34 * scale)))
        self.orbs = {
            "logo": get_automelinium_logo(logo_sz),
            "selected": make_glass_orb_image(r16, (0, 120, 235), (120, 235, 255)),
            "restored": make_glass_orb_image(r16, (16, 185, 100), (167, 255, 140)),
            "to_delete": make_glass_orb_image(r16, (235, 55, 85), (255, 175, 190)),
            "deleted_gmail": make_glass_orb_image(r16, (148, 163, 184), (226, 232, 240)),
            "sender_spam": make_glass_orb_image(r14, (239, 68, 68), (252, 165, 165)),
            "sender_med": make_glass_orb_image(r14, (245, 158, 11), (253, 224, 71)),
            "sender_good": make_glass_orb_image(r14, (16, 185, 129), (110, 231, 183)),
        }

    def apply_scale(self):
        scale = self.get_scale_factor()
        self._compute_fonts(scale)
        self._rebuild_orbs(scale)
        save_settings({"scale": self.scale_var.get()})

        style = ttk.Style()
        style.configure("TLabel", font=self.font_body)
        style.configure("Card.TLabel", font=self.font_body)
        style.configure("CardBold.TLabel", font=self.font_body_bold)
        style.configure("Section.TLabel", font=self.font_heading)
        style.configure("TRadiobutton", font=self.font_body_bold)

        row_h = max(18, int(round(24 * scale)))
        style.configure(
            "Treeview",
            rowheight=row_h,
            font=self.font_body,
        )
        style.configure(
            "Treeview.Heading",
            font=self.font_heading,
        )

        # Update Treeview tags
        self.delete_tree.tag_configure("to_delete", font=self.font_body)
        self.delete_tree.tag_configure("deleted_gmail", font=self.font_body_strike)
        self.restore_tree.tag_configure("repopulated", font=self.font_body_bold)
        self.restore_tree.tag_configure("kept_inbox", font=self.font_body)

        # Update registered gel buttons
        for btn in self.registered_buttons:
            try:
                btn.configure(font=self.font_body_bold)
            except Exception:
                pass

        # Update specific labeled widgets
        if hasattr(self, "step1_title_lbl"):
            self.step1_title_lbl.configure(font=self.font_heading)
        if hasattr(self, "purge_title_lbl"):
            self.purge_title_lbl.configure(font=self.font_heading)
        if hasattr(self, "active_company_banner_lbl"):
            self.active_company_banner_lbl.configure(font=self.font_heading)
        if hasattr(self, "step2_title_lbl"):
            self.step2_title_lbl.configure(font=self.font_heading)
        if hasattr(self, "bucket_sort_lbl"):
            self.bucket_sort_lbl.configure(font=self.font_heading)
        if hasattr(self, "threshold_val_lbl"):
            self.threshold_val_lbl.configure(font=self.font_body_bold)
        if hasattr(self, "del_bucket_hdr_lbl"):
            self.del_bucket_hdr_lbl.configure(font=self.font_heading)
        if hasattr(self, "del_bucket_sub_lbl"):
            self.del_bucket_sub_lbl.configure(font=self.font_body_bold)
        if hasattr(self, "rest_bucket_hdr_lbl"):
            self.rest_bucket_hdr_lbl.configure(font=self.font_heading)
        if hasattr(self, "rest_bucket_sub_lbl"):
            self.rest_bucket_sub_lbl.configure(font=self.font_body_bold)
        if hasattr(self, "insp_bucket_badge"):
            self.insp_bucket_badge.configure(font=self.font_heading)
        if hasattr(self, "detail_text"):
            self.detail_text.configure(font=self.font_body)
        if hasattr(self, "status_bar_lbl"):
            self.status_bar_lbl.configure(font=self.font_body_bold)

        # Update top header logo, labels, and active company logo
        if hasattr(self, "hdr_logo_lbl"):
            self.hdr_logo_lbl.configure(image=self.orbs["logo"])
        if hasattr(self, "hdr_title_lbl"):
            self.hdr_title_lbl.configure(font=self.font_heading)
        if hasattr(self, "hdr_sub_lbl"):
            self.hdr_sub_lbl.configure(font=self.font_body)
        if hasattr(self, "conn_status_lbl"):
            self.conn_status_lbl.configure(font=self.font_body_bold)
        if hasattr(self, "auto_send_check"):
            self.auto_send_check.configure(font=self.font_body_bold)
        if hasattr(self, "scale_hdr_lbl"):
            self.scale_hdr_lbl.configure(font=self.font_body_bold)
        if hasattr(self, "scan_hdr_lbl"):
            self.scan_hdr_lbl.configure(font=self.font_body_bold)
        if hasattr(self, "status_hint_lbl"):
            self.status_hint_lbl.configure(font=self.font_body)
        if hasattr(self, "active_company_logo_lbl") and (not self.active_sender or self.active_sender == "__ALL__"):
            self.active_company_logo_lbl.configure(image=self.orbs["logo"])

        # Scale Treeview column widths
        self.sender_tree.column("#0", width=max(28, int(round(40 * scale))))
        self.sender_tree.column("domain", width=max(120, int(round(165 * scale))))
        self.sender_tree.column("count", width=max(40, int(round(52 * scale))))
        self.sender_tree.column("avg_use", width=max(44, int(round(56 * scale))))
        self.sender_tree.column("avg_imp", width=max(44, int(round(56 * scale))))

        for tree in (self.delete_tree, self.restore_tree):
            tree.column("#0", width=max(28, int(round(38 * scale))))
            tree.column("state", width=max(80, int(round(110 * scale))))
            tree.column("score", width=max(60, int(round(82 * scale))))
            tree.column("use_imp", width=max(60, int(round(82 * scale))))
            tree.column("subject", width=max(160, int(round(220 * scale))))
            tree.column("date", width=max(75, int(round(100 * scale))))

        # Re-populate trees and update logos with newly computed sizes
        if self.emails:
            self._populate_sender_list(auto_select_first=False)
            self._refresh_split_buckets()

        self.status_bar_var.set(f"UI scale updated to {int(round(scale * 100))}% (Headings: Trebuchet MS, Body: Tahoma).")

    def _configure_styles(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TFrame", background=AERO_BG_OUTER)
        style.configure("Card.TFrame", background=AERO_BG_CARD)
        style.configure("TLabel", background=AERO_BG_OUTER, foreground=AERO_TEXT_DARK, font=self.font_body)
        style.configure("Card.TLabel", background=AERO_BG_CARD, foreground=AERO_TEXT_DARK, font=self.font_body)
        style.configure("CardBold.TLabel", background=AERO_BG_CARD, foreground=AERO_TEXT_DARK, font=self.font_body_bold)
        style.configure("Section.TLabel", background=AERO_BG_CARD, foreground="#03406A", font=self.font_heading)
        style.configure("TRadiobutton", background=AERO_BG_CARD, foreground=AERO_TEXT_DARK, font=self.font_body_bold)

        row_h = max(18, int(round(24 * self.scale_factor)))
        style.configure(
            "Treeview",
            background="#F9FEFF",
            fieldbackground="#F9FEFF",
            foreground=AERO_TEXT_DARK,
            rowheight=row_h,
            font=self.font_body,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background="#B9E6FE",
            foreground="#03304D",
            font=self.font_heading,
            relief="raised",
            padding=(5, 4),
        )
        style.map(
            "Treeview",
            background=[("selected", "#0078D7")],
            foreground=[("selected", "#FFFFFF")],
        )

    def _make_aero_card(self, parent, border_col=AERO_BORDER_OUTER, bg_col=AERO_BG_CARD, padx=10, pady=8):
        outer = tk.Frame(parent, bg=border_col, padx=2, pady=2)
        mid = tk.Frame(outer, bg="#FFFFFF", padx=1, pady=1)
        mid.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(mid, bg=bg_col, padx=padx, pady=pady)
        inner.pack(fill=tk.BOTH, expand=True)
        return outer, inner

    def _create_gel_button(
        self,
        parent,
        text: str,
        command=None,
        style_name: str = "aero_blue",
        width: int = None,
        height: int = 26,
        bg_parent: str = AERO_BG_CARD,
    ) -> AeroGelButton:
        btn = AeroGelButton(
            parent,
            text=text,
            command=command,
            style_name=style_name,
            width=width,
            height=height,
            font=self.font_body_bold,
            bg_parent=bg_parent,
        )
        self.registered_buttons.append(btn)
        return btn

    def _build_layout(self):
        # ---------------------------------------------------------------------
        # 1. Streamlined Frutiger Aero Header Toolbar (No Bubble Canvas)
        # ---------------------------------------------------------------------
        self.top_header_frame = tk.Frame(self.root, bg="#032B45", padx=8, pady=4)
        self.top_header_frame.pack(fill=tk.X)

        hdr_card = tk.Frame(self.top_header_frame, bg="#0A4C73", padx=8, pady=4, relief="ridge", bd=1)
        hdr_card.pack(fill=tk.X)

        # Left: Logo, Title, and Connected Email Status
        hdr_left = tk.Frame(hdr_card, bg="#0A4C73")
        hdr_left.pack(side=tk.LEFT, fill=tk.Y)

        self.hdr_logo_lbl = tk.Label(hdr_left, image=self.orbs["logo"], bg="#0A4C73")
        self.hdr_logo_lbl.pack(side=tk.LEFT, padx=(2, 8))

        title_frame = tk.Frame(hdr_left, bg="#0A4C73")
        title_frame.pack(side=tk.LEFT, padx=(0, 10))

        self.hdr_title_lbl = tk.Label(
            title_frame,
            text="AUTOMELINIUM",
            bg="#0A4C73",
            fg="#FFFFFF",
            font=self.font_heading,
        )
        self.hdr_title_lbl.pack(anchor=tk.W)

        self.hdr_sub_lbl = tk.Label(
            title_frame,
            text="Company-First Auto-Deleter",
            bg="#0A4C73",
            fg="#BAE6FD",
            font=self.font_body,
        )
        self.hdr_sub_lbl.pack(anchor=tk.W)

        self.conn_status_lbl = tk.Label(
            hdr_left,
            textvariable=self.conn_status_var,
            bg="#02253B",
            fg="#7DD3FC",
            font=self.font_body_bold,
            padx=10,
            pady=4,
            relief="groove",
            bd=1,
        )
        self.conn_status_lbl.pack(side=tk.LEFT, padx=(4, 4))

        # Right: Auto-Send to Inbox, Scale, Scan Max, Scan Button, Help Keys (?)
        hdr_right = tk.Frame(hdr_card, bg="#0A4C73")
        hdr_right.pack(side=tk.RIGHT, fill=tk.Y)

        self.auto_send_check = tk.Checkbutton(
            hdr_right,
            text="Auto-Send to Inbox",
            variable=self.auto_send_inbox_var,
            command=self._on_auto_send_toggled,
            bg="#0A4C73",
            fg="#F0F9FF",
            activebackground="#0A4C73",
            activeforeground="#38BDF8",
            selectcolor="#02253B",
            font=self.font_body_bold,
            cursor="hand2",
            padx=4,
        )
        self.auto_send_check.pack(side=tk.LEFT, padx=(0, 10))

        # Scale Selector
        self.scale_hdr_lbl = tk.Label(
            hdr_right, text="Scale:", bg="#0A4C73", fg="#FFFFFF", font=self.font_body_bold
        )
        self.scale_hdr_lbl.pack(side=tk.LEFT, padx=(2, 2))
        self.scale_combo = ttk.Combobox(
            hdr_right,
            values=["75%", "90%", "100%", "110%", "125%", "150%", "175%", "200%"],
            textvariable=self.scale_var,
            width=6,
            state="readonly",
            font=self.font_body,
        )
        self.scale_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.scale_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_scale())

        # Scan Max Results
        self.scan_hdr_lbl = tk.Label(
            hdr_right, text="Scan:", bg="#0A4C73", fg="#FFFFFF", font=self.font_body_bold
        )
        self.scan_hdr_lbl.pack(side=tk.LEFT, padx=(2, 2))
        self.max_spin = ttk.Spinbox(
            hdr_right,
            from_=100,
            to=10000,
            increment=500,
            textvariable=self.max_results_var,
            width=6,
            font=self.font_body,
        )
        self.max_spin.pack(side=tk.LEFT, padx=(0, 8))

        self.fetch_btn = self._create_gel_button(
            hdr_right,
            text="Scan Inbox Senders",
            style_name="aero_blue",
            width=140,
            height=26,
            command=self.fetch_emails_async,
            bg_parent="#0A4C73",
        )
        self.fetch_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.help_keys_btn = self._create_gel_button(
            hdr_right,
            text="Keys (?)",
            style_name="aero_glass",
            width=68,
            height=26,
            command=self._show_shortcuts_dialog,
            bg_parent="#0A4C73",
        )
        self.help_keys_btn.pack(side=tk.LEFT)

        # ---------------------------------------------------------------------
        # 2. Main Split Layout:
        #    LEFT (Step 1: Pick Corporate Company & Purge/Unsubscribe)
        #    RIGHT (Step 2 & 3: Sort by Use/Importance & Split Buckets)
        # ---------------------------------------------------------------------
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 6))

        # =====================================================================
        # LEFT COLUMN: Step 1 -- Corporate Company Directory & 1-Click Purge
        # =====================================================================
        left_outer, left_col = self._make_aero_card(main_paned, border_col="#0284C7", bg_col=AERO_BG_CARD, padx=10, pady=8)
        main_paned.add(left_outer, weight=1)

        self.step1_title_lbl = tk.Label(
            left_col,
            text="STEP 1: PICK A COMPANY",
            bg="#0284C7",
            fg="#FFFFFF",
            font=self.font_heading,
            padx=8,
            pady=5,
            anchor=tk.W,
        )
        self.step1_title_lbl.pack(fill=tk.X, pady=(0, 6))

        search_row = tk.Frame(left_col, bg=AERO_BG_CARD)
        search_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(search_row, text="Find Company:", style="CardBold.TLabel").pack(side=tk.LEFT)
        s_entry = ttk.Entry(search_row, textvariable=self.sender_search_var, width=18, font=self.font_body)
        s_entry.pack(side=tk.LEFT, padx=(6, 4), fill=tk.X, expand=True)
        s_entry.bind("<KeyRelease>", lambda _e: self._populate_sender_list())
        self.sender_search_entry = s_entry
        self._create_gel_button(
            search_row,
            text="Clear",
            style_name="aero_glass",
            width=54,
            height=24,
            command=lambda: (self.sender_search_var.set(""), self._populate_sender_list()),
        ).pack(side=tk.LEFT)

        sort_row = tk.Frame(left_col, bg=AERO_BG_CARD)
        sort_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(sort_row, text="Sort:", style="CardBold.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        self.sender_sort_combo = ttk.Combobox(
            sort_row,
            values=[
                "Most Emails",
                "Least Emails",
                "Lowest Value",
                "Highest Value",
                "Highest Usefulness",
                "Highest Importance",
                "Company (A-Z)",
                "Company (Z-A)",
            ],
            textvariable=self.sender_sort_display_var,
            state="readonly",
            font=self.font_body,
        )
        self.sender_sort_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.sender_sort_combo.bind("<<ComboboxSelected>>", self._on_sender_sort_combo_selected)

        # Company Treeview with Real Scaled Company Logos
        s_tree_frame = tk.Frame(left_col, bg=AERO_BG_CARD)
        s_tree_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        s_cols = ("domain", "count", "avg_use", "avg_imp")
        self.sender_tree = ttk.Treeview(s_tree_frame, columns=s_cols, show="tree headings", selectmode="browse")
        self.sender_tree.heading("#0", text="Logo")
        self.sender_tree.column("#0", width=max(28, int(round(40 * self.scale_factor))), stretch=False, anchor=tk.CENTER)
        self.sender_tree.heading("domain", text="Company", command=lambda: self._on_sender_tree_column_clicked("domain"))
        self.sender_tree.heading("count", text="Emails", command=lambda: self._on_sender_tree_column_clicked("count"))
        self.sender_tree.heading("avg_use", text="Avg Use", command=lambda: self._on_sender_tree_column_clicked("avg_use"))
        self.sender_tree.heading("avg_imp", text="Avg Imp", command=lambda: self._on_sender_tree_column_clicked("avg_imp"))
        self.sender_tree.column("domain", width=max(120, int(round(165 * self.scale_factor))), anchor=tk.W, stretch=True)
        self.sender_tree.column("count", width=max(40, int(round(52 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.sender_tree.column("avg_use", width=max(44, int(round(56 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.sender_tree.column("avg_imp", width=max(44, int(round(56 * self.scale_factor))), anchor=tk.CENTER, stretch=False)

        s_scroll = ttk.Scrollbar(s_tree_frame, orient=tk.VERTICAL, command=self.sender_tree.yview)
        self.sender_tree.configure(yscrollcommand=s_scroll.set)
        self.sender_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        s_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.sender_tree.bind("<<TreeviewSelect>>", self._on_sender_selected)
        self.sender_tree.bind("<FocusIn>", lambda _e: setattr(self, "_last_focused_tree", self.sender_tree))

        # Company Purge & Unsubscribe Actions
        self.purge_title_lbl = tk.Label(
            left_col,
            text="Company Purge & Unsubscribe:",
            bg=AERO_BG_CARD,
            fg=AERO_TEXT_DARK,
            font=self.font_heading,
            anchor=tk.W,
        )
        self.purge_title_lbl.pack(fill=tk.X, pady=(0, 4))

        self.auto_del_comp_btn = self._create_gel_button(
            left_col,
            text="Auto-Delete ALL From This Company in Gmail",
            style_name="aero_coral",
            width=310,
            height=28,
            command=self.auto_delete_entire_sender_in_gmail,
        )
        self.auto_del_comp_btn.pack(fill=tk.X, pady=(0, 4))

        self.sync_buckets_btn = self._create_gel_button(
            left_col,
            text="Auto-Delete < Cutoff & Re-Populate >= Cutoff",
            style_name="aero_aqua",
            width=310,
            height=28,
            command=self.sync_sender_buckets_to_gmail,
        )
        self.sync_buckets_btn.pack(fill=tk.X, pady=(0, 4))

        self.unsub_comp_btn = self._create_gel_button(
            left_col,
            text="Unsubscribe from Active Company",
            style_name="aero_amber",
            width=310,
            height=28,
            command=self.unsubscribe_active_company,
        )
        self.unsub_comp_btn.pack(fill=tk.X, pady=(0, 4))

        self.demote_comp_btn = self._create_gel_button(
            left_col,
            text="Lessen Importance in Algorithms",
            style_name="aero_purple",
            width=310,
            height=28,
            command=self.toggle_active_company_lessen_importance,
        )
        self.demote_comp_btn.pack(fill=tk.X, pady=(0, 4))

        self.block_comp_btn = self._create_gel_button(
            left_col,
            text="Block in Gmail (Choose Rule)",
            style_name="aero_coral",
            width=310,
            height=28,
            command=self.open_company_block_rules_dialog,
        )
        self.block_comp_btn.pack(fill=tk.X)

        # =====================================================================
        # RIGHT WORKSPACE: Step 2 (Sort Controls) + Step 3 (Side-by-Side Split Buckets)
        # =====================================================================
        right_workspace = tk.Frame(main_paned, bg=AERO_BG_OUTER)
        main_paned.add(right_workspace, weight=3)

        # Top Control Card: Step 2 -- Sort & Auto-Partition Active Company by Usefulness or Importance
        ctrl_outer, ctrl_card = self._make_aero_card(right_workspace, border_col="#0284C7", bg_col=AERO_BG_CARD, padx=12, pady=8)
        ctrl_outer.pack(fill=tk.X, pady=(0, 6))

        banner_row = tk.Frame(ctrl_card, bg="#0369A1", padx=10, pady=5)
        banner_row.pack(fill=tk.X, pady=(0, 8))
        self.active_company_logo_lbl = tk.Label(
            banner_row,
            image=self.orbs["logo"],
            bg="#0369A1",
            bd=0,
        )
        self.active_company_logo_lbl.pack(side=tk.LEFT, padx=(0, 8))
        self.active_company_banner_lbl = tk.Label(
            banner_row,
            textvariable=self.active_sender_banner_var,
            bg="#0369A1",
            fg="#FFFFFF",
            font=self.font_heading,
        )
        self.active_company_banner_lbl.pack(side=tk.LEFT)

        self._create_gel_button(
            banner_row,
            text="View All Companies Combined",
            style_name="aero_glass",
            width=215,
            height=24,
            command=self._select_all_corporate_senders_mode,
            bg_parent="#0369A1",
        ).pack(side=tk.RIGHT)

        # Sorting Metric & Auto-Partition Slider Row
        sort_ctrl_row = tk.Frame(ctrl_card, bg=AERO_BG_CARD)
        sort_ctrl_row.pack(fill=tk.X, pady=(0, 6))

        self.step2_title_lbl = tk.Label(
            sort_ctrl_row,
            text="STEP 2 -- Sort & Re-Populate By:",
            bg=AERO_BG_CARD,
            fg="#03406A",
            font=self.font_heading,
        )
        self.step2_title_lbl.pack(side=tk.LEFT, padx=(0, 8))
        for label_txt, val in [
            ("Estimated Level of USE (Usefulness)", "usefulness"),
            ("Estimated Level of IMPORTANCE", "importance"),
            ("Combined Score", "combined"),
        ]:
            ttk.Radiobutton(
                sort_ctrl_row,
                text=label_txt,
                value=val,
                variable=self.metric_mode_var,
                command=self._on_sort_or_threshold_changed,
            ).pack(side=tk.LEFT, padx=(6, 6))

        slider_row = tk.Frame(ctrl_card, bg=AERO_BG_CARD)
        slider_row.pack(fill=tk.X)

        ttk.Label(slider_row, text="Auto-Partition Cutoff:", style="CardBold.TLabel").pack(side=tk.LEFT)
        self.threshold_scale = tk.Scale(
            slider_row,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.threshold_var,
            showvalue=False,
            length=200,
            bg=AERO_BG_CARD,
            highlightthickness=0,
            troughcolor="#7DD3FC",
            activebackground="#0284C7",
            command=lambda _v: self._on_sort_or_threshold_changed(),
        )
        self.threshold_scale.pack(side=tk.LEFT, padx=(8, 8))

        self.threshold_val_lbl = tk.Label(
            slider_row,
            textvariable=self.threshold_label_var,
            bg="#E0F2FE",
            fg="#03406A",
            font=self.font_body_bold,
            padx=8,
            pady=3,
            bd=1,
            relief="solid",
        )
        self.threshold_val_lbl.pack(side=tk.LEFT, padx=(0, 10))

        # Quick cutoff presets
        for preset_txt, cutoff_val in [
            ("Delete All (100)", 100),
            ("Keep Only High (>=65)", 65),
            ("Keep Useful/Imp (>=45)", 45),
            ("Keep Most (>=25)", 25),
            ("Re-Populate All (0)", 0),
        ]:
            self._create_gel_button(
                slider_row,
                text=preset_txt,
                style_name="aero_glass",
                height=24,
                command=lambda v=cutoff_val: self._set_cutoff_preset(v),
            ).pack(side=tk.LEFT, padx=2)

        # Email Sorting Row in Buckets
        email_sort_row = tk.Frame(ctrl_card, bg=AERO_BG_CARD)
        email_sort_row.pack(fill=tk.X, pady=(6, 2))

        self.bucket_sort_lbl = tk.Label(
            email_sort_row,
            text="Sort Emails In Buckets:",
            bg=AERO_BG_CARD,
            fg="#03406A",
            font=self.font_heading,
        )
        self.bucket_sort_lbl.pack(side=tk.LEFT, padx=(0, 6))

        self.bucket_sort_combo = ttk.Combobox(
            email_sort_row,
            values=[
                "Score (Highest First)",
                "Score (Lowest First)",
                "Date (Newest First)",
                "Date (Oldest First)",
                "Subject (A to Z)",
                "Subject (Z to A)",
                "Category (Fiscal / Critical First)",
                "Sender / Company (A to Z)",
            ],
            textvariable=self.bucket_sort_var,
            width=26,
            state="readonly",
            font=self.font_body,
        )
        self.bucket_sort_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.bucket_sort_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_bucket_sort_combo_selected())

        for s_title, s_mode in [
            ("Score", "Score (Highest First)"),
            ("Date", "Date (Newest First)"),
            ("Subject", "Subject (A to Z)"),
            ("Fiscal", "Category (Fiscal / Critical First)"),
        ]:
            self._create_gel_button(
                email_sort_row,
                text=s_title,
                style_name="aero_glass",
                height=22,
                command=lambda m=s_mode: self._set_bucket_sort(m),
            ).pack(side=tk.LEFT, padx=2)

        # ---------------------------------------------------------------------
        # STEP 3: Side-by-Side Split Buckets (Deleted/To-Delete vs Re-Populated/Restored)
        # ---------------------------------------------------------------------
        buckets_paned = ttk.PanedWindow(right_workspace, orient=tk.HORIZONTAL)
        buckets_paned.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # =====================================================================
        # BUCKET A (LEFT): AUTO-DELETE / SWEPT FROM SENDER (Coral/Amber Glass)
        # =====================================================================
        del_outer, del_card = self._make_aero_card(buckets_paned, border_col="#E11D48", bg_col="#FFF5F7", padx=8, pady=8)
        buckets_paned.add(del_outer, weight=1)

        del_hdr = tk.Frame(del_card, bg="#BE123C", padx=10, pady=5)
        del_hdr.pack(fill=tk.X, pady=(0, 6))
        self.del_bucket_hdr_lbl = tk.Label(
            del_hdr,
            textvariable=self.delete_bucket_hdr_var,
            bg="#BE123C",
            fg="#FFFFFF",
            font=self.font_heading,
        )
        self.del_bucket_hdr_lbl.pack(side=tk.LEFT)
        self.del_bucket_sub_lbl = tk.Label(
            del_hdr,
            text="Below Cutoff / Slated to Delete",
            bg="#BE123C",
            fg="#FFE4E6",
            font=self.font_body_bold,
        )
        self.del_bucket_sub_lbl.pack(side=tk.RIGHT)

        del_tree_box = tk.Frame(del_card, bg="#FFF5F7")
        del_tree_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        b_cols = ("state", "score", "use_imp", "subject", "date")
        self.delete_tree = ttk.Treeview(del_tree_box, columns=b_cols, show="tree headings", selectmode="extended")
        self.delete_tree.heading("#0", text="Logo")
        self.delete_tree.column("#0", width=max(28, int(round(38 * self.scale_factor))), stretch=False, anchor=tk.CENTER)
        self.delete_tree.heading("state", text="Bucket State", command=lambda: self._on_bucket_column_clicked("state"))
        self.delete_tree.heading("score", text="Active Score", command=lambda: self._on_bucket_column_clicked("score"))
        self.delete_tree.heading("use_imp", text="Use / Imp", command=lambda: self._on_bucket_column_clicked("use_imp"))
        self.delete_tree.heading("subject", text="Subject (Click to Inspect / Select)", command=lambda: self._on_bucket_column_clicked("subject"))
        self.delete_tree.heading("date", text="Date", command=lambda: self._on_bucket_column_clicked("date"))

        self.delete_tree.column("state", width=max(80, int(round(105 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.delete_tree.column("score", width=max(60, int(round(82 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.delete_tree.column("use_imp", width=max(60, int(round(82 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.delete_tree.column("subject", width=max(160, int(round(220 * self.scale_factor))), anchor=tk.W, stretch=True)
        self.delete_tree.column("date", width=max(75, int(round(100 * self.scale_factor))), anchor=tk.CENTER, stretch=False)

        self.delete_tree.tag_configure("to_delete", background="#FFE4E6", foreground="#881337", font=self.font_body)
        self.delete_tree.tag_configure("deleted_gmail", background="#E2E8F0", foreground="#475569", font=self.font_body_strike)

        del_scroll = ttk.Scrollbar(del_tree_box, orient=tk.VERTICAL, command=self.delete_tree.yview)
        self.delete_tree.configure(yscrollcommand=del_scroll.set)
        self.delete_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        del_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.delete_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_bucket_item_selected(self.delete_tree, "DELETE BUCKET"))
        self.delete_tree.bind("<FocusIn>", lambda _e: setattr(self, "_last_focused_tree", self.delete_tree))

        del_btn_bar = tk.Frame(del_card, bg="#FFF5F7")
        del_btn_bar.pack(fill=tk.X)
        self._create_gel_button(
            del_btn_bar,
            text="Delete This Red Bucket in Gmail Now",
            style_name="aero_coral",
            width=235,
            height=26,
            command=self.execute_delete_bucket_in_gmail,
            bg_parent="#FFF5F7",
        ).pack(side=tk.LEFT, padx=(0, 6))

        self._create_gel_button(
            del_btn_bar,
            text="Move Selected to Re-Populated >>",
            style_name="aero_emerald",
            width=220,
            height=26,
            command=self.move_selected_from_delete_to_restore,
            bg_parent="#FFF5F7",
        ).pack(side=tk.RIGHT)

        # =====================================================================
        # BUCKET B (RIGHT): RE-POPULATED / RESTORED BY USE OR IMPORTANCE (Mint-Emerald Glass)
        # =====================================================================
        rest_outer, rest_card = self._make_aero_card(buckets_paned, border_col="#059669", bg_col="#ECFDF5", padx=8, pady=8)
        buckets_paned.add(rest_outer, weight=1)

        rest_hdr = tk.Frame(rest_card, bg="#047857", padx=10, pady=5)
        rest_hdr.pack(fill=tk.X, pady=(0, 6))
        self.rest_bucket_hdr_lbl = tk.Label(
            rest_hdr,
            textvariable=self.restore_bucket_hdr_var,
            bg="#047857",
            fg="#FFFFFF",
            font=self.font_heading,
        )
        self.rest_bucket_hdr_lbl.pack(side=tk.LEFT)
        self.rest_bucket_sub_lbl = tk.Label(
            rest_hdr,
            text="Sorted Highest Use / Importance First",
            bg="#047857",
            fg="#A7F3D0",
            font=self.font_body_bold,
        )
        self.rest_bucket_sub_lbl.pack(side=tk.RIGHT)

        rest_tree_box = tk.Frame(rest_card, bg="#ECFDF5")
        rest_tree_box.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        self.restore_tree = ttk.Treeview(rest_tree_box, columns=b_cols, show="tree headings", selectmode="extended")
        self.restore_tree.heading("#0", text="Logo")
        self.restore_tree.column("#0", width=max(28, int(round(38 * self.scale_factor))), stretch=False, anchor=tk.CENTER)
        self.restore_tree.heading("state", text="Restored State", command=lambda: self._on_bucket_column_clicked("state"))
        self.restore_tree.heading("score", text="Active Score", command=lambda: self._on_bucket_column_clicked("score"))
        self.restore_tree.heading("use_imp", text="Use / Imp", command=lambda: self._on_bucket_column_clicked("use_imp"))
        self.restore_tree.heading("subject", text="Subject (Click to Inspect / Select)", command=lambda: self._on_bucket_column_clicked("subject"))
        self.restore_tree.heading("date", text="Date", command=lambda: self._on_bucket_column_clicked("date"))

        self.restore_tree.column("state", width=max(80, int(round(115 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.restore_tree.column("score", width=max(60, int(round(82 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.restore_tree.column("use_imp", width=max(60, int(round(82 * self.scale_factor))), anchor=tk.CENTER, stretch=False)
        self.restore_tree.column("subject", width=max(160, int(round(220 * self.scale_factor))), anchor=tk.W, stretch=True)
        self.restore_tree.column("date", width=max(75, int(round(100 * self.scale_factor))), anchor=tk.CENTER, stretch=False)

        self.restore_tree.tag_configure("repopulated", background="#A7F3D0", foreground="#022C22", font=self.font_body_bold)
        self.restore_tree.tag_configure("kept_inbox", background="#D1FAE5", foreground="#064E3B", font=self.font_body)

        rest_scroll = ttk.Scrollbar(rest_tree_box, orient=tk.VERTICAL, command=self.restore_tree.yview)
        self.restore_tree.configure(yscrollcommand=rest_scroll.set)
        self.restore_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        rest_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.restore_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_bucket_item_selected(self.restore_tree, "RE-POPULATED BUCKET"))
        self.restore_tree.bind("<FocusIn>", lambda _e: setattr(self, "_last_focused_tree", self.restore_tree))

        rest_btn_bar = tk.Frame(rest_card, bg="#ECFDF5")
        rest_btn_bar.pack(fill=tk.X)
        self._create_gel_button(
            rest_btn_bar,
            text="<< Send Selected to Auto-Delete",
            style_name="aero_amber",
            width=210,
            height=26,
            command=self.move_selected_from_restore_to_delete,
            bg_parent="#ECFDF5",
        ).pack(side=tk.LEFT, padx=(0, 6))

        self._create_gel_button(
            rest_btn_bar,
            text="Re-Populate This Green Bucket to Gmail Inbox",
            style_name="aero_emerald",
            width=268,
            height=26,
            command=self.execute_restore_bucket_in_gmail,
            bg_parent="#ECFDF5",
        ).pack(side=tk.RIGHT)

        # =====================================================================
        # BOTTOM COMPACT INSPECTOR: Gauges, Email Preview & Unsubscribe Panel
        # =====================================================================
        insp_outer, insp_card = self._make_aero_card(right_workspace, border_col=AERO_BORDER_OUTER, bg_col=AERO_BG_CARD, padx=10, pady=6)
        insp_outer.pack(fill=tk.X)

        insp_left = tk.Frame(insp_card, bg=AERO_BG_CARD)
        insp_left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        self.insp_bucket_badge = tk.Label(
            insp_left,
            text="CLICK ANY EMAIL TO INSPECT",
            bg="#0284C7",
            fg="#FFFFFF",
            font=self.font_heading,
            padx=10,
            pady=4,
            width=28,
        )
        self.insp_bucket_badge.pack(fill=tk.X, pady=(0, 4))

        self.gauge_canvas = tk.Canvas(
            insp_left, width=290, height=74, bg="#E6F6FF", highlightthickness=1, highlightbackground=AERO_BORDER_OUTER
        )
        self.gauge_canvas.pack()
        self._draw_score_gauges(50, 50, 50)

        self.detail_text = tk.Text(
            insp_card,
            wrap=tk.WORD,
            font=self.font_body,
            bg="#F0FAFF",
            fg=AERO_TEXT_DARK,
            relief="flat",
            padx=10,
            pady=6,
            height=5,
            highlightthickness=1,
            highlightbackground="#BAE6FD",
        )
        self.detail_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.detail_text.insert(
            "1.0",
            "HOW TO USE:\n"
            "1. Click any Company on the Left (e.g. GitHub, LinkedIn, E*TRADE, Handshake, Depop).\n"
            "2. Choose 'Estimated Level of USE' or 'Estimated Level of IMPORTANCE' and adjust the cutoff slider.\n"
            "3. Emails below the cutoff go to the Red Auto-Delete Bucket (left); valuable ones immediately re-populate into the Green Bucket (right).\n"
            "4. Use 'Unsubscribe from Sender' to trigger RFC 8058 One-Click HTTP POST or RFC 2369 mailto unsubscribe directly.",
        )
        self.detail_text.configure(state=tk.DISABLED)

        # Right Inspector Action Panel: Direct Unsubscribe
        insp_actions = tk.Frame(insp_card, bg=AERO_BG_CARD, padx=8)
        insp_actions.pack(side=tk.RIGHT, fill=tk.Y)

        self.unsub_btn = self._create_gel_button(
            insp_actions,
            text="Unsubscribe from Sender",
            style_name="aero_coral",
            width=180,
            height=28,
            command=self.unsubscribe_active_email,
            bg_parent=AERO_BG_CARD,
        )
        self.unsub_btn.pack(pady=(6, 3))

        self.demote_email_btn = self._create_gel_button(
            insp_actions,
            text="Lessen Email Importance",
            style_name="aero_slate",
            width=180,
            height=28,
            command=self.toggle_active_email_lessen_importance,
            bg_parent=AERO_BG_CARD,
        )
        self.demote_email_btn.pack(pady=(3, 3))

        self.block_email_btn = self._create_gel_button(
            insp_actions,
            text="Block Sender in Gmail",
            style_name="aero_coral",
            width=180,
            height=28,
            command=self.open_email_block_rules_dialog,
            bg_parent=AERO_BG_CARD,
        )
        self.block_email_btn.pack(pady=(3, 6))

        # Bottom Status Bar
        status_bar = tk.Frame(self.root, bg="#075985", padx=14, pady=5)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_bar_lbl = tk.Label(
            status_bar,
            textvariable=self.status_bar_var,
            bg="#075985",
            fg="#F0F9FF",
            font=self.font_body_bold,
        )
        self.status_bar_lbl.pack(side=tk.LEFT)

        self.status_hint_lbl = tk.Label(
            status_bar,
            text="Shortcuts: [j/k] Navigate  [e/y] Keep  [Del/#] Trash  [o] Sort  [s] Needed  [d] Demote  [b] Block  [u] Unsub  [r] Scan  [Cmd+Z] Undo  [?] Help",
            bg="#075985",
            fg="#BAE6FD",
            font=self.font_body,
        )
        self.status_hint_lbl.pack(side=tk.RIGHT)

        self._update_sender_tree_headers()
        self._update_bucket_tree_headers()

    def _draw_score_gauges(self, usefulness: int, importance: int, combined: int):
        c = self.gauge_canvas
        c.delete("all")
        bars = [
            ("Usefulness", usefulness, "#0D9488", "#2DD4BF", "#99F6E4", 6),
            ("Importance", importance, "#059669", "#34D399", "#A7F3D0", 29),
            ("Combined", combined, "#0284C7", "#38BDF8", "#BAE6FD", 52),
        ]
        bar_x0 = 84
        bar_w = 148
        for label, val, dark_col, mid_col, spec_col, y in bars:
            c.create_text(6, y + 7, anchor=tk.W, text=label, fill=AERO_TEXT_DARK, font=self.font_body_bold)
            c.create_rectangle(bar_x0, y, bar_x0 + bar_w, y + 14, fill="#CBEAFE", outline="#7DD3FC")
            fill_w = int(bar_w * max(0, min(100, val)) / 100.0)
            if fill_w > 2:
                c.create_rectangle(bar_x0 + 1, y + 1, bar_x0 + fill_w, y + 13, fill=mid_col, outline=dark_col)
                c.create_rectangle(bar_x0 + 2, y + 2, bar_x0 + fill_w - 1, y + 6, fill=spec_col, outline="")
            c.create_text(bar_x0 + bar_w + 6, y + 7, anchor=tk.W, text=f"{val}/100", fill=AERO_TEXT_DARK, font=self.font_body_bold)

    # -------------------------------------------------------------------------
    # Connection & Fetching
    # -------------------------------------------------------------------------
    def _set_busy(self, busy: bool, msg: str = ""):
        self.is_busy = busy
        if msg:
            self.status_bar_var.set(msg)
        state = tk.DISABLED if busy else tk.NORMAL
        self.fetch_btn.configure(state=state)
        self.root.update_idletasks()

    def _initial_connect_and_fetch(self):
        def worker():
            try:
                self.root.after(0, lambda: self._set_busy(True, "Connecting to Automatisch Gmail API..."))
                email_addr, _scope = self.client.connect_and_verify()
                self.root.after(0, lambda: self.conn_status_var.set(f"Connected: {email_addr}"))
                self.root.after(0, self.fetch_emails_async)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: self.conn_status_var.set("Connection Error"))
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Error connecting to Gmail API: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def fetch_emails_async(self):
        try:
            raw_val = int(self.max_results_var.get())
        except Exception:
            raw_val = 10000
        max_res = max(1, min(10000, raw_val))

        def worker():
            try:
                self.root.after(
                    0,
                    lambda: self._set_busy(
                        True,
                        f"Scanning up to {max_res} emails from Gmail...",
                    ),
                )
                items = self.client.fetch_emails(
                    source_scope="inbox",
                    target_senders=None,
                    corporate_only=True,
                    max_results=max_res,
                    skip_unwanted_recall=True,
                    progress_cb=lambda m: self.root.after(0, lambda msg=m: self.status_bar_var.set(msg)),
                )
                self.emails = items
                self.item_by_id = {it.msg_id: it for it in items}
                self.root.after(0, self._rebuild_sender_directory)
                self.root.after(
                    0,
                    lambda: self._set_busy(
                        False,
                        f"Scanned {len(items)} corporate emails across {len(self.sender_groups)} companies. Pick any company on the left.",
                    ),
                )
                if self.auto_send_inbox_var.get():
                    self.root.after(600, lambda: self.auto_send_qualifying_to_inbox(silent=True))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Failed to scan emails: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # Step 1: Corporate Company Directory, Logos & Selection
    # -------------------------------------------------------------------------
    def _rebuild_sender_directory(self):
        self.sender_groups.clear()
        self.company_logo_domains.clear()
        self.company_raw_domains.clear()
        all_logo_doms: Set[str] = set()

        for it in self.emails:
            if not it.company_name:
                it.company_name, it.logo_domain = resolve_company_identity(
                    it.sender_domain, it.sender_name, it.sender_email
                )
            comp = it.company_name
            self.sender_groups[comp].append(it)
            if it.logo_domain and comp not in self.company_logo_domains:
                self.company_logo_domains[comp] = it.logo_domain
            if it.sender_domain:
                self.company_raw_domains[comp].add(it.sender_domain)
            if it.logo_domain:
                all_logo_doms.add(it.logo_domain)

        self._populate_sender_list(auto_select_first=True)
        self.logo_mgr.prefetch_domains_async(
            all_logo_doms,
            on_batch_complete_main_thread=lambda: self.root.after(0, self._refresh_company_logos_in_tree),
        )

    def _refresh_company_logos_in_tree(self):
        """Update Treeview row icons and active company banner logo once background downloads complete."""
        scale = self.get_scale_factor()
        tree_sz = max(12, int(round(16 * scale)))
        banner_sz = max(20, int(round(32 * scale)))

        for iid in self.sender_tree.get_children():
            comp = iid.replace("sender::", "")
            logo_dom = self.company_logo_domains.get(comp, "")
            img_sz = self.logo_mgr.get_logo(comp, logo_dom, size=tree_sz)
            self.sender_tree.item(iid, image=img_sz)

        if self.active_sender and self.active_sender != "__ALL__":
            logo_dom = self.company_logo_domains.get(self.active_sender, "")
            img_b = self.logo_mgr.get_logo(self.active_sender, logo_dom, size=banner_sz)
            self.active_company_logo_lbl.configure(image=img_b)

    def _populate_sender_list(self, auto_select_first: bool = False):
        self.sender_tree.delete(*self.sender_tree.get_children())
        query = self.sender_search_var.get().strip().lower()
        sort_mode = self.sender_sort_var.get()
        scale = self.get_scale_factor()
        tree_sz = max(12, int(round(16 * scale)))

        entries = []
        for comp, grp in self.sender_groups.items():
            if query:
                raw_doms = self.company_raw_domains.get(comp, set())
                if (
                    query not in comp.lower()
                    and not any(query in d.lower() for d in raw_doms)
                    and not any(query in x.sender_name.lower() for x in grp)
                ):
                    continue
            avg_use = sum(x.usefulness_score for x in grp) / len(grp)
            avg_imp = sum(x.importance_score for x in grp) / len(grp)
            avg_comb = sum(x.combined_score for x in grp) / len(grp)
            entries.append((comp, grp, avg_use, avg_imp, avg_comb))

        if sort_mode == "most_clutter":
            entries.sort(key=lambda x: (len(x[1]), -x[4]), reverse=True)
        elif sort_mode == "least_clutter":
            entries.sort(key=lambda x: (len(x[1]), x[4]), reverse=False)
        elif sort_mode == "lowest_score":
            entries.sort(key=lambda x: (x[4], -len(x[1])), reverse=False)
        elif sort_mode == "highest_score":
            entries.sort(key=lambda x: (x[4], len(x[1])), reverse=True)
        elif sort_mode == "highest_use":
            entries.sort(key=lambda x: (x[2], x[3]), reverse=True)
        elif sort_mode == "lowest_use":
            entries.sort(key=lambda x: (x[2], x[3]), reverse=False)
        elif sort_mode == "highest_imp":
            entries.sort(key=lambda x: (x[3], x[2]), reverse=True)
        elif sort_mode == "lowest_imp":
            entries.sort(key=lambda x: (x[3], x[2]), reverse=False)
        elif sort_mode == "za":
            entries.sort(key=lambda x: x[0].lower(), reverse=True)
        else:  # "az"
            entries.sort(key=lambda x: x[0].lower(), reverse=False)

        for comp, grp, avg_use, avg_imp, avg_comb in entries:
            logo_dom = self.company_logo_domains.get(comp, "")
            logo_img = self.logo_mgr.get_logo(comp, logo_dom, size=tree_sz)
            self.sender_tree.insert(
                "",
                tk.END,
                iid=f"sender::{comp}",
                image=logo_img,
                values=(comp, len(grp), f"{int(round(avg_use))}", f"{int(round(avg_imp))}"),
            )

        if auto_select_first and entries:
            target_comp = self.active_sender if self.active_sender in self.sender_groups else entries[0][0]
            iid = f"sender::{target_comp}"
            if self.sender_tree.exists(iid):
                self.sender_tree.selection_set(iid)
                self.sender_tree.see(iid)
                self._set_active_sender(target_comp)

    def _update_sender_tree_headers(self):
        sort_mode = self.sender_sort_var.get()
        dom_txt = "Company"
        cnt_txt = "Emails"
        use_txt = "Avg Use"
        imp_txt = "Avg Imp"

        if sort_mode == "most_clutter":
            cnt_txt = "Emails [v]"
        elif sort_mode == "least_clutter":
            cnt_txt = "Emails [^]"
        elif sort_mode == "lowest_score":
            dom_txt = "Company [Lowest Val]"
        elif sort_mode == "highest_score":
            dom_txt = "Company [Highest Val]"
        elif sort_mode == "highest_use":
            use_txt = "Avg Use [v]"
        elif sort_mode == "lowest_use":
            use_txt = "Avg Use [^]"
        elif sort_mode == "highest_imp":
            imp_txt = "Avg Imp [v]"
        elif sort_mode == "lowest_imp":
            imp_txt = "Avg Imp [^]"
        elif sort_mode == "az":
            dom_txt = "Company [A-Z]"
        elif sort_mode == "za":
            dom_txt = "Company [Z-A]"

        if hasattr(self, "sender_tree"):
            self.sender_tree.heading("domain", text=dom_txt)
            self.sender_tree.heading("count", text=cnt_txt)
            self.sender_tree.heading("avg_use", text=use_txt)
            self.sender_tree.heading("avg_imp", text=imp_txt)

    def _on_sender_tree_column_clicked(self, col: str):
        cur = self.sender_sort_var.get()
        if col == "domain":
            new_mode = "za" if cur == "az" else "az"
        elif col == "count":
            new_mode = "least_clutter" if cur == "most_clutter" else "most_clutter"
        elif col == "avg_use":
            new_mode = "lowest_use" if cur == "highest_use" else "highest_use"
        elif col == "avg_imp":
            new_mode = "lowest_imp" if cur == "highest_imp" else "highest_imp"
        else:
            new_mode = "most_clutter"
        self._set_sender_sort(new_mode)

    def _set_sender_sort(self, mode: str):
        self.sender_sort_var.set(mode)
        disp = SENDER_SORT_REVERSE_MAP.get(mode, "Most Emails")
        self.sender_sort_display_var.set(disp)
        save_settings({"sender_sort": mode})
        self._update_sender_tree_headers()
        self._populate_sender_list(auto_select_first=False)
        self.status_bar_var.set(f"Sorted corporate companies by: {disp}.")

    def _on_sender_sort_combo_selected(self, _e=None):
        disp = self.sender_sort_display_var.get()
        mode = SENDER_SORT_MAP.get(disp, "most_clutter")
        self._set_sender_sort(mode)

    def _on_sender_selected(self, _event=None):
        sel = self.sender_tree.selection()
        if not sel:
            return
        comp = sel[0].replace("sender::", "")
        self._set_active_sender(comp)

    def _select_all_corporate_senders_mode(self):
        self.sender_tree.selection_remove(self.sender_tree.selection())
        self._set_active_sender("__ALL__")

    def _set_active_sender(self, company_name: str):
        self.active_sender = company_name
        if hasattr(self, "demote_comp_btn"):
            if company_name and company_name != "__ALL__":
                is_lessened = self.recall_mgr.is_company_importance_lessened(company_name)
                if is_lessened:
                    self.demote_comp_btn.configure(
                        text="Importance Lessened in Algorithms (-40)",
                        style_name="aero_amber",
                    )
                else:
                    self.demote_comp_btn.configure(
                        text="Lessen Importance in Algorithms",
                        style_name="aero_purple",
                    )
            else:
                self.demote_comp_btn.configure(
                    text="Lessen Importance in Algorithms",
                    style_name="aero_purple",
                )
        if hasattr(self, "block_comp_btn"):
            if company_name and company_name != "__ALL__":
                active_rule = self.recall_mgr.get_rule_for_target(company_name)
                if active_rule:
                    self.block_comp_btn.configure(
                        text=f"Blocked in Gmail ({active_rule.upper()})",
                        style_name="aero_slate",
                    )
                else:
                    self.block_comp_btn.configure(
                        text="Block in Gmail (Choose Rule)",
                        style_name="aero_coral",
                    )
            else:
                self.block_comp_btn.configure(
                    text="Block in Gmail (Choose Rule)",
                    style_name="aero_coral",
                )
        self._refresh_split_buckets()

    # -------------------------------------------------------------------------
    # Step 2 & 3: Sorting by Usefulness / Importance into Side-by-Side Split Buckets
    # -------------------------------------------------------------------------
    def _get_active_metric(self, item: EmailItem) -> int:
        mode = self.metric_mode_var.get()
        if mode == "usefulness":
            return item.usefulness_score
        if mode == "importance":
            return item.importance_score
        return item.combined_score

    def _get_active_sender_pool(self) -> List[EmailItem]:
        if not self.active_sender or self.active_sender == "__ALL__":
            return list(self.emails)
        return list(self.sender_groups.get(self.active_sender, []))

    def _set_cutoff_preset(self, cutoff_val: int):
        for it in self._get_active_sender_pool():
            self.manual_bucket_override.pop(it.msg_id, None)
        self.threshold_var.set(cutoff_val)
        self._refresh_split_buckets()
        metric_name = self.metric_mode_var.get().upper()
        self.status_bar_var.set(
            f"Sorted '{self.active_sender}' by {metric_name} (Cutoff {cutoff_val}): "
            f"{len(self.delete_bucket_items)} in Auto-Delete Bucket  |  {len(self.restore_bucket_items)} in Re-Populated Bucket."
        )

    def _on_sort_or_threshold_changed(self):
        self._refresh_split_buckets()

    def _refresh_split_buckets(self):
        pool = self._get_active_sender_pool()
        thresh = self.threshold_var.get()
        metric_mode = self.metric_mode_var.get()
        metric_label = {
            "usefulness": "Usefulness",
            "importance": "Importance",
            "combined": "Combined",
        }.get(metric_mode, "Score")

        self.threshold_label_var.set(f"Auto-Delete < {thresh}  |  Re-Populate ({metric_label}) >= {thresh}")

        scale = self.get_scale_factor()
        tree_sz = max(12, int(round(16 * scale)))
        banner_sz = max(20, int(round(32 * scale)))

        if self.active_sender == "__ALL__":
            self.active_company_logo_lbl.configure(image=self.orbs["logo"])
            self.active_sender_banner_var.set(f"Active Target: ALL COMPANIES ({len(pool)} emails)")
        elif self.active_sender:
            logo_dom = self.company_logo_domains.get(self.active_sender, "")
            img_b = self.logo_mgr.get_logo(self.active_sender, logo_dom, size=banner_sz)
            self.active_company_logo_lbl.configure(image=img_b)
            raw_doms = sorted(self.company_raw_domains.get(self.active_sender, set()))
            dom_summary = ", ".join(raw_doms[:3]) + (f" +{len(raw_doms) - 3} more" if len(raw_doms) > 3 else "")
            self.active_sender_banner_var.set(
                f"Active Company: {self.active_sender} ({len(pool)} emails)   |   Addresses: {dom_summary}"
            )
        else:
            self.active_company_logo_lbl.configure(image=self.orbs["logo"])
            self.active_sender_banner_var.set("Select a Company on the Left")

        del_list: List[EmailItem] = []
        rest_list: List[EmailItem] = []

        for it in pool:
            override = self.manual_bucket_override.get(it.msg_id)
            if override == "delete":
                del_list.append(it)
            elif override == "restore":
                rest_list.append(it)
            else:
                if self._get_active_metric(it) >= thresh:
                    rest_list.append(it)
                else:
                    del_list.append(it)

        del_list = self._sort_bucket_email_list(del_list)
        rest_list = self._sort_bucket_email_list(rest_list)

        self.delete_bucket_items = del_list
        self.restore_bucket_items = rest_list

        self.delete_bucket_hdr_var.set(f"AUTO-DELETE BUCKET ({len(del_list)} Emails)")
        self.restore_bucket_hdr_var.set(f"RE-POPULATED BY {metric_label.upper()} ({len(rest_list)} Emails)")

        # Populate Left Bucket (Auto-Delete)
        self.delete_tree.delete(*self.delete_tree.get_children())
        for it in del_list:
            act_sc = self._get_active_metric(it)
            is_synced_del = (it.msg_id in self.gmail_deleted_ids) or (it.status in ("Trashed", "Quarantined"))
            state_txt = "DELETED" if is_synced_del else "TO DELETE"
            row_img = (
                self.orbs["deleted_gmail"]
                if is_synced_del
                else self.logo_mgr.get_logo(it.company_name, it.logo_domain, size=tree_sz)
            )
            tag = "deleted_gmail" if is_synced_del else "to_delete"
            subj_prefix = f"[{it.company_name}] " if self.active_sender == "__ALL__" else ""

            self.delete_tree.insert(
                "",
                tk.END,
                iid=it.msg_id,
                image=row_img,
                values=(
                    state_txt,
                    f"{act_sc}/100",
                    f"U:{it.usefulness_score} | I:{it.importance_score}",
                    f"{subj_prefix}{it.subject}",
                    it.date_str,
                ),
                tags=(tag,),
            )

        # Populate Right Bucket (Re-Populated / Restored by Usefulness or Importance)
        self.restore_tree.delete(*self.restore_tree.get_children())
        for it in rest_list:
            act_sc = self._get_active_metric(it)
            is_synced_rest = (it.msg_id in self.gmail_restored_ids) or (it.status == "Restored")
            if is_synced_rest:
                state_txt = "RE-POPULATED"
            elif it.category == "Fiscal Transactions":
                state_txt = f"FISCAL (KEEP {act_sc})"
            else:
                state_txt = f"KEEP ({metric_label[:3]} {act_sc})"
            subj_prefix = f"[{it.company_name}] " if self.active_sender == "__ALL__" else ""
            row_img = self.logo_mgr.get_logo(it.company_name, it.logo_domain, size=tree_sz)

            self.restore_tree.insert(
                "",
                tk.END,
                iid=it.msg_id,
                image=row_img,
                values=(
                    state_txt,
                    f"{act_sc}/100",
                    f"U:{it.usefulness_score} | I:{it.importance_score}",
                    f"{subj_prefix}{it.subject}",
                    it.date_str,
                ),
                tags=("repopulated" if is_synced_rest else "kept_inbox",),
            )

    # -------------------------------------------------------------------------
    # Split Bucket Email Sorting Engine & Column Header Interactions
    # -------------------------------------------------------------------------
    def _sort_bucket_email_list(self, items: List[EmailItem]) -> List[EmailItem]:
        sort_mode = self.bucket_sort_var.get()
        res = list(items)
        if sort_mode == "Score (Lowest First)":
            res.sort(key=lambda x: (self._get_active_metric(x), -x.timestamp), reverse=False)
        elif sort_mode == "Date (Newest First)":
            res.sort(key=lambda x: x.timestamp, reverse=True)
        elif sort_mode == "Date (Oldest First)":
            res.sort(key=lambda x: x.timestamp, reverse=False)
        elif sort_mode == "Subject (A to Z)":
            res.sort(key=lambda x: ((x.subject or "").lower(), -x.timestamp), reverse=False)
        elif sort_mode == "Subject (Z to A)":
            res.sort(key=lambda x: ((x.subject or "").lower(), -x.timestamp), reverse=True)
        elif sort_mode == "Category (Fiscal / Critical First)":
            def cat_rank(x: EmailItem) -> int:
                if x.category == "Fiscal Transactions":
                    return 4
                if "Security" in x.category:
                    return 3
                if "Meetings" in x.category:
                    return 2
                if "Career" in x.category or "Logistics" in x.category:
                    return 1
                return 0
            res.sort(key=lambda x: (cat_rank(x), self._get_active_metric(x), x.timestamp), reverse=True)
        elif sort_mode == "Sender / Company (A to Z)":
            res.sort(key=lambda x: (((x.company_name or x.sender_domain or "").lower()), -self._get_active_metric(x)), reverse=False)
        elif sort_mode == "State (Action State)":
            res.sort(key=lambda x: ((x.status or ""), -self._get_active_metric(x)), reverse=False)
        elif sort_mode == "Usefulness (Highest First)":
            res.sort(key=lambda x: (x.usefulness_score, x.timestamp), reverse=True)
        elif sort_mode == "Importance (Highest First)":
            res.sort(key=lambda x: (x.importance_score, x.timestamp), reverse=True)
        else:
            # Default: Score (Highest First)
            res.sort(key=lambda x: (self._get_active_metric(x), x.timestamp), reverse=True)
        return res

    def _update_bucket_tree_headers(self):
        sort_mode = self.bucket_sort_var.get()
        del_state_txt = "Bucket State"
        rest_state_txt = "Restored State"
        score_txt = "Active Score"
        use_imp_txt = "Use / Imp"
        subj_txt = "Subject (Click to Inspect / Select)"
        date_txt = "Date"

        if sort_mode == "Score (Highest First)":
            score_txt = "Active Score [v]"
        elif sort_mode == "Score (Lowest First)":
            score_txt = "Active Score [^]"
        elif sort_mode == "Date (Newest First)":
            date_txt = "Date [v]"
        elif sort_mode == "Date (Oldest First)":
            date_txt = "Date [^]"
        elif sort_mode == "Subject (A to Z)":
            subj_txt = "Subject [A-Z]"
        elif sort_mode == "Subject (Z to A)":
            subj_txt = "Subject [Z-A]"
        elif sort_mode == "Category (Fiscal / Critical First)":
            score_txt = "Score [Fiscal 1st]"
        elif sort_mode == "Sender / Company (A to Z)":
            subj_txt = "Subject [Company A-Z]"
        elif sort_mode == "State (Action State)":
            del_state_txt = "State [v]"
            rest_state_txt = "State [v]"
        elif sort_mode == "Usefulness (Highest First)":
            use_imp_txt = "Use [v] / Imp"
        elif sort_mode == "Importance (Highest First)":
            use_imp_txt = "Use / Imp [v]"

        if hasattr(self, "delete_tree"):
            self.delete_tree.heading("state", text=del_state_txt)
            self.delete_tree.heading("score", text=score_txt)
            self.delete_tree.heading("use_imp", text=use_imp_txt)
            self.delete_tree.heading("subject", text=subj_txt)
            self.delete_tree.heading("date", text=date_txt)
        if hasattr(self, "restore_tree"):
            self.restore_tree.heading("state", text=rest_state_txt)
            self.restore_tree.heading("score", text=score_txt)
            self.restore_tree.heading("use_imp", text=use_imp_txt)
            self.restore_tree.heading("subject", text=subj_txt)
            self.restore_tree.heading("date", text=date_txt)

    def _on_bucket_column_clicked(self, col: str):
        cur = self.bucket_sort_var.get()
        if col == "score":
            new_mode = "Score (Lowest First)" if cur == "Score (Highest First)" else "Score (Highest First)"
        elif col == "date":
            new_mode = "Date (Oldest First)" if cur == "Date (Newest First)" else "Date (Newest First)"
        elif col == "subject":
            new_mode = "Subject (Z to A)" if cur == "Subject (A to Z)" else "Subject (A to Z)"
        elif col == "state":
            new_mode = "State (Action State)"
        elif col == "use_imp":
            cur_metric = self.metric_mode_var.get()
            next_metric = "importance" if cur_metric == "usefulness" else "usefulness"
            self.metric_mode_var.set(next_metric)
            self._on_sort_or_threshold_changed()
            self.status_bar_var.set(f"Switched active metric to {next_metric.capitalize()}.")
            return
        else:
            new_mode = "Score (Highest First)"
        self._set_bucket_sort(new_mode)

    def _set_bucket_sort(self, sort_name: str):
        self.bucket_sort_var.set(sort_name)
        save_settings({"bucket_sort": sort_name})
        self._update_bucket_tree_headers()
        self._refresh_split_buckets()
        self.status_bar_var.set(f"Sorted split buckets by: {sort_name}.")

    def _on_bucket_sort_combo_selected(self, _e=None):
        val = self.bucket_sort_var.get()
        self._set_bucket_sort(val)

    def _shortcut_cycle_sort(self, event=None):
        if self._is_text_entry_focused():
            return None
        cycle = [
            "Score (Highest First)",
            "Date (Newest First)",
            "Category (Fiscal / Critical First)",
            "Subject (A to Z)",
            "Score (Lowest First)",
        ]
        cur = self.bucket_sort_var.get()
        try:
            idx = cycle.index(cur)
            next_sort = cycle[(idx + 1) % len(cycle)]
        except ValueError:
            next_sort = cycle[0]
        self._set_bucket_sort(next_sort)
        return "break"

    # -------------------------------------------------------------------------
    # Manual Bucket Transfers & Inspector Selection
    # -------------------------------------------------------------------------
    def move_selected_from_delete_to_restore(self):
        sel_ids = list(self.delete_tree.selection())
        if not sel_ids:
            messagebox.showinfo(
                "Select Emails",
                "Select one or more emails in the red Auto-Delete bucket on the left to move to the green Re-Populated bucket.",
            )
            return
        for mid in sel_ids:
            self.manual_bucket_override[mid] = "restore"
        self._refresh_split_buckets()
        self.status_bar_var.set(f"Moved {len(sel_ids)} email(s) to the Green Re-Populated Bucket.")
        if self.auto_send_inbox_var.get():
            self.auto_send_qualifying_to_inbox(silent=True)

    def move_selected_from_restore_to_delete(self):
        sel_ids = list(self.restore_tree.selection())
        if not sel_ids:
            messagebox.showinfo(
                "Select Emails",
                "Select one or more emails in the green Re-Populated bucket on the right to send to the red Auto-Delete bucket.",
            )
            return
        for mid in sel_ids:
            self.manual_bucket_override[mid] = "delete"
        self._refresh_split_buckets()
        self.status_bar_var.set(f"Moved {len(sel_ids)} email(s) to the Red Auto-Delete Bucket.")

    def _on_bucket_item_selected(self, tree_widget: ttk.Treeview, bucket_name: str):
        sel = tree_widget.selection()
        if not sel:
            return
        it = self.item_by_id.get(sel[0])
        if not it:
            return

        self.active_inspected_item = it

        if "DELETE" in bucket_name:
            self.insp_bucket_badge.configure(
                text=f"IN AUTO-DELETE BUCKET ({len(sel)} selected)",
                bg="#BE123C",
                fg="#FFFFFF",
            )
        else:
            self.insp_bucket_badge.configure(
                text=f"IN RE-POPULATED BUCKET ({len(sel)} selected)",
                bg="#047857",
                fg="#FFFFFF",
            )

        self._draw_score_gauges(it.usefulness_score, it.importance_score, it.combined_score)

        if hasattr(self, "demote_email_btn"):
            snap = self.recall_mgr.get_snapshot()
            is_demoted = it.msg_id in getattr(snap, "demoted_msg_ids", set())
            if is_demoted:
                self.demote_email_btn.configure(
                    text="Email Demoted (-45 Imp)",
                    style_name="aero_amber",
                )
            else:
                self.demote_email_btn.configure(
                    text="Lessen Email Importance",
                    style_name="aero_slate",
                )

        if hasattr(self, "block_email_btn"):
            active_rule = (
                self.recall_mgr.get_rule_for_target(it.sender_email)
                or self.recall_mgr.get_rule_for_target(it.sender_domain)
                or self.recall_mgr.get_rule_for_target(it.company_name)
            )
            if active_rule:
                self.block_email_btn.configure(
                    text=f"Blocked in Gmail ({active_rule.upper()})",
                    style_name="aero_slate",
                )
            else:
                self.block_email_btn.configure(
                    text="Block Sender in Gmail",
                    style_name="aero_coral",
                )

        reasons_str = "  |  ".join(it.reasons)
        unsub_status = "Available (RFC 8058/2369)" if (it.has_unsubscribe or it.unsubscribe_header) else "Not Specified"
        detail = (
            f"Company: {it.company_name}   |   From Address: {it.sender}   |   Category: {it.category}   |   Date: {it.date_str}\n"
            f"Subject: {it.subject}\n"
            f"Why Scored (Usefulness {it.usefulness_score}/100, Importance {it.importance_score}/100):  {reasons_str}\n"
            f"Unsubscribe Support: {unsub_status}   |   Snippet: {it.snippet}"
        )
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", detail)
        self.detail_text.configure(state=tk.DISABLED)

    # -------------------------------------------------------------------------
    # Direct Unsubscribe Actions (RFC 8058 One-Click HTTP POST & RFC 2369 Mailto)
    # -------------------------------------------------------------------------
    def unsubscribe_active_email(self):
        it = self.active_inspected_item
        if not it:
            messagebox.showinfo("Select Email", "Click an email in either bucket first to inspect and unsubscribe.")
            return

        comp = it.company_name or it.sender_domain or "this sender"
        confirm = messagebox.askyesno(
            "Confirm Unsubscribe",
            f"Are you sure you want to unsubscribe from {comp}?\n\n"
            f"Sender: {it.sender}\n"
            f"Subject: {it.subject}\n\n"
            "This will execute RFC 8058 One-Click HTTP POST or RFC 2369 mailto unsubscribe directly via your Gmail account, "
            "and record this sender as unwanted in your Recall Memory Log.",
        )
        if not confirm:
            return

        def worker():
            try:
                self.root.after(0, lambda: self._set_busy(True, f"Attempting unsubscribe from {comp}..."))
                success, msg = self.client.unsubscribe_from_message(it.msg_id, it)
                if success:
                    self.recall_mgr.record_decisions([it], decision="unwanted", action_source="gui_email_unsubscribe")
                    self.root.after(0, lambda: messagebox.showinfo("Unsubscribed Successfully", msg))
                    self.root.after(0, lambda: self._set_busy(False, msg))
                else:
                    def ask_block():
                        blk = messagebox.askyesno(
                            "Unsubscribe Notice",
                            f"{msg}\n\nWould you like to block '{comp}' in the Recall Memory Log so future scans automatically exclude it?",
                        )
                        if blk:
                            self.recall_mgr.set_company_skip_on_scan(comp, True)
                            self._set_busy(False, f"Blocked '{comp}' in Recall Memory Log.")
                        else:
                            self._set_busy(False, msg)

                    self.root.after(0, ask_block)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("Unsubscribe Error", msg))
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Unsubscribe failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def unsubscribe_active_company(self):
        if not self.active_sender or self.active_sender == "__ALL__":
            messagebox.showinfo("Pick a Company", "Select a specific corporate company on the left first.")
            return

        comp = self.active_sender
        pool = self.sender_groups.get(comp, [])
        if not pool:
            messagebox.showinfo("No Emails", f"No emails loaded for {comp}.")
            return

        target_item = None
        for it in pool:
            if getattr(it, "has_unsubscribe", False) or getattr(it, "unsubscribe_header", ""):
                target_item = it
                break
        if not target_item:
            target_item = pool[0]

        confirm = messagebox.askyesno(
            "Confirm Company Unsubscribe",
            f"Are you sure you want to unsubscribe from all corporate emails from '{comp}' ({len(pool)} cached emails)?\n\n"
            f"Automelinium will inspect email headers to trigger an RFC 8058 One-Click HTTP POST or RFC 2369 mailto unsubscribe, "
            f"and set '{comp}' to Skip on Scan in the Recall Memory Log.",
        )
        if not confirm:
            return

        def worker():
            try:
                self.root.after(0, lambda: self._set_busy(True, f"Unsubscribing from '{comp}'..."))
                success, msg = self.client.unsubscribe_from_message(target_item.msg_id, target_item)
                self.recall_mgr.record_decisions(
                    pool, decision="unwanted", action_source="gui_company_unsubscribe", mark_company_skip_on_scan=True
                )
                if success:
                    self.root.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Unsubscribed from Company",
                            f"{msg}\n\n'{comp}' is now set to Skip on Scan in the Recall Memory Log.",
                        ),
                    )
                    self.root.after(0, lambda: self._set_busy(False, msg))
                else:
                    def ask_block():
                        blk = messagebox.askyesno(
                            "Unsubscribe Notice",
                            f"{msg}\n\nWould you like to set '{comp}' to Skip on Scan in the Recall Memory Log anyway so future emails are automatically omitted?",
                        )
                        if blk:
                            self.recall_mgr.set_company_skip_on_scan(comp, True)
                            self._set_busy(False, f"Blocked '{comp}' in Recall Memory Log.")
                        else:
                            self._set_busy(False, msg)

                    self.root.after(0, ask_block)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("Unsubscribe Error", msg))
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Unsubscribe failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # Algorithm Influence Actions (Lessen Importance in Automelinium & Gmail ML)
    # -------------------------------------------------------------------------
    def toggle_active_company_lessen_importance(self):
        if not self.active_sender or self.active_sender == "__ALL__":
            self.status_bar_var.set("Please select a specific company from the left panel first.")
            return

        comp = self.active_sender
        is_lessened = self.recall_mgr.is_company_importance_lessened(comp)
        new_state = not is_lessened

        # Update persistent recall log
        self.recall_mgr.set_company_lessen_importance(comp, lessen=new_state)

        def worker():
            try:
                comp_emails = [it for it in self.emails if it.company_name == comp or it.sender_domain == comp]
                msg_ids = [it.msg_id for it in comp_emails]

                if new_state and msg_ids:
                    # Influence Gmail's machine learning algorithm by stripping IMPORTANT and STARRED markers
                    self.client.demote_email_importance_in_gmail(msg_ids)
                    for it in comp_emails:
                        it.labels = [lbl for lbl in it.labels if lbl not in ("IMPORTANT", "STARRED")]

                # Recalculate scores for all emails
                score_emails(self.emails, user_email=self.client.user_email)

                def update_ui():
                    if new_state:
                        self.demote_comp_btn.configure(
                            text="Importance Lessened in Algorithms (-40)",
                            style_name="aero_amber",
                        )
                        msg = f"Algorithms influenced for '{comp}': Applied -40 Imp penalty and stripped Gmail Importance markers."
                    else:
                        self.demote_comp_btn.configure(
                            text="Lessen Importance in Algorithms",
                            style_name="aero_purple",
                        )
                        msg = f"Restored baseline algorithm importance for '{comp}'."

                    self._rebuild_sender_directory()
                    self._refresh_split_buckets()
                    self.status_bar_var.set(msg)

                self.root.after(0, update_ui)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self.status_bar_var.set(f"Error influencing algorithms: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def toggle_active_email_lessen_importance(self):
        it = self.active_inspected_item
        if not it:
            self.status_bar_var.set("Click an email in either bucket first to inspect and lessen its importance.")
            return

        snap = self.recall_mgr.get_snapshot()
        is_demoted = it.msg_id in getattr(snap, "demoted_msg_ids", set())
        new_state = not is_demoted

        self.recall_mgr.demote_message_importance(it, lessen=new_state)

        def worker():
            try:
                if new_state:
                    self.client.demote_email_importance_in_gmail([it.msg_id])
                    it.labels = [lbl for lbl in it.labels if lbl not in ("IMPORTANT", "STARRED")]

                score_emails([it], user_email=self.client.user_email)

                def update_ui():
                    if new_state:
                        self.demote_email_btn.configure(
                            text="Email Demoted (-45 Imp)",
                            style_name="aero_amber",
                        )
                        msg = f"Algorithms influenced for '{it.subject[:35]}': Applied -45 Imp penalty and removed Gmail Importance marker."
                    else:
                        self.demote_email_btn.configure(
                            text="Lessen Email Importance",
                            style_name="aero_slate",
                        )
                        msg = f"Restored baseline importance for '{it.subject[:35]}'."

                    self._draw_score_gauges(it.usefulness_score, it.importance_score, it.combined_score)
                    self._refresh_split_buckets()
                    self.status_bar_var.set(msg)

                self.root.after(0, update_ui)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self.status_bar_var.set(f"Error influencing algorithms for email: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # Gmail 3-Rule Blocking Feature
    # -------------------------------------------------------------------------
    def open_company_block_rules_dialog(self):
        if not self.active_sender or self.active_sender == "__ALL__":
            self.status_bar_var.set("Please select a specific company from the left panel first.")
            return

        comp = self.active_sender
        pool = self.sender_groups.get(comp, [])
        if not pool:
            pool = [it for it in self.emails if it.company_name == comp or it.sender_domain == comp]

        target_domain = pool[0].sender_domain if pool else comp
        self._show_three_rule_block_dialog(
            target_label=comp,
            target_domain=target_domain,
            pool=pool,
            target_type="company",
        )

    def open_email_block_rules_dialog(self):
        it = self.active_inspected_item
        if not it:
            self.status_bar_var.set("Click an email in either bucket first to inspect and block.")
            return

        target_label = it.company_name or it.sender_name or it.sender_domain
        target_domain = it.sender_domain or it.sender_email
        pool = [msg for msg in self.emails if msg.sender_domain == it.sender_domain or msg.sender_email == it.sender_email]
        if not pool:
            pool = [it]

        self._show_three_rule_block_dialog(
            target_label=target_label,
            target_domain=target_domain,
            pool=pool,
            target_type="domain",
        )

    def _show_three_rule_block_dialog(
        self,
        target_label: str,
        target_domain: str,
        pool: List[EmailItem],
        target_type: str = "company",
    ):
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Gmail Blocking Rules: {target_label}")
        dlg.configure(bg=AERO_BG_OUTER)
        dlg.transient(self.root)
        dlg.grab_set()

        dlg_w = int(520 * self.scale_factor)
        dlg_h = int(500 * self.scale_factor)
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        pos_x = max(0, root_x + (root_w - dlg_w) // 2)
        pos_y = max(0, root_y + (root_h - dlg_h) // 2)
        dlg.geometry(f"{dlg_w}x{dlg_h}+{pos_x}+{pos_y}")
        dlg.resizable(False, False)

        content = tk.Frame(dlg, bg=AERO_BG_OUTER, padx=16, pady=14)
        content.pack(fill=tk.BOTH, expand=True)

        hdr_frame = tk.Frame(content, bg=AERO_BG_OUTER)
        hdr_frame.pack(fill=tk.X, pady=(0, 10))

        title_lbl = tk.Label(
            hdr_frame,
            text=f"Block '{target_label}' in Gmail",
            bg=AERO_BG_OUTER,
            fg=AERO_TEXT_DARK,
            font=self.font_title,
            anchor=tk.W,
        )
        title_lbl.pack(fill=tk.X)

        sub_lbl = tk.Label(
            hdr_frame,
            text=f"Choose one of Gmail's 3 main rule sets for '{target_domain}' ({len(pool)} cached emails).\nClicking any rule immediately executes it in your Gmail account:",
            bg=AERO_BG_OUTER,
            fg="#0369A1",
            font=self.font_body,
            justify=tk.LEFT,
            anchor=tk.W,
        )
        sub_lbl.pack(fill=tk.X, pady=(3, 0))

        def execute_chosen_rule(rule_choice: str):
            dlg.destroy()
            self._apply_block_rule_action(target_label, target_domain, pool, rule_choice, target_type)

        # Card 1: Send to Spam (Gmail Official Block)
        c1_outer, c1 = self._make_aero_card(content, border_col="#D97706", bg_col="#FFFBEB", padx=10, pady=8)
        c1_outer.pack(fill=tk.X, pady=(0, 8))

        b1 = AeroGelButton(
            c1,
            text="1. Send to Spam (Gmail Official Block)",
            style_name="aero_amber",
            width=int(460 * self.scale_factor),
            height=int(30 * self.scale_factor),
            font=self.font_body_bold,
            command=lambda: execute_chosen_rule("spam"),
            bg_parent="#FFFBEB",
        )
        b1.pack(fill=tk.X)
        d1 = tk.Label(
            c1,
            text="Gmail's native block: Routes all messages directly to Spam, strips importance, and removes from Inbox.",
            bg="#FFFBEB",
            fg="#92400E",
            font=self.font_body,
            anchor=tk.W,
            justify=tk.LEFT,
        )
        d1.pack(fill=tk.X, pady=(4, 0))

        # Card 2: Direct to Trash (Instant Auto-Delete)
        c2_outer, c2 = self._make_aero_card(content, border_col="#E11D48", bg_col="#FFF1F2", padx=10, pady=8)
        c2_outer.pack(fill=tk.X, pady=(0, 8))

        b2 = AeroGelButton(
            c2,
            text="2. Direct to Trash (Instant Auto-Delete)",
            style_name="aero_coral",
            width=int(460 * self.scale_factor),
            height=int(30 * self.scale_factor),
            font=self.font_body_bold,
            command=lambda: execute_chosen_rule("trash"),
            bg_parent="#FFF1F2",
        )
        b2.pack(fill=tk.X)
        d2 = tk.Label(
            c2,
            text="Hard delete: Moves all emails immediately to Gmail Trash, removes from Inbox, and purges from workflow.",
            bg="#FFF1F2",
            fg="#9F1239",
            font=self.font_body,
            anchor=tk.W,
            justify=tk.LEFT,
        )
        d2.pack(fill=tk.X, pady=(4, 0))

        # Card 3: Archive & Mute (Skip the Inbox)
        c3_outer, c3 = self._make_aero_card(content, border_col="#0284C7", bg_col="#F0F9FF", padx=10, pady=8)
        c3_outer.pack(fill=tk.X, pady=(0, 10))

        b3 = AeroGelButton(
            c3,
            text="3. Archive & Mute (Skip the Inbox)",
            style_name="aero_aqua",
            width=int(460 * self.scale_factor),
            height=int(30 * self.scale_factor),
            font=self.font_body_bold,
            command=lambda: execute_chosen_rule("archive"),
            bg_parent="#F0F9FF",
        )
        b3.pack(fill=tk.X)
        d3 = tk.Label(
            c3,
            text="Silent mute: Removes messages from Inbox, marks as read, and keeps searchable in All Mail without clutter.",
            bg="#F0F9FF",
            fg="#075985",
            font=self.font_body,
            anchor=tk.W,
            justify=tk.LEFT,
        )
        d3.pack(fill=tk.X, pady=(4, 0))

        # Cancel Button
        cancel_btn = AeroGelButton(
            content,
            text="Cancel",
            style_name="aero_glass",
            width=int(120 * self.scale_factor),
            height=int(26 * self.scale_factor),
            font=self.font_body,
            command=dlg.destroy,
            bg_parent=AERO_BG_OUTER,
        )
        cancel_btn.pack(side=tk.RIGHT)

    def _apply_block_rule_action(
        self,
        target_label: str,
        target_domain: str,
        pool: List[EmailItem],
        rule_choice: str,
        target_type: str = "company",
    ):
        msg_ids = [it.msg_id for it in pool]
        self._set_busy(True, f"Blocking '{target_label}' in Gmail via rule set: {rule_choice.upper()}...")

        def worker():
            try:
                self.recall_mgr.add_block_rule(target_domain, rule_choice, target_type=target_type)
                if target_label != target_domain:
                    self.recall_mgr.add_block_rule(target_label, rule_choice, target_type=target_type)

                res = self.client.block_sender_with_rule(msg_ids, rule=rule_choice, record_undo=True)
                xml_path = self.client.generate_gmail_filter_xml(target_domain, rule=rule_choice)

                new_status = "Spam" if rule_choice == "spam" else ("Trashed" if rule_choice == "trash" else "Archived")
                for it in pool:
                    it.status = new_status
                    if rule_choice == "spam":
                        it.labels = [lbl for lbl in it.labels if lbl not in ("INBOX", "IMPORTANT", "STARRED")] + ["SPAM"]
                    elif rule_choice == "trash":
                        it.labels = [lbl for lbl in it.labels if lbl not in ("INBOX", "IMPORTANT", "STARRED")] + ["TRASH"]
                    else:
                        it.labels = [lbl for lbl in it.labels if lbl not in ("INBOX", "UNREAD", "IMPORTANT", "STARRED")]

                def on_done():
                    self._set_busy(
                        False,
                        f"Blocked '{target_label}' in Gmail: {len(msg_ids)} emails routed to {rule_choice.capitalize()} (Filter XML saved).",
                    )
                    self._rebuild_sender_directory()
                    self._refresh_split_buckets()

                    if hasattr(self, "block_comp_btn") and self.active_sender == target_label:
                        self.block_comp_btn.configure(
                            text=f"Blocked in Gmail ({rule_choice.upper()})",
                            style_name="aero_slate",
                        )
                    if hasattr(self, "block_email_btn"):
                        self.block_email_btn.configure(
                            text=f"Sender Blocked ({rule_choice.upper()})",
                            style_name="aero_slate",
                        )

                    messagebox.showinfo(
                        "Gmail Block Complete",
                        f"Successfully blocked '{target_label}' in Gmail!\n\n"
                        f"Rule Applied: {rule_choice.upper()}\n"
                        f"Affected emails: {len(msg_ids)}\n"
                        f"Filter XML created: {os.path.basename(xml_path)}\n\n"
                        "Future scans will automatically filter this sender out.",
                    )

            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Failed to apply block rule: {msg}"))
                self.root.after(0, lambda msg=err_msg: messagebox.showerror("Block Error", f"Error applying Gmail block rule: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # Gmail Execution Actions (Auto-Delete Specific Company & Re-Populate)
    # -------------------------------------------------------------------------
    def _dismiss_items_from_service(self, items: List[EmailItem]):
        """
        Permanently dismiss re-populated or deleted emails from the current
        working set so they do not linger in the service.
        """
        if not items:
            return
        dismiss_ids = {it.msg_id for it in items}
        self.emails = [it for it in self.emails if it.msg_id not in dismiss_ids]
        for mid in dismiss_ids:
            self.item_by_id.pop(mid, None)
            self.manual_bucket_override.pop(mid, None)
            self.gmail_restored_ids.discard(mid)
            self.gmail_deleted_ids.discard(mid)

        # Clear inspection pane if active item was dismissed
        if self.active_inspected_item and self.active_inspected_item.msg_id in dismiss_ids:
            self._clear_inspection_pane()

        # Rebuild sender groups and refresh view
        self._rebuild_sender_directory()
        self._refresh_split_buckets()

    def _clear_inspection_pane(self):
        """Reset the inspection details pane when no item or a dismissed item is active."""
        self.active_inspected_item = None
        if hasattr(self, "insp_bucket_badge"):
            self.insp_bucket_badge.configure(
                text="SELECT AN EMAIL TO INSPECT",
                bg="#E2E8F0",
                fg="#475569",
            )
        if hasattr(self, "gauge_canvas"):
            self._draw_score_gauges(0, 0, 0)
        if hasattr(self, "detail_text"):
            self.detail_text.configure(state=tk.NORMAL)
            self.detail_text.delete("1.0", tk.END)
            self.detail_text.insert(
                "1.0",
                "Select any corporate email from either bucket above to inspect details and actions.",
            )
            self.detail_text.configure(state=tk.DISABLED)

    def auto_delete_entire_sender_in_gmail(self):
        if self.is_busy:
            return
        pool = self._get_active_sender_pool()
        if not pool:
            messagebox.showinfo("Pick a Company", "Select a corporate company on the left first.")
            return

        sender_label = self.active_sender if self.active_sender != "__ALL__" else "All Corporate Companies"
        msg_ids = [it.msg_id for it in pool]

        def worker():
            try:
                self.root.after(
                    0,
                    lambda: self._set_busy(
                        True, f"Auto-deleting all {len(msg_ids)} emails from '{sender_label}' in Gmail..."
                    ),
                )
                count = self.client.trash_emails(msg_ids)
                self.recall_mgr.record_decisions(
                    pool, decision="unwanted", action_source="auto_delete_company", mark_company_skip_on_scan=True
                )

                def on_done():
                    self._dismiss_items_from_service(pool)
                    self._set_busy(
                        False,
                        f"Deleted all {count} emails from '{sender_label}' in Gmail and cleared from service.",
                    )

                self.root.after(0, on_done)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Auto-delete failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def execute_delete_bucket_in_gmail(self):
        if self.is_busy:
            return
        targets = list(self.delete_bucket_items)
        if not targets:
            messagebox.showinfo("Bucket Empty", "The Auto-Delete bucket is currently empty.")
            return

        msg_ids = [it.msg_id for it in targets]
        sender_label = self.active_sender if self.active_sender != "__ALL__" else "Filtered Companies"

        def worker():
            try:
                self.root.after(
                    0,
                    lambda: self._set_busy(
                        True, f"Deleting {len(msg_ids)} low-value emails from '{sender_label}' in Gmail..."
                    ),
                )
                count = self.client.trash_emails(msg_ids)
                self.recall_mgr.record_decisions(targets, decision="unwanted", action_source="delete_bucket")

                def on_done():
                    self._dismiss_items_from_service(targets)
                    self._set_busy(
                        False,
                        f"Deleted {count} emails from '{sender_label}' in Gmail Trash and cleared from service.",
                    )

                self.root.after(0, on_done)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Delete failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def execute_restore_bucket_in_gmail(self):
        if self.is_busy:
            return
        targets = list(self.restore_bucket_items)
        if not targets:
            messagebox.showinfo(
                "Bucket Empty",
                "The Re-Populated bucket is currently empty. Lower the cutoff slider or move items over first.",
            )
            return

        metric_mode = self.metric_mode_var.get()
        sender_label = self.active_sender if self.active_sender != "__ALL__" else "Filtered Companies"

        def worker():
            try:
                self.root.after(
                    0,
                    lambda: self._set_busy(
                        True, f"Re-populating {len(targets)} emails from '{sender_label}' to Gmail Inbox by {metric_mode}..."
                    ),
                )
                count = self.client.repopulate_emails(
                    targets, metric_mode=metric_mode, mark_priority_above=65, star_above=80
                )
                self.recall_mgr.record_repopulated(targets, action_source="restore_bucket")

                def on_done():
                    self._dismiss_items_from_service(targets)
                    self._set_busy(
                        False,
                        f"Re-Populated {count} emails from '{sender_label}' to Gmail Inbox and cleared from service.",
                    )

                self.root.after(0, on_done)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Re-populate failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def sync_sender_buckets_to_gmail(self):
        if self.is_busy:
            return
        del_targets = list(self.delete_bucket_items)
        rest_targets = list(self.restore_bucket_items)
        if not del_targets and not rest_targets:
            return

        metric_mode = self.metric_mode_var.get()
        sender_label = self.active_sender if self.active_sender != "__ALL__" else "All Corporate Companies"

        def worker():
            try:
                self.root.after(
                    0,
                    lambda: self._set_busy(
                        True,
                        f"Syncing '{sender_label}': Deleting {len(del_targets)} low-score emails & Re-Populating {len(rest_targets)} high-{metric_mode} emails...",
                    ),
                )
                if del_targets:
                    self.client.trash_emails([it.msg_id for it in del_targets])
                    self.recall_mgr.record_decisions(del_targets, decision="unwanted", action_source="sync_buckets_delete")

                if rest_targets:
                    self.client.repopulate_emails(
                        rest_targets, metric_mode=metric_mode, mark_priority_above=65, star_above=80
                    )
                    self.recall_mgr.record_repopulated(rest_targets, action_source="sync_buckets_restore")

                def on_done():
                    dismissed = list(rest_targets) + list(del_targets)
                    self._dismiss_items_from_service(dismissed)
                    self._set_busy(
                        False,
                        f"Synced '{sender_label}' with Gmail: {len(del_targets)} deleted & {len(rest_targets)} re-populated (cleared from service).",
                    )

                self.root.after(0, on_done)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Sync error: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def undo_last_gmail_action(self):
        if self.is_busy:
            return
        if not self.client.undo_stack:
            messagebox.showinfo(
                "Nothing to Undo", "No Gmail delete/re-populate actions have been executed in this session yet."
            )
            return

        def worker():
            try:
                self.root.after(0, lambda: self._set_busy(True, "Undoing last Gmail action..."))
                res = self.client.undo_last_action()
                if res:
                    desc, affected_ids, target_status = res
                    for mid in affected_ids:
                        if mid in self.item_by_id:
                            self.item_by_id[mid].status = target_status
                        self.gmail_deleted_ids.discard(mid)
                        self.gmail_restored_ids.discard(mid)

                    # Remove decisions from Recall Memory Log
                    self.recall_mgr.remove_decisions_for_msg_ids(affected_ids)

                    self.root.after(0, self._refresh_split_buckets)
                    self.root.after(
                        0,
                        lambda: self._set_busy(
                            False, f"Reverted: {desc} ({len(affected_ids)} emails restored to previous state)."
                        ),
                    )
                else:
                    self.root.after(0, lambda: self._set_busy(False, "Nothing to undo."))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Undo failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------------------
    # Automated Inbox Sending & Periodic Background Synchronization
    # -------------------------------------------------------------------------
    def _on_auto_send_toggled(self):
        is_enabled = self.auto_send_inbox_var.get()
        save_settings({"auto_send_inbox": is_enabled})
        if is_enabled:
            self.status_bar_var.set("Automated Inbox Sending enabled. Processing qualifying emails...")
            self.auto_send_qualifying_to_inbox(silent=False)
        else:
            self.status_bar_var.set("Automated Inbox Sending disabled. Emails will remain in buckets until manually synced.")

    def auto_send_qualifying_to_inbox(self, silent: bool = False):
        if not self.auto_send_inbox_var.get():
            return
        if self.is_busy:
            return

        thresh = self.threshold_var.get()
        qualifying = []
        for it in self.emails:
            override = self.manual_bucket_override.get(it.msg_id)
            is_keep = False
            if override == "restore":
                is_keep = True
            elif override == "delete":
                is_keep = False
            else:
                is_keep = self._get_active_metric(it) >= thresh

            if is_keep and (it.msg_id not in self.gmail_restored_ids or it.status in ("Trashed", "Quarantined")):
                qualifying.append(it)

        if not qualifying:
            if not silent:
                self.status_bar_var.set("Automated Sending: All qualifying keep-bucket emails are already synced to Gmail Inbox.")
            return

        metric_mode = self.metric_mode_var.get()

        def worker():
            try:
                if not silent:
                    self.root.after(
                        0,
                        lambda: self._set_busy(
                            True, f"Auto-sending {len(qualifying)} qualifying emails to Gmail Inbox..."
                        ),
                    )
                count = self.client.repopulate_emails(
                    qualifying,
                    metric_mode=metric_mode,
                    mark_priority_above=65,
                    star_above=80,
                )
                self.recall_mgr.record_repopulated(qualifying, action_source="auto_send_inbox")

                def on_done():
                    self._dismiss_items_from_service(qualifying)
                    self._set_busy(
                        False,
                        f"Auto-Sent: {count} qualifying emails re-populated to Gmail Inbox and cleared from service.",
                    )

                self.root.after(0, on_done)
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda msg=err_msg: self._set_busy(False, f"Auto-send to inbox failed: {msg}"))

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_auto_scan(self):
        self.root.after(300000, self._auto_scan_tick)

    def _auto_scan_tick(self):
        try:
            if self.auto_send_inbox_var.get() and not self.is_busy:
                self.fetch_emails_async()
        except Exception:
            pass
        finally:
            self._schedule_auto_scan()

    # -------------------------------------------------------------------------
    # Standard Email Keyboard Shortcuts
    # -------------------------------------------------------------------------
    def _setup_keyboard_shortcuts(self):
        # Navigation
        self.root.bind("j", self._shortcut_next)
        self.root.bind("J", self._shortcut_next)
        self.root.bind("<Down>", self._shortcut_next)
        self.root.bind("k", self._shortcut_prev)
        self.root.bind("K", self._shortcut_prev)
        self.root.bind("<Up>", self._shortcut_prev)
        self.root.bind("<Tab>", self._shortcut_tab_focus)

        # Organization & Bucket Actions
        self.root.bind("e", self._shortcut_keep)
        self.root.bind("E", self._shortcut_keep)
        self.root.bind("y", self._shortcut_keep)
        self.root.bind("Y", self._shortcut_keep)

        self.root.bind("<Delete>", self._shortcut_delete)
        self.root.bind("<BackSpace>", self._shortcut_delete)
        self.root.bind("#", self._shortcut_delete)

        self.root.bind("<Command-a>", self._shortcut_select_all)
        self.root.bind("<Control-a>", self._shortcut_select_all)

        self.root.bind("o", self._shortcut_cycle_sort)
        self.root.bind("O", self._shortcut_cycle_sort)

        # Smart Actions
        self.root.bind("s", self._shortcut_star_needed)
        self.root.bind("S", self._shortcut_star_needed)
        self.root.bind("d", self._shortcut_demote)
        self.root.bind("D", self._shortcut_demote)
        self.root.bind("b", self._shortcut_block)
        self.root.bind("B", self._shortcut_block)
        self.root.bind("u", self._shortcut_unsubscribe)
        self.root.bind("U", self._shortcut_unsubscribe)

        # System & Search
        self.root.bind("r", self._shortcut_refresh)
        self.root.bind("R", self._shortcut_refresh)
        self.root.bind("<F5>", self._shortcut_refresh)
        self.root.bind("<Command-r>", self._shortcut_refresh)
        self.root.bind("<Control-r>", self._shortcut_refresh)

        self.root.bind("<Command-z>", self._shortcut_undo)
        self.root.bind("<Control-z>", self._shortcut_undo)

        self.root.bind("/", self._shortcut_search_focus)
        self.root.bind("?", self._shortcut_help)

    def _is_text_entry_focused(self) -> bool:
        focused = self.root.focus_get()
        if not focused:
            return False
        widget_class = focused.winfo_class()
        if widget_class in ("Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox"):
            return True
        if isinstance(focused, (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox, ttk.Combobox)):
            return True
        return False

    def _get_focused_tree(self) -> Optional[ttk.Treeview]:
        focused = self.root.focus_get()
        for tree in (self.delete_tree, self.restore_tree, self.sender_tree):
            if focused == tree:
                return tree
        last = getattr(self, "_last_focused_tree", None)
        if last in (self.delete_tree, self.restore_tree, self.sender_tree):
            return last
        if self.delete_tree.selection():
            return self.delete_tree
        if self.restore_tree.selection():
            return self.restore_tree
        return self.sender_tree

    def _shortcut_next(self, event=None):
        if self._is_text_entry_focused():
            return None
        tree = self._get_focused_tree()
        if not tree:
            return "break"
        children = tree.get_children()
        if not children:
            return "break"
        sel = tree.selection()
        if not sel:
            next_idx = 0
        else:
            cur_idx = children.index(sel[-1]) if sel[-1] in children else -1
            next_idx = min(len(children) - 1, cur_idx + 1)
        target = children[next_idx]
        tree.selection_set(target)
        tree.see(target)
        tree.focus(target)
        if tree == self.sender_tree:
            self._on_sender_selected(None)
        elif tree == self.delete_tree:
            self._on_bucket_item_selected(tree, "DELETE BUCKET")
        elif tree == self.restore_tree:
            self._on_bucket_item_selected(tree, "RE-POPULATED BUCKET")
        return "break"

    def _shortcut_prev(self, event=None):
        if self._is_text_entry_focused():
            return None
        tree = self._get_focused_tree()
        if not tree:
            return "break"
        children = tree.get_children()
        if not children:
            return "break"
        sel = tree.selection()
        if not sel:
            prev_idx = len(children) - 1
        else:
            cur_idx = children.index(sel[0]) if sel[0] in children else 0
            prev_idx = max(0, cur_idx - 1)
        target = children[prev_idx]
        tree.selection_set(target)
        tree.see(target)
        tree.focus(target)
        if tree == self.sender_tree:
            self._on_sender_selected(None)
        elif tree == self.delete_tree:
            self._on_bucket_item_selected(tree, "DELETE BUCKET")
        elif tree == self.restore_tree:
            self._on_bucket_item_selected(tree, "RE-POPULATED BUCKET")
        return "break"

    def _shortcut_keep(self, event=None):
        if self._is_text_entry_focused():
            return None
        del_sel = list(self.delete_tree.selection())
        if del_sel:
            self.move_selected_from_delete_to_restore()
            return "break"
        rest_sel = list(self.restore_tree.selection())
        if rest_sel:
            for mid in rest_sel:
                self.manual_bucket_override[mid] = "restore"
            self._refresh_split_buckets()
            self.status_bar_var.set(f"Confirmed {len(rest_sel)} email(s) kept in Re-Populated bucket.")
            if self.auto_send_inbox_var.get():
                self.auto_send_qualifying_to_inbox(silent=True)
            return "break"
        if self.active_inspected_item:
            self.manual_bucket_override[self.active_inspected_item.msg_id] = "restore"
            self._refresh_split_buckets()
            self.status_bar_var.set(f"Kept '{self.active_inspected_item.subject[:35]}' in Re-Populated bucket.")
            if self.auto_send_inbox_var.get():
                self.auto_send_qualifying_to_inbox(silent=True)
            return "break"
        return "break"

    def _shortcut_delete(self, event=None):
        if self._is_text_entry_focused():
            return None
        rest_sel = list(self.restore_tree.selection())
        if rest_sel:
            self.move_selected_from_restore_to_delete()
            return "break"
        del_sel = list(self.delete_tree.selection())
        if del_sel:
            self.execute_delete_bucket_in_gmail()
            return "break"
        if self.active_inspected_item:
            self.manual_bucket_override[self.active_inspected_item.msg_id] = "delete"
            self._refresh_split_buckets()
            self.status_bar_var.set(f"Moved '{self.active_inspected_item.subject[:35]}' to Auto-Delete bucket.")
            return "break"
        return "break"

    def _shortcut_star_needed(self, event=None):
        if self._is_text_entry_focused():
            return None
        it = self.active_inspected_item
        if it:
            self.recall_mgr.record_decisions([it], decision="needed", action_source="shortcut_s")
            self.manual_bucket_override[it.msg_id] = "restore"
            self._refresh_split_buckets()
            self.status_bar_var.set(f"Marked '{it.subject[:40]}' as Needed in Recall Learning Log.")
            if self.auto_send_inbox_var.get():
                self.auto_send_qualifying_to_inbox(silent=True)
        return "break"

    def _shortcut_demote(self, event=None):
        if self._is_text_entry_focused():
            return None
        if self.active_inspected_item:
            self.toggle_active_email_lessen_importance()
        elif self.active_sender and self.active_sender != "__ALL__":
            self.toggle_company_lessen_importance()
        return "break"

    def _shortcut_block(self, event=None):
        if self._is_text_entry_focused():
            return None
        if self.active_inspected_item:
            self.open_email_block_rules_dialog()
        elif self.active_sender and self.active_sender != "__ALL__":
            self.open_company_block_rules_dialog()
        return "break"

    def _shortcut_unsubscribe(self, event=None):
        if self._is_text_entry_focused():
            return None
        if self.active_inspected_item:
            self.unsubscribe_active_email()
        elif self.active_sender and self.active_sender != "__ALL__":
            self.unsubscribe_active_company()
        return "break"

    def _shortcut_refresh(self, event=None):
        if self._is_text_entry_focused():
            return None
        self.fetch_emails_async()
        return "break"

    def _shortcut_undo(self, event=None):
        if self._is_text_entry_focused():
            return None
        self.undo_last_gmail_action()
        return "break"

    def _shortcut_search_focus(self, event=None):
        if hasattr(self, "sender_search_entry"):
            self.sender_search_entry.focus_set()
            self.sender_search_entry.select_range(0, tk.END)
            return "break"
        return "break"

    def _shortcut_tab_focus(self, event=None):
        if self._is_text_entry_focused():
            return None
        trees = [self.sender_tree, self.delete_tree, self.restore_tree]
        focused = self.root.focus_get()
        try:
            curr_idx = trees.index(focused)
            next_tree = trees[(curr_idx + 1) % len(trees)]
        except ValueError:
            next_tree = self.delete_tree
        next_tree.focus_set()
        self._last_focused_tree = next_tree
        return "break"

    def _shortcut_select_all(self, event=None):
        if self._is_text_entry_focused():
            return None
        tree = self._get_focused_tree()
        if tree:
            tree.selection_set(tree.get_children())
            return "break"
        return "break"

    def _shortcut_help(self, event=None):
        if self._is_text_entry_focused():
            return None
        self._show_shortcuts_dialog()
        return "break"

    def _show_shortcuts_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Automelinium Keyboard Shortcuts")
        dlg.configure(bg=AERO_BG_OUTER)
        dlg.transient(self.root)
        dlg.grab_set()

        dlg_w = int(580 * self.scale_factor)
        dlg_h = int(520 * self.scale_factor)
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        pos_x = max(0, root_x + (root_w - dlg_w) // 2)
        pos_y = max(0, root_y + (root_h - dlg_h) // 2)
        dlg.geometry(f"{dlg_w}x{dlg_h}+{pos_x}+{pos_y}")
        dlg.resizable(False, False)

        content = tk.Frame(dlg, bg=AERO_BG_OUTER, padx=16, pady=14)
        content.pack(fill=tk.BOTH, expand=True)

        title_lbl = tk.Label(
            content,
            text="Common Email Keyboard Shortcuts",
            bg=AERO_BG_OUTER,
            fg=AERO_TEXT_DARK,
            font=self.font_title,
            anchor=tk.W,
        )
        title_lbl.pack(fill=tk.X, pady=(0, 2))

        sub_lbl = tk.Label(
            content,
            text="Standard email client hotkeys for fast inbox sorting, purging, and management:",
            bg=AERO_BG_OUTER,
            fg="#0369A1",
            font=self.font_body,
            anchor=tk.W,
        )
        sub_lbl.pack(fill=tk.X, pady=(0, 10))

        shortcut_groups = [
            (
                "Navigation & Focus",
                "#0284C7",
                [
                    ("j  or  Down", "Move selection down (next email or company)"),
                    ("k  or  Up", "Move selection up (previous email or company)"),
                    ("Tab", "Cycle focus between Company List and Split Buckets"),
                    ("/ (Slash)", "Focus Find Company search box"),
                ],
            ),
            (
                "Keep & Auto-Delete Actions",
                "#059669",
                [
                    ("e  or  y", "Keep / Re-populate selected email to Inbox (Auto-Sends)"),
                    ("Delete / BackSpace / #", "Move selected email to Auto-Delete bucket / Trash"),
                    ("o", "Cycle bucket sort orders (Score, Date, Fiscal, Subject)"),
                    ("Cmd+A  or  Ctrl+A", "Select all emails in active bucket"),
                ],
            ),
            (
                "Smart Algorithmic Controls",
                "#7C3AED",
                [
                    ("s", "Star / Mark as Needed in Recall Learning Log"),
                    ("d", "Lessen Importance in Algorithms (-45 Imp penalty)"),
                    ("b", "Block sender in Gmail (Opens 3-rule set modal)"),
                    ("u", "Unsubscribe directly from company (RFC 8058 / Mailto)"),
                ],
            ),
            (
                "System & Scanning",
                "#D97706",
                [
                    ("r  or  F5  or  Cmd+R", "Scan Inbox Senders"),
                    ("Cmd+Z  or  Ctrl+Z", "Undo Last Gmail Action"),
                    ("? (Question)", "Open this Keyboard Shortcuts cheat sheet"),
                ],
            ),
        ]

        for group_title, border_c, items in shortcut_groups:
            c_outer, c_inner = self._make_aero_card(content, border_col=border_c, bg_col="#FFFFFF", padx=8, pady=4)
            c_outer.pack(fill=tk.X, pady=(0, 6))

            g_lbl = tk.Label(c_inner, text=group_title, bg="#FFFFFF", fg=border_c, font=self.font_body_bold, anchor=tk.W)
            g_lbl.pack(fill=tk.X, pady=(0, 2))

            for key_combo, desc in items:
                row = tk.Frame(c_inner, bg="#FFFFFF")
                row.pack(fill=tk.X, pady=1)
                k_lbl = tk.Label(
                    row,
                    text=key_combo,
                    bg="#E0F2FE",
                    fg="#03406A",
                    font=self.font_body_bold,
                    padx=6,
                    pady=1,
                    relief="solid",
                    bd=1,
                    width=18,
                    anchor=tk.W,
                )
                k_lbl.pack(side=tk.LEFT)
                d_lbl = tk.Label(row, text=desc, bg="#FFFFFF", fg=AERO_TEXT_DARK, font=self.font_body, anchor=tk.W)
                d_lbl.pack(side=tk.LEFT, padx=(8, 0))

        btn_row = tk.Frame(content, bg=AERO_BG_OUTER)
        btn_row.pack(fill=tk.X, pady=(6, 0))
        AeroGelButton(
            btn_row,
            text="Close (Esc)",
            style_name="aero_blue",
            width=int(120 * self.scale_factor),
            height=int(26 * self.scale_factor),
            font=self.font_body_bold,
            command=dlg.destroy,
            bg_parent=AERO_BG_OUTER,
        ).pack(side=tk.RIGHT)

        dlg.bind("<Escape>", lambda _e: dlg.destroy())


def run_cli_check():
    client = AutomatischGmailClient()
    email_addr, scope = client.connect_and_verify()
    print(f"[+] Connected to Automatisch Gmail API as: {email_addr}")
    print(f"[+] Active OAuth Scopes: {scope}")
    items = client.fetch_emails(source_scope="inbox", corporate_only=True, max_results=50)
    print(f"[+] Fetched & scored {len(items)} corporate emails:")
    for it in items[:12]:
        print(
            f"  - [{it.category:18s}] Comb:{it.combined_score:3d} "
            f"(Imp:{it.importance_score:3d}, Use:{it.usefulness_score:3d}) | "
            f"{it.sender_domain:24s} | {it.subject[:50]}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Automelinium Aero -- Company-First Corporate Auto-Deleter & Smart Re-Populator"
    )
    parser.add_argument("--check", action="store_true", help="Run headless connection & scoring verification and exit")
    args = parser.parse_args()

    if args.check:
        run_cli_check()
        return

    root = tk.Tk()
    app = AutomeliniumGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
