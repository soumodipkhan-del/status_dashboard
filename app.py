"""
Closio reply review dashboard
------------------------------
Reads customer messages + GPT replies from the `inference_log` table and
writes good/bad ratings back to the `feedback` table in Supabase.

Run locally:   streamlit run app.py
Deploy:        Streamlit Community Cloud (see README.md)

The Supabase API key is NEVER hardcoded here. It is read from Streamlit
secrets (.streamlit/secrets.toml locally, or the Secrets box on Streamlit
Cloud). See README.md.
"""

from datetime import datetime, date, timedelta

import streamlit as st
from supabase import create_client, Client

# Translation is optional at import time so the app still loads if the
# library is missing for any reason.
try:
    from deep_translator import GoogleTranslator
    TRANSLATE_AVAILABLE = True
except Exception:
    TRANSLATE_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SUPABASE_URL = "https://ejbbsnkqhbmldxbrewof.supabase.co"

# Map the friendly flag to the int2 `rating` column in the feedback table.
GOOD_RATING = 1
BAD_RATING = 0

st.set_page_config(page_title="Reply Review", page_icon="✅", layout="wide")


# --------------------------------------------------------------------------- #
# Supabase client
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_client() -> Client:
    """Create a single Supabase client for the session."""
    key = st.secrets.get("SUPABASE_KEY")
    if not key:
        st.error(
            "No SUPABASE_KEY found. Add it to .streamlit/secrets.toml locally, "
            "or to the Secrets box in Streamlit Cloud. See README.md."
        )
        st.stop()
    return create_client(SUPABASE_URL, key)


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=120)
def load_logs(start: date, end: date):
    """Fetch inference_log rows in the date window (deleted rows excluded)."""
    client = get_client()
    start_iso = datetime.combine(start, datetime.min.time()).isoformat()
    end_iso = datetime.combine(end, datetime.max.time()).isoformat()

    resp = (
        client.table("inference_log")
        .select(
            "id, created_at, conversation_id, customer_message, "
            "customer_language, customer_sentiment, customer_intent, "
            "our_reply, our_escalation, chatgpt_provided"
        )
        .gte("created_at", start_iso)
        .lte("created_at", end_iso)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@st.cache_data(ttl=30)
def load_feedback(inference_ids: tuple):
    """Fetch existing feedback rows for a set of inference ids."""
    if not inference_ids:
        return {}
    client = get_client()
    resp = (
        client.table("feedback")
        .select("id, inference_id, rating, rater, reason, notes, created_at")
        .in_("inference_id", list(inference_ids))
        .order("created_at", desc=True)
        .execute()
    )
    # Keep the most recent feedback per inference_id.
    latest = {}
    for row in resp.data or []:
        iid = row["inference_id"]
        if iid not in latest:
            latest[iid] = row
    return latest


def save_feedback(inference_id: str, rating: int, rater: str, reason: str, notes: str):
    """Insert a feedback row."""
    client = get_client()
    payload = {
        "inference_id": inference_id,
        "rating": rating,
        "rater": rater or None,
        "reason": reason or None,
        "notes": notes or None,
    }
    client.table("feedback").insert(payload).execute()


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #
def translate_it_en(text: str) -> str:
    if not TRANSLATE_AVAILABLE:
        return "(translation library not available)"
    if not text:
        return ""
    try:
        return GoogleTranslator(source="it", target="en").translate(text)
    except Exception as exc:
        return f"(translation failed: {exc})"


# --------------------------------------------------------------------------- #
# Sidebar — controls
# --------------------------------------------------------------------------- #
st.sidebar.title("Reply Review")
rater_name = st.sidebar.text_input("Your name (saved as rater)", value="")

st.sidebar.subheader("Date range")
today = date.today()
default_start = today - timedelta(days=2)
date_range = st.sidebar.date_input(
    "Created between",
    value=(default_start, today),
    max_value=today,
)
# date_input returns a single date until both ends are picked.
if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range if isinstance(date_range, date) else today

if st.sidebar.button("🔄 Refresh data"):
    st.cache_data.clear()
    st.rerun()


# --------------------------------------------------------------------------- #
# Load + group
# --------------------------------------------------------------------------- #
logs = load_logs(start_date, end_date)

if not logs:
    st.info("No messages found in this date range.")
    st.stop()

# Group rows by the customer message text. The same message can appear on
# several rows with different replies, so each group holds all of them.
groups = {}
for row in logs:
    msg = (row.get("customer_message") or "").strip()
    if not msg:
        msg = "(empty message)"
    groups.setdefault(msg, []).append(row)

message_texts = list(groups.keys())


def label_for(msg: str) -> str:
    n = len(groups[msg])
    preview = msg if len(msg) <= 70 else msg[:67] + "..."
    tag = f"  [{n} replies]" if n > 1 else ""
    return preview + tag


st.sidebar.subheader("Messages")
search = st.sidebar.text_input("Filter messages", value="")
filtered = [m for m in message_texts if search.lower() in m.lower()] or message_texts

selected_msg = st.sidebar.radio(
    f"{len(filtered)} message(s)",
    options=filtered,
    format_func=label_for,
)


# --------------------------------------------------------------------------- #
# Main panel
# --------------------------------------------------------------------------- #
rows = groups[selected_msg]
existing = load_feedback(tuple(r["id"] for r in rows))

st.subheader("Customer message")
st.markdown(f"> {selected_msg}")

if st.toggle("🌐 Translate message to English", key="tr_customer_msg"):
    st.info(translate_it_en(selected_msg))

meta = rows[0]
cols = st.columns(4)
cols[0].metric("Language", meta.get("customer_language") or "—")
cols[1].metric("Sentiment", meta.get("customer_sentiment") or "—")
cols[2].metric("Intent", meta.get("customer_intent") or "—")
cols[3].metric("Replies", len(rows))

st.divider()
st.subheader(f"Replies ({len(rows)})")

for i, row in enumerate(rows, start=1):
    rid = row["id"]
    reply = row.get("our_reply") or "(no reply text)"
    prior = existing.get(rid)

    flag_label = ""
    if prior:
        flag_label = " — ✅ GOOD" if prior["rating"] == GOOD_RATING else " — ❌ BAD"

    with st.expander(f"Reply {i}{flag_label}", expanded=(len(rows) == 1)):
        left, right = st.columns([3, 2])

        with left:
            st.markdown("**Reply (original)**")
            st.write(reply)

            if st.toggle("🌐 Translate to English", key=f"tr_{rid}"):
                st.markdown("**Translation**")
                st.info(translate_it_en(reply))

            caption_bits = []
            if row.get("our_escalation"):
                caption_bits.append("escalated")
            if row.get("chatgpt_provided"):
                caption_bits.append("chatgpt provided")
            caption_bits.append(str(row.get("created_at", "")))
            st.caption(" · ".join(b for b in caption_bits if b))

        with right:
            st.markdown("**Flag this reply**")
            if prior:
                who = prior.get("rater") or "someone"
                st.caption(f"Current: {'GOOD' if prior['rating']==GOOD_RATING else 'BAD'} (by {who})")

            reason = st.text_input("Reason (short)", key=f"reason_{rid}")
            notes = st.text_area("Notes (optional)", key=f"notes_{rid}", height=80)

            b1, b2 = st.columns(2)
            if b1.button("✅ Good", key=f"good_{rid}", use_container_width=True):
                save_feedback(rid, GOOD_RATING, rater_name, reason, notes)
                load_feedback.clear()
                st.success("Saved as GOOD")
                st.rerun()
            if b2.button("❌ Bad", key=f"bad_{rid}", use_container_width=True):
                save_feedback(rid, BAD_RATING, rater_name, reason, notes)
                load_feedback.clear()
                st.success("Saved as BAD")
                st.rerun()
