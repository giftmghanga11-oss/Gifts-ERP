import streamlit as st
import bcrypt
from db import run_query, execute_write

def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

def create_user(username: str, full_name: str, plain_password: str, role: str):
    pw_hash = hash_password(plain_password)
    execute_write(
        """
        INSERT INTO users (username, full_name, password_hash, role)
        VALUES (:username, :full_name, :password_hash, :role)
        """,
        {
            "username": username.strip().lower(),
            "full_name": full_name.strip(),
            "password_hash": pw_hash,
            "role": role,
        },
    )

def authenticate_user(username: str, plain_password: str):
    df = run_query(
        """
        SELECT user_id, username, full_name, password_hash, role, is_active
        FROM users
        WHERE username = :username
        """,
        {"username": username.strip().lower()},
    )
    if df.empty:
        return None
    user = df.iloc[0]
    if not user["is_active"]:
        return "inactive"
    if verify_password(plain_password, user["password_hash"]):
        return {
            "user_id": int(user["user_id"]),
            "username": user["username"],
            "full_name": user["full_name"],
            "role": user["role"],
        }
    return None

def require_login(allowed_roles=None):
    """Blocks unauthenticated users and restricts pages by role."""
    if "user" not in st.session_state or st.session_state["user"] is None:
        st.warning("Please log in to access this page.")
        st.stop()

    user = st.session_state["user"]
    if allowed_roles and user["role"] not in allowed_roles:
        st.error(f"Access Denied: This section requires {', '.join(allowed_roles)} privileges.")
        st.stop()
    return user

def render_login_screen():
    # Check if any users exist yet; if none, show first-time Admin Setup
    existing_users = run_query("SELECT COUNT(*) AS cnt FROM users;")
    if existing_users.iloc[0]["cnt"] == 0:
        st.subheader("Initial System Setup — Create Admin Account")
        st.info("No users exist in the database yet. Create your primary Administrator account to begin.")
        with st.form("setup_admin_form"):
            full_name = st.text_input("Full Name", placeholder="e.g., Gift Mwanyumba")
            username = st.text_input("Admin Username", placeholder="e.g., admin")
            password = st.text_input("Password", type="password")
            confirm = st.text_input("Confirm Password", type="password")
            submitted = st.form_submit_button("Create Admin Account")

            if submitted:
                if not full_name or not username or not password:
                    st.error("All fields are required.")
                elif password != confirm:
                    st.error("Passwords do not match.")
                else:
                    create_user(username, full_name, password, "admin")
                    st.success("Admin account created! You can now log in.")
                    st.rerun()
        return

    # Standard Login Form
    st.subheader("System Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log In")

        if submitted:
            result = authenticate_user(username, password)
            if result == "inactive":
                st.error("This account has been deactivated. Contact an administrator.")
            elif result:
                st.session_state["user"] = result
                st.rerun()
            else:
                st.error("Invalid username or password.")