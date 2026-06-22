"""
Reply Review Portal
-------------------
Reads customer messages + GPT replies from `inference_log` and writes a
good/bad rating PER REPLY to the `feedback` table (linked by inference_id).

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

# Table that maps model_version_id -> friendly tag (v6, v8, ...).
# Change this if your table is named differently; the app falls back to
# short UUIDs if the name is wrong or the table can't be read.
MODEL_VERSIONS_TABLE = "model_versions"

SHADES = ["#eef4ff", "#eafbf0"]          # alternating card colors
SHADE_BORDER = ["#c7dbff", "#bff0d0"]

st.set_page_config(page_title="Reply Review Portal", page_icon="📝", layout="wide")

# Slightly smaller, tidier text throughout the cards.
st.markdown(
    "<style>.rv-text{font-size:0.85rem;line-height:1.45;}"
    ".rv-box{font-size:0.85rem;line-height:1.45;padding:9px 12px;border-radius:8px;}"
    ".rv-sep{margin:8px 0;border:none;border-top:1px dashed #b9b9b9;}</style>",
    unsafe_allow_html=True,
)


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
        .select("id, created_at, customer_message, our_reply, our_escalation, model_version_id")
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


@st.cache_data(ttl=600)
def load_version_tags():
    """Return {version_id: version_tag}. Empty dict if the table is unreadable."""
    try:
        client = get_client()
        resp = client.table(MODEL_VERSIONS_TABLE).select("id, version_tag").execute()
        return {r["id"]: r.get("version_tag") for r in (resp.data or [])}
    except Exception:
        return {}


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
# Translation  (always returns a string, never None)
# --------------------------------------------------------------------------- #
def translate_it_en(text) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if not TRANSLATE_AVAILABLE:
        return "(translation unavailable)"
    cache = st.session_state.setdefault("_tr_cache", {})
    if text in cache:
        return cache[text]
    try:
        out = GoogleTranslator(source="it", target="en").translate(text)
    except Exception as exc:
        out = f"(translation failed: {exc})"
    out = out if out else text          # guard against None / empty result
    cache[text] = str(out)
    return cache[text]


# --------------------------------------------------------------------------- #
# Render helpers
# --------------------------------------------------------------------------- #
def small_text(text):
    safe = html.escape(text or "").replace("\n", "<br>")
    st.markdown(f"<div class='rv-text'>{safe}</div>", unsafe_allow_html=True)


def msg_box(text, idx, label):
    shade, border = SHADES[idx % 2], SHADE_BORDER[idx % 2]
    safe = html.escape(text or "").replace("\n", "<br>")
    st.markdown(
        f"<div class='rv-box' style='background:{shade};border:1px solid {border};'>"
        f"<b>{label}</b><br>{safe}</div>",
        unsafe_allow_html=True,
    )


def status_badge(prior):
    if not prior:
        return ""
    if prior["rating"] == GOOD_RATING:
        return "<span style='color:#0a7a32;font-weight:600'>✅ GOOD</span>"
    return "<span style='color:#b00020;font-weight:600'>❌ BAD</span>"


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

# Model version filter (top of the table)
version_tags = load_version_tags()


def version_label(v):
    if v == "All versions":
        return "All versions"
    if v == "—":
        return "(no version)"
    tag = version_tags.get(v)
    return tag if tag else f"{str(v)[:8]}…"


versions = sorted(
    {(r.get("model_version_id") or "—") for r in logs},
    key=lambda v: version_label(v),
)
vcol, _ = st.columns([2, 4])
with vcol:
    selected_version = st.selectbox(
        "Model version",
        options=["All versions"] + versions,
        format_func=version_label,
    )
if selected_version != "All versions":
    logs = [r for r in logs if (r.get("model_version_id") or "—") == selected_version]
    if not logs:
        st.info("No messages for this model version in the date range.")
        st.stop()

groups = {}
for row in logs:
    msg = (row.get("customer_message") or "").strip() or "(empty message)"
    groups.setdefault(msg, []).append(row)

groups_list = list(groups.items())
total = len(groups_list)
total_pages = max(1, math.ceil(total / PAGE_SIZE))

page = max(0, min(st.session_state.get("page", 0), total_pages - 1))
st.session_state["page"] = page

start_i = page * PAGE_SIZE
page_groups = groups_list[start_i:start_i + PAGE_SIZE]

page_ids = tuple(r["id"] for _, rows in page_groups for r in rows)
existing = load_feedback(page_ids)

st.caption(
    f"{total} customer message(s) in range · "
    f"showing {start_i + 1}–{min(start_i + PAGE_SIZE, total)} (page {page + 1} of {total_pages})"
)


# --------------------------------------------------------------------------- #
# Main table
# --------------------------------------------------------------------------- #
for gidx, (msg, rows) in enumerate(page_groups):
    with st.container(border=True):
        msg_box(msg, gidx, "💬 Customer message")

        # Per-message translate button (sits right under the message)
        tkey = f"tr_{rows[0]['id']}"
        if st.button("🌐 Translate", key=f"btn_{tkey}"):
            st.session_state[tkey] = not st.session_state.get(tkey, False)
        show_tr = st.session_state.get(tkey, False)
        if show_tr:
            msg_box(translate_it_en(msg), gidx, "💬 Customer message (EN)")

        st.markdown("<hr class='rv-sep'>", unsafe_allow_html=True)

        h = st.columns([3, 2, 2, 3])
        h[0].caption("Reply")
        h[1].caption("Reason")
        h[2].caption("Notes")
        h[3].caption("Feedback")

        for j, row in enumerate(rows, start=1):
            rid = row["id"]
            reply = row.get("our_reply") or "(no reply text)"
            prior = existing.get(rid)

            c = st.columns([3, 2, 2, 3])
            with c[0]:
                tag = " · escalated" if row.get("our_escalation") else ""
                st.markdown(f"**Reply {j}**{tag}")
                small_text(reply)
                if show_tr:
                    st.markdown("<i class='rv-text'>EN:</i>", unsafe_allow_html=True)
                    small_text(translate_it_en(reply))
                if prior:
                    st.markdown(status_badge(prior), unsafe_allow_html=True)
                    if prior.get("rater"):
                        st.caption(f"by {prior['rater']}")
            reason = c[1].text_area("reason", key=f"reason_{rid}",
                                     label_visibility="collapsed", placeholder="reason", height=70)
            notes = c[2].text_area("notes", key=f"notes_{rid}",
                                   label_visibility="collapsed", placeholder="notes", height=70)
            with c[3]:
                fb = st.columns(2)
                if fb[0].button("✅ Good", key=f"good_{rid}", use_container_width=True, type="primary"):
                    save_feedback(rid, GOOD_RATING, rater_name, reason, notes)
                    load_feedback.clear()
                    st.rerun()
                if fb[1].button("❌ Bad", key=f"bad_{rid}", use_container_width=True):
                    save_feedback(rid, BAD_RATING, rater_name, reason, notes)
                    load_feedback.clear()
                    st.rerun()

            # divider between replies (not after the last one)
            if j < len(rows):
                st.markdown("<hr class='rv-sep'>", unsafe_allow_html=True)


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
