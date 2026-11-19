import streamlit as st
from datetime import datetime
from fpdf import FPDF
from sqlalchemy import text
from db import get_engine, run_query
from common import (
    COMPANY_NAME,
    CURRENCY,
    ON_ACCOUNT,
    PAYMENT_METHODS,
    PRICES_INCLUDE_VAT,
    account_id,
    pdf_line,
    pdf_row,
    post_entry,
)


def price_line(price, qty, tax_rate):
    """Return (net, vat, total) for one cart line."""
    rate = float(tax_rate or 0) / 100
    base = round(float(price) * int(qty), 2)
    if PRICES_INCLUDE_VAT:
        net = round(base / (1 + rate), 2)
        return net, round(base - net, 2), base
    vat = round(base * rate, 2)
    return base, vat, round(base + vat, 2)


def cart_totals(cart):
    net = vat = total = 0.0
    for it in cart:
        n, v, t = price_line(it["price"], it["qty"], it["tax_rate"])
        net, vat, total = net + n, vat + v, total + t
    return round(net, 2), round(vat, 2), round(total, 2)


def process_sale(cart, customer, payment, user_id):
    """Stock, revenue, VAT and COGS all commit together or not at all."""
    with get_engine().begin() as conn:
        lines_out, net_total, vat_total, gross_total, cogs_total = [], 0.0, 0.0, 0.0, 0.0

        for it in cart:
            # Atomic stock check + decrement: no overselling, no race between cashiers
            row = conn.execute(
                text(
                    """
                    UPDATE products SET stock_qty = stock_qty - :q
                    WHERE product_id = :p AND stock_qty >= :q
                    RETURNING cost_price
                    """
                ),
                {"q": it["qty"], "p": it["product_id"]},
            ).fetchone()
            if row is None:
                raise ValueError(f"Not enough stock for {it['name']}.")
            cogs_total += float(row[0] or 0) * it["qty"]
            net, vat, total = price_line(it["price"], it["qty"], it["tax_rate"])
            net_total, vat_total, gross_total = net_total + net, vat_total + vat, gross_total + total
            lines_out.append({**it, "total": total})

        net_total, vat_total, gross_total = round(net_total, 2), round(vat_total, 2), round(gross_total, 2)
        cogs_total = round(cogs_total, 2)

        sales_acc = account_id(conn, "sales")
        vat_acc = account_id(conn, "vat", required=False)
        on_credit = payment == ON_ACCOUNT
        debit_acc = account_id(conn, "ar") if on_credit else account_id(conn, PAYMENT_METHODS[payment])

        lines = [(debit_acc, gross_total, 0)]
        if vat_acc and vat_total > 0:
            lines += [(sales_acc, 0, net_total), (vat_acc, 0, vat_total)]
        else:
            lines += [(sales_acc, 0, gross_total)]
        if cogs_total > 0:
            lines += [(account_id(conn, "cogs"), cogs_total, 0), (account_id(conn, "inventory"), 0, cogs_total)]

        if customer is not None:
            who = f"{customer['customer_name']} ({customer['account_code']})"
        else:
            who = "Walk-in"
        desc = f"POS credit sale to {who}" if on_credit else f"POS sale to {who} ({payment})"

        entry_id = post_entry(
            conn,
            "SALE",
            desc,
            user_id,
            lines,
            customer_id=int(customer["customer_id"]) if customer is not None else None,
        )

    return {
        "entry_id": entry_id,
        "lines": lines_out,
        "net": net_total,
        "vat": vat_total,
        "total": gross_total,
        "payment": payment,
    }


def generate_pdf_receipt(sale, customer=None, cashier=""):
    pdf = FPDF(format="A5")
    pdf.add_page()
    pdf.set_margins(10, 10, 10)
    W = pdf.w - 20

    pdf_line(pdf, f"{COMPANY_NAME} - Official Receipt", h=8, align="C", style="B", size=14)
    pdf_line(pdf, f"Receipt No: POS-{sale['entry_id']:06d}", align="C")
    pdf_line(pdf, datetime.now().strftime("%Y-%m-%d %H:%M"), align="C")
    if cashier:
        pdf_line(pdf, f"Served by: {cashier}", align="C")
    pdf.ln(3)

    if customer is not None:
        pdf_line(pdf, "Customer", style="B", size=10)
        pdf_line(pdf, f"{customer['customer_name']}  ({customer['account_code']})")
        pdf_line(pdf, f"Phone: {customer.get('phone_no') or 'N/A'}    KRA PIN: {customer.get('kra_pin') or 'N/A'}")
    else:
        pdf_line(pdf, "Customer: Walk-in", style="I")
    pdf.ln(3)

    w = [W - 15 - 25 - 28, 15, 25, 28]
    pdf_row(pdf, [(w[0], "Item", "L"), (w[1], "Qty", "C"), (w[2], "Price", "R"), (w[3], "Total", "R")], style="B")
    for it in sale["lines"]:
        pdf_row(
            pdf,
            [
                (w[0], str(it["name"])[:34], "L"),
                (w[1], str(it["qty"]), "C"),
                (w[2], f"{it['price']:,.2f}", "R"),
                (w[3], f"{it['total']:,.2f}", "R"),
            ],
            h=5,
        )
    pdf.ln(2)
    label_w = W - w[3]
    pdf_row(pdf, [(label_w, "Subtotal (excl. VAT)", "R"), (w[3], f"{sale['net']:,.2f}", "R")], h=5)
    pdf_row(pdf, [(label_w, "VAT", "R"), (w[3], f"{sale['vat']:,.2f}", "R")], h=5)
    pdf_row(pdf, [(label_w, f"TOTAL ({CURRENCY})", "R"), (w[3], f"{sale['total']:,.2f}", "R")], h=7, style="B", size=10)
    pdf.ln(2)
    pdf_line(pdf, f"Payment: {sale['payment']}")
    pdf.ln(4)
    pdf_line(pdf, "Thank you for your business!", align="C", style="I", size=8)
    return bytes(pdf.output())


def render_pos_page(user):
    st.header("Point of Sale")
    st.caption(f"Cashier: {user['full_name']} ({user['role'].capitalize()})")

    cart = st.session_state.setdefault("cart", [])

    flash = st.session_state.pop("pos_flash", None)
    if flash:
        st.warning(flash)

    last = st.session_state.get("last_receipt")
    if last:
        st.success(last["msg"])
        a, b = st.columns([1, 4])
        if last["pdf"]:
            a.download_button(
                "Download receipt (PDF)",
                data=last["pdf"],
                file_name=f"Receipt_POS-{last['ref']:06d}.pdf",
                mime="application/pdf",
            )
        if b.button("Start new sale"):
            st.session_state.pop("last_receipt")
            st.rerun()

    inventory_df = run_query(
        """
        SELECT product_id, sku_barcode, name, selling_price, tax_rate, stock_qty
        FROM products WHERE stock_qty > 0 ORDER BY name;
        """
    )
    customers_df = run_query(
        "SELECT customer_id, account_code, customer_name, phone_no, kra_pin FROM customers ORDER BY customer_name;"
    )

    col1, col2 = st.columns([2, 1])

    # ------------------------------------------------------------ products
    with col1:
        st.subheader("Products")
        search = st.text_input("Search name or scan barcode", key="pos_search")
        shown = inventory_df
        if search:
            m = inventory_df["name"].str.contains(search, case=False, regex=False) | inventory_df[
                "sku_barcode"
            ].astype(str).str.contains(search, case=False, regex=False)
            shown = inventory_df[m]
        if inventory_df.empty:
            st.warning("No products in stock. Add stock under Inventory & Products.")
        elif shown.empty:
            st.info("No product matches your search.")
        for _, row in shown.head(40).iterrows():
            pid = int(row["product_id"])
            stock = int(row["stock_qty"])
            c = st.columns([4, 1.3, 1])
            c[0].write(f"**{row['name']}**  \nStock {stock} | {CURRENCY} {float(row['selling_price']):,.2f}")
            qty = c[1].number_input("Qty", min_value=1, max_value=stock, value=1, key=f"qty_{pid}", label_visibility="collapsed")
            if c[2].button("Add", key=f"add_{pid}"):
                existing = next((i for i in cart if i["product_id"] == pid), None)
                new_qty = (existing["qty"] if existing else 0) + int(qty)
                if new_qty > stock:
                    st.session_state["pos_flash"] = f"Only {stock} of {row['name']} in stock."
                elif existing:
                    existing["qty"] = new_qty
                else:
                    cart.append(
                        {
                            "product_id": pid,
                            "name": row["name"],
                            "price": float(row["selling_price"]),
                            "tax_rate": float(row["tax_rate"] or 0),
                            "qty": int(qty),
                        }
                    )
                st.rerun()

    # ---------------------------------------------------------------- cart
    with col2:
        st.subheader("Current sale")
        if not cart:
            st.info("Cart is empty.")
            return

        for idx, it in enumerate(cart):
            _, _, line_total = price_line(it["price"], it["qty"], it["tax_rate"])
            r = st.columns([5, 1])
            r[0].write(f"{it['name']} x {it['qty']} = {CURRENCY} {line_total:,.2f}")
            if r[1].button("x", key=f"rm_{idx}", help="Remove line"):
                cart.pop(idx)
                st.rerun()

        net, vat, total = cart_totals(cart)
        st.divider()
        st.write(f"Subtotal: {CURRENCY} {net:,.2f}")
        st.write(f"VAT: {CURRENCY} {vat:,.2f}")
        st.markdown(f"### Total: {CURRENCY} {total:,.2f}")

        cust_map = {
            f"{r['customer_name']} ({r['account_code']})": r.to_dict() for _, r in customers_df.iterrows()
        }
        cust_label = st.selectbox("Customer", ["Walk-in customer"] + list(cust_map.keys()))
        customer = cust_map.get(cust_label)

        methods = list(PAYMENT_METHODS.keys()) + ([ON_ACCOUNT] if customer else [])
        payment = st.selectbox("Payment", methods)

        if st.button("Complete sale", type="primary"):
            try:
                sale = process_sale(cart, customer, payment, user["user_id"])
            except Exception as e:
                st.error(f"Sale not recorded: {e}")
                return
            # The sale is committed; a PDF problem must not look like a failed sale.
            try:
                pdf_bytes = generate_pdf_receipt(sale, customer, user["full_name"])
            except Exception as e:
                pdf_bytes = None
                st.warning(f"Sale saved, but the receipt PDF failed: {e}")
            st.session_state["last_receipt"] = {
                "pdf": pdf_bytes,
                "ref": sale["entry_id"],
                "msg": f"Sale POS-{sale['entry_id']:06d} recorded: {CURRENCY} {sale['total']:,.2f} ({sale['payment']}).",
            }
            st.session_state["cart"] = []
            st.rerun()

        if st.button("Clear cart"):
            st.session_state["cart"] = []
            st.rerun()
