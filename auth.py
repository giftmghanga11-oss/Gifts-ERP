import streamlit as st
import bcrypt
from db import run_query, execute_write


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


def require_login(allowed_roles: list = None):
    if "user" not in st.session_state or st.session_state["user"] is None:
        render_login_screen()
        st.stop()

    user = st.session_state["user"]
    if allowed_roles and user["role"] not in allowed_roles:
        st.error("Access Denied: You do not have permission to view this page.")
        st.stop()


def create_user(username: str, password: str, full_name: str, role: str):
    hashed_pw = hash_password(password)
    execute_write(
        """
        INSERT INTO users (username, password_hash, full_name, role)
        VALUES (:uname, :phash, :fname, :role)
        """,
        {"uname": username, "phash": hashed_pw, "fname": full_name, "role": role}
    )


LOGIN_CSS = """
<style>
.stApp {
    background-color: #0b0b0b;
}

/* The st.form IS the white card. Streamlit wraps every st.markdown call in its
   own element, so a <div class="login-card"> opened in one call can never
   contain the widgets that follow it. */
div[data-testid="stForm"] {
    background-color: #ffffff !important;
    border: none !important;
    border-radius: 16px !important;
    padding: 40px !important;
    box-shadow: 0 4px 25px rgba(0,0,0,0.5);
}

/* Header and note text inside the card */
.login-header {
    text-align: center;
    font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
    font-weight: bold;
    font-size: 24px;
    color: #111111 !important;
    margin-bottom: 20px;
}
.login-note {
    text-align: center;
    font-size: 14px;
    color: #444444 !important;
    margin-bottom: 20px;
}

/* ---- LABELS ("Login", "Password", etc.) -> black ---- */
div[data-testid="stForm"] label,
div[data-testid="stForm"] [data-testid="stWidgetLabel"],
div[data-testid="stForm"] [data-testid="stWidgetLabel"] p,
div[data-testid="stForm"] [data-testid="stWidgetLabel"] * {
    color: #111111 !important;
    font-weight: 700 !important;
    font-size: 15px !important;
}

/* ---- INPUT BOXES -> light background, dark text ---- */
div[data-testid="stForm"] div[data-baseweb="input"],
div[data-testid="stForm"] div[data-baseweb="base-input"],
div[data-testid="stForm"] div[data-testid="stTextInput"] input {
    background-color: #f9f9f9 !important;
    color: #111111 !important;
    -webkit-text-fill-color: #111111 !important;
}
div[data-testid="stForm"] div[data-baseweb="input"] {
    border: 1px solid #cccccc !important;
}
/* Show/hide password eye icon */
div[data-testid="stForm"] div[data-baseweb="input"] button {
    background-color: transparent !important;
}
div[data-testid="stForm"] div[data-baseweb="input"] svg {
    fill: #555555 !important;
    color: #555555 !important;
}

/* ---- SUBMIT BUTTON ---- */
div[data-testid="stFormSubmitButton"] button {
    background-color: #008f39 !important;
    color: white !important;
    border-radius: 25px !important;
    width: 100% !important;
    font-weight: bold;
    border: none;
    padding: 10px;
}
div[data-testid="stFormSubmitButton"] button:hover {
    background-color: #007530 !important;
}
</style>
"""


def render_login_screen():
    st.markdown(LOGIN_CSS, unsafe_allow_html=True)

    existing_users = run_query("SELECT COUNT(*) AS cnt FROM users;")

    _, col_center, _ = st.columns([1, 1.2, 1])

    with col_center:
        if existing_users.iloc[0]["cnt"] == 0:
            with st.form("setup_admin_form"):
                st.markdown('<div class="login-header">Initial System Setup — Admin</div>', unsafe_allow_html=True)
                st.markdown(
                    '<div class="login-note">No users exist in the database yet. Create your primary Admin account.</div>',
                    unsafe_allow_html=True
                )

                username = st.text_input("Admin Username")
                full_name = st.text_input("Full Name")
                password = st.text_input("Password", type="password")
                confirm_password = st.text_input("Confirm Password", type="password")

                submitted = st.form_submit_button("Create Admin Account")
                if submitted:
                    if not username or not full_name or not password:
                        st.error("All fields are required.")
                    elif password != confirm_password:
                        st.error("Passwords do not match.")
                    else:
                        create_user(username, password, full_name, "admin")
                        st.success("Admin account created successfully! Please refresh.")
                        st.rerun()
        else:
            with st.form("login_form"):
                st.markdown('<div class="login-header">Sage ERP</div>', unsafe_allow_html=True)

                username = st.text_input("Login")
                password = st.text_input("Password", type="password")

                submitted = st.form_submit_button("Sign in")
                if submitted:
                    users_df = run_query(
                        "SELECT user_id, username, password_hash, full_name, role FROM users WHERE username = :uname;",
                        {"uname": username}
                    )
                    if users_df.empty:
                        st.error("Invalid username or password.")
                    else:
                        user_row = users_df.iloc[0]
                        if verify_password(password, user_row["password_hash"]):
                            st.session_state["user"] = {
                                "user_id": int(user_row["user_id"]),
                                "username": user_row["username"],
                                "full_name": user_row["full_name"],
                                "role": user_row["role"]
                            }
                            st.success("Login successful!")
                            st.rerun()
                        else:
                            st.error("Invalid username or password.")
