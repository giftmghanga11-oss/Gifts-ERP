import streamlit as st
import bcrypt
from db import run_query, execute_write

MIN_PASSWORD_LEN = 8


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_user(username: str, password: str, full_name: str, role: str):
    execute_write(
        """
        INSERT INTO users (username, password_hash, full_name, role)
        VALUES (:uname, :phash, :fname, :role)
        """,
        {"uname": username.strip(), "phash": hash_password(password), "fname": full_name.strip(), "role": role},
    )


def require_login(allowed_roles: list = None):
    if "user" not in st.session_state or st.session_state["user"] is None:
        render_login_screen()
        st.stop()
    user = st.session_state["user"]
    if allowed_roles and user["role"] not in allowed_roles:
        st.error("Access denied: you do not have permission to view this page.")
        st.stop()


# Sage X3 look: black page, green wordmark, white rounded card,
# pale-blue inputs with an amber focus ring, green pill button.
LOGIN_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Quicksand:wght@600;700&family=Barlow:wght@400;500;600&display=swap');

header[data-testid="stHeader"], div[data-testid="stToolbar"],
section[data-testid="stSidebar"], footer { display: none !important; }

.stApp { background: #000000; }
.block-container { max-width: 520px !important; padding-top: 3.2rem !important; }

.brand {
    text-align: center; color: #00d639; margin-bottom: 34px;
    font-family: 'Quicksand', 'Trebuchet MS', sans-serif;
    font-weight: 700; font-size: 58px; letter-spacing: -1px; line-height: 1;
}

/* the form itself is the white card */
div[data-testid="stForm"] {
    background: #ffffff; border: none; border-radius: 40px;
    padding: 44px 40px 36px 40px;
    font-family: 'Barlow', 'Helvetica Neue', Arial, sans-serif;
}
.card-title {
    text-align: center; color: #1a1a1a; font-weight: 600; font-size: 26px;
    margin: 0 0 26px 0; font-family: 'Barlow', Arial, sans-serif;
}
div[data-testid="stForm"] label p {
    color: #1a1a1a !important; font-size: 16px !important; font-weight: 400 !important;
}
div[data-testid="stForm"] div[data-baseweb="input"] {
    background: #e8eefc !important; border: 1px solid #6b7f95 !important;
    border-radius: 6px !important;
}
div[data-testid="stForm"] div[data-baseweb="input"]:focus-within {
    border-color: #f2b400 !important; box-shadow: 0 0 0 3px #f2b400 !important;
}
div[data-testid="stForm"] input {
    background: transparent !important; color: #111111 !important; font-size: 15px !important;
}
div[data-testid="stForm"] div[data-baseweb="input"] button { background: transparent !important; }
div[data-testid="stForm"] div[data-baseweb="input"] svg { fill: #3a4a5c !important; }

.forgot {
    text-align: right; color: #00814a; font-weight: 700; font-size: 14px; margin: 4px 0 18px 0;
}
div[data-testid="stFormSubmitButton"] button {
    width: 100%; background: #00844b !important; color: #ffffff !important;
    border: none !important; border-radius: 999px !important;
    padding: 12px 0 !important; font-size: 16px !important; font-weight: 600 !important;
    letter-spacing: 0.5px;
}
div[data-testid="stFormSubmitButton"] button:hover { background: #006b3d !important; }
div[data-testid="stFormSubmitButton"] button:focus-visible { outline: 3px solid #f2b400 !important; }
</style>
"""


def render_login_screen():
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)
    st.markdown('<div class="brand">MyOdoo</div>', unsafe_allow_html=True)

    user_count = int(run_query("SELECT COUNT(*) AS cnt FROM users;").iloc[0]["cnt"])

    if user_count == 0:
        with st.form("setup_admin_form"):
            st.markdown('<div class="card-title">Create admin account</div>', unsafe_allow_html=True)
            username = st.text_input("Login")
            full_name = st.text_input("Full name")
            password = st.text_input("Password", type="password")
            confirm = st.text_input("Confirm password", type="password")
            if st.form_submit_button("Create admin"):
                if not username or not full_name or not password:
                    st.error("All fields are required.")
                elif len(password) < MIN_PASSWORD_LEN:
                    st.error(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
                elif password != confirm:
                    st.error("Passwords do not match.")
                else:
                    create_user(username, password, full_name, "admin")
                    st.rerun()
        return

    with st.form("login_form"):
        st.markdown('<div class="card-title">MyOdoo ERP</div>', unsafe_allow_html=True)
        username = st.text_input("Login")
        password = st.text_input("Password", type="password")
        st.markdown('<div class="forgot">Forgot password? Ask your administrator.</div>', unsafe_allow_html=True)
        submitted = st.form_submit_button("Sign in")

    if submitted:
        users_df = run_query(
            "SELECT user_id, username, password_hash, full_name, role, is_active "
            "FROM users WHERE username = :u;",
            {"u": username.strip()},
        )
        if users_df.empty or not verify_password(password, users_df.iloc[0]["password_hash"]):
            st.error("Invalid login or password.")
        elif not bool(users_df.iloc[0]["is_active"]):
            st.error("This account is disabled. Contact your administrator.")
        else:
            row = users_df.iloc[0]
            st.session_state["user"] = {
                "user_id": int(row["user_id"]),
                "username": row["username"],
                "full_name": row["full_name"],
                "role": row["role"],
            }
            st.rerun()
