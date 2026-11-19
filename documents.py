from datetime import date, timedelta

import pandas as pd
import streamlit as st
from fpdf import FPDF

from db import run_query
from common import (
    ACCOUNTS,
    COMPANY_NAME,
    CURRENCY,
    pdf_line,
    pdf_row,
    show_df,
)
from pos import generate_pdf_receipt

PAY_LABELS = {"cash": "Cash", "mpesa": "M-Pesa", "bank": "Bank / Card", "ar": "On Account (Credit)"}
PAY_CODES = {ACCOUNTS[k][0]: label for k, label in PAY_LABELS.items()}
PAY_CODES_SQL = ",".join(f"'{c}'" for c in PAY_CODES)  # constants from common.py, not user input


# ------------------------------------------------------------------ loaders
def _customer_for(customer_id):
    if customer_id is None or pd.isna(customer_id):
        return None
    df = run_query(
        "SELECT customer_id, account_code, customer_name, phone_no, kra_pin FROM customers WHERE customer_id = :c;",
        {"c": int(customer_id)},
    )
    return None if df.empty else df.iloc[0].to_dict()


def load_sale(entry_id: int):
    """Everything needed to (re)print a receipt for POS-entry_id."""
    head = run_query(
        """
        SELECT je.entry_id, je.entry_date, je.customer_id, u.full_name
        FROM journal_entries je LEFT JOIN users u ON u.user_id = je.created_by
        WHERE je.entry_id = :e AND je.reference_type = 'SALE';
        """,
        {"e": entry_id},
    )
    if head.empty:
        raise ValueError("Sale not found.")
    h = head.iloc[0]

    led = run_query(
        """
        SELECT c.account_code AS code, CAST(jl.debit AS FLOAT) AS debit, CAST(jl.credit AS FLOAT) AS credit
        FROM journal_lines jl JOIN chart_of_accounts c ON c.account_id = jl.account_id
        WHERE jl.entry_id = :e;
        """,
        {"e": entry_id},
    )
    paid = led[led["code"].isin(list(PAY_CODES))]
    payment = PAY_CODES[paid.iloc[0]["code"]] if not paid.empty else "N/A"

    items = run_query(
        """
        SELECT product_name AS name, qty, CAST(unit_price AS FLOAT) AS price,
               CAST(total_amount AS FLOAT) AS total, CAST(net_amount AS FLOAT) AS net,
               CAST(vat_amount AS FLOAT) AS vat, returned_qty
        FROM pos_sale_items WHERE entry_id = :e ORDER BY item_id;
        """,
        {"e": entry_id},
    )

    detailed = not items.empty
    if detailed:
        lines = [
            {"name": r["name"], "qty": int(r["qty"]), "price": r["price"], "total": r["total"]}
            for _, r in items.iterrows()
        ]
        net, vat, total = float(items["net"].sum()), float(items["vat"].sum()), float(items["total"].sum())
        note = "Some items on this sale were later returned; see the credit note." if items["returned_qty"].sum() > 0 else ""
    else:
        total = float(paid["debit"].sum())
        vat = float(led[led["code"] == ACCOUNTS["vat"][0]]["credit"].sum())
        net = total - vat
        lines = [{"name": "Goods sold (item detail not recorded)", "qty": 1, "price": total, "total": total}]
        note = "Item detail was not recorded for this sale; totals are taken from the ledger."

    sale = {
        "entry_id": int(entry_id),
        "lines": lines,
        "net": round(net, 2),
        "vat": round(vat, 2),
        "total": round(total, 2),
        "payment": payment,
    }
    return {
        "sale": sale,
        "customer": _customer_for(h["customer_id"]),
        "issued": pd.to_datetime(h["entry_date"]).to_pydatetime(),
        "cashier": h["full_name"] or "",
        "note": note,
        "detailed": detailed,
    }


def load_credit_note(cn_id: int):
    head = run_query(
        """
        SELECT je.entry_id, je.entry_date, je.reference_id, u.full_name
        FROM journal_entries je LEFT JOIN users u ON u.user_id = je.created_by
        WHERE je.entry_id = :e AND je.reference_type = 'SALE_RETURN';
        """,
        {"e": cn_id},
    )
    if head.empty:
        raise ValueError("Return not found.")
    h = head.iloc[0]
    items = run_query(
        """
        SELECT product_name, qty, CAST(unit_price AS FLOAT) AS unit_price, CAST(net_amount AS FLOAT) AS net,
               CAST(vat_amount AS FLOAT) AS vat, CAST(total_amount AS FLOAT) AS total, reason, settled_via
        FROM pos_return_items WHERE return_entry_id = :e ORDER BY return_item_id;
        """,
        {"e": cn_id},
    )
    if items.empty:
        raise ValueError("No item detail was stored for this return.")
    sale_id = int(h["reference_id"])
    cust_row = run_query("SELECT customer_id FROM journal_entries WHERE entry_id = :e;", {"e": sale_id})
    return {
        "cn_id": int(cn_id),
        "sale_id": sale_id,
        "issued": pd.to_datetime(h["entry_date"]).to_pydatetime(),
        "processed_by": h["full_name"] or "",
        "customer": _customer_for(cust_row.iloc[0]["customer_id"]) if not cust_row.empty else None,
        "items": items,
        "reason": items.iloc[0]["reason"],
        "settled_via": items.iloc[0]["settled_via"],
    }


# ---------------------------------------------------------------- credit note
def credit_note_pdf(cn) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_margins(10, 10, 10)

    pdf_line(pdf, COMPANY_NAME, h=9, style="B", size=18)
    pdf_line(pdf, "CREDIT NOTE (RETURN INVOICE)", h=7, style="B", size=12)
    pdf.ln(2)
    pdf_line(pdf, f"Credit note no: CN-{cn['cn_id']:06d}", h=6, style="B", size=10)
    pdf_line(pdf, f"Date: {cn['issued']:%d %b %Y %H:%M}", h=5)
    pdf_line(pdf, f"Original receipt: POS-{cn['sale_id']:06d}", h=5)
    if cn["processed_by"]:
        pdf_line(pdf, f"Processed by: {cn['processed_by']}", h=5)
    pdf.ln(2)

    c = cn["customer"]
    if c:
        pdf_line(pdf, f"Customer: {c['customer_name']}  ({c['account_code']})", h=5, style="B")
        pdf_line(pdf, f"Phone: {c.get('phone_no') or 'N/A'}   KRA PIN: {c.get('kra_pin') or 'N/A'}", h=5)
    else:
        pdf_line(pdf, "Customer: Walk-in", h=5, style="I")
    pdf.ln(3)

    w = [85, 15, 28, 27, 35]  # 190 mm
    pdf_row(pdf, [(w[0], "Item returned", "L"), (w[1], "Qty", "C"), (w[2], "Unit price", "R"),
                  (w[3], "VAT", "R"), (w[4], "Total", "R")], style="B")
    for _, r in cn["items"].iterrows():
        pdf_row(
            pdf,
            [(w[0], str(r["product_name"])[:45], "L"), (w[1], str(int(r["qty"])), "C"),
             (w[2], f"{r['unit_price']:,.2f}", "R"), (w[3], f"{r['vat']:,.2f}", "R"),
             (w[4], f"{r['total']:,.2f}", "R")],
            h=6, size=9,
        )
    pdf.ln(2)
    label_w = 190 - w[4]
    net, vat, total = float(cn["items"]["net"].sum()), float(cn["items"]["vat"].sum()), float(cn["items"]["total"].sum())
    pdf_row(pdf, [(label_w, "Subtotal (excl. VAT)", "R"), (w[4], f"{net:,.2f}", "R")], h=5)
    pdf_row(pdf, [(label_w, "VAT", "R"), (w[4], f"{vat:,.2f}", "R")], h=5)
    pdf_row(pdf, [(label_w, f"TOTAL CREDITED ({CURRENCY})", "R"), (w[4], f"{total:,.2f}", "R")], h=7, style="B", size=10)

    pdf.ln(4)
    pdf_line(pdf, f"Settled: {cn['settled_via']}", h=5, style="B")
    pdf_line(pdf, f"Reason for return: {cn['reason']}", h=5)
    pdf.ln(14)
    pdf_line(pdf, "Customer signature: ______________________      Authorised by: ______________________", h=6)
    return bytes(pdf.output())


# ------------------------------------------------------------- receipts page
def render_receipts_page(user):
    st.header("Receipts")
    st.caption("Find any past sale and download a duplicate receipt.")

    c1, c2, c3 = st.columns([2, 1.2, 1.2])
    q = c1.text_input("Receipt number, customer or product")
    today = date.today()
    d_from = c2.date_input("From", value=today - timedelta(days=30))
    d_to = c3.date_input("To", value=today)
    if d_from > d_to:
        st.error("'From' date must be on or before 'To' date.")
        return

    q_clean = q.strip()
    if q_clean.upper().startswith("POS"):
        q_clean = q_clean.upper().replace("POS", "").lstrip("-").lstrip("0")

    sales = run_query(
        f"""
        SELECT je.entry_id, CAST(je.entry_date AS date) AS d, je.description,
               CAST(COALESCE(SUM(CASE WHEN c.account_code IN ({PAY_CODES_SQL}) THEN jl.debit END), 0) AS FLOAT) AS total
        FROM journal_entries je
        JOIN journal_lines jl ON jl.entry_id = je.entry_id
        JOIN chart_of_accounts c ON c.account_id = jl.account_id
        WHERE je.reference_type = 'SALE'
          AND CAST(je.entry_date AS date) BETWEEN :d_from AND :d_to
          AND (:q = '' OR CAST(je.entry_id AS TEXT) LIKE :like OR je.description ILIKE :like
               OR EXISTS (SELECT 1 FROM pos_sale_items si WHERE si.entry_id = je.entry_id AND si.product_name ILIKE :like))
        GROUP BY je.entry_id, je.entry_date, je.description
        ORDER BY je.entry_id DESC LIMIT 100;
        """,
        {"d_from": d_from, "d_to": d_to, "q": q_clean, "like": f"%{q_clean}%"},
    )
    if sales.empty:
        st.info("No sales found for those filters.")
        return

    options = {
        f"POS-{int(r['entry_id']):06d} | {r['d']} | {CURRENCY} {r['total']:,.2f} | {r['description']}": int(r["entry_id"])
        for _, r in sales.iterrows()
    }
    label = st.selectbox("Sale", list(options.keys()))
    entry_id = options[label]

    try:
        data = load_sale(entry_id)
        if data["note"]:
            st.caption(data["note"])
        show_df(
            pd.DataFrame(data["sale"]["lines"]).rename(
                columns={"name": "Item", "qty": "Qty", "price": "Price", "total": "Total"}
            )
        )
        pdf = generate_pdf_receipt(
            data["sale"], data["customer"], data["cashier"],
            issued=data["issued"], reprint=True, note=data["note"],
        )
        st.download_button(
            "Download duplicate receipt (PDF)",
            data=pdf,
            file_name=f"Receipt_POS-{entry_id:06d}_copy.pdf",
            mime="application/pdf",
            type="primary",
        )
    except Exception as e:
        st.error(f"Could not prepare the receipt: {e}")
