import streamlit as st
import json
import os
import psycopg
from psycopg.rows import dict_row

st.set_page_config(page_title="Mouthpiece Engine", page_icon="🎙️", layout="wide")
DATABASE_URL = os.getenv("NEON_DATABASE_URL")


# Reactive Database Context Fetcher
def fetch_neon_records():
    if not DATABASE_URL:
        return []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM mouthpiece_queue ORDER BY updated_at DESC;")
            return cur.fetchall()


# Database state mutations
def execute_db_action(action_type: str, email: str, subject: str = None, body: str = None):
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            if action_type == "DISCARD":
                cur.execute("DELETE FROM mouthpiece_queue WHERE recipient_email = %s;", (email,))
            elif action_type == "APPROVE":
                cur.execute("UPDATE mouthpiece_queue SET status = 'DISPATCHED' WHERE recipient_email = %s;", (email,))


# SIDEBAR CONFIG & BLUEPRINTS GENERATOR
with st.sidebar:
    st.title("🎙️ Configuration")
    openai_key = st.text_input("OpenAI API Key (BYOK)", type="password")
    st.markdown("---")
    st.subheader("📥 Blueprint Framework")
    # Quick download payload setup block
    st.download_button(
        label="Download Blueprint JSON",
        data=json.dumps({"info": "Mouthpiece Framework Node Blueprint Matrix"}, indent=2),
        file_name="mouthpiece_n8n_blueprint.json",
        mime="application/json",
        use_container_width=True
    )

st.title("📬 Real-Time Dashboard")


@st.fragment(run_every=3)
def render_live_pipeline():
    records = fetch_neon_records()

    active_processing = [r for r in records if r["status"] in ["HYDRATING", "GENERATING"]]
    review_queue = [r for r in records if r["status"] == "READY"]

    if active_processing:
        st.subheader("⚙️ Live Inbound Generation Activity")
        for active in active_processing:
            cols = st.columns([3, 7])
            with cols[0]:
                st.markdown(f"**{active['recipient_email']}**")
            with cols[1]:
                status_msg = "🔄 Querying CRM Threads via n8n..." if active[
                                                                        'status'] == "HYDRATING" else "🧠 Processing Complex LLM Copywriting..."
                st.info(status_msg)
        st.markdown("---")

    st.subheader("✍️ Pending Email Outbox Review Queue")
    if not review_queue:
        st.info("No documents are currently awaiting manual approval verification.")
        return

    for idx, item in enumerate(review_queue):
        email = item["recipient_email"]
        with st.container(border=True):
            st.markdown(f"### Lead Match: **{email}**")

            # Capture real-time live modifications within independent text assets
            edited_subject = st.text_input("Subject Line", value=item["subject_line"], key=f"subj_{email}_{idx}")
            edited_body = st.text_area("Body Markdown Copy", value=item["email_body_markdown"], height=180,
                                       key=f"body_{email}_{idx}")

            b1, b2, _ = st.columns([2, 2, 6])
            with b1:
                if st.button("🚀 Approve & Send", key=f"app_{email}_{idx}", use_container_width=True):
                    execute_db_action("APPROVE", email, edited_subject, edited_body)
                    st.toast(f"Outbound queue package dispatched for {email}!")
                    st.rerun()
            with b2:
                if st.button("🗑️ Discard", key=f"disc_{email}_{idx}", type="secondary", use_container_width=True):
                    execute_db_action("DISCARD", email)
                    st.toast(f"Removed item context target: {email}")
                    st.rerun()


# Run the live fragment block loop
render_live_pipeline()
