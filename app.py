"""
Reply Review Portal
-------------------
Reads customer messages + GPT replies from `inference_log` and writes a
good/bad rating PER REPLY to the `feedback` table (linked by inference_id).

Layout:
  - Name + date filter across the top
  - A paginated table (10 customer comments per page) of shaded cards
  - Each card: the customer message once, then each reply on its own line
    with Reason | Notes | Good/Bad
  - Translate button -> a second, English table below the main one

Run locally:   streamlit run app.py
The Supabase key is read from Streamlit secrets, never hardcoded.
"""

import html
import math
from datetime import datetime, date, timedelta

import streamlit as st
from supabase import create_client, Client

try:
    from deep_translator import GoogleTranslator
    TRANSLATE_AVAILABLE = True
except Exception:
    TRANSLATE_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SUPABASE_URL = "https://ejbbsnkqhbmldxbrewof.supabase.co"
GOOD_RATING = 1
BAD_RATING = 0
PAGE_SIZE = 10

# Alternating card colors so each comment group is easy to tell apart.
SHADES = ["#eef4ff", "#eafbf0"]          # light blue / light green
SHADE_BORDER = ["#c7dbff", "#bff0d0"]

st.set_page_config(page_title="Reply Review Portal", page_icon="📝", layout="wide")


# --------------------------------------------------------------------------- #
# Supabase client
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_client() -> Client:
    key = st.secrets.get("SUPABASE_KEY")
    if not key:
        st.error(
            "No SUPABASE_KEY found. Add it to .streamlit/secrets.toml locally, "
            "or to the Secrets box in Streamlit Cloud."
        )
        st.stop()
    return create_client(SUPABASE_URL, key)


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=120)
def load_logs(start: date, end: date):
    client = get_client()
    start_iso = datetime.combine(start, datetime.min.time()).isoformat()
    end_iso = datetime.combine(end, datetime.max.time()).isoformat()
    resp = (
        client.table("inference_log")
        .select("id, created_at, customer_message, our_reply, our_escalation")
        .gte("created_at", start_iso)
        .lte("created_at", end_iso)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@st.cache_data(ttl=30)
def load_feedback(inference_ids: tuple):
    if not inference_ids:
        return {}
    client = get_client()
    resp = (
        client.table("feedback")
        .select("inference_id, rating, rater, reason, notes, created_at")
        .in_("inference_id", list(inference_ids))
        .order("created_at", desc=True)
        .execute()
    )
    latest = {}
    for row in resp.data or []:
        latest.setdefault(row["inference_id"], row)
    return latest


def save_feedback(inference_id, rating, rater, reason, notes):
    client = get_client()
    client.table("feedback").insert({
        "inference_id": inference_id,
        "rating": rating,
        "rater": rater or None,
        "reason": reason or None,
        "notes": notes or None,
    }).execute()


# --------------------------------------------------------------------------- #
# Translation (cached in session to avoid re-translating on every rerun)
# --------------------------------------------------------------------------- #
def translate_it_en(text: str) -> str:
    if not text:
        return ""
    if not TRANSLATE_AVAILABLE:
        return "(translation library not available)"
    cache = st.session_state.setdefault("_tr_cache", {})
    if text in cache:
        return cache[text]
    try:
        out = GoogleTranslator(source="it", target="en").translate(text)
    except Exception as exc:
        out = f"(translation failed: {exc})"
    cache[text] = out
    return out


# --------------------------------------------------------------------------- #
# Top bar — title + filters
# --------------------------------------------------------------------------- #
st.title("📝 Reply Review Portal")

today = date.today()
f1, f2, f3 = st.columns([2, 3, 1])
with f1:
    rater_name = st.text_input("Your name", value="", placeholder="reviewer name")
with f2:
    date_range = st.date_input(
        "Date range",
        value=(today - timedelta(days=2), today),
        max_value=today,
    )
with f3:
    st.write("")
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range if isinstance(date_range, date) else today

st.divider()


# --------------------------------------------------------------------------- #
# Load + group by customer message
# --------------------------------------------------------------------------- #
logs = load_logs(start_date, end_date)
if not logs:
    st.info("No messages found in this date range.")
    st.stop()

groups = {}
for row in logs:
    msg = (row.get("customer_message") or "").strip() or "(empty message)"
    groups.setdefault(msg, []).append(row)

groups_list = list(groups.items())
total = len(groups_list)
total_pages = max(1, math.ceil(total / PAGE_SIZE))

# Page state
page = st.session_state.get("page", 0)
page = max(0, min(page, total_pages - 1))
st.session_state["page"] = page

start_i = page * PAGE_SIZE
page_groups = groups_list[start_i:start_i + PAGE_SIZE]

# Feedback for everything on this page
page_ids = tuple(r["id"] for _, rows in page_groups for r in rows)
existing = load_feedback(page_ids)

st.caption(
    f"{total} customer message(s) in range · "
    f"showing {start_i + 1}–{min(start_i + PAGE_SIZE, total)} (page {page + 1} of {total_pages})"
)


# --------------------------------------------------------------------------- #
# Helpers for rendering
# --------------------------------------------------------------------------- #
def status_badge(prior):
    if not prior:
        return ""
    if prior["rating"] == GOOD_RATING:
        return "<span style='color:#0a7a32;font-weight:600'>✅ GOOD</span>"
    return "<span style='color:#b00020;font-weight:600'>❌ BAD</span>"


def card_header(msg, idx):
    shade = SHADES[idx % 2]
    border = SHADE_BORDER[idx % 2]
    st.markdown(
        f"<div style='background:{shade};border:1px solid {border};"
        f"padding:10px 14px;border-radius:8px;margin-bottom:6px'>"
        f"<b>💬 Customer message</b><br>{html.escape(msg)}</div>",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Main table
# --------------------------------------------------------------------------- #
for gidx, (msg, rows) in enumerate(page_groups):
    with st.container(border=True):
        card_header(msg, gidx)

        # column header row
        h = st.columns([3, 2, 2, 2])
        h[0].caption("Reply")
        h[1].caption("Reason")
        h[2].caption("Notes")
        h[3].caption("Feedback")

        for j, row in enumerate(rows, start=1):
            rid = row["id"]
            reply = row.get("our_reply") or "(no reply text)"
            prior = existing.get(rid)

            c = st.columns([3, 2, 2, 2])
            with c[0]:
                tag = " · escalated" if row.get("our_escalation") else ""
                st.markdown(f"**Reply {j}**{tag}")
                st.write(reply)
                if prior:
                    st.markdown(status_badge(prior), unsafe_allow_html=True)
                    if prior.get("rater"):
                        st.caption(f"by {prior['rater']}")
            reason = c[1].text_input("reason", key=f"reason_{rid}",
                                     label_visibility="collapsed", placeholder="reason")
            notes = c[2].text_area("notes", key=f"notes_{rid}",
                                   label_visibility="collapsed", placeholder="notes", height=70)
            with c[3]:
                if st.button("✅ Good", key=f"good_{rid}", use_container_width=True, type="primary"):
                    save_feedback(rid, GOOD_RATING, rater_name, reason, notes)
                    load_feedback.clear()
                    st.rerun()
                if st.button("❌ Bad", key=f"bad_{rid}", use_container_width=True):
                    save_feedback(rid, BAD_RATING, rater_name, reason, notes)
                    load_feedback.clear()
                    st.rerun()


# --------------------------------------------------------------------------- #
# Pagination controls
# --------------------------------------------------------------------------- #
st.write("")
p1, p2, p3 = st.columns([1, 2, 1])
with p1:
    if page > 0 and st.button("⬅️ Previous", use_container_width=True):
        st.session_state["page"] = page - 1
        st.rerun()
with p2:
    st.markdown(
        f"<div style='text-align:center;padding-top:6px'>Page {page + 1} of {total_pages}</div>",
        unsafe_allow_html=True,
    )
with p3:
    if page < total_pages - 1 and st.button("Next ➡️", use_container_width=True):
        st.session_state["page"] = page + 1
        st.rerun()


# --------------------------------------------------------------------------- #
# Translate the whole (visible) table -> English table below
# --------------------------------------------------------------------------- #
st.divider()
if st.toggle("🌐 Translate this page to English"):
    st.subheader("Translated (English)")
    for gidx, (msg, rows) in enumerate(page_groups):
        with st.container(border=True):
            shade = SHADES[gidx % 2]
            border = SHADE_BORDER[gidx % 2]
            st.markdown(
                f"<div style='background:{shade};border:1px solid {border};"
                f"padding:10px 14px;border-radius:8px;margin-bottom:6px'>"
                f"<b>💬 Customer message (EN)</b><br>{html.escape(translate_it_en(msg))}</div>",
                unsafe_allow_html=True,
            )
            for j, row in enumerate(rows, start=1):
                st.markdown(f"**Reply {j} (EN)**")
                st.write(translate_it_en(row.get("our_reply") or ""))
