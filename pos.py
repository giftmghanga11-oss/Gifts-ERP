import streamlit as st
import pandas as pd
from datetime import datetime
from fpdf import FPDF
from db import run_query, execute_write


def generate_pdf_receipt(receipt_data, customer_info=None):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=10)

    # Store Header / Title
    pdf.set_font("Arial", "B", 14)
    pdf.cell(200, 8, txt="MyOdoo POS - Official Receipt", ln=True, align="C")
    pdf.set_font("Arial", size=9)
    pdf.cell(200, 5, txt=f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ln=True, align="C")
    pdf.ln(5)

    # Customer Details Section (Account Customer vs Walk-in)
    if customer_info:
        pdf.set_font("Arial", "B", 10)
        pdf.cell(200, 5, txt="Customer Details:", ln=True, align="L")
        pdf.set_font("Arial", size=9)
        pdf.cell(200, 5, txt=f"Account Name: {customer_info['customer_name']}", ln=True, align="L")
        pdf.cell(200, 5, txt=f"Account Code: {customer_info['account_code']}", ln=True, align="L")
        pdf.cell(200, 5, txt=f"Phone No: {customer_info.get('phone_no') or 'N/A'}", ln=True, align="L")
        pdf.cell(200, 5, txt=f"KRA PIN: {customer_info.get('kra_pin') or 'N/A'}", ln=True, align="L")
        pdf.ln(5)
    else:
        pdf.set_font("Arial", "I", 9)
        pdf.cell(200, 5, txt="Customer: Walk-in Customer (Cash Sale)", ln=True, align="L")
        pdf.ln(5)

    # Table Header
    pdf.set_font("Arial", "B", 9)
    pdf.cell(90, 6, "Item Description", 1)
    pdf.cell(30, 6, "Qty", 1, align="C")
    pdf.cell(35, 6, "Price", 1, align="R")
    pdf.cell(35, 6, "Total", 1, ln=True, align="R")

    # Items
    pdf.set_font("Arial", size=9)
    subtotal = 0
    for item in receipt_data["cart"]:
        item_total = item["price"] * item["qty"]
        subtotal += item_total
        pdf.cell(90, 5, str(item["name"]), 1)
        pdf.cell(30, 5, str(item["qty"]), 1, align="C")
        pdf.cell(35, 5, f"{item['price']:,.2f}", 1, align="R")
        pdf.cell(35, 5, f"{item_total:,.2f}", 1, ln=True, align="R")

    # Total
    pdf.set_font("Arial", "B", 10)
    pdf.cell(155, 7, "Grand Total:", 1, align="R")
    pdf.cell(35, 7, f"{subtotal:,.2f}", 1, ln=True, align="R")

    pdf.ln(10)
    pdf.set_font("Arial", "I", 8)
    pdf.cell(200, 5, txt="Thank you for your business!", ln=True, align="C")

    return bytes(pdf.output())


def render_pos_page(user=None):
    st.header("Point of Sale (POS) Terminal")
    if user:
        st.caption(f"Cashier: {user['full_name']} ({user['role'].capitalize()})")

    if "cart" not in st.session_state:
        st.session_state["cart"] = []

    # Fetch inventory & customers
    inventory_df = run_query(
        "SELECT product_id, name, selling_price, stock_quantity FROM inventory WHERE stock_quantity > 0;"
    )
    customers_df = run_query(
        "SELECT customer_id, account_code, customer_name, phone_no, kra_pin FROM customers ORDER BY customer_name;"
    )

    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("Select Products")
        if inventory_df.empty:
            st.warning("No products available in inventory.")
        else:
            for _, row in inventory_df.iterrows():
                cols = st.columns([3, 1, 1])
                cols[0].write(
                    f"**{row['name']}** (Stock: {row['stock_quantity']}) - KES {row['selling_price']:,.2f}"
                )
                qty = cols[1].number_input(
                    "Qty",
                    min_value=1,
                    max_value=int(row["stock_quantity"]),
                    value=1,
                    key=f"qty_{row['product_id']}_pos",
                )
                if cols[2].button("Add", key=f"add_{row['product_id']}_pos"):
                    cart_item = {
                        "product_id": int(row["product_id"]),
                        "name": row["name"],
                        "price": float(row["selling_price"]),
                        "qty": int(qty),
                    }
                    st.session_state["cart"].append(cart_item)
                    st.rerun()

    with col2:
        st.subheader("Current Sale / Cart")
        if not st.session_state["cart"]:
            st.info("Cart is empty.")
        else:
            cart_total = 0
            for item in st.session_state["cart"]:
                item_sum = item["price"] * item["qty"]
                cart_total += item_sum
                st.write(f"{item['name']} x {item['qty']} = KES {item_sum:,.2f}")

            st.divider()
            st.markdown(f"### Total: KES {cart_total:,.2f}")

            # Customer Selection for Checkout
            cust_options = ["Walk-in Customer (Cash Sale)"]
            cust_map = {}
            if not customers_df.empty:
                for _, crow in customers_df.iterrows():
                    label = f"{crow['customer_name']} ({crow['account_code']})"
                    cust_options.append(label)
                    cust_map[label] = crow.to_dict()

            selected_cust_label = st.selectbox("Customer Account", cust_options)
            is_walkin = "Walk-in" in selected_cust_label
            customer_data = None if is_walkin else cust_map[selected_cust_label]

            if st.button("Complete Sale & Print Receipt", type="primary"):
                try:
                    sales_acc = run_query(
                        "SELECT account_id FROM chart_of_accounts WHERE account_name LIKE '%Sales%' OR account_name LIKE '%Revenue%' LIMIT 1;"
                    )
                    cash_acc = run_query(
                        "SELECT account_id FROM chart_of_accounts WHERE account_name LIKE '%Cash%' OR account_name LIKE '%M-Pesa%' OR account_name LIKE '%Bank%' LIMIT 1;"
                    )
                    ar_acc = run_query(
                        "SELECT account_id FROM chart_of_accounts WHERE account_name LIKE '%Receivable%' LIMIT 1;"
                    )

                    if sales_acc.empty or (cash_acc.empty and is_walkin) or (ar_acc.empty and not is_walkin):
                        st.error(
                            "Accounting error: Required accounts (Sales, Cash, or Accounts Receivable) not found in Chart of Accounts."
                        )
                        return

                    sales_acc_id = int(sales_acc.iloc[0]["account_id"])

                    # Create Journal Entry header
                    desc = (
                        "POS Sale to Walk-in"
                        if is_walkin
                        else f"POS Credit Sale to {customer_data['customer_name']} ({customer_data['account_code']})"
                    )
                    res = run_query(
                        "INSERT INTO journal_entries (description) VALUES (:desc) RETURNING journal_entry_id;",
                        {"desc": desc},
                    )
                    je_id = int(res.iloc[0]["journal_entry_id"])

                    # Post Double Entries
                    if is_walkin:
                        target_asset_id = int(cash_acc.iloc[0]["account_id"])
                        execute_write(
                            "INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit) VALUES (:je, :acc, :amt, 0)",
                            {"je": je_id, "acc": target_asset_id, "amt": cart_total},
                        )
                    else:
                        target_asset_id = int(ar_acc.iloc[0]["account_id"])
                        execute_write(
                            "INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit) VALUES (:je, :acc, :amt, 0)",
                            {"je": je_id, "acc": target_asset_id, "amt": cart_total},
                        )

                    # Credit Sales Revenue
                    execute_write(
                        "INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit) VALUES (:je, :acc, 0, :amt)",
                        {"je": je_id, "acc": sales_acc_id, "amt": cart_total},
                    )

                    # Deduct stock
                    for item in st.session_state["cart"]:
                        execute_write(
                            "UPDATE inventory SET stock_quantity = stock_quantity - :qty WHERE product_id = :pid;",
                            {"qty": item["qty"], "pid": item["product_id"]},
                        )

                    # Generate PDF Receipt
                    pdf_bytes = generate_pdf_receipt(
                        {"cart": st.session_state["cart"]}, customer_info=customer_data
                    )

                    st.success("Sale completed successfully and recorded to ledger!")
                    st.download_button(
                        label="Download Receipt PDF",
                        data=pdf_bytes,
                        file_name=f"Receipt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf",
                        mime="application/pdf",
                    )

                    st.session_state["cart"] = []
                except Exception as e:
                    st.error(f"Transaction failed: {e}")

            if st.button("Clear Cart"):
                st.session_state["cart"] = []
                st.rerun()
