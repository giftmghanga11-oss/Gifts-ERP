from datetime import date, datetime
from io import BytesIO

import pandas as pd
import streamlit as st
from fpdf import FPDF

from db import run_query
from common import (
    ACCOUNTS,
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


# ------------------------------------------------------------------ stock
STOCK_W = [28, 66, 16, 26, 26, 28]  # 190 mm


def load_stock() -> pd.DataFrame:
    return run_query(
        """
        SELECT sku_barcode AS "SKU", name AS "Product", stock_qty AS "Qty",
               CAST(cost_price AS FLOAT) AS "Unit Cost",
               CAST(selling_price AS FLOAT) AS "Selling Price",
               CAST(cost_price * stock_qty AS FLOAT) AS "Stock Value"
        FROM products ORDER BY name;
        """
    )


def ledger_inventory_balance() -> float:
    inv_id = lookup_account_id("inventory")
    df = run_query(
        "SELECT CAST(COALESCE(SUM(debit - credit), 0) AS FLOAT) AS b FROM journal_lines WHERE account_id = :a;",
        {"a": inv_id},
    )
    return float(df.iloc[0]["b"])


class StockPDF(FPDF):
    def header(self):
        pdf_line(self, f"{COMPANY_NAME} - Stock Sheet", h=8, style="B", size=14)
        pdf_line(self, f"As at {datetime.now():%d %b %Y %H:%M}   |   Currency: {CURRENCY}", h=5)
        self.ln(2)
        pdf_row(
            self,
            [(STOCK_W[0], "SKU", "L"), (STOCK_W[1], "Product", "L"), (STOCK_W[2], "Qty", "R"),
             (STOCK_W[3], "Unit cost", "R"), (STOCK_W[4], "Selling", "R"), (STOCK_W[5], "Stock value", "R")],
            style="B", size=8,
        )


def stock_pdf(df: pd.DataFrame) -> bytes:
    pdf = StockPDF()
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    for _, r in df.iterrows():
        pdf_row(
            pdf,
            [(STOCK_W[0], str(r["SKU"])[:16], "L"), (STOCK_W[1], str(r["Product"])[:36], "L"),
             (STOCK_W[2], f"{int(r['Qty'])}", "R"), (STOCK_W[3], f"{r['Unit Cost']:,.2f}", "R"),
             (STOCK_W[4], f"{r['Selling Price']:,.2f}", "R"), (STOCK_W[5], f"{r['Stock Value']:,.2f}", "R")],
            h=5, size=8,
        )
    pdf_row(
        pdf,
        [(STOCK_W[0] + STOCK_W[1], f"TOTAL ({len(df)} items)", "R"), (STOCK_W[2], f"{int(df['Qty'].sum())}", "R"),
         (STOCK_W[3] + STOCK_W[4], "", "R"), (STOCK_W[5], f"{df['Stock Value'].sum():,.2f}", "R")],
        h=6, style="B", size=9,
    )
    return bytes(pdf.output())


def stock_excel(df: pd.DataFrame) -> bytes:
    """Sheet 1: valuation. Sheet 2: blank count sheet with variance formulas."""
    from openpyxl.styles import Font, PatternFill

    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="D9E2F3")
    input_fill = PatternFill("solid", fgColor="FFF2CC")
    n = len(df)
    stamp = f"As at {datetime.now():%d %b %Y %H:%M}"

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        # ---- sheet 1: valuation
        df.to_excel(xw, sheet_name="Stock Valuation", index=False, startrow=3)
        ws = xw.sheets["Stock Valuation"]
        ws["A1"] = f"{COMPANY_NAME} - Stock Valuation"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A2"] = f"{stamp}   Currency: {CURRENCY}"
        for c in ws[4]:
            c.font, c.fill = bold, head_fill
        for i in range(n):
            r = 5 + i
            ws.cell(row=r, column=6, value=f"=C{r}*D{r}")  # live formula
        last = 4 + n
        tr = last + 1
        ws.cell(row=tr, column=2, value="TOTAL").font = bold
        ws.cell(row=tr, column=3, value=f"=SUM(C5:C{last})").font = bold
        ws.cell(row=tr, column=6, value=f"=SUM(F5:F{last})").font = bold
        for row in ws.iter_rows(min_row=5, max_row=tr, min_col=4, max_col=6):
            for c in row:
                c.number_format = "#,##0.00"
        for col, w in zip("ABCDEF", [16, 40, 10, 14, 14, 16]):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A5"

        # ---- sheet 2: count sheet
        cnt = pd.DataFrame(
            {
                "SKU": df["SKU"], "Product": df["Product"], "System Qty": df["Qty"],
                "Physical Count": None, "Variance": None, "Unit Cost": df["Unit Cost"], "Variance Value": None,
            }
        )
        cnt.to_excel(xw, sheet_name="Count Sheet", index=False, startrow=4)
        wc = xw.sheets["Count Sheet"]
        wc["A1"] = f"{COMPANY_NAME} - Stock Count Sheet"
        wc["A1"].font = Font(bold=True, size=14)
        wc["A2"] = f"System quantities {stamp}"
        wc["A3"] = "Counted by: ____________________    Date: ______________    Checked by: ____________________"
        for c in wc[5]:
            c.font, c.fill = bold, head_fill
        for i in range(n):
            r = 6 + i
            wc.cell(row=r, column=4).fill = input_fill
            wc.cell(row=r, column=5, value=f'=IF(D{r}="","",D{r}-C{r})')
            wc.cell(row=r, column=7, value=f'=IF(D{r}="","",E{r}*F{r})')
            wc.cell(row=r, column=6).number_format = "#,##0.00"
            wc.cell(row=r, column=7).number_format = "#,##0.00"
        lr = 5 + n
        wc.cell(row=lr + 1, column=2, value="TOTAL VARIANCE VALUE").font = bold
        wc.cell(row=lr + 1, column=7, value=f"=SUM(G6:G{lr})").font = bold
        wc.cell(row=lr + 1, column=7).number_format = "#,##0.00"
        for col, w in zip("ABCDEFG", [16, 40, 12, 16, 12, 14, 16]):
            wc.column_dimensions[col].width = w
        wc.freeze_panes = "A6"
    return buf.getvalue()


# ------------------------------------------------------------------ sales
PAY_LABELS = {
    ACCOUNTS["cash"][0]: "Cash",
    ACCOUNTS["mpesa"][0]: "M-Pesa",
    ACCOUNTS["bank"][0]: "Bank / Card",
    ACCOUNTS["ar"][0]: "On Account (Credit)",
}
SALES_METHODS = ["Cash", "M-Pesa", "Bank / Card", "On Account (Credit)", "Other"]
PAY_CODES_SQL = ",".join(f"'{c}'" for c in PAY_LABELS)  # constants from common.py, not user input
SALES_COLS = ["Date", "Receipt", "Type", "Customer", "Payment", "Net", "VAT", "Total", "Cashier"]
SW = [22, 28, 55, 34, 30, 28, 32, 48]  # landscape A4, 277 mm


def load_sales_report(d_from: date, d_to: date) -> pd.DataFrame:
    """Sales (positive) and returns (negative), with how each was paid."""
    df = run_query(
        f"""
        SELECT je.entry_id, CAST(je.entry_date AS date) AS txn_date, je.reference_type,
               COALESCE(cu.customer_name, '') AS customer_name,
               COALESCE(u.full_name, '') AS cashier,
               MAX(CASE WHEN c.account_code IN ({PAY_CODES_SQL}) THEN c.account_code END) AS pay_code,
               CAST(COALESCE(SUM(CASE WHEN c.account_code IN ({PAY_CODES_SQL})
                                      THEN jl.debit - jl.credit END), 0) AS FLOAT) AS gross,
               CAST(COALESCE(SUM(CASE WHEN c.account_code = :vat
                                      THEN jl.credit - jl.debit END), 0) AS FLOAT) AS vat
        FROM journal_entries je
        JOIN journal_lines jl ON jl.entry_id = je.entry_id
        JOIN chart_of_accounts c ON c.account_id = jl.account_id
        LEFT JOIN journal_entries orig ON orig.entry_id = je.reference_id AND je.reference_type = 'SALE_RETURN'
        LEFT JOIN customers cu ON cu.customer_id = COALESCE(je.customer_id, orig.customer_id)
        LEFT JOIN users u ON u.user_id = je.created_by
        WHERE je.reference_type IN ('SALE', 'SALE_RETURN')
          AND CAST(je.entry_date AS date) BETWEEN :d_from AND :d_to
        GROUP BY je.entry_id, je.entry_date, je.reference_type, cu.customer_name, u.full_name
        ORDER BY je.entry_id;
        """,
        {"d_from": d_from, "d_to": d_to, "vat": ACCOUNTS["vat"][0]},
    )
    if df.empty:
        return pd.DataFrame(columns=SALES_COLS)
    is_sale = df["reference_type"] == "SALE"
    out = pd.DataFrame(
        {
            "Date": df["txn_date"].astype(str),
            "Receipt": [
                ("POS-" if s else "CN-") + f"{int(i):06d}" for s, i in zip(is_sale, df["entry_id"])
            ],
            "Type": is_sale.map({True: "Sale", False: "Return"}),
            "Customer": df["customer_name"].replace("", "Walk-in"),
            "Payment": df["pay_code"].map(PAY_LABELS).fillna("Other"),
            "Net": (df["gross"] - df["vat"]).round(2),
            "VAT": df["vat"].round(2),
            "Total": df["gross"].round(2),
            "Cashier": df["cashier"],
        }
    )
    return out.reset_index(drop=True)


def payment_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m in SALES_METHODS:
        sold = df[(df["Payment"] == m) & (df["Type"] == "Sale")]
        ret = df[(df["Payment"] == m) & (df["Type"] == "Return")]
        s, rt = float(sold["Total"].sum()), float(ret["Total"].sum())
        if m == "Other" and not len(sold) and not len(ret):
            continue
        rows.append({"Payment": m, "Transactions": len(sold), "Sales": s, "Returns": rt, "Net": round(s + rt, 2)})
    out = pd.DataFrame(rows)
    total = float(out["Net"].sum())
    out["Share %"] = (out["Net"] / total * 100).round(1) if total else 0.0
    return out


class SalesPDF(FPDF):
    def header(self):
        pdf_line(self, f"{COMPANY_NAME} - Sales Report", h=8, style="B", size=14)
        pdf_line(self, getattr(self, "subtitle", ""), h=5)
        self.ln(2)
        pdf_row(
            self,
            [(SW[0], "Date", "L"), (SW[1], "Receipt", "L"), (SW[2], "Customer", "L"), (SW[3], "Payment", "L"),
             (SW[4], "Net", "R"), (SW[5], "VAT", "R"), (SW[6], "Total", "R"), (SW[7], "Cashier", "L")],
            style="B", size=8,
        )


def sales_pdf(df: pd.DataFrame, summ: pd.DataFrame, d_from: date, d_to: date) -> bytes:
    pdf = SalesPDF(orientation="L")
    pdf.subtitle = f"{d_from:%d %b %Y} to {d_to:%d %b %Y}   |   Currency: {CURRENCY}   |   Printed {datetime.now():%d %b %Y %H:%M}"
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    for _, r in df.iterrows():
        label = r["Receipt"] + ("" if r["Type"] == "Sale" else " (R)")
        pdf_row(
            pdf,
            [(SW[0], r["Date"], "L"), (SW[1], label, "L"), (SW[2], str(r["Customer"])[:30], "L"),
             (SW[3], r["Payment"], "L"), (SW[4], f"{r['Net']:,.2f}", "R"), (SW[5], f"{r['VAT']:,.2f}", "R"),
             (SW[6], f"{r['Total']:,.2f}", "R"), (SW[7], str(r["Cashier"])[:26], "L")],
            h=5, size=8,
        )
    pdf_row(
        pdf,
        [(sum(SW[:4]), "TOTAL (returns shown negative)", "R"), (SW[4], f"{df['Net'].sum():,.2f}", "R"),
         (SW[5], f"{df['VAT'].sum():,.2f}", "R"), (SW[6], f"{df['Total'].sum():,.2f}", "R"), (SW[7], "", "L")],
        h=6, style="B", size=9,
    )
    pdf.ln(6)
    pdf_line(pdf, "Summary by payment method", h=6, style="B", size=10)
    sw = [60, 30, 40, 40, 40, 30]
    pdf_row(pdf, [(sw[0], "Payment", "L"), (sw[1], "Transactions", "R"), (sw[2], "Sales", "R"),
                  (sw[3], "Returns", "R"), (sw[4], "Net", "R"), (sw[5], "Share %", "R")], style="B", size=8)
    for _, r in summ.iterrows():
        pdf_row(pdf, [(sw[0], r["Payment"], "L"), (sw[1], str(int(r["Transactions"])), "R"),
                      (sw[2], f"{r['Sales']:,.2f}", "R"), (sw[3], f"{r['Returns']:,.2f}", "R"),
                      (sw[4], f"{r['Net']:,.2f}", "R"), (sw[5], f"{r['Share %']:.1f}", "R")], h=5, size=8)
    return bytes(pdf.output())


def sales_excel(df: pd.DataFrame, summ: pd.DataFrame, d_from: date, d_to: date) -> bytes:
    from openpyxl.styles import Font, PatternFill

    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="D9E2F3")
    n = len(df)
    last = 4 + n
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Sales Report", index=False, startrow=3)
        ws = xw.sheets["Sales Report"]
        ws["A1"] = f"{COMPANY_NAME} - Sales Report"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A2"] = f"{d_from} to {d_to}   Currency: {CURRENCY}   (returns shown as negatives)"
        for c in ws[4]:
            c.font, c.fill = bold, fill
        if n:
            tr = last + 1
            ws.cell(row=tr, column=5, value="TOTAL").font = bold
            for col in (6, 7, 8):
                letter = "ABCDEFGHI"[col - 1]
                ws.cell(row=tr, column=col, value=f"=SUM({letter}5:{letter}{last})").font = bold
            for row in ws.iter_rows(min_row=5, max_row=tr, min_col=6, max_col=8):
                for c in row:
                    c.number_format = "#,##0.00"
        for col, w in zip("ABCDEFGHI", [12, 14, 9, 30, 20, 14, 12, 14, 22]):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A5"

        # summary sheet driven by formulas over the sales sheet
        pd.DataFrame({"Payment": summ["Payment"]}).to_excel(xw, sheet_name="By Payment Method", index=False, startrow=3)
        wm = xw.sheets["By Payment Method"]
        wm["A1"] = f"{COMPANY_NAME} - Sales by Payment Method"
        wm["A1"].font = Font(bold=True, size=14)
        wm["A2"] = f"{d_from} to {d_to}"
        for j, h in enumerate(["Transactions", "Sales", "Returns", "Net"], start=2):
            wm.cell(row=4, column=j, value=h)
        for c in wm[4]:
            c.font, c.fill = bold, fill
        rng = lambda col: f"'Sales Report'!${col}$5:${col}${max(last, 5)}"
        for i in range(len(summ)):
            r = 5 + i
            wm.cell(row=r, column=2, value=f'=COUNTIFS({rng("E")},$A{r},{rng("C")},"Sale")')
            wm.cell(row=r, column=3, value=f'=SUMIFS({rng("H")},{rng("E")},$A{r},{rng("C")},"Sale")')
            wm.cell(row=r, column=4, value=f'=SUMIFS({rng("H")},{rng("E")},$A{r},{rng("C")},"Return")')
            wm.cell(row=r, column=5, value=f"=C{r}+D{r}")
            for col in (3, 4, 5):
                wm.cell(row=r, column=col).number_format = "#,##0.00"
        tr = 5 + len(summ)
        wm.cell(row=tr, column=1, value="TOTAL").font = bold
        for col in (2, 3, 4, 5):
            letter = "ABCDE"[col - 1]
            c = wm.cell(row=tr, column=col, value=f"=SUM({letter}5:{letter}{tr - 1})")
            c.font = bold
            if col > 2:
                c.number_format = "#,##0.00"
        for col, w in zip("ABCDE", [24, 14, 16, 16, 16]):
            wm.column_dimensions[col].width = w
    return buf.getvalue()


# -------------------------------------------------------------------- page
def render_reports_page(user):
    st.header("Reports")
    tab1, tab2, tab3, tab4 = st.tabs(["Customer Statement", "Customer Balances", "Stock Sheet", "Sales Report"])

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

    with tab3:
        stock = load_stock()
        if stock.empty:
            st.info("No products yet. Add stock under Inventory & Products.")
        else:
            c1, c2, c3 = st.columns([2.5, 1.5, 1.2])
            q = c1.text_input("Search SKU or product", key="stock_q")
            status = c2.selectbox("Show", ["All items", "In stock", "Low stock", "Out of stock"], key="stock_status")
            low = c3.number_input("Low stock at or below", min_value=1, value=5, step=1, key="stock_low")

            view = stock
            if q:
                m = stock["Product"].str.contains(q, case=False, regex=False) | stock["SKU"].astype(str).str.contains(q, case=False, regex=False)
                view = view[m]
            if status == "In stock":
                view = view[view["Qty"] > 0]
            elif status == "Low stock":
                view = view[(view["Qty"] > 0) & (view["Qty"] <= low)]
            elif status == "Out of stock":
                view = view[view["Qty"] <= 0]

            sys_value = float(stock["Stock Value"].sum())
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Items listed", len(view))
            m2.metric("Units in stock", f"{int(view['Qty'].sum()):,}")
            m3.metric("Stock value (at cost)", f"{CURRENCY} {view['Stock Value'].sum():,.2f}")
            try:
                led = ledger_inventory_balance()
                m4.metric("Ledger inventory (1200)", f"{CURRENCY} {led:,.2f}", delta=f"{sys_value - led:,.2f} vs stock sheet", delta_color="off")
            except Exception:
                m4.metric("Ledger inventory (1200)", "n/a")

            show_df(view.style.format({"Unit Cost": "{:,.2f}", "Selling Price": "{:,.2f}", "Stock Value": "{:,.2f}"}))

            b1, b2, _ = st.columns([1, 1.6, 2.4])
            try:
                b1.download_button(
                    "Download PDF",
                    data=stock_pdf(view),
                    file_name=f"Stock_Sheet_{date.today()}.pdf",
                    mime="application/pdf",
                    type="primary",
                )
            except Exception as e:
                b1.error(f"PDF failed: {e}")
            b2.download_button(
                "Download Excel (valuation + count sheet)",
                data=stock_excel(view),
                file_name=f"Stock_Sheet_{date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            st.caption(
                "The Excel file has a Count Sheet with a Physical Count column and variance formulas for stock-taking."
            )

    with tab4:
        today = date.today()
        f1, f2, f3 = st.columns([1.2, 1.2, 3])
        s_from = f1.date_input("From", value=today.replace(day=1), key="sales_from")
        s_to = f2.date_input("To", value=today, key="sales_to")
        methods = f3.multiselect("Payment method", SALES_METHODS, default=SALES_METHODS, key="sales_methods")
        g1, g2 = st.columns([3, 1.5])
        sq = g1.text_input("Search customer, receipt or cashier", key="sales_q")
        with_returns = g2.checkbox("Include returns (as negatives)", value=True, key="sales_returns")

        if s_from > s_to:
            st.error("'From' date must be on or before 'To' date.")
        else:
            sales = load_sales_report(s_from, s_to)
            if len(sales):
                sales = sales[sales["Payment"].isin(methods)]
                if not with_returns:
                    sales = sales[sales["Type"] == "Sale"]
                if sq:
                    m = (
                        sales["Customer"].str.contains(sq, case=False, regex=False)
                        | sales["Receipt"].str.contains(sq, case=False, regex=False)
                        | sales["Cashier"].str.contains(sq, case=False, regex=False)
                    )
                    sales = sales[m]

            if sales.empty:
                st.info("No sales found for those filters.")
            else:
                summ = payment_summary(sales)
                k = st.columns(4)
                k[0].metric("Sales", f"{CURRENCY} {sales[sales['Type'] == 'Sale']['Total'].sum():,.2f}")
                k[1].metric("Returns", f"{CURRENCY} {sales[sales['Type'] == 'Return']['Total'].sum():,.2f}")
                k[2].metric("Net total (incl. VAT)", f"{CURRENCY} {sales['Total'].sum():,.2f}")
                k[3].metric("VAT", f"{CURRENCY} {sales['VAT'].sum():,.2f}")

                st.caption("By payment method")
                show_df(summ.style.format({"Sales": "{:,.2f}", "Returns": "{:,.2f}", "Net": "{:,.2f}", "Share %": "{:.1f}"}))
                st.caption("Transactions")
                show_df(sales.style.format({"Net": "{:,.2f}", "VAT": "{:,.2f}", "Total": "{:,.2f}"}))

                d1, d2, _ = st.columns([1, 1, 3])
                try:
                    d1.download_button(
                        "Download PDF",
                        data=sales_pdf(sales, summ, s_from, s_to),
                        file_name=f"Sales_Report_{s_from}_{s_to}.pdf",
                        mime="application/pdf",
                        type="primary",
                    )
                except Exception as e:
                    d1.error(f"PDF failed: {e}")
                d2.download_button(
                    "Download Excel",
                    data=sales_excel(sales, summ, s_from, s_to),
                    file_name=f"Sales_Report_{s_from}_{s_to}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
