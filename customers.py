import re
import streamlit as st
from sqlalchemy import text
from db import get_engine, run_query, execute_write
from common import (
    CURRENCY,
    PAYMENT_METHODS,
    account_id,
    lookup_account_id,
    post_entry,
    show_df,
)

KRA_PIN_RE = re.compile(r"^[AP]\d{9}[A-Z]$")


def normalize_phone(raw: str):
    """Return +2547XXXXXXXX / +2541XXXXXXXX, '' if blank, None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("254") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+254" + digits[1:]
    if len(digits) == 9 and digits[0] in "71":
        return "+254" + digits
    return None


def next_account_code() -> str:
    n = int(run_query("SELECT COALESCE(MAX(customer_id), 0) + 1 AS n FROM customers;").iloc[0]["n"])
    return f"CUST-{n:04d}"


def customer_balances_df():
    """One row per customer with the amount they owe (debit - credit on Accounts Receivable)."""
    ar_id = lookup_account_id("ar")
    return run_query(
        """
        SELECT c.customer_id,
               c.account_code AS "Account Code",
               c.customer_name AS "Customer",
               c.phone_no AS "Phone",
               c.kra_pin AS "KRA PIN",
               CAST(COALESCE(SUM(jl.debit - jl.credit), 0) AS FLOAT) AS "Balance"
        FROM customers c
        LEFT JOIN journal_entries je ON je.customer_id = c.customer_id
        LEFT JOIN journal_lines jl ON jl.entry_id = je.entry_id AND jl.account_id = :ar
        GROUP BY c.customer_id, c.account_code, c.customer_name, c.phone_no, c.kra_pin
        ORDER BY c.customer_name;
        """,
        {"ar": ar_id},
    )


def create_customer(code, name, phone, pin):
    execute_write(
        """
        INSERT INTO customers (account_code, customer_name, phone_no, kra_pin)
        VALUES (:code, :name, :phone, :pin)
        """,
        {"code": code, "name": name, "phone": phone, "pin": pin},
    )


def record_receipt(customer, amount, method_label, reference, user_id):
    """Customer pays: Dr Cash/M-Pesa/Bank, Cr Accounts Receivable."""
    with get_engine().begin() as conn:
        pay_acc = account_id(conn, PAYMENT_METHODS[method_label])
        ar_acc = account_id(conn, "ar")
        desc = f"Payment from {customer['Customer']} ({customer['Account Code']}) via {method_label}"
        if reference:
            desc += f" ref {reference.strip()}"
        return post_entry(
            conn,
            "CUST_RECEIPT",
            desc,
            user_id,
            [(pay_acc, amount, 0), (ar_acc, 0, amount)],
            customer_id=int(customer["customer_id"]),
        )


def render_customers_page(user):
    st.header("Customer Accounts")
    st.caption("Account customers can buy on credit; their balance feeds the customer statement in Reports.")

    flash = st.session_state.pop("cust_flash", None)
    if flash:
        st.success(flash)

    tab1, tab2, tab3 = st.tabs(["Customer Directory", "New Customer", "Receive Payment"])

    with tab1:
        df = customer_balances_df()
        if df.empty:
            st.info("No customers yet. Use the New Customer tab to add one.")
        else:
            q = st.text_input("Search by name, account code, phone or KRA PIN")
            view = df
            if q:
                mask = (
                    df[["Account Code", "Customer", "Phone", "KRA PIN"]]
                    .fillna("")
                    .astype(str)
                    .apply(lambda col: col.str.contains(q, case=False, regex=False))
                    .any(axis=1)
                )
                view = df[mask]
            c1, c2 = st.columns(2)
            c1.metric("Customers", len(df))
            c2.metric("Total receivables", f"{CURRENCY} {df['Balance'].sum():,.2f}")
            show_df(view.drop(columns=["customer_id"]).rename(columns={"Balance": f"Balance ({CURRENCY})"}))

    with tab2:
        with st.form("new_customer_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                code = st.text_input("Account code", value=next_account_code())
                name = st.text_input("Customer / company name")
            with c2:
                phone = st.text_input("Phone number", placeholder="0712 345 678")
                pin = st.text_input("KRA PIN (optional)", placeholder="A123456789Z")

            if st.form_submit_button("Save customer"):
                code, name, pin = code.strip().upper(), name.strip(), pin.strip().upper()
                phone_norm = normalize_phone(phone)
                if not code or not name:
                    st.error("Account code and customer name are required.")
                elif phone_norm is None:
                    st.error("Phone number is not valid. Use e.g. 0712345678 or +254712345678.")
                elif pin and not KRA_PIN_RE.match(pin):
                    st.error("KRA PIN must look like A123456789Z.")
                else:
                    dup = run_query(
                        """
                        SELECT customer_name, account_code FROM customers
                        WHERE account_code = :code
                           OR (:pin <> '' AND kra_pin = :pin)
                           OR (:phone <> '' AND phone_no = :phone)
                        LIMIT 1;
                        """,
                        {"code": code, "pin": pin, "phone": phone_norm},
                    )
                    if not dup.empty:
                        d = dup.iloc[0]
                        st.error(f"Already registered: {d['customer_name']} ({d['account_code']}).")
                    else:
                        try:
                            create_customer(code, name, phone_norm, pin)
                            st.session_state["cust_flash"] = f"Customer {name} ({code}) created."
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not save customer: {e}")

    with tab3:
        df = customer_balances_df()
        if df.empty:
            st.info("Add a customer first.")
        else:
            options = {f"{r['Customer']} ({r['Account Code']})": r for _, r in df.iterrows()}
            label = st.selectbox("Customer", list(options.keys()))
            cust = options[label]
            st.metric("Outstanding balance", f"{CURRENCY} {cust['Balance']:,.2f}")

            with st.form("receipt_form", clear_on_submit=True):
                amount = st.number_input(f"Amount received ({CURRENCY})", min_value=0.0, step=100.0)
                method = st.selectbox("Received via", list(PAYMENT_METHODS.keys()))
                ref = st.text_input("Reference (M-Pesa code, cheque no., bank ref)")
                if st.form_submit_button("Post payment"):
                    if amount <= 0:
                        st.error("Enter an amount greater than zero.")
                    else:
                        try:
                            record_receipt(cust, round(amount, 2), method, ref, user["user_id"])
                            st.session_state["cust_flash"] = (
                                f"Payment of {CURRENCY} {amount:,.2f} posted to {cust['Customer']}."
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not post payment: {e}")
