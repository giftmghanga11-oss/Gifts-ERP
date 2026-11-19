from datetime import date
from io import BytesIO

import pandas as pd
import streamlit as st
from fpdf import FPDF

from db import run_query
from common import (
    COMPANY_NAME,
    CURRENCY,
    lookup_account_id,
    pdf_line,
    pdf_row,
    show_df,
)
from customers import customer_balances_df

BUCKETS = ["Current (0-30)", "31-60 days", "61-90 days", "Over 90 days"]


# ------------------------------------------------------------------- data
def load_customer_ledger(customer_id: int, d_to: date) -> pd.DataFrame:
    """Every Accounts Receivable movement for one customer up to d_to."""
    ar_id = lookup_account_id("ar")
    df = run_query(
        """
        SELECT CAST(je.entry_date AS date) AS txn_date,
               je.entry_id,
               je.reference_type,
               je.description,
               CAST(jl.debit AS FLOAT)  AS debit,
               CAST(jl.credit AS FLOAT) AS credit
        FROM journal_lines jl
        JOIN journal_entries je ON je.entry_id = jl.entry_id
        WHERE je.customer_id = :cid
          AND jl.account_id = :ar
          AND CAST(je.entry_date AS date) <= :d_to
        ORDER BY je.entry_date, je.entry_id, jl.line_id;
        """,
        {"cid": customer_id, "ar": ar_id, "d_to": d_to},
    )
    df["txn_date"] = pd.to_datetime(df["txn_date"]).dt.date
    return df


def build_statement(ledger: pd.DataFrame, d_from: date, d_to: date):
    """Return (display_df, opening, total_debits, total_credits, closing)."""
    before = ledger[ledger["txn_date"] < d_from]
    period = ledger[ledger["txn_date"] >= d_from].copy()

    opening = round(float((before["debit"] - before["credit"]).sum()), 2)
    period["balance"] = opening + (period["debit"] - period["credit"]).cumsum()
    total_dr = round(float(period["debit"].sum()), 2)
    total_cr = round(float(period["credit"].sum()), 2)
    closing = round(opening + total_dr - total_cr, 2)

    rows = [
        {"Date": str(d_from), "Reference": "", "Description": "Opening balance", "Debit": None, "Credit": None, "Balance": opening}
    ]
    for _, r in period.iterrows():
        rows.append(
            {
                "Date": str(r["txn_date"]),
                "Reference": f"{r['reference_type']}-{int(r['entry_id'])}",
                "Description": r["description"],
                "Debit": r["debit"] if r["debit"] else None,
                "Credit": r["credit"] if r["credit"] else None,
                "Balance": round(float(r["balance"]), 2),
            }
        )
    rows.append(
        {"Date": str(d_to), "Reference": "", "Description": "Closing balance", "Debit": total_dr, "Credit": total_cr, "Balance": closing}
    )
    return pd.DataFrame(rows), opening, total_dr, total_cr, closing


def fifo_ageing(ledger: pd.DataFrame, as_of: date) -> dict:
    """Allocate all credits to the oldest debits first; age what is left."""
    out = {b: 0.0 for b in BUCKETS}
    credits = float(ledger["credit"].sum())
    for _, r in ledger[ledger["debit"] > 0].sort_values(["txn_date", "entry_id"]).iterrows():
        left = float(r["debit"])
        used = min(left, credits)
        credits -= used
        left -= used
        if left <= 0.005:
            continue
        age = (as_of - r["txn_date"]).days
        key = BUCKETS[0] if age <= 30 else BUCKETS[1] if age <= 60 else BUCKETS[2] if age <= 90 else BUCKETS[3]
        out[key] += left
    return {k: round(v, 2) for k, v in out.items()}


# ---------------------------------------------------------------- exports
def _num(v):
    return "" if v is None or pd.isna(v) else f"{v:,.2f}"


def statement_pdf(customer, d_from, d_to, stmt: pd.DataFrame, ageing: dict) -> bytes:
    pdf = FPDF()  # A4 portrait, 190 mm usable
    pdf.add_page()
    pdf.set_margins(10, 10, 10)

    pdf_line(pdf, COMPANY_NAME, h=9, style="B", size=18)
    pdf_line(pdf, "CUSTOMER STATEMENT", h=7, style="B", size=12)
    pdf.ln(2)
    pdf_line(pdf, f"{customer['Customer']}   |   Account: {customer['Account Code']}", h=6, style="B", size=10)
    pdf_line(pdf, f"Phone: {customer['Phone'] or 'N/A'}   KRA PIN: {customer['KRA PIN'] or 'N/A'}", h=5)
    pdf_line(pdf, f"Period: {d_from:%d %b %Y} to {d_to:%d %b %Y}   |   Printed: {date.today():%d %b %Y}   |   Currency: {CURRENCY}", h=5)
    pdf.ln(3)

    w = [24, 28, 62, 25, 25, 26]
    pdf_row(
        pdf,
        [(w[0], "Date", "L"), (w[1], "Reference", "L"), (w[2], "Description", "L"),
         (w[3], "Debit", "R"), (w[4], "Credit", "R"), (w[5], "Balance", "R")],
        style="B",
    )
    last = len(stmt) - 1
    for i, r in stmt.iterrows():
        bold = "B" if i in (0, last) else ""
        pdf_row(
            pdf,
            [(w[0], r["Date"], "L"), (w[1], r["Reference"], "L"), (w[2], str(r["Description"])[:40], "L"),
             (w[3], _num(r["Debit"]), "R"), (w[4], _num(r["Credit"]), "R"), (w[5], _num(r["Balance"]), "R")],
            h=5, style=bold, size=8,
        )

    pdf.ln(5)
    pdf_line(pdf, "Ageing of outstanding balance (oldest invoices settled first)", h=6, style="B", size=9)
    aw = 190 / 4
    pdf_row(pdf, [(aw, b, "C") for b in BUCKETS], h=6, style="B", size=8)
    pdf_row(pdf, [(aw, f"{ageing[b]:,.2f}", "C") for b in BUCKETS], h=6, size=9)
    pdf.ln(4)
    pdf_line(pdf, "Please quote your account code when making payment. Report any difference within 7 days.", h=5, style="I", size=8)
    return bytes(pdf.output())


def statement_excel(customer, d_from, d_to, stmt: pd.DataFrame, ageing: dict) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        stmt.to_excel(xw, sheet_name="Statement", index=False, startrow=5)
        ws = xw.sheets["Statement"]
        ws["A1"] = f"{COMPANY_NAME} - Customer Statement"
        ws["A2"] = f"{customer['Customer']} ({customer['Account Code']})"
        ws["A3"] = f"Period: {d_from} to {d_to}   Currency: {CURRENCY}"
        for col, width in zip("ABCDEF", [12, 18, 52, 15, 15, 16]):
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows(min_row=7, min_col=4, max_col=6):
            for cell in row:
                cell.number_format = "#,##0.00"
        start = len(stmt) + 9
        ws.cell(row=start, column=1, value="Ageing (oldest invoices settled first)")
        for j, b in enumerate(BUCKETS):
            ws.cell(row=start + 1, column=1 + j, value=b)
            ws.cell(row=start + 2, column=1 + j, value=ageing[b]).number_format = "#,##0.00"
    return buf.getvalue()


def balances_excel(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Customer Balances", index=False)
        ws = xw.sheets["Customer Balances"]
        for col, width in zip("ABCDE", [14, 34, 16, 14, 16]):
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows(min_row=2, min_col=5, max_col=5):
            for cell in row:
                cell.number_format = "#,##0.00"
    return buf.getvalue()


# -------------------------------------------------------------------- page
def render_reports_page(user):
    st.header("Reports")
    tab1, tab2 = st.tabs(["Customer Statement", "Customer Balances"])

    with tab1:
        balances = customer_balances_df()
        if balances.empty:
            st.info("No customers yet. Create one under Customers.")
        else:
            options = {f"{r['Customer']} ({r['Account Code']})": r for _, r in balances.iterrows()}
            c1, c2, c3 = st.columns([3, 1.4, 1.4])
            label = c1.selectbox("Customer", list(options.keys()), key="stmt_customer")
            today = date.today()
            d_from = c2.date_input("From", value=today.replace(day=1), key="stmt_from")
            d_to = c3.date_input("To", value=today, key="stmt_to")

            if d_from > d_to:
                st.error("'From' date must be on or before 'To' date.")
            else:
                customer = options[label]
                ledger = load_customer_ledger(int(customer["customer_id"]), d_to)
                stmt, opening, tdr, tcr, closing = build_statement(ledger, d_from, d_to)
                ageing = fifo_ageing(ledger, d_to)

                m = st.columns(4)
                m[0].metric("Opening balance", f"{CURRENCY} {opening:,.2f}")
                m[1].metric("Invoiced (debits)", f"{CURRENCY} {tdr:,.2f}")
                m[2].metric("Received (credits)", f"{CURRENCY} {tcr:,.2f}")
                m[3].metric("Closing balance", f"{CURRENCY} {closing:,.2f}")

                if len(stmt) == 2:
                    st.info("No transactions in this period.")
                show_df(
                    stmt.style.format({"Debit": "{:,.2f}", "Credit": "{:,.2f}", "Balance": "{:,.2f}"}, na_rep="")
                )

                st.caption("Ageing of outstanding balance as at the 'To' date")
                show_df(pd.DataFrame([ageing]).style.format("{:,.2f}"))

                safe = "".join(ch if ch.isalnum() else "_" for ch in customer["Customer"])[:30]
                base = f"Statement_{customer['Account Code']}_{safe}_{d_from}_{d_to}"
                b1, b2, _ = st.columns([1, 1, 3])
                try:
                    b1.download_button(
                        "Download PDF",
                        data=statement_pdf(customer, d_from, d_to, stmt, ageing),
                        file_name=base + ".pdf",
                        mime="application/pdf",
                        type="primary",
                    )
                except Exception as e:
                    b1.error(f"PDF failed: {e}")
                b2.download_button(
                    "Download Excel",
                    data=statement_excel(customer, d_from, d_to, stmt, ageing),
                    file_name=base + ".xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

    with tab2:
        df = customer_balances_df().drop(columns=["customer_id"])
        only_owing = st.checkbox("Show only customers with a balance", value=True)
        view = df[df["Balance"].abs() > 0.005] if only_owing else df
        st.metric("Total receivables", f"{CURRENCY} {df['Balance'].sum():,.2f}")
        show_df(view.style.format({"Balance": "{:,.2f}"}))
        st.download_button(
            "Download Excel",
            data=balances_excel(view),
            file_name=f"Customer_Balances_{date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
