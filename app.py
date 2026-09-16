"""
Reply Review Portal
-------------------
Reads customer messages + GPT replies from `inference_log` and writes a
good/bad rating PER REPLY to the `feedback` table (linked by inference_id).

Run locally:   streamlit run app.py
The Supabase key is read from Streamlit secrets, never hardcoded.
"""

import html
import re
import json
import math
import time
from datetime import datetime, date, timedelta

import streamlit as st
from supabase import create_client, Client

try:
    from deep_translator import GoogleTranslator
    TRANSLATE_AVAILABLE = True
except Exception:
    TRANSLATE_AVAILABLE = False

try:
    from deep_translator import MyMemoryTranslator
    MYMEMORY_AVAILABLE = True
except Exception:
    MYMEMORY_AVAILABLE = False

try:
    from langdetect import detect as _lang_detect
    LANGDETECT_AVAILABLE = True
except Exception:
    LANGDETECT_AVAILABLE = False


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

# Professional theme + compact, tidy text.
st.markdown(
    """
    <style>
      .rv-text{font-size:0.85rem;line-height:1.45;}
      .rv-box{font-size:0.85rem;line-height:1.45;padding:9px 12px;border-radius:8px;}
      .rv-sep{margin:8px 0;border:none;border-top:1px dashed #c7d2e0;}
      /* bold every widget label */
      [data-testid="stWidgetLabel"] p{font-weight:600 !important;}
      /* column header style */
      .rv-colhead{font-weight:700;color:#1e3a8a;font-size:0.72rem;
                  text-transform:uppercase;letter-spacing:.05em;}
      /* good / bad button colors (matched by Streamlit key class) */
      [class*="st-key-good_"] button{background:#16a34a !important;
          border-color:#16a34a !important;color:#fff !important;font-weight:600;}
      [class*="st-key-good_"] button:hover{background:#15803d !important;}
      [class*="st-key-bad_"] button{background:#e11d48 !important;
          border-color:#e11d48 !important;color:#fff !important;font-weight:600;}
      [class*="st-key-bad_"] button:hover{background:#be123c !important;}
      /* card containers a touch softer */
      [data-testid="stVerticalBlockBorderWrapper"]{border-radius:10px;}
      /* section zones */
      .rv-zonehead{font-weight:700;color:#1e3a8a;font-size:0.95rem;margin:2px 0 8px;}
      .rv-zonebar{background:#e8f0ff;border:1px solid #cdd9ee;color:#1e3a8a;
                  font-weight:700;padding:8px 12px;border-radius:10px;margin:8px 0 10px;}
      [class*="st-key-zone_filters"]{background:#eef3fb !important;
                  border:1px solid #cdd9ee !important;}
      [class*="st-key-zone_pager"]{background:#fff4e8 !important;
                  border:1px solid #f4dcbb !important;}
      /* make the input boxes easy to spot and click */
      [data-baseweb="input"], [data-baseweb="select"]{
          background:#ffffff !important;border:1.5px solid #94a3b8 !important;
          border-radius:8px !important;}
      [data-baseweb="textarea"]{
          background:#ffffff !important;border:1.5px solid #cbd5e1 !important;
          border-radius:8px !important;}
      [data-baseweb="input"]:focus-within,
      [data-baseweb="select"]:focus-within,
      [data-baseweb="textarea"]:focus-within{
          border-color:#2563eb !important;
          box-shadow:0 0 0 2px rgba(37,99,235,.18) !important;}
      /* conversation expander bar in light yellow */
      [data-testid="stExpander"] details{background:#fff9db !important;
          border:1px solid #f2e29a !important;border-radius:10px !important;}
      [data-testid="stExpander"] summary{background:#fff9db !important;
          border-radius:10px !important;}
      [data-testid="stExpander"] summary:hover{background:#fff3bf !important;}
    </style>
    """,
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
        .select("id, created_at, conversation_id, conversation_turn, customer_message, customer_intent, our_reply, our_escalation, model_version_id, history_json")
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
    """Insert one feedback row. Returns None on success, or an error string."""
    try:
        client = get_client()
        client.table("feedback").insert({
            "inference_id": inference_id,
            "rating": rating,
            "rater": rater or None,
            "reason": reason or None,
            "notes": notes or None,
        }).execute()
        return None
    except Exception as e:
        return str(e)


# --------------------------------------------------------------------------- #
# Translation  (resilient: retries + fallback, always returns a string)
# --------------------------------------------------------------------------- #
FAIL_MSG = "(translation temporarily unavailable — click Translate again)"


def _looks_like_error(s):
    if not s:
        return True
    low = s.lower()
    return any(t in low for t in (
        "error 500", "that's an error", "please try again later",
        "1500.that", "service unavailable",
        "invalid source language", "langpair", "no content",
    ))


def _chunks(text, size=460):
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


def _deepl(text):
    """DeepL free/pro API — reliable from servers, auto-detects source.
    Uses DEEPL_API_KEY from Streamlit secrets; skipped if not set."""
    key = st.secrets.get("DEEPL_API_KEY")
    if not key:
        return None
    import urllib.request
    import urllib.parse
    base = "https://api-free.deepl.com" if key.strip().endswith(":fx") else "https://api.deepl.com"
    data = urllib.parse.urlencode({"text": text, "target_lang": "EN"}).encode()
    req = urllib.request.Request(
        base + "/v2/translate",
        data=data,
        headers={
            "Authorization": f"DeepL-Auth-Key {key.strip()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    out = " ".join(t.get("text", "") for t in j.get("translations", [])).strip()
    return out or None


LINGVA_INSTANCES = [
    "https://lingva.ml",
    "https://translate.plausibility.cloud",
    "https://lingva.garudalinux.org",
    "https://lingva.lunar.icu",
]


def _lingva(text):
    """Keyless Google-Translate proxy with several fallback servers."""
    import urllib.request
    import urllib.parse
    q = urllib.parse.quote(text[:4500], safe="")
    for base in LINGVA_INSTANCES:
        try:
            req = urllib.request.Request(
                f"{base}/api/v1/auto/en/{q}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                j = json.loads(resp.read().decode("utf-8"))
            out = (j.get("translation") or "").strip()
            if out and not _looks_like_error(out):
                return out
        except Exception:
            continue
    return None


def _google_direct(text):
    """Google's lightweight endpoint — works from server IPs where the
    scraped web endpoint gets blocked. Stdlib only, no API key."""
    import urllib.request
    import urllib.parse
    url = ("https://translate.googleapis.com/translate_a/single"
           "?client=gtx&sl=auto&tl=en&dt=t&q=" + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = [seg[0] for seg in data[0] if seg and seg[0]]
    out = "".join(parts).strip()
    return out if out and not _looks_like_error(out) else None


def _google(text):
    if not TRANSLATE_AVAILABLE:
        return None
    out = GoogleTranslator(source="auto", target="en").translate(text)
    return out if out and not _looks_like_error(out) else None


_IT_WORDS = set("il la lo le gli un una di che e è sono per con non ciao grazie prego "
                "come cosa questo posso vorrei buongiorno salve serve indirizzo numero "
                "telefono città vuoi fare puoi bene si no della dello".split())
_RO_WORDS = set("și este nu de la cu pentru bună mulțumesc salut care ce vreau acest "
                "telefon adresă număr poți oraș vrei face bine da dumneavoastră".split())
_EN_WORDS = set("the is are and you for with this what can would hello thanks please your "
                "need address number phone city want make well yes from".split())


def _detect_lang(text):
    """Guess the source language. Uses langdetect if present, else a small
    built-in heuristic — so translation needs no extra package."""
    if LANGDETECT_AVAILABLE:
        try:
            code = _lang_detect(text[:500])
            if code:
                return code.split("-")[0]
        except Exception:
            pass
    if re.search(r"[\u0400-\u04FF]", text):      # Cyrillic -> Bulgarian
        return "bg"
    words = set(re.findall(r"[a-zàèéìòùâîășțç]+", text.lower()))
    if not words:
        return "it"
    scores = {"it": len(words & _IT_WORDS),
              "ro": len(words & _RO_WORDS),
              "en": len(words & _EN_WORDS)}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "it"


def _mymemory(text):
    """MyMemory's real API — free, no key, stdlib only. Uses MYMEMORY_EMAIL
    from secrets (if set) to raise the daily limit."""
    src = _detect_lang(text)
    if not src or src == "en":
        return None
    import urllib.request
    import urllib.parse
    email = st.secrets.get("MYMEMORY_EMAIL", "")
    outs = []
    for chunk in _chunks(text, 480):
        params = {"q": chunk, "langpair": f"{src}|en"}
        if email:
            params["de"] = email
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        seg = ((j.get("responseData") or {}).get("translatedText") or "").strip()
        if not seg or _looks_like_error(seg):
            return None
        outs.append(seg)
    joined = " ".join(o for o in outs if o).strip()
    return joined or None


def _run(fn, name, text, debug):
    try:
        out = fn(text)
    except Exception as e:
        debug.append(f"{name}: {type(e).__name__}: {str(e)[:90]}")
        return None
    if out and _looks_like_error(out):
        debug.append(f"{name}: error response — {str(out)[:60]}")
        return None
    if out and out.strip().lower() == text.strip().lower():
        debug.append(f"{name}: returned original text unchanged (not translated)")
        return None
    if out:
        debug.append(f"{name}: ok")
        return out
    debug.append(f"{name}: no result")
    return None


def translate_it_en(text) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    cache = st.session_state.setdefault("_tr_cache", {})
    if text in cache:
        return cache[text]

    debug = [
        f"env: deepl_key={'set' if st.secrets.get('DEEPL_API_KEY') else 'MISSING'}"
        f", langdetect={'yes' if LANGDETECT_AVAILABLE else 'NO'}"
        f", mymemory={'yes' if MYMEMORY_AVAILABLE else 'NO'}"
    ]
    result = None
    for name, fn in (("deepl", _deepl), ("mymemory", _mymemory),
                     ("lingva", _lingva), ("googleapis", _google_direct),
                     ("google", _google)):
        result = _run(fn, name, text, debug)
        if result:
            break

    if result:
        cache[text] = result
        st.session_state["_tr_debug"] = debug + ["=> used above"]
        return result
    st.session_state["_tr_debug"] = debug + ["=> ALL failed"]
    return FAIL_MSG


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


ROLE_KEYS = ("role", "sender", "from", "speaker", "author", "type")
TEXT_KEYS = ("content", "text", "message", "body", "msg", "value")
AGENT_ROLES = {"assistant", "agent", "bot", "ai", "system", "support",
               "operator", "our", "reply", "answer", "response", "staff"}


def _first(d, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _flatten(text):
    if isinstance(text, list):
        parts = []
        for p in text:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
            else:
                parts.append(str(p))
        return " ".join(parts)
    return str(text)


def normalize_history(hist):
    """Turn whatever is in history_json into a list of (role, text)."""
    if hist is None:
        return []
    if isinstance(hist, str):
        hist = hist.strip()
        if not hist:
            return []
        try:
            hist = json.loads(hist)
        except Exception:
            return [("", hist)]
    if isinstance(hist, dict):
        for k in ("messages", "history", "conversation", "turns", "chat"):
            if isinstance(hist.get(k), list):
                hist = hist[k]
                break
        else:
            hist = [hist]
    if not isinstance(hist, list):
        return []

    out = []
    for m in hist:
        if isinstance(m, str):
            out.append(("", m))
            continue
        if not isinstance(m, dict):
            continue
        role = _first(m, ROLE_KEYS)
        text = _first(m, TEXT_KEYS)
        if text is not None:
            out.append((str(role or ""), _flatten(text)))
            continue
        # pair style: {"user": "...", "assistant": "..."}
        u = _first(m, ("user", "customer", "human", "client"))
        a = _first(m, ("assistant", "agent", "bot", "reply", "answer", "response"))
        if u is not None:
            out.append(("user", _flatten(u)))
        if a is not None:
            out.append(("assistant", _flatten(a)))
    return out


def _is_agent(role):
    return (role or "").strip().lower() in AGENT_ROLES


def render_history(hist, translate=False):
    msgs = normalize_history(hist)
    if not msgs:
        st.caption("No conversation history available for this message.")
        return
    bubbles = []
    for role, text in msgs:
        agent = _is_agent(role)
        disp = translate_it_en(text) if translate else text
        safe = html.escape(disp or "").replace("\n", "<br>")
        label = html.escape(role) if role else ("Agent" if agent else "Customer")
        if agent:
            bubbles.append(
                "<div style='display:flex;justify-content:flex-end;margin:5px 0'>"
                "<div style='max-width:78%;background:#dcf8c6;border-radius:12px 12px 2px 12px;"
                "padding:7px 11px;font-size:0.83rem;line-height:1.4'>"
                f"<div style='font-size:0.62rem;color:#4b5563;font-weight:700;"
                f"text-transform:uppercase;letter-spacing:.03em'>{label}</div>{safe}</div></div>"
            )
        else:
            bubbles.append(
                "<div style='display:flex;justify-content:flex-start;margin:5px 0'>"
                "<div style='max-width:78%;background:#ffffff;border:1px solid #e2e8f0;"
                "border-radius:12px 12px 12px 2px;padding:7px 11px;font-size:0.83rem;line-height:1.4'>"
                f"<div style='font-size:0.62rem;color:#4b5563;font-weight:700;"
                f"text-transform:uppercase;letter-spacing:.03em'>{label}</div>{safe}</div></div>"
            )
    st.markdown(
        "<div style='background:#fff9db;border:1px solid #f2e29a;border-radius:10px;"
        "padding:10px;max-height:380px;overflow-y:auto'>" + "".join(bubbles) + "</div>",
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
st.markdown(
    "<div style='text-align:center;background:linear-gradient(90deg,#1e3a8a,#2563eb);"
    "color:#fff;padding:18px 16px;border-radius:14px;margin-bottom:16px;"
    "box-shadow:0 2px 8px rgba(30,58,138,.25)'>"
    "<h1 style='margin:0;font-size:1.9rem'>📝 Reply Review Portal</h1>"
    "<div style='opacity:.92;font-size:.9rem;margin-top:2px'>"
    "Review and rate generated replies</div></div>",
    unsafe_allow_html=True,
)

today = date.today()
filter_zone = st.container(border=True, key="zone_filters")
with filter_zone:
    st.markdown(
        "<div class='rv-zonehead'>🔍 Selection — reviewer, date &amp; model version</div>",
        unsafe_allow_html=True,
    )
    f1, f2, f3 = st.columns([2, 3, 1])
    with f1:
        rater_name = st.text_input("**Your name**", value="", placeholder="reviewer name")
    with f2:
        date_range = st.date_input(
            "**Date range**",
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
intents = sorted({(r.get("customer_intent") or "—") for r in logs}, key=str)
with filter_zone:
    vcol, icol, _ = st.columns([2, 2, 2])
    with vcol:
        selected_version = st.selectbox(
            "**Model version**",
            options=["All versions"] + versions,
            format_func=version_label,
        )
    with icol:
        selected_intent = st.selectbox(
            "**Customer intent**",
            options=["All intents"] + intents,
            format_func=lambda x: (
                "All intents" if x == "All intents"
                else "(none)" if x == "—" else str(x)
            ),
        )
if selected_version != "All versions":
    logs = [r for r in logs if (r.get("model_version_id") or "—") == selected_version]
if selected_intent != "All intents":
    logs = [r for r in logs if (r.get("customer_intent") or "—") == selected_intent]
if not logs:
    st.info("No messages match these filters in the date range.")
    st.stop()

# Group by conversation + turn, so the same message text in two different
# conversations stays as two separate cards (each with its own history/reply).
groups = {}
for row in logs:
    conv = row.get("conversation_id") or "—"
    turn = row.get("conversation_turn")
    turn_part = turn if turn is not None else (row.get("customer_message") or "")
    groups.setdefault((conv, turn_part), []).append(row)

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
    f"{total} conversation message(s) in range · "
    f"showing {start_i + 1}–{min(start_i + PAGE_SIZE, total)} (page {page + 1} of {total_pages})"
)


# --------------------------------------------------------------------------- #
# Main table
# --------------------------------------------------------------------------- #
st.markdown(
    "<div class='rv-zonebar'>📋 Messages &amp; Replies — review and rate each reply</div>",
    unsafe_allow_html=True,
)
for gidx, ((conv, turn_part), rows) in enumerate(page_groups):
    msg = rows[0].get("customer_message") or "(empty message)"
    with st.container(border=True):
        # small conversation tag so identical messages are distinguishable
        conv_short = str(conv)[:8] if conv and conv != "—" else "unknown"
        turn = rows[0].get("conversation_turn")
        turn_txt = f" · turn {turn}" if turn is not None else ""
        intent = rows[0].get("customer_intent")
        intent_txt = f" · intent: {html.escape(str(intent))}" if intent else ""
        st.markdown(
            f"<div style='font-size:0.72rem;color:#64748b;font-weight:600'>"
            f"🧵 Conversation {conv_short}{turn_txt}{intent_txt}</div>",
            unsafe_allow_html=True,
        )
        msg_box(msg, gidx, "💬 Customer message")

        # Per-message translate button (sits right under the message)
        tkey = f"tr_{rows[0]['id']}"
        if st.button("🌐 Translate", key=f"btn_{tkey}"):
            st.session_state[tkey] = not st.session_state.get(tkey, False)
        show_tr = st.session_state.get(tkey, False)
        if show_tr:
            with st.spinner("Translating…"):
                _tr = translate_it_en(msg)
            msg_box(_tr, gidx, "💬 Customer message (EN)")
            if _tr == FAIL_MSG:
                st.caption("⚠️ " + " | ".join(st.session_state.get("_tr_debug", [])))

        # Full conversation context, shown like a chat (WhatsApp style).
        # History has its OWN translate toggle so opening/translating it does
        # not slow down the main message Translate button.
        with st.expander("💬 View full conversation (context)"):
            tr_hist = st.checkbox(
                "🌐 Translate conversation to English",
                key=f"trh_{rows[0]['id']}",
            )
            render_history(rows[0].get("history_json"), translate=tr_hist)

        st.markdown("<hr class='rv-sep'>", unsafe_allow_html=True)

        h = st.columns([3, 2, 2, 3])
        h[0].markdown("<span class='rv-colhead'>Reply</span>", unsafe_allow_html=True)
        h[1].markdown("<span class='rv-colhead'>Reason</span>", unsafe_allow_html=True)
        h[2].markdown("<span class='rv-colhead'>Notes</span>", unsafe_allow_html=True)
        h[3].markdown("<span class='rv-colhead'>Feedback</span>", unsafe_allow_html=True)

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
                    with st.spinner("Translating…"):
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
                    err = save_feedback(rid, GOOD_RATING, rater_name, reason, notes)
                    if err:
                        st.error(f"Could not save: {err}")
                    else:
                        load_feedback.clear()
                        st.rerun()
                if fb[1].button("❌ Bad", key=f"bad_{rid}", use_container_width=True):
                    err = save_feedback(rid, BAD_RATING, rater_name, reason, notes)
                    if err:
                        st.error(f"Could not save: {err}")
                    else:
                        load_feedback.clear()
                        st.rerun()

            # divider between replies (not after the last one)
            if j < len(rows):
                st.markdown("<hr class='rv-sep'>", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Pagination controls
# --------------------------------------------------------------------------- #
st.write("")
pager_zone = st.container(border=True, key="zone_pager")
with pager_zone:
    st.markdown("<div class='rv-zonehead'>📄 Page navigation</div>", unsafe_allow_html=True)
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
# Translation diagnostics (open this if Translate isn't working)
# --------------------------------------------------------------------------- #
st.write("")
_dbg = st.session_state.get("_tr_debug")
with st.expander("🛠 Translation status (open this if Translate isn't working)"):
    if not _dbg:
        st.caption("Click Translate on any message first, then reopen this panel.")
    else:
        for line in _dbg:
            st.write("• " + line)
    st.caption(
        "For cardless free translation the app uses MyMemory — this needs "
        "langdetect installed (upload the requirements.txt that includes it). "
        "Optional: add MYMEMORY_EMAIL = \"you@example.com\" in secrets to raise "
        "the daily limit. deepl_key is only needed if you later add a DeepL key."
    )
