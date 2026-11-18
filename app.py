import streamlit as st
from db import run_query, execute_write
from auth import render_login_screen, require_login, create_user
from inventory import render_inventory_page
from pos import render_pos_page
from accounting import render_accounting_page

st.set_page_config(page_title="MyOdoo", layout="wide")
if "user" not in st.session_state:
    st.session_state["user"] = None

st.title("MyOdoo")

# 1. Enforce Authentication
if st.session_state["user"] is None:
    render_login_screen()
    st.stop()

user = st.session_state["user"]

# 2. Sidebar Navigation Based on Role
with st.sidebar:
    st.markdown(f"**Logged in as:** {user['full_name']}")
    st.caption(f"Role: `{user['role'].upper()}`")
    st.divider()

    if user["role"] == "admin":
        menu_options = [
            "POS Checkout",
            "Inventory & Products",
            "Accounting & Journal",
            "User Management",
        ]
    else:
        menu_options = ["POS Checkout"]

    choice = st.radio("Navigation", menu_options)
    st.divider()
    if st.button("Log Out", use_container_width=True):
        st.session_state["user"] = None
        st.rerun()

# 3. Route Views with Explicit Role Checks
if choice == "POS Checkout":
    require_login(["admin", "cashier"])
    render_pos_page(user)
    require_login(["admin", "cashier"])
    st.header("Point of Sale (POS) Checkout")
    st.info("Ready to build the barcode/product cart and automatic double-entry posting here next.")

elif choice == "Inventory & Products":
    require_login(["admin"])
    render_inventory_page(user)
    require_login(["admin"])
    st.header("Inventory & Products Management")
    st.info("Ready to build product catalog, stock levels, and restock journal entries here.")

elif choice == "Accounting & Journal":
    require_login(["admin"])
    render_accounting_page(user)
    require_login(["admin"])
    st.header("Accounting & Chart of Accounts")
    df = run_query("SELECT account_code, account_name, account_type FROM chart_of_accounts ORDER BY account_code;")
    st.dataframe(df, use_container_width=True, hide_index=True)

elif choice == "User Management":
    require_login(["admin"])
    st.header("Staff & Role Management")

    col1, col2 = st.columns([1, 2])
    with col1:
        st.subheader("Add New User")
        with st.form("add_user_form"):
            new_name = st.text_input("Full Name")
            new_user = st.text_input("Username")
            new_pass = st.text_input("Temporary Password", type="password")
            new_role = st.selectbox("Role", ["cashier", "admin"])
            if st.form_submit_button("Create User"):
                if new_name and new_user and new_pass:
                    try:
                        create_user(new_user, new_name, new_pass, new_role)
                        st.success(f"User '{new_user}' created as {new_role}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not create user: {e}")
                else:
                    st.warning("Please fill in all fields.")

    with col2:
        st.subheader("Existing Staff Accounts")
        users_df = run_query(
            "SELECT user_id, username, full_name, role, is_active, created_at FROM users ORDER BY user_id;"
        )
        st.dataframe(users_df, use_container_width=True, hide_index=True)
