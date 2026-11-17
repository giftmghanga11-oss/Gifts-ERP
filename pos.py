import streamlit as st
from datetime import datetime
from sqlalchemy import text
from fpdf import FPDF
from db import get_engine, run_query


PAYMENT_ACCOUNT_MAP = {
    "Cash": "1010",
    "M-Pesa": "1020",
    "Card": "1030",
}


def generate_receipt_pdf(receipt_data: dict) -> bytes:
    pdf = FPDF(format=(80, 180))  # 80mm thermal receipt width
    pdf.set_auto_page_break(auto=True, margin=6)
    pdf.add_page()
    pdf.set_margins(5, 5, 5)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "POS & ACCOUNTING RECEIPT", ln=True, align="C")
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 4, f"Receipt #: {receipt_data['receipt_number']}", ln=True, align="C")
    pdf.cell(0, 4, f"Date: {receipt_data['sale_date']}", ln=True, align="C")
    pdf.cell(0, 4, f"Cashier: {receipt_data['cashier_name']}", ln=True, align="C")
    pdf.cell(0, 4, f"Payment: {receipt_data['payment_method']}", ln=True, align="C")
    pdf.ln(2)
    pdf.cell(0, 2, "-" * 42, ln=True, align="C")

    # Line items
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(34, 5, "Item")
    pdf.cell(10, 5, "Qty", align="R")
    pdf.cell(26, 5, "Total", align="R", ln=True)
    pdf.set_font("Helvetica", "", 8)

    for item in receipt_data["items"]:
        name = item["name"][:18]
        pdf.cell(34, 5, name)
        pdf.cell(10, 5, str(item["qty"]), align="R")
        pdf.cell(26, 5, f"{item['line_total']:,.2f}", align="R", ln=True)

    pdf.cell(0, 2, "-" * 42, ln=True, align="C")
    pdf.cell(44, 5, "Subtotal (Excl. VAT):", align="R")
    pdf.cell(26, 5, f"KES {receipt_data['subtotal']:,.2f}", align="R", ln=True)

    pdf.cell(44, 5, "VAT / Tax:", align="R")
    pdf.cell(26, 5, f"KES {receipt_data['tax_total']:,.2f}", align="R", ln=True)

    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(44, 6, "GRAND TOTAL:", align="R")
    pdf.cell(26, 6, f"KES {receipt_data['grand_total']:,.2f}", align="R", ln=True)

    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "Thank you for your business!", ln=True, align="C")

    return bytes(pdf.output())


def process_sale_transaction(cart: list, payment_method: str, user: dict) -> dict:
    """
    Atomically processes sale, updates stock, and posts double-entry accounting lines.
    Prices are treated as VAT-inclusive at checkout for standard retail POS workflow.
    """
    engine = get_engine()
    receipt_number = f"RCP-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    sale_timestamp = datetime.now()

    subtotal_excl_tax = 0.0
    tax_total = 0.0
    grand_total = 0.0
    total_cogs = 0.0
    enriched_items = []

    with engine.begin() as conn:
        # 1. Lock and verify current stock & costs for each cart item
        for item in cart:
            row = conn.execute(
                text(
                    """
                    SELECT product_id, name, cost_price, selling_price, tax_rate, stock_qty
                    FROM products
                    WHERE product_id = :pid
                    FOR UPDATE
                    """
                ),
                {"pid": item["product_id"]},
            ).fetchone()

            if not row:
                raise ValueError(f"Product ID {item['product_id']} no longer exists.")
            if row.stock_qty < item["qty"]:
                raise ValueError(f"Insufficient stock for '{row.name}'. Available: {row.stock_qty}")

            qty = int(item["qty"])
            unit_sell = float(row.selling_price)
            unit_cost = float(row.cost_price)
            tax_rate = float(row.tax_rate)

            line_gross = round(unit_sell * qty, 2)
            # Extract VAT from tax-inclusive retail price
            line_net = round(line_gross / (1 + tax_rate / 100.0), 2)
            line_tax = round(line_gross - line_net, 2)
            line_cogs = round(unit_cost * qty, 2)

            subtotal_excl_tax = round(subtotal_excl_tax + line_net, 2)
            tax_total = round(tax_total + line_tax, 2)
            grand_total = round(grand_total + line_gross, 2)
            total_cogs = round(total_cogs + line_cogs, 2)

            enriched_items.append(
                {
                    "product_id": row.product_id,
                    "name": row.name,
                    "qty": qty,
                    "unit_price": unit_sell,
                    "unit_cost": unit_cost,
                    "line_total": line_gross,
                }
            )

        # 2. Insert Sales Header
        sale_res = conn.execute(
            text(
                """
                INSERT INTO sales (receipt_number, cashier_id, sale_date, subtotal, tax_total, grand_total, payment_method)
                VALUES (:rcp, :cid, :sdate, :sub, :tax, :grand, :pm)
                RETURNING sale_id
                """
            ),
            {
                "rcp": receipt_number,
                "cid": user["user_id"],
                "sdate": sale_timestamp,
                "sub": subtotal_excl_tax,
                "tax": tax_total,
                "grand": grand_total,
                "pm": payment_method,
            },
        )
        sale_id = sale_res.scalar()

        # 3. Insert Sale Line Items & Deduct Stock
        for ei in enriched_items:
            conn.execute(
                text(
                    """
                    INSERT INTO sale_items (sale_id, product_id, qty, unit_price, unit_cost, line_total)
                    VALUES (:sid, :pid, :qty, :uprice, :ucost, :ltotal)
                    """
                ),
                {
                    "sid": sale_id,
                    "pid": ei["product_id"],
                    "qty": ei["qty"],
                    "uprice": ei["unit_price"],
                    "ucost": ei["unit_cost"],
                    "ltotal": ei["line_total"],
                },
            )
            conn.execute(
                text("UPDATE products SET stock_qty = stock_qty - :qty WHERE product_id = :pid"),
                {"qty": ei["qty"], "pid": ei["product_id"]},
            )

        # 4. Create Double-Entry Journal Header
        je_res = conn.execute(
            text(
                """
                INSERT INTO journal_entries (entry_date, reference_type, reference_id, description, created_by)
                VALUES (:edate, 'POS_SALE', :sid, :desc, :uid)
                RETURNING entry_id
                """
            ),
            {
                "edate": sale_timestamp,
                "sid": sale_id,
                "desc": f"POS Sale {receipt_number} ({payment_method})",
                "uid": user["user_id"],
            },
        )
        entry_id = je_res.scalar()

        # 5. Post Balanced Journal Lines
        asset_code = PAYMENT_ACCOUNT_MAP[payment_method]

        # Debit Cash / M-Pesa / Bank (Grand Total)
        conn.execute(
            text(
                """
                INSERT INTO journal_lines (entry_id, account_id, debit, credit)
                VALUES (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = :code), :amt, 0)
                """
            ),
            {"eid": entry_id, "code": asset_code, "amt": grand_total},
        )

        # Credit Sales Revenue 4000 (Net Subtotal)
        conn.execute(
            text(
                """
                INSERT INTO journal_lines (entry_id, account_id, debit, credit)
                VALUES (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = '4000'), 0, :amt)
                """
            ),
            {"eid": entry_id, "amt": subtotal_excl_tax},
        )

        # Credit VAT Payable 2100 (Tax Total)
        if tax_total > 0:
            conn.execute(
                text(
                    """
                    INSERT INTO journal_lines (entry_id, account_id, debit, credit)
                    VALUES (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = '2100'), 0, :amt)
                    """
                ),
                {"eid": entry_id, "amt": tax_total},
            )

        # Debit COGS 5000 & Credit Inventory Asset 1200 (Total Cost)
        if total_cogs > 0:
            conn.execute(
                text(
                    """
                    INSERT INTO journal_lines (entry_id, account_id, debit, credit)
                    VALUES
                    (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = '5000'), :amt, 0),
                    (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = '1200'), 0, :amt)
                    """
                ),
                {"eid": entry_id, "amt": total_cogs},
            )

    return {
        "receipt_number": receipt_number,
        "sale_date": sale_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "cashier_name": user["full_name"],
        "payment_method": payment_method,
        "items": enriched_items,
        "subtotal": subtotal_excl_tax,
        "tax_total": tax_total,
        "grand_total": grand_total,
    }


def render_pos_page(user):
    st.header("Point of Sale (POS) Checkout")

    if "cart" not in st.session_state:
        st.session_state["cart"] = []
    if "last_receipt" not in st.session_state:
        st.session_state["last_receipt"] = None

    products_df = run_query(
        """
        SELECT product_id, sku_barcode, name, selling_price, tax_rate, stock_qty
        FROM products
        WHERE stock_qty > 0
        ORDER BY name;
        """
    )

    col_left, col_right = st.columns([1.2, 1.3])

    with col_left:
        st.subheader("Select Item")
        if products_df.empty:
            st.warning("No in-stock products available. Add stock in Inventory first.")
        else:
            product_map = {
                f"{r['name']} [{r['sku_barcode']}] — KES {r['selling_price']:,.2f} ({r['stock_qty']} left)": r
                for _, r in products_df.iterrows()
            }
            selected_key = st.selectbox("Search by Product Name or SKU", list(product_map.keys()))
            selected_prod = product_map[selected_key]

            qty_to_add = st.number_input(
                "Quantity",
                min_value=1,
                max_value=int(selected_prod["stock_qty"]),
                value=1,
                step=1,
            )

            if st.button("Add to Cart", use_container_width=True):
                pid = int(selected_prod["product_id"])
                existing = next((c for c in st.session_state["cart"] if c["product_id"] == pid), None)
                if existing:
                    new_qty = existing["qty"] + qty_to_add
                    if new_qty > int(selected_prod["stock_qty"]):
                        st.error("Cannot exceed available stock.")
                    else:
                        existing["qty"] = new_qty
                        st.rerun()
                else:
                    st.session_state["cart"].append(
                        {
                            "product_id": pid,
                            "sku": selected_prod["sku_barcode"],
                            "name": selected_prod["name"],
                            "unit_price": float(selected_prod["selling_price"]),
                            "tax_rate": float(selected_prod["tax_rate"]),
                            "qty": int(qty_to_add),
                        }
                    )
                    st.rerun()

    with col_right:
        st.subheader("Current Cart")
        if not st.session_state["cart"]:
            st.info("Cart is empty.")
        else:
            cart_rows = []
            total_due = 0.0
            for idx, item in enumerate(st.session_state["cart"]):
                line_tot = round(item["unit_price"] * item["qty"], 2)
                total_due += line_tot
                cart_rows.append(
                    {
                        "Product": item["name"],
                        "Qty": item["qty"],
                        "Unit Price": f"{item['unit_price']:,.2f}",
                        "Line Total": f"{line_tot:,.2f}",
                    }
                )

            st.dataframe(cart_rows, use_container_width=True, hide_index=True)
            st.metric("Total Payable (VAT Inclusive)", f"KES {total_due:,.2f}")

            payment_method = st.selectbox("Payment Method", ["Cash", "M-Pesa", "Card"])

            c_pay, c_clear = st.columns(2)
            with c_pay:
                if st.button("Complete Sale", type="primary", use_container_width=True):
                    try:
                        receipt = process_sale_transaction(st.session_state["cart"], payment_method, user)
                        st.session_state["last_receipt"] = receipt
                        st.session_state["cart"] = []
                        st.rerun()
                    except Exception as e:
                        st.error(f"Checkout failed: {e}")

            with c_clear:
                if st.button("Clear Cart", use_container_width=True):
                    st.session_state["cart"] = []
                    st.rerun()

    # Display receipt & PDF download after successful checkout
    if st.session_state["last_receipt"]:
        st.divider()
        rcp = st.session_state["last_receipt"]
        st.success(f"Sale Completed! Receipt #{rcp['receipt_number']} — Total: KES {rcp['grand_total']:,.2f}")
        pdf_bytes = generate_receipt_pdf(rcp)
        st.download_button(
            label="Download PDF Receipt",
            data=pdf_bytes,
            file_name=f"{rcp['receipt_number']}.pdf",
            mime="application/pdf",
        )