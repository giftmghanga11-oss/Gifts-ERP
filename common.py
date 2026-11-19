"""Shared settings and accounting helpers for MyOdoo.

Every page posts to the ledger through post_entry() so that:
  * all journal tables/columns are named in ONE place,
  * every entry is balanced before it is written,
  * the whole sale / receipt / expense is one database transaction.
"""
import streamlit as st
from sqlalchemy import text
from db import get_engine

# ---------------------------------------------------------------- settings
COMPANY_NAME = "MyOdoo"
CURRENCY = "KES"
PRICES_INCLUDE_VAT = True  # True: selling_price already contains VAT

# key -> (account_code, fallback name pattern).
# Check these against SELECT * FROM chart_of_accounts and edit to match yours.
ACCOUNTS = {
    "cash": ("1010", "%cash%"),
    "mpesa": ("1020", "%m-pesa%"),
    "bank": ("1030", "%bank%"),
    "ar": ("1100", "%receivable%"),
    "inventory": ("1200", "%inventory%"),
    "vat": ("2100", "%vat%"),
    "sales": ("4000", "%sales%"),
    "cogs": ("5000", "%cost of goods%"),
}

PAYMENT_METHODS = {"Cash": "cash", "M-Pesa": "mpesa", "Bank / Card": "bank"}
ON_ACCOUNT = "On Account (Credit)"

# Who sees what. Move "Reports" into "cashier" if cashiers may print statements.
ROLE_PAGES = {
    "admin": [
        "POS Checkout",
        "Customers",
        "Inventory & Products",
        "Accounting & Journal",
        "Reports",
        "User Management",
    ],
    "cashier": ["POS Checkout", "Customers"],
}


# ------------------------------------------------------------------ UI helpers
def show_df(df, **kwargs):
    """st.dataframe full width, working on old and new Streamlit versions."""
    try:
        st.dataframe(df, width="stretch", hide_index=True, **kwargs)
    except TypeError:
        st.dataframe(df, use_container_width=True, hide_index=True, **kwargs)


# --------------------------------------------------------------- ledger helpers
def account_id(conn, key, required=True):
    """Resolve a chart_of_accounts.account_id by code, then by name pattern."""
    code, pattern = ACCOUNTS[key]
    row = conn.execute(
        text("SELECT account_id FROM chart_of_accounts WHERE account_code = :c"),
        {"c": code},
    ).fetchone()
    if row is None:
        row = conn.execute(
            text(
                "SELECT account_id FROM chart_of_accounts "
                "WHERE account_name ILIKE :p ORDER BY account_code LIMIT 1"
            ),
            {"p": pattern},
        ).fetchone()
    if row is None:
        if required:
            raise ValueError(
                f"Account '{key}' (code {code}) not found in chart_of_accounts. "
                "Run sql/migration.sql or edit ACCOUNTS in common.py."
            )
        return None
    return int(row[0])


def lookup_account_id(key, required=True):
    with get_engine().connect() as conn:
        return account_id(conn, key, required)


def post_entry(conn, ref_type, description, user_id, lines, customer_id=None, ref_id=None):
    """Write a balanced journal entry inside the caller's transaction.

    lines = [(account_id, debit, credit), ...]
    """
    dr = round(sum(float(l[1]) for l in lines), 2)
    cr = round(sum(float(l[2]) for l in lines), 2)
    if abs(dr - cr) > 0.005:
        raise ValueError(f"Unbalanced entry: debits {dr:,.2f} <> credits {cr:,.2f}")
    if dr <= 0:
        raise ValueError("Journal entry has no value.")

    entry_id = conn.execute(
        text(
            """
            INSERT INTO journal_entries
                (reference_type, reference_id, description, created_by, customer_id)
            VALUES (:rt, :rid, :d, :u, :cid)
            RETURNING entry_id
            """
        ),
        {"rt": ref_type, "rid": ref_id, "d": description.strip(), "u": user_id, "cid": customer_id},
    ).scalar()

    for acc, debit, credit in lines:
        if float(debit) == 0 and float(credit) == 0:
            continue
        conn.execute(
            text(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) "
                "VALUES (:e, :a, :d, :c)"
            ),
            {"e": entry_id, "a": acc, "d": round(float(debit), 2), "c": round(float(credit), 2)},
        )
    return int(entry_id)


# ----------------------------------------------------------------- PDF helpers
def safe_text(value):
    """fpdf2 core fonts are Latin-1 only; replace anything else instead of crashing."""
    return str(value if value is not None else "").encode("latin-1", "replace").decode("latin-1")


def pdf_line(pdf, txt, h=5, align="L", style="", size=9):
    pdf.set_font("Helvetica", style, size)
    pdf.cell(0, h, text=safe_text(txt), align=align, new_x="LMARGIN", new_y="NEXT")


def pdf_row(pdf, cells, h=6, border=1, style="", size=9):
    """cells = [(width, text, align), ...]; the row ends with a line break."""
    pdf.set_font("Helvetica", style, size)
    for i, (w, txt, align) in enumerate(cells):
        last = i == len(cells) - 1
        pdf.cell(
            w,
            h,
            text=safe_text(txt),
            border=border,
            align=align,
            new_x="LMARGIN" if last else "RIGHT",
            new_y="NEXT" if last else "TOP",
        )
