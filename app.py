from __future__ import annotations

import re
import time
from urllib.parse import urlparse, urlunparse

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="LinkedIn Profile Scraper", page_icon="🔗", layout="wide")

APIFY_BASE    = "https://api.apify.com/v2"
DEFAULT_ACTOR = "harvestapi~linkedin-profile-scraper"

# ─── helpers ──────────────────────────────────────────────────────────────────

def clean_url(url) -> str:
    if not url or isinstance(url, float):
        return ""
    url = str(url).strip()
    if not url:
        return ""
    if url.startswith("www.") or re.match(r"^[a-z]{2}\.linkedin", url):
        url = "https://" + url
    elif not url.startswith("http"):
        url = "https://" + url
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, "www.linkedin.com", p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url


def slug_from_url(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([\w%-]+)", url, re.I)
    return m.group(1) if m else ""


def best_col(columns, hints):
    for h in hints:
        for c in columns:
            if h.lower() in c.lower():
                return c
    return columns[0]


# ─── Apify ────────────────────────────────────────────────────────────────────

def apify_start(urls: list, token: str, actor_id: str) -> str:
    # Apify uses ~ as separator; accept / from user input and normalise
    actor_id = actor_id.replace("/", "~")
    r = requests.post(
        f"{APIFY_BASE}/acts/{actor_id}/runs",
        params={"token": token},
        json={
            "queries": urls,
            "profileScraperMode": "Profile details no email ($4 per 1k)",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]["id"]


def apify_status(run_id: str, token: str) -> dict:
    r = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": token}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def apify_items(run_id: str, token: str) -> list:
    r = requests.get(
        f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items",
        params={"token": token, "format": "json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


# ─── result mapping ───────────────────────────────────────────────────────────

def _extract_title(entry: dict) -> str:
    for key in ("title", "position", "jobTitle", "role", "designation"):
        v = entry.get(key, "")
        if v:
            return str(v)
    return ""


def _extract_company(entry: dict) -> str:
    # harvestapi nests as {"name": ..., "id": ...}
    company_obj = entry.get("company")
    if isinstance(company_obj, dict):
        name = company_obj.get("name", "")
        if name:
            return str(name)
    if isinstance(company_obj, str) and company_obj:
        return company_obj
    for key in ("companyName", "organization", "employer"):
        v = entry.get(key, "")
        if v:
            return str(v)
    return ""


def current_position(item: dict) -> tuple[str, str]:
    for field in ("currentPosition", "positions", "experience", "experiences", "workHistory"):
        entries = item.get(field) or []
        if not isinstance(entries, list) or not entries:
            continue
        # prefer entries with no endDate (still active)
        active = [e for e in entries if not e.get("endDate")]
        entry  = active[0] if active else entries[0]
        title   = _extract_title(entry)
        company = _extract_company(entry)
        if title or company:
            return title, company

    # flat-field fallback
    return str(item.get("headline", item.get("jobTitle", ""))), str(item.get("currentCompany", ""))


def map_item(item: dict, fallback_email: str = "") -> dict:
    title, company = current_position(item)

    # harvestapi field is linkedinUrl (lowercase i)
    raw_url = (
        item.get("linkedinUrl")
        or item.get("linkedInUrl")
        or item.get("url")
        or item.get("profileUrl")
        or ""
    )

    return {
        "Email":        fallback_email or item.get("email") or item.get("emailAddress") or "",
        "LinkedIn URL": clean_url(raw_url),
        "First Name":   item.get("firstName", ""),
        "Last Name":    item.get("lastName", ""),
        "Job Title":    title,
        "Company":      company,
    }


# ─── UI ───────────────────────────────────────────────────────────────────────

st.title("🔗 LinkedIn Profile Scraper")
st.markdown(
    "Scrape LinkedIn profiles via Apify and get "
    "**Email · LinkedIn URL · First Name · Last Name · Job Title · Company**."
)

with st.sidebar:
    st.header("⚙️ Configuration")
    apify_token = st.text_input("Apify API Token", type="password", placeholder="apify_api_...")
    actor_id    = st.text_input("Actor ID", value=DEFAULT_ACTOR)
    st.caption("Get your token at console.apify.com → Settings → Integrations")

# ── Input: CSV upload  OR  manual entry (or both) ────────────────────────────
tab_csv, tab_manual = st.tabs(["📂 Upload CSV", "✏️ Enter manually"])

email_lookup   = {}   # linkedin_url -> email (from CSV)
urls_from_csv  = []
urls_from_manual = []

with tab_csv:
    uploaded = st.file_uploader("Upload CSV", type=["csv"])
    if uploaded:
        raw_df = pd.read_csv(uploaded).fillna("")
        st.dataframe(raw_df, use_container_width=True)

        cols = raw_df.columns.tolist()
        c1, c2 = st.columns(2)
        with c1:
            linkedin_col = st.selectbox("LinkedIn URL column", cols,
                                        index=cols.index(best_col(cols, ["linkedin", "url", "profile"])))
        with c2:
            email_col = st.selectbox("Email column (fallback)", cols,
                                     index=cols.index(best_col(cols, ["email", "mail"])))

        raw_df[linkedin_col] = raw_df[linkedin_col].apply(clean_url)
        has_url = raw_df[linkedin_col].apply(slug_from_url).astype(bool)
        urls_from_csv = raw_df[has_url][linkedin_col].unique().tolist()
        email_lookup  = dict(zip(raw_df[linkedin_col], raw_df[email_col]))

        skipped = (~has_url).sum()
        st.caption(f"{len(urls_from_csv)} valid URL(s) found" + (f", {skipped} skipped (no URL)" if skipped else ""))

with tab_manual:
    manual_text = st.text_area(
        "Paste LinkedIn profile URLs — one per line",
        placeholder="https://www.linkedin.com/in/username",
        height=180,
    )
    manual_email_text = st.text_area(
        "Corresponding emails (optional, same order as above — one per line)",
        placeholder="user@example.com",
        height=100,
    )
    if manual_text.strip():
        raw_manual_urls  = [u.strip() for u in manual_text.strip().splitlines() if u.strip()]
        raw_manual_emails = [e.strip() for e in manual_email_text.strip().splitlines()]
        urls_from_manual = [clean_url(u) for u in raw_manual_urls if slug_from_url(clean_url(u))]
        for i, url in enumerate(urls_from_manual):
            email = raw_manual_emails[i] if i < len(raw_manual_emails) else ""
            if url and email:
                email_lookup[url] = email
        st.caption(f"{len(urls_from_manual)} valid URL(s) entered manually")

# ── Merge both sources ────────────────────────────────────────────────────────
urls_to_scrape = list(dict.fromkeys(urls_from_csv + urls_from_manual))  # dedup, preserve order

st.divider()

st.info(f"**{len(urls_to_scrape)}** unique LinkedIn URLs will be sent to Apify.")

if not apify_token:
    st.warning("Enter your Apify API token in the sidebar.")
    st.stop()

if not urls_to_scrape:
    st.error("No valid LinkedIn URLs found. Check your column mapping.")
    st.stop()

if st.button("🚀 Scrape LinkedIn Profiles", type="primary"):

    # start run
    with st.spinner("Starting Apify actor run…"):
        try:
            run_id = apify_start(urls_to_scrape, apify_token, actor_id)
        except requests.HTTPError as e:
            st.error(f"Failed to start Apify run: {e.response.text}")
            st.stop()

    st.info(f"Run started — ID: `{run_id}`")

    # poll
    status_box = st.empty()
    prog       = st.progress(0, text="Waiting for Apify…")
    elapsed    = 0

    while True:
        time.sleep(4)
        elapsed += 4
        try:
            run_data   = apify_status(run_id, apify_token)
        except Exception as e:
            status_box.warning(f"Poll error (retrying): {e}")
            continue

        status     = run_data.get("status", "")
        stats      = run_data.get("stats", {})
        done       = stats.get("itemCount", stats.get("requestsFinished", 0))
        total      = stats.get("requestsTotal", len(urls_to_scrape))

        prog.progress(min(done / max(total, 1), 0.99),
                      text=f"{status} — {done}/{total} scraped — {elapsed}s elapsed")
        status_box.markdown(f"**Status:** `{status}` | **Scraped:** {done} | **Elapsed:** {elapsed}s")

        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    prog.progress(1.0)
    status_box.empty()

    if run_data.get("status") != "SUCCEEDED":
        st.error(f"Run ended with status: {run_data.get('status')}")
        st.stop()

    # fetch items
    with st.spinner("Fetching results…"):
        try:
            items = apify_items(run_id, apify_token)
        except Exception as e:
            st.error(f"Failed to fetch results: {e}")
            st.stop()

    if not items:
        st.warning("Apify returned 0 items. The actor may require a LinkedIn session cookie configured inside Apify.")
        st.stop()

    # map to output schema
    results = []
    for item in items:
        # use lowercase linkedinUrl — harvestapi's actual field name
        raw_url = (
            item.get("linkedinUrl")
            or item.get("linkedInUrl")
            or item.get("url")
            or item.get("profileUrl")
            or ""
        )
        cleaned = clean_url(raw_url)
        # look up email from the user's input CSV by matching on cleaned URL
        fallback_email = email_lookup.get(cleaned, "")
        # also try matching on just the slug in case URL format differs
        if not fallback_email:
            slug = slug_from_url(cleaned)
            for k, v in email_lookup.items():
                if slug and slug_from_url(k) == slug:
                    fallback_email = v
                    break
        results.append(map_item(item, fallback_email))

    result_df = pd.DataFrame(results, columns=["Email", "LinkedIn URL", "First Name", "Last Name", "Job Title", "Company"])

    st.success(f"Scraped **{len(result_df)}** profiles!")

    r1, r2, r3 = st.columns(3)
    r1.metric("Profiles scraped",       len(result_df))
    r2.metric("With email",             (result_df["Email"] != "").sum())
    r3.metric("With title + company",   ((result_df["Job Title"] != "") & (result_df["Company"] != "")).sum())

    with st.expander("🔍 Raw Apify response (first item — for debugging)"):
        first = items[0] if items else {}
        st.json(first)
        if first:
            st.markdown("**Position fields in first item:**")
            for f in ("currentPosition", "positions", "experience", "headline", "jobTitle"):
                if f in first:
                    st.write(f"`{f}`:", first[f])

    st.subheader("Results")
    st.dataframe(result_df, use_container_width=True)

    st.download_button(
        "⬇️ Download CSV",
        data=result_df.to_csv(index=False).encode("utf-8"),
        file_name="linkedin_scraped_profiles.csv",
        mime="text/csv",
    )
