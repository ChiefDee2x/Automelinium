"""
Company Name Resolution & Real Company Logo Fetcher/Cache for Automelinium Aero.

Transforms raw corporate sender domains/addresses (e.g. 'e.etradefinancial.com',
'ohhey.depop.com', 'mail.joinhandshake.com', 'learn.mail.monday.com') into clean
Company Names (e.g. 'E*TRADE', 'Depop', 'Handshake', 'monday.com') and fetches
authentic 16x16 and 32x32 PNG company logos with local disk caching.
"""

import base64
import concurrent.futures
import os
import re
import sqlite3
import ssl
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple, Callable, Set

from aero_theme import _encode_rgba_png

CONFIG_DIR = os.path.expanduser("~/.config/automelinium")
SQLITE_CACHE_PATH = os.path.join(CONFIG_DIR, "email_cache.db")
LOGO_CACHE_DIR = os.path.join(CONFIG_DIR, "logos")

# ---------------------------------------------------------------------------
# Curated Domain / Subdomain -> (Company Name, Canonical Logo Domain)
# ---------------------------------------------------------------------------
KNOWN_COMPANIES: Dict[str, Tuple[str, str]] = {
    # Tech, Dev & AI
    "github.com": ("GitHub", "github.com"),
    "google.com": ("Google", "google.com"),
    "accounts.google.com": ("Google", "google.com"),
    "docs.google.com": ("Google", "google.com"),
    "googlemail.com": ("Google", "google.com"),
    "youtube.com": ("YouTube", "youtube.com"),
    "fitbit.com": ("Fitbit", "fitbit.com"),
    "e.fitbit.com": ("Fitbit", "fitbit.com"),
    "linkedin.com": ("LinkedIn", "linkedin.com"),
    "em.linkedin.com": ("LinkedIn", "linkedin.com"),
    "e.linkedin.com": ("LinkedIn", "linkedin.com"),
    "calendly.com": ("Calendly", "calendly.com"),
    "send.calendly.com": ("Calendly", "calendly.com"),
    "otter.ai": ("Otter.ai", "otter.ai"),
    "reclaim.ai": ("Reclaim.ai", "reclaim.ai"),
    "mindstudio.ai": ("MindStudio", "mindstudio.ai"),
    "rentahuman.ai": ("RentAHuman", "rentahuman.ai"),
    "qodo.ai": ("Qodo", "qodo.ai"),
    "xmind.ai": ("Xmind", "xmind.ai"),
    "marketing.xmind.ai": ("Xmind", "xmind.ai"),
    "hyperwrite.ai": ("HyperWrite", "hyperwrite.ai"),
    "relevanceai.com": ("Relevance AI", "relevanceai.com"),
    "chatwithyourdata.io": ("Julius AI", "julius.ai"),
    "idiolect.app": ("Idiolect AI", "idiolect.app"),
    "ollama.com": ("Ollama", "ollama.com"),
    "mail.ollama.com": ("Ollama", "ollama.com"),
    "openai.com": ("OpenAI", "openai.com"),
    "email.openai.com": ("OpenAI", "openai.com"),
    "anthropic.com": ("Anthropic (Claude)", "anthropic.com"),
    "mail.anthropic.com": ("Anthropic (Claude)", "anthropic.com"),
    "claude.com": ("Anthropic (Claude)", "claude.ai"),
    "email.claude.com": ("Anthropic (Claude)", "claude.ai"),
    "supabase.com": ("Supabase", "supabase.com"),
    "formspree.io": ("Formspree", "formspree.io"),
    "airtable.com": ("Airtable", "airtable.com"),
    "mail.airtable.com": ("Airtable", "airtable.com"),
    "zapier.com": ("Zapier", "zapier.com"),
    "send.zapier.com": ("Zapier", "zapier.com"),
    "mail.zapier.com": ("Zapier", "zapier.com"),
    "monday.com": ("monday.com", "monday.com"),
    "learn.mail.monday.com": ("monday.com", "monday.com"),
    "wordpress.com": ("WordPress", "wordpress.com"),
    "jetpack.com": ("Jetpack", "jetpack.com"),
    "squarespace.com": ("Squarespace", "squarespace.com"),
    "canva.com": ("Canva", "canva.com"),
    "engage.canva.com": ("Canva", "canva.com"),
    "semrush.com": ("Semrush", "semrush.com"),
    "team.semrush.com": ("Semrush", "semrush.com"),
    "adobe.com": ("Adobe", "adobe.com"),
    "mail.adobe.com": ("Adobe", "adobe.com"),
    "e.adobe.com": ("Adobe", "adobe.com"),
    "stackblitz.com": ("StackBlitz / Bolt.new", "stackblitz.com"),
    "bolt.new": ("StackBlitz / Bolt.new", "bolt.new"),
    "cal.com": ("Cal.com", "cal.com"),
    "cloudflare.com": ("Cloudflare", "cloudflare.com"),
    "notify.cloudflare.com": ("Cloudflare", "cloudflare.com"),
    "em1.cloudflare.com": ("Cloudflare", "cloudflare.com"),
    "digitalocean.com": ("DigitalOcean", "digitalocean.com"),
    "info.digitalocean.com": ("DigitalOcean", "digitalocean.com"),
    "ovhcloud.com": ("OVHcloud", "ovhcloud.com"),
    "us.ovhcloud.com": ("OVHcloud", "ovhcloud.com"),
    "lovable.dev": ("Lovable", "lovable.dev"),
    "md.lovable-app.email": ("Lovable", "lovable.dev"),
    "base44.com": ("Base44", "base44.com"),
    "marketing.base44.com": ("Base44", "base44.com"),
    "descript.com": ("Descript", "descript.com"),
    "marketing.descript.com": ("Descript", "descript.com"),
    "slack.com": ("Slack", "slack.com"),
    "zoom.us": ("Zoom", "zoom.us"),
    "e.zoom.us": ("Zoom", "zoom.us"),
    "microsoft.com": ("Microsoft", "microsoft.com"),
    "accountprotection.microsoft.com": ("Microsoft", "microsoft.com"),
    "communication.microsoft.com": ("Microsoft", "microsoft.com"),
    "microsoftonline.com": ("Microsoft", "microsoft.com"),
    "notion.so": ("Notion", "notion.so"),
    "updates.notion.so": ("Notion", "notion.so"),
    "dropbox.com": ("Dropbox", "dropbox.com"),
    "em-s.dropbox.com": ("Dropbox", "dropbox.com"),
    "trello.com": ("Trello", "trello.com"),
    "asana.com": ("Asana", "asana.com"),
    "go.asana.com": ("Asana", "asana.com"),
    "discord.com": ("Discord", "discord.com"),
    "builder.io": ("Builder.io", "builder.io"),
    "team.builder.io": ("Builder.io", "builder.io"),
    "codesandbox.io": ("CodeSandbox", "codesandbox.io"),
    "railway.app": ("Railway", "railway.app"),
    "notify.railway.app": ("Railway", "railway.app"),
    "fly.io": ("Fly.io", "fly.io"),
    "streamlit.io": ("Streamlit", "streamlit.io"),
    "anaconda.com": ("Anaconda", "anaconda.com"),
    "try.anaconda.com": ("Anaconda", "anaconda.com"),
    "obsidian.md": ("Obsidian", "obsidian.md"),
    "autodesk.com": ("Autodesk", "autodesk.com"),
    "signin.autodesk.com": ("Autodesk", "autodesk.com"),
    "parallels.com": ("Parallels", "parallels.com"),
    "m.parallels.com": ("Parallels", "parallels.com"),
    "lucid.co": ("Lucidchart", "lucid.co"),
    "app.lucid.co": ("Lucidchart", "lucid.co"),
    "e.lucid.co": ("Lucidchart", "lucid.co"),
    "quillbot.com": ("QuillBot", "quillbot.com"),
    "mail.quillbot.com": ("QuillBot", "quillbot.com"),
    "grammarly.com": ("Grammarly", "grammarly.com"),
    "info.grammarly.com": ("Grammarly", "grammarly.com"),
    "apollo.io": ("Apollo.io", "apollo.io"),
    "mail.apollo.io": ("Apollo.io", "apollo.io"),
    "hunter.io": ("Hunter.io", "hunter.io"),
    "heygen.com": ("HeyGen", "heygen.com"),
    "email.heygen.com": ("HeyGen", "heygen.com"),
    "vidiq.com": ("vidIQ", "vidiq.com"),
    "send.vidiq.com": ("vidIQ", "vidiq.com"),
    "opus.pro": ("OpusClip", "opus.pro"),
    "app.opus.pro": ("OpusClip", "opus.pro"),
    "capcut.com": ("CapCut", "capcut.com"),
    "mail.capcut.com": ("CapCut", "capcut.com"),
    "artlist.io": ("Artlist", "artlist.io"),
    "newsletter.artlist.io": ("Artlist", "artlist.io"),
    "pixabay.com": ("Pixabay", "pixabay.com"),
    "community.pixabay.com": ("Pixabay", "pixabay.com"),
    "sketchfab.com": ("Sketchfab", "sketchfab.com"),
    "odoo.com": ("Odoo", "odoo.com"),

    # Career, Recruiting & Networking
    "joinhandshake.com": ("Handshake", "joinhandshake.com"),
    "mail.joinhandshake.com": ("Handshake", "joinhandshake.com"),
    "g.joinhandshake.com": ("Handshake", "joinhandshake.com"),
    "notifications.joinhandshake.com": ("Handshake", "joinhandshake.com"),
    "ziprecruiter.com": ("ZipRecruiter", "ziprecruiter.com"),
    "indeed.com": ("Indeed", "indeed.com"),
    "match.indeed.com": ("Indeed", "indeed.com"),
    "jm.indeed.com": ("Indeed", "indeed.com"),
    "glassdoor.com": ("Glassdoor", "glassdoor.com"),
    "ycombinator.com": ("Y Combinator", "ycombinator.com"),
    "f6s.com": ("F6S", "f6s.com"),
    "idealist.org": ("Idealist", "idealist.org"),
    "mail.idealist.org": ("Idealist", "idealist.org"),
    "myworkday.com": ("Workday", "workday.com"),
    "arena.run": ("Arena", "arena.run"),
    "speakerhub.com": ("SpeakerHUB", "speakerhub.com"),
    "mail.speakerhub.com": ("SpeakerHUB", "speakerhub.com"),
    "upwork.com": ("Upwork", "upwork.com"),
    "email.upwork.com": ("Upwork", "upwork.com"),
    "12twenty.com": ("12twenty", "12twenty.com"),
    "employer.12twenty.com": ("12twenty", "12twenty.com"),
    "workablemail.com": ("Workable", "workable.com"),
    "candidates.workablemail.com": ("Workable", "workable.com"),
    "greenhouse-mail.io": ("Greenhouse", "greenhouse.com"),
    "us.greenhouse-mail.io": ("Greenhouse", "greenhouse.com"),
    "micromentor.org": ("MicroMentor", "micromentor.org"),
    "startups.com": ("Startups.com", "startups.com"),
    "youexec.com": ("You Exec", "youexec.com"),
    "skool.com": ("Skool", "skool.com"),

    # Retail, Marketplaces & Food
    "poshmark.com": ("Poshmark", "poshmark.com"),
    "depop.com": ("Depop", "depop.com"),
    "ohhey.depop.com": ("Depop", "depop.com"),
    "alerts.depop.com": ("Depop", "depop.com"),
    "grailed.com": ("Grailed", "grailed.com"),
    "mail.grailed.com": ("Grailed", "grailed.com"),
    "stockx.com": ("StockX", "stockx.com"),
    "mercari.com": ("Mercari", "mercari.com"),
    "hello.us.mercari.com": ("Mercari", "mercari.com"),
    "alerts.us.mercari.com": ("Mercari", "mercari.com"),
    "mercariapp.com": ("Mercari", "mercari.com"),
    "chipotle.com": ("Chipotle", "chipotle.com"),
    "email.chipotle.com": ("Chipotle", "chipotle.com"),
    "notify.chipotle.com": ("Chipotle", "chipotle.com"),
    "goinggoing.com": ("Going, Going, Gone!", "goinggoing.com"),
    "e.goinggoing.com": ("Going, Going, Gone!", "goinggoing.com"),
    "dcsg.com": ("DICK'S Sporting Goods", "dickssportinggoods.com"),
    "e.dcsg.com": ("DICK'S Sporting Goods", "dickssportinggoods.com"),
    "indiegogo.com": ("Indiegogo", "indiegogo.com"),
    "p.indiegogo.com": ("Indiegogo", "indiegogo.com"),
    "legal.indiegogo.com": ("Indiegogo", "indiegogo.com"),
    "gelato.com": ("Gelato", "gelato.com"),
    "puma.com": ("PUMA", "puma.com"),
    "email.us.puma.com": ("PUMA", "puma.com"),
    "toogoodtogo.com": ("Too Good To Go", "toogoodtogo.com"),
    "mail.toogoodtogo.com": ("Too Good To Go", "toogoodtogo.com"),
    "sanrio.com": ("Sanrio", "sanrio.com"),
    "gamestop.com": ("GameStop", "gamestop.com"),
    "emails.gamestop.com": ("GameStop", "gamestop.com"),
    "childrenofkhan.com": ("Children Of Khan", "childrenofkhan.com"),
    "webstate.shop": ("WEBSTATE", "webstate.shop"),
    "walmart.com": ("Walmart", "walmart.com"),
    "amazon.com": ("Amazon", "amazon.com"),
    "asos.com": ("ASOS", "asos.com"),
    "josbank.com": ("Jos. A. Bank", "josbank.com"),
    "imktg.josbank.com": ("Jos. A. Bank", "josbank.com"),
    "lifetouch.com": ("Lifetouch", "lifetouch.com"),
    "e.lifetouch.com": ("Lifetouch", "lifetouch.com"),
    "thereformation.us": ("Reformation", "thereformation.com"),

    # Finance, Banking & Business
    "etrade.com": ("E*TRADE", "etrade.com"),
    "e.etradefinancial.com": ("E*TRADE", "etrade.com"),
    "etradefrommorganstanley.com": ("E*TRADE", "etrade.com"),
    "proxyvote.com": ("ProxyVote (E*TRADE)", "proxyvote.com"),
    "fidelity.com": ("Fidelity Investments", "fidelity.com"),
    "mail.fidelity.com": ("Fidelity Investments", "fidelity.com"),
    "shareholderdocs.fidelity.com": ("Fidelity Investments", "fidelity.com"),
    "fmr.com": ("Fidelity Investments", "fidelity.com"),
    "truist.com": ("Truist", "truist.com"),
    "message.truist.com": ("Truist", "truist.com"),
    "mail.mktg.truist.com": ("Truist", "truist.com"),
    "venmo.com": ("Venmo", "venmo.com"),
    "email.venmo.com": ("Venmo", "venmo.com"),
    "usaa.com": ("USAA", "usaa.com"),
    "exmac.usaa.com": ("USAA", "usaa.com"),
    "mailcenter.usaa.com": ("USAA", "usaa.com"),
    "zenbusiness.com": ("ZenBusiness", "zenbusiness.com"),
    "engage.zenbusiness.com": ("ZenBusiness", "zenbusiness.com"),
    "zenbusiness-email.com": ("ZenBusiness", "zenbusiness.com"),
    "paypal.com": ("PayPal", "paypal.com"),
    "news.paypal.com": ("PayPal", "paypal.com"),
    "squareup.com": ("Square", "squareup.com"),
    "messaging.squareup.com": ("Square", "squareup.com"),
    "stripe.com": ("Stripe", "stripe.com"),
    "link.com": ("Stripe Link", "link.com"),
    "coinbase.com": ("Coinbase", "coinbase.com"),
    "mail.coinbase.com": ("Coinbase", "coinbase.com"),
    "klarna.com": ("Klarna", "klarna.com"),
    "intuit.com": ("Intuit", "intuit.com"),
    "salliemae.com": ("Sallie Mae", "salliemae.com"),
    "soslprospect.salliemae.com": ("Sallie Mae", "salliemae.com"),
    "edfinancial.studentaid.gov": ("Edfinancial Services", "studentaid.gov"),
    "studentaid.gov": ("Federal Student Aid", "studentaid.gov"),
    "adp.com": ("ADP", "adp.com"),
    "afternic.com": ("Afternic", "afternic.com"),
    "mailmc.afternic.com": ("Afternic", "afternic.com"),

    # Media, Gaming, Sports & Social
    "twitch.tv": ("Twitch", "twitch.tv"),
    "substack.com": ("Substack", "substack.com"),
    "mg1.substack.com": ("Substack", "substack.com"),
    "quora.com": ("Quora", "quora.com"),
    "soundcloud.com": ("SoundCloud", "soundcloud.com"),
    "announcements.soundcloud.com": ("SoundCloud", "soundcloud.com"),
    "notifications.soundcloud.com": ("SoundCloud", "soundcloud.com"),
    "facebookmail.com": ("Facebook", "facebook.com"),
    "priority.facebookmail.com": ("Facebook", "facebook.com"),
    "support.facebook.com": ("Facebook", "facebook.com"),
    "instagram.com": ("Instagram", "instagram.com"),
    "mail.instagram.com": ("Instagram", "instagram.com"),
    "pinterest.com": ("Pinterest", "pinterest.com"),
    "legal.pinterest.com": ("Pinterest", "pinterest.com"),
    "inspire.pinterest.com": ("Pinterest", "pinterest.com"),
    "explore.pinterest.com": ("Pinterest", "pinterest.com"),
    "reddit.com": ("Reddit", "reddit.com"),
    "snapchat.com": ("Snapchat", "snapchat.com"),
    "spotify.com": ("Spotify", "spotify.com"),
    "legal.spotify.com": ("Spotify", "spotify.com"),
    "patreon.com": ("Patreon", "patreon.com"),
    "email.patreon.com": ("Patreon", "patreon.com"),
    "mailgun.patreon.com": ("Patreon", "patreon.com"),
    "nintendo.net": ("Nintendo", "nintendo.com"),
    "blizzard.com": ("Blizzard Entertainment", "blizzard.com"),
    "e.blizzard.com": ("Blizzard Entertainment", "blizzard.com"),
    "battle.net": ("Battle.net (Blizzard)", "battle.net"),
    "e.battle.net": ("Battle.net (Blizzard)", "battle.net"),
    "steampowered.com": ("Steam", "steampowered.com"),
    "ea.com": ("Electronic Arts (EA)", "ea.com"),
    "e.ea.com": ("Electronic Arts (EA)", "ea.com"),
    "fanduel.com": ("FanDuel", "fanduel.com"),
    "ma.sb.fanduel.com": ("FanDuel", "fanduel.com"),
    "racing.fanduel.com": ("FanDuel", "fanduel.com"),
    "ab.fanduel.com": ("FanDuel", "fanduel.com"),
    "draftkings.com": ("DraftKings", "draftkings.com"),
    "account.draftkings.com": ("DraftKings", "draftkings.com"),
    "auth.draftkings.com": ("DraftKings", "draftkings.com"),
    "espnbet.com": ("ESPN BET", "espnbet.com"),
    "notifications.espnbet.com": ("ESPN BET", "espnbet.com"),
    "promos.espnbet.com": ("ESPN BET", "espnbet.com"),
    "thescore.bet": ("theScore Bet", "thescore.bet"),
    "promos.thescore.bet": ("theScore Bet", "thescore.bet"),
    "marketing.thescore.bet": ("ESPN BET", "espnbet.com"),
    "bet365.com": ("bet365", "bet365.com"),
    "my.bet365.com": ("bet365", "bet365.com"),
    "pennplay.com": ("PENN Play", "pennplay.com"),
    "e.pennplay.com": ("PENN Play", "pennplay.com"),
    "gametime.co": ("Gametime", "gametime.co"),
    "m.gametime.co": ("Gametime", "gametime.co"),
    "e.gametime.co": ("Gametime", "gametime.co"),
    "seatgeek.com": ("SeatGeek", "seatgeek.com"),
    "mlb.com": ("MLB", "mlb.com"),
    "account-mail.mlb.com": ("MLB", "mlb.com"),
    "ncaa.com": ("NCAA", "ncaa.com"),
    "mail2.ncaa.com": ("NCAA", "ncaa.com"),
    "maxpreps-email.com": ("MaxPreps", "maxpreps.com"),
    "fans.maxpreps-email.com": ("MaxPreps", "maxpreps.com"),
    "drafthouse.com": ("Alamo Drafthouse", "drafthouse.com"),
    "eventbrite.com": ("Eventbrite", "eventbrite.com"),
    "plans.eventbrite.com": ("Eventbrite", "eventbrite.com"),

    # Health, Fitness, Travel, Education & Civic
    "lifestance.com": ("LifeStance Health", "lifestance.com"),
    "care.lifestance.com": ("LifeStance Health", "lifestance.com"),
    "phreesia-mail.com": ("LifeStance Health", "lifestance.com"),
    "advancedmd.com": ("LifeStance Health", "lifestance.com"),
    "closeknithealth.com": ("CloseKnit Health", "closeknithealth.com"),
    "email.closeknit.com": ("CloseKnit Health", "closeknithealth.com"),
    "patient-message.com": ("CloseKnit Health", "closeknithealth.com"),
    "cpc-health.com": ("Comprehensive Primary Care", "cpc-health.com"),
    "eclinicalmail.com": ("Comprehensive Primary Care", "cpc-health.com"),
    "culinahealth.com": ("Culina Health", "culinahealth.com"),
    "mail.culinahealth.com": ("Culina Health", "culinahealth.com"),
    "cvs.com": ("CVS / MinuteClinic", "cvs.com"),
    "alerts.cvs.com": ("CVS / MinuteClinic", "cvs.com"),
    "onelifefitness.com": ("Onelife Fitness", "onelifefitness.com"),
    "trainheroic.com": ("TrainHeroic", "trainheroic.com"),
    "mail.trainheroic.com": ("TrainHeroic", "trainheroic.com"),
    "capitaldigestivecare.com": ("Capital Digestive Care", "capitaldigestivecare.com"),
    "rideempower.com": ("Empower Ride", "rideempower.com"),
    "amtrak.com": ("Amtrak", "amtrak.com"),
    "e-mail.amtrak.com": ("Amtrak", "amtrak.com"),
    "acn.amtrak.com": ("Amtrak", "amtrak.com"),
    "email-sendonly.amtrak.com": ("Amtrak", "amtrak.com"),
    "airbnb.com": ("Airbnb", "airbnb.com"),
    "marriott.com": ("Marriott", "marriott.com"),
    "ancestry.com": ("Ancestry", "ancestry.com"),
    "email.ancestry.com": ("Ancestry", "ancestry.com"),
    "aacpl.net": ("Anne Arundel County Library", "aacpl.net"),
    "montgomerycountymd.gov": ("Montgomery County MD", "montgomerycountymd.gov"),
    "georgetown.edu": ("Georgetown University", "georgetown.edu"),
    "catholic.campusespmail.com": ("Catholic University", "catholic.edu"),
    "cua.edu": ("Catholic University", "catholic.edu"),
    "umbc.edu": ("UMBC", "umbc.edu"),
    "gwu.edu": ("George Washington University", "gwu.edu"),
    "vt.edu": ("Virginia Tech", "vt.edu"),
    "american.edu": ("American University", "american.edu"),
    "harvard.edu": ("Harvard University", "harvard.edu"),
    "careers-notifications.harvard.edu": ("Harvard University", "harvard.edu"),
    "recruiting.jh.edu": ("Johns Hopkins University", "jhu.edu"),
    "newhaven.edu": ("University of New Haven", "newhaven.edu"),
    "chegg.com": ("Chegg", "chegg.com"),
    "emails.chegg.com": ("Chegg", "chegg.com"),
    "theceshop.com": ("The CE Shop", "theceshop.com"),
    "academia-mail.com": ("Academia.edu", "academia.edu"),
    "collegeboard.org": ("College Board", "collegeboard.org"),
    "e.collegeboard.org": ("College Board", "collegeboard.org"),
    "16personalities.com": ("16Personalities", "16personalities.com"),
    "theanalystacademy.com": ("The Analyst Academy", "theanalystacademy.com"),
    "peacecorps.gov": ("Peace Corps", "peacecorps.gov"),
    "dctutormentor.org": ("DC Tutor & Mentor Initiative", "dctutormentor.org"),
    "mobilize.us": ("Mobilize", "mobilize.us"),
    "newsletter.mobilize.us": ("Mobilize", "mobilize.us"),
    "133493994.mailchimpapp.com": ("Beat the Streets DC", "btsdc.org"),
    "housecommunications.gov": ("U.S. House of Representatives", "house.gov"),
    "mail8.housecommunications.gov": ("U.S. House of Representatives", "house.gov"),
    "mail.house.gov": ("U.S. House of Representatives", "house.gov"),
    "men4choice.org": ("Men4Choice", "men4choice.org"),
    "progressivepipeline.org": ("Progressive Pipeline", "progressivepipeline.org"),
    "mail.progressivepipeline.org": ("Progressive Pipeline", "progressivepipeline.org"),
    "secondday.org": ("Second Day", "secondday.org"),
    "nlc.org": ("New Leaders Council", "nlc.org"),
    "environmentamerica.org": ("Environment America", "environmentamerica.org"),
    "id.me": ("ID.me", "id.me"),
    "mywallet.id.me": ("ID.me", "id.me"),
    "feedback.id.me": ("ID.me", "id.me"),
    "surveytakingjunkie.com": ("Survey Junkie", "surveyjunkie.com"),
    "surveyjunkiemail.com": ("Survey Junkie", "surveyjunkie.com"),
    "swagbucks.com": ("Swagbucks", "swagbucks.com"),
    "housesitter.com": ("HouseSitter.com", "housesitter.com"),
    "mail.housesitter.com": ("HouseSitter.com", "housesitter.com"),
    "wagwalking.com": ("Wag!", "wagwalking.com"),
    "email.wagwalking.com": ("Wag!", "wagwalking.com"),
    "hinge.co": ("Hinge", "hinge.co"),
    "mail.hinge.co": ("Hinge", "hinge.co"),
}

# Subdomain prefixes that should be stripped to find the root company domain
MAILER_SUBDOMAIN_TOKENS = {
    "mail", "email", "emails", "e-mail", "e", "em", "em1", "em-s", "p", "g", "m",
    "ohhey", "alerts", "hello", "us", "engage", "learn", "send", "team",
    "announcements", "newsletter", "notifications", "notify", "message", "messaging",
    "mktg", "imktg", "shareholderdocs", "priority", "care", "ma", "sb", "exmac",
    "mailcenter", "info", "news", "updates", "account", "accounts", "account-mail",
    "accountprotection", "auth", "signin", "legal", "support", "community",
    "promos", "marketing", "racing", "ab", "fans", "mail2", "mail8", "mailmc",
    "mailgun", "mg1", "md", "app", "go", "try", "explore", "inspire", "plans",
    "candidates", "employer", "recruiting", "careers-notifications", "services",
    "email-sendonly", "acn", "my", "mywallet", "feedback", "checkout",
}

GENERIC_SENDER_NAMES = {
    "no-reply", "noreply", "do-not-reply", "donotreply", "do not reply",
    "support", "info", "hello", "notifications", "updates", "alerts",
    "team", "admin", "billing", "service", "customer service", "unknown",
}


def extract_root_domain(domain: str) -> str:
    """Reduce a subdomain like 'learn.mail.monday.com' or 'email.us.puma.com' to 'monday.com' / 'puma.com'."""
    d = (domain or "").strip().lower()
    if not d:
        return ""
    if d in KNOWN_COMPANIES:
        return KNOWN_COMPANIES[d][1]

    parts = [p for p in d.split(".") if p]
    while len(parts) > 2 and parts[0] in MAILER_SUBDOMAIN_TOKENS:
        parts.pop(0)

    if len(parts) > 2 and parts[-1] in ("com", "org", "net", "edu", "gov", "io", "ai", "co", "us", "app", "dev", "tv", "me", "bet", "shop", "run", "md", "so"):
        root_candidate = ".".join(parts[-2:])
        return root_candidate
    return ".".join(parts)


def _clean_display_name_for_company(sender_name: str) -> str:
    """Clean a raw From display name into a concise company name if possible."""
    if not sender_name:
        return ""
    s = sender_name.strip()
    if "@" in s and " " not in s:
        return ""
    # Handle "... via Company" or "... (Company)"
    via_m = re.search(r"\bvia\s+([A-Za-z0-9.*&\-\s]+)$", s, flags=re.IGNORECASE)
    if via_m:
        cand = via_m.group(1).strip()
        if cand and cand.lower() not in GENERIC_SENDER_NAMES:
            return cand
    # Handle "Person at/from Company"
    from_m = re.search(r"\b(?:from|at|@)\s+([A-Z][A-Za-z0-9.*&\-\s]{1,28})$", s)
    if from_m:
        cand = from_m.group(1).strip()
        if cand and cand.lower() not in GENERIC_SENDER_NAMES:
            return cand

    # Strip common noise suffixes
    s = re.sub(
        r"\s*(?:\|.*|\b(?:Team|Support|Alerts|Notifications|Digest|Shopping|Info|Updates|Insights|Newsletter|Special Offers|Service Alerts|Account Service|Sportsbook|Community Cloud)\b.*)$",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    s = re.sub(r"^The\s+(.+?)\s+Team$", r"\1", s, flags=re.IGNORECASE).strip()
    if s.lower() in GENERIC_SENDER_NAMES or len(s) < 2:
        return ""
    return s


def resolve_company_identity(sender_domain: str, sender_name: str = "", sender_email: str = "") -> Tuple[str, str]:
    """
    Return (company_name, canonical_logo_domain) for an email sender.
    Never returns an email address as the company name.
    """
    dom = (sender_domain or "").strip().lower()
    if not dom and "@" in (sender_email or ""):
        dom = sender_email.split("@")[-1].strip().lower()

    # Special handling for personal/free domains that matched corporate filters (e.g. newsletters sent via gmail.com)
    if dom in ("gmail.com", "icloud.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com"):
        cleaned = _clean_display_name_for_company(sender_name)
        if cleaned:
            return (f"{cleaned} (via {dom.split('.')[0].capitalize()})", dom)
        return (dom.split(".")[0].capitalize(), dom)

    # 1. Exact domain match in curated table
    if dom in KNOWN_COMPANIES:
        return KNOWN_COMPANIES[dom]

    # 2. Root domain match in curated table
    root_dom = extract_root_domain(dom)
    if root_dom in KNOWN_COMPANIES:
        return KNOWN_COMPANIES[root_dom]

    # 3. Try cleaned display name if it looks like a real brand/organization
    cleaned_name = _clean_display_name_for_company(sender_name)
    if cleaned_name and not re.match(r"^[a-z0-9._+-]+@[a-z0-9.-]+$", cleaned_name.lower()):
        # Avoid using a single person's first+last name when the domain itself is the company brand
        stem = root_dom.split(".")[0] if root_dom else ""
        if stem and (
            stem in cleaned_name.lower().replace(" ", "")
            or len(cleaned_name.split()) != 2
            or any(tok in cleaned_name for tok in ("Inc", "LLC", "Health", "University", "Center", "County", "Foundation", "Club", "Academy", "Studio", "Group", "Care", "Bank", "Shop", "Media", "Sports", "AI", " Lab"))
        ):
            return (cleaned_name, root_dom or dom)

    # 4. Derive human-readable Company Name from the root domain stem
    if root_dom:
        stem = root_dom.split(".")[0]
        stripped = re.sub(r"(-mail|-email|mail|email|app)$", "", stem)
        if len(stripped) >= 3:
            stem = stripped
        pretty = " ".join(w.capitalize() if len(w) > 2 else w.upper() for w in re.split(r"[-_]+", stem) if w)
        if pretty:
            return (pretty, root_dom)

    if cleaned_name:
        return (cleaned_name, root_dom or dom or "example.com")

    return ("Corporate Sender", root_dom or dom or "example.com")


# ---------------------------------------------------------------------------
# Company Logo Manager (16x16 Treeview Logos & 32x32 Header Banner Logos)
# ---------------------------------------------------------------------------
def _lerp(a: int, b: int, t: float) -> int:
    return max(0, min(255, int(round(a + (b - a) * t))))


def _make_fallback_company_badge(company_name: str, size: int = 16) -> tk.PhotoImage:
    """
    Generate a glossy Frutiger Aero 3D rounded square company badge in memory
    (deterministic color per company name) used before/if a network favicon loads.
    """
    palettes = [
        ((2, 132, 199), (125, 211, 252)),   # Aero Blue
        ((13, 148, 136), (153, 246, 228)),  # Aero Teal
        ((5, 150, 105), (167, 243, 208)),   # Aero Emerald
        ((79, 70, 229), (199, 210, 254)),   # Aero Indigo
        ((217, 119, 6), (253, 230, 138)),   # Aero Amber
        ((225, 29, 72), (254, 205, 211)),   # Aero Coral
    ]
    h = sum(ord(c) for c in (company_name or "C"))
    base_rgb, spec_rgb = palettes[h % len(palettes)]

    rows = []
    rad = max(3, size // 4)
    for y in range(size):
        row = bytearray(b"\x00")
        t = y / max(1, size - 1)
        for x in range(size):
            # Rounded corner distance check
            dx = max(0, rad - x, x - (size - 1 - rad))
            dy = max(0, rad - y, y - (size - 1 - rad))
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > rad + 0.5:
                row.extend((0, 0, 0, 0))
                continue
            alpha = 255 if dist <= rad - 0.5 else int(255 * (rad + 0.5 - dist))
            is_border = (x == 0 or x == size - 1 or y == 0 or y == size - 1 or dist >= rad - 1.1)
            if is_border:
                r = int(base_rgb[0] * 0.65)
                g = int(base_rgb[1] * 0.65)
                b = int(base_rgb[2] * 0.65)
            elif t < 0.45:
                lt = t / 0.45
                r = _lerp(spec_rgb[0], base_rgb[0], lt)
                g = _lerp(spec_rgb[1], base_rgb[1], lt)
                b = _lerp(spec_rgb[2], base_rgb[2], lt)
            else:
                lt = (t - 0.45) / 0.55
                r = _lerp(base_rgb[0], min(255, base_rgb[0] + 35), lt)
                g = _lerp(base_rgb[1], min(255, base_rgb[1] + 45), lt)
                b = _lerp(base_rgb[2], min(255, base_rgb[2] + 45), lt)

            # Inner center subtle white emblem dot/square so it reads as a company icon
            cx, cy = (size - 1) / 2.0, (size - 1) / 2.0
            if abs(x - cx) <= size * 0.22 and abs(y - cy) <= size * 0.22 and not is_border:
                r = _lerp(r, 255, 0.78)
                g = _lerp(g, 255, 0.78)
                b = _lerp(b, 255, 0.78)

            row.extend((r, g, b, alpha))
        rows.append(bytes(row))

    b64_png = _encode_rgba_png(size, size, rows)
    return tk.PhotoImage(data=b64_png)


class CompanyLogoManager:
    """
    Fetches and caches real 16x16 and 32x32 PNG company logos by canonical domain,
    storing authentic PNG BLOBs directly in SQLite (company_logos table) with
    zero loose disk files, converting them into Tkinter PhotoImage objects on the main thread.
    """

    def __init__(self, ssl_ctx: ssl.SSLContext, db_path: str = SQLITE_CACHE_PATH):
        self.ssl_ctx = ssl_ctx
        self.db_path = db_path
        self._photo_cache: Dict[Tuple[str, int], tk.PhotoImage] = {}
        self._fallback_cache: Dict[Tuple[str, int], tk.PhotoImage] = {}
        self._in_flight: Set[str] = set()
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        self._init_logo_db()
        self.migrate_loose_logos_to_db()

    def _init_logo_db(self):
        with self._db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
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
                conn.close()
            except Exception:
                pass

    def migrate_loose_logos_to_db(self):
        """Import any existing loose PNG files into SQLite company_logos and remove loose directory."""
        if not os.path.exists(LOGO_CACHE_DIR):
            return
        try:
            entries = os.listdir(LOGO_CACHE_DIR)
        except Exception:
            return
        if not entries:
            try:
                os.rmdir(LOGO_CACHE_DIR)
            except Exception:
                pass
            return

        rows = []
        now_ts = int(time.time())
        files_to_remove = []
        for fname in entries:
            if fname.endswith(".png"):
                full_p = os.path.join(LOGO_CACHE_DIR, fname)
                base = fname[:-4]
                if "_" in base:
                    dom, sz_str = base.rsplit("_", 1)
                    try:
                        sz = int(sz_str)
                    except ValueError:
                        sz = 16
                else:
                    dom = base
                    sz = 16
                try:
                    with open(full_p, "rb") as f:
                        data = f.read()
                    if data.startswith(b"\x89PNG\r\n\x1a\n"):
                        rows.append((dom.lower().strip(), sz, data, now_ts))
                    files_to_remove.append(full_p)
                except Exception:
                    pass

        if rows:
            with self._db_lock:
                try:
                    conn = sqlite3.connect(self.db_path)
                    conn.executemany(
                        "INSERT OR REPLACE INTO company_logos (domain, size, logo_png, updated_at) VALUES (?, ?, ?, ?)",
                        rows,
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

        for p in files_to_remove:
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(LOGO_CACHE_DIR)
        except Exception:
            pass

    def get_logo(self, company_name: str, logo_domain: str, size: int = 16) -> tk.PhotoImage:
        """Return a scaled PhotoImage (e.g. 16, 20, 24, 32, 48) for the company from SQLite BLOB cache."""
        dom = (logo_domain or "").lower().strip()
        key = (dom, size)
        if dom and key in self._photo_cache:
            return self._photo_cache[key]

        # Query SQLite company_logos table
        if dom:
            blob = None
            with self._db_lock:
                try:
                    conn = sqlite3.connect(self.db_path)
                    cur = conn.cursor()
                    cur.execute("SELECT size, logo_png FROM company_logos WHERE domain = ?", (dom,))
                    results = cur.fetchall()
                    conn.close()
                    if results:
                        exact = [r for r in results if r[0] == size]
                        if exact:
                            blob = exact[0][1]
                        else:
                            pref = 32 if size > 20 else 16
                            pref_match = [r for r in results if r[0] == pref]
                            if pref_match:
                                blob = pref_match[0][1]
                            else:
                                blob = results[0][1]
                except Exception:
                    pass

            if blob and blob.startswith(b"\x89PNG\r\n\x1a\n"):
                try:
                    b64 = base64.b64encode(blob).decode("ascii")
                    img = tk.PhotoImage(data=b64)
                    if img.width() > 0 and img.height() > 0:
                        w, h = img.width(), img.height()
                        if w > size * 1.3:
                            factor = max(1, round(w / size))
                            img = img.subsample(factor, factor)
                        elif w < size * 0.7:
                            factor = max(1, round(size / w))
                            img = img.zoom(factor, factor)
                        self._photo_cache[key] = img
                        return img
                except Exception:
                    pass

        fb_key = (company_name or "C", size)
        if fb_key not in self._fallback_cache:
            self._fallback_cache[fb_key] = _make_fallback_company_badge(company_name, size)
        return self._fallback_cache[fb_key]

    def get_logo_16(self, company_name: str, logo_domain: str) -> tk.PhotoImage:
        """Backward compatible helper for 16x16 logo."""
        return self.get_logo(company_name, logo_domain, size=16)

    def get_logo_32(self, company_name: str, logo_domain: str) -> tk.PhotoImage:
        """Backward compatible helper for 32x32 logo."""
        return self.get_logo(company_name, logo_domain, size=32)

    def prefetch_domains_async(self, domains: Set[str], on_batch_complete_main_thread: Optional[Callable[[], None]] = None):
        """Download any uncached 16x16 and 32x32 PNG company logos in background threads and store as BLOBs in SQLite."""
        missing = []
        cached_domains = set()
        with self._db_lock:
            try:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                cached_domains = {r[0] for r in cur.execute("SELECT DISTINCT domain FROM company_logos").fetchall()}
                conn.close()
            except Exception:
                pass

        with self._lock:
            for d in domains:
                dom = (d or "").lower().strip()
                if not dom or dom in self._in_flight or dom in cached_domains:
                    continue
                self._in_flight.add(dom)
                missing.append(dom)

        if not missing:
            return

        def fetch_one(dom: str) -> bool:
            fetched_rows = []
            now_ts = int(time.time())
            for sz in (16, 32):
                url = f"https://www.google.com/s2/favicons?domain={urllib.parse.quote(dom)}&sz={sz}"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=6) as resp:
                        data = resp.read()
                    if data and data.startswith(b"\x89PNG\r\n\x1a\n"):
                        fetched_rows.append((dom, sz, data, now_ts))
                except Exception:
                    pass

            if fetched_rows:
                with self._db_lock:
                    try:
                        conn = sqlite3.connect(self.db_path)
                        conn.executemany(
                            "INSERT OR REPLACE INTO company_logos (domain, size, logo_png, updated_at) VALUES (?, ?, ?, ?)",
                            fetched_rows,
                        )
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass

            with self._lock:
                self._in_flight.discard(dom)
            return bool(fetched_rows)

        def coordinator():
            completed_since_notify = 0
            futures = [self._executor.submit(fetch_one, d) for d in missing]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    if fut.result():
                        completed_since_notify += 1
                except Exception:
                    pass
                if completed_since_notify >= 15 and on_batch_complete_main_thread:
                    completed_since_notify = 0
                    on_batch_complete_main_thread()
            if on_batch_complete_main_thread:
                on_batch_complete_main_thread()

        threading.Thread(target=coordinator, daemon=True).start()


AUTOMELINIUM_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_AUTOMELINIUM_LOGO_CACHE: Dict[int, tk.PhotoImage] = {}


def get_automelinium_logo(size: int = 32) -> tk.PhotoImage:
    """
    Return a scaled PhotoImage of the official Automelinium logo
    (Windows Millennium Edition style waving logo with crimson motif).
    Scales smoothly with high-quality resampling and caches the result.
    """
    target_size = max(12, int(size))
    if target_size in _AUTOMELINIUM_LOGO_CACHE:
        return _AUTOMELINIUM_LOGO_CACHE[target_size]

    # 1. Check exact pre-rendered file in assets
    exact_path = os.path.join(AUTOMELINIUM_ASSETS_DIR, f"automelinium_logo_{target_size}.png")
    if os.path.exists(exact_path):
        try:
            with open(exact_path, "rb") as f:
                data = f.read()
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                img = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
                _AUTOMELINIUM_LOGO_CACHE[target_size] = img
                return img
        except Exception:
            pass

    # 2. Resample from master centered image using Pillow if available
    master_path = os.path.join(AUTOMELINIUM_ASSETS_DIR, "automelinium_logo_centered.png")
    if not os.path.exists(master_path):
        master_path = os.path.join(AUTOMELINIUM_ASSETS_DIR, "automelinium_logo_master.png")

    if os.path.exists(master_path):
        try:
            from PIL import Image
            import io
            with Image.open(master_path) as im:
                resized = im.resize((target_size, target_size), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                resized.save(buf, format="PNG")
                png_bytes = buf.getvalue()
                img = tk.PhotoImage(data=base64.b64encode(png_bytes).decode("ascii"))
                _AUTOMELINIUM_LOGO_CACHE[target_size] = img
                return img
        except Exception:
            pass

    # 3. Fallback to subsampling or zooming closest pre-rendered file
    candidate_files = []
    if os.path.exists(AUTOMELINIUM_ASSETS_DIR):
        for fn in os.listdir(AUTOMELINIUM_ASSETS_DIR):
            m = re.match(r"^automelinium_logo_(\d+)\.png$", fn)
            if m:
                candidate_files.append((int(m.group(1)), os.path.join(AUTOMELINIUM_ASSETS_DIR, fn)))

    if candidate_files:
        candidate_files.sort(key=lambda x: abs(x[0] - target_size))
        best_sz, best_path = candidate_files[0]
        try:
            with open(best_path, "rb") as f:
                data = f.read()
            img = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
            w = img.width()
            if w > target_size * 1.3:
                fct = max(1, round(w / target_size))
                img = img.subsample(fct, fct)
            elif w < target_size * 0.7:
                fct = max(1, round(target_size / w))
                img = img.zoom(fct, fct)
            _AUTOMELINIUM_LOGO_CACHE[target_size] = img
            return img
        except Exception:
            pass

    # 4. Ultimate fallback to crimson aero orb
    from aero_theme import make_glass_orb_image
    fallback = make_glass_orb_image(target_size, (190, 18, 60), (255, 180, 190))
    _AUTOMELINIUM_LOGO_CACHE[target_size] = fallback
    return fallback
