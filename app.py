import streamlit as st

from db import run_query, execute_write
from auth import render_login_screen, require_login, create_user, MIN_PASSWORD_LEN
from common import ROLE_PAGES, show_df
from inventory import render_inventory_page
from pos import render_pos_page
from accounting import render_accounting_page
from customers import render_customers_page
from reports import render_reports_page
from returns import render_returns_page
from documents import render_receipts_page

st.set_page_config(page_title="MyOdoo", layout="wide")

if "user" not in st.session_state:
    st.session_state["user"] = None

# 1. Authentication (the login screen draws its own branding)
if st.session_state["user"] is None:
    render_login_screen()
    st.stop()

user = st.session_state["user"]


def render_users_page(user):
    st.header("Staff & Role Management")
    col1, col2 = st.columns([1, 2])

    with col1:
        st.subheader("Add user")
        with st.form("add_user_form", clear_on_submit=True):
            new_name = st.text_input("Full name")
            new_user = st.text_input("Login")
            new_pass = st.text_input("Temporary password", type="password")
            new_role = st.selectbox("Role", ["cashier", "admin"])
            if st.form_submit_button("Create user"):
                if not (new_name and new_user and new_pass):
                    st.warning("Please fill in all fields.")
                elif len(new_pass) < MIN_PASSWORD_LEN:
                    st.warning(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
                else:
                    try:
                        # keyword arguments: the old positional call mixed up password and full name
                        create_user(username=new_user, password=new_pass, full_name=new_name, role=new_role)
                        st.success(f"User '{new_user}' created as {new_role}.")
                    except Exception as e:
                        st.error(f"Could not create user: {e}")

    with col2:
        st.subheader("Staff accounts")
        users_df = run_query(
            "SELECT user_id, username, full_name, role, is_active, created_at FROM users ORDER BY user_id;"
        )
        show_df(users_df)

        st.subheader("Enable / disable an account")
        others = users_df[users_df["user_id"] != user["user_id"]]
        if others.empty:
            st.caption("No other accounts yet.")
        else:
            labels = {f"{r['username']} ({'active' if r['is_active'] else 'disabled'})": r for _, r in others.iterrows()}
            pick = labels[st.selectbox("Account", list(labels.keys()))]
            action = "Disable" if pick["is_active"] else "Enable"
            if st.button(f"{action} account"):
                execute_write(
                    "UPDATE users SET is_active = :a WHERE user_id = :id",
                    {"a": not bool(pick["is_active"]), "id": int(pick["user_id"])},
                )
                st.rerun()


# page name -> (allowed roles, renderer)
PAGES = {
    "POS Checkout": (["admin", "cashier"], render_pos_page),
    "Receipts": (["admin", "cashier"], render_receipts_page),
    "Customers": (["admin", "cashier"], render_customers_page),
    "Sales Returns": (["admin"], render_returns_page),
    "Inventory & Products": (["admin"], render_inventory_page),
    "Accounting & Journal": (["admin"], render_accounting_page),
    "Reports": (["admin"], render_reports_page),
    "User Management": (["admin"], render_users_page),
}

# 2. Sidebar navigation based on role
with st.sidebar:
    st.markdown(f"**{user['full_name']}**")
    st.caption(f"Role: {user['role'].upper()}")
    st.divider()
    menu = [p for p in ROLE_PAGES.get(user["role"], []) if p in PAGES]
    choice = st.radio("Navigation", menu)
    st.divider()
    if st.button("Log out"):
        st.session_state["user"] = None
        st.rerun()

# 3. Route, re-checking the role on the server side
roles, renderer = PAGES[choice]
require_login(roles)
renderer(user)
