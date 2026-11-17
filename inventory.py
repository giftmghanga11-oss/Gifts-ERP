import streamlit as st
from sqlalchemy import text
from db import get_engine, run_query


def add_product_and_initial_stock(
    sku: str,
    name: str,
    cost_price: float,
    selling_price: float,
    tax_rate: float,
    initial_qty: int,
    funding_account_code: str,
    user_id: int,
):
    engine = get_engine()
    with engine.begin() as conn:
        # 1. Insert product
        res = conn.execute(
            text(
                """
                INSERT INTO products (sku_barcode, name, cost_price, selling_price, tax_rate, stock_qty)
                VALUES (:sku, :name, :cost, :sell, :tax, :qty)
                RETURNING product_id
                """
            ),
            {
                "sku": sku.strip(),
                "name": name.strip(),
                "cost": cost_price,
                "sell": selling_price,
                "tax": tax_rate,
                "qty": initial_qty,
            },
        )
        product_id = res.scalar()

        # 2. If initial stock > 0, record double-entry journal entry
        total_cost = round(cost_price * initial_qty, 2)
        if initial_qty > 0 and total_cost > 0:
            je = conn.execute(
                text(
                    """
                    INSERT INTO journal_entries (reference_type, reference_id, description, created_by)
                    VALUES ('STOCK_IN', :ref_id, :desc, :uid)
                    RETURNING entry_id
                    """
                ),
                {
                    "ref_id": product_id,
                    "desc": f"Initial stock: {initial_qty}x {name.strip()} ({sku.strip()})",
                    "uid": user_id,
                },
            )
            entry_id = je.scalar()

            # Debit Inventory Asset (1200), Credit Funding Account (1010 Cash / 1030 Bank / 3000 Owner Equity)
            conn.execute(
                text(
                    """
                    INSERT INTO journal_lines (entry_id, account_id, debit, credit)
                    VALUES
                    (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = '1200'), :amt, 0),
                    (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = :fund_code), 0, :amt)
                    """
                ),
                {"eid": entry_id, "amt": total_cost, "fund_code": funding_account_code},
            )


def restock_product(
    product_id: int,
    add_qty: int,
    unit_cost: float,
    funding_account_code: str,
    user_id: int,
):
    engine = get_engine()
    with engine.begin() as conn:
        prod = conn.execute(
            text("SELECT name, sku_barcode FROM products WHERE product_id = :pid"),
            {"pid": product_id},
        ).fetchone()

        conn.execute(
            text(
                """
                UPDATE products
                SET stock_qty = stock_qty + :qty,
                    cost_price = :cost
                WHERE product_id = :pid
                """
            ),
            {"qty": add_qty, "cost": unit_cost, "pid": product_id},
        )

        total_cost = round(unit_cost * add_qty, 2)
        if total_cost > 0:
            je = conn.execute(
                text(
                    """
                    INSERT INTO journal_entries (reference_type, reference_id, description, created_by)
                    VALUES ('RESTOCK', :pid, :desc, :uid)
                    RETURNING entry_id
                    """
                ),
                {
                    "pid": product_id,
                    "desc": f"Restocked {add_qty}x {prod.name} ({prod.sku_barcode})",
                    "uid": user_id,
                },
            )
            entry_id = je.scalar()

            conn.execute(
                text(
                    """
                    INSERT INTO journal_lines (entry_id, account_id, debit, credit)
                    VALUES
                    (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = '1200'), :amt, 0),
                    (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = :fund_code), 0, :amt)
                    """
                ),
                {"eid": entry_id, "amt": total_cost, "fund_code": funding_account_code},
            )


def render_inventory_page(user):
    st.header("Inventory & Products Management")

    tab1, tab2, tab3 = st.tabs(["Product Catalog", "Add New Product", "Restock Existing Item"])

    with tab1:
        products_df = run_query(
            """
            SELECT product_id, sku_barcode AS "SKU / Barcode", name AS "Product Name",
                   cost_price AS "Cost Price", selling_price AS "Selling Price",
                   tax_rate AS "VAT %", stock_qty AS "In Stock",
                   ROUND(cost_price * stock_qty, 2) AS "Stock Value"
            FROM products
            ORDER BY name;
            """
        )
        if products_df.empty:
            st.info("No products added yet. Use the 'Add New Product' tab to create your first item.")
        else:
            total_val = products_df["Stock Value"].sum()
            st.metric("Total Inventory Asset Value", f"KES {total_val:,.2f}")
            st.dataframe(products_df, use_container_width=True, hide_index=True)

    with tab2:
        st.subheader("Register New Product")
        with st.form("new_product_form"):
            c1, c2 = st.columns(2)
            with c1:
                sku = st.text_input("SKU / Barcode", placeholder="e.g., 1001")
                name = st.text_input("Product Name", placeholder="e.g., Sugar 1kg")
                tax_rate = st.number_input("VAT / Tax Rate (%)", min_value=0.0, max_value=100.0, value=16.0, step=1.0)
            with c2:
                cost_price = st.number_input("Unit Cost Price (KES)", min_value=0.0, step=1.0)
                selling_price = st.number_input("Unit Selling Price (KES)", min_value=0.0, step=1.0)
                initial_qty = st.number_input("Opening Stock Quantity", min_value=0, step=1)

            funding_label = st.selectbox(
                "How was opening stock paid/funded?",
                [
                    "3000 - Owner Equity (Opening Capital)",
                    "1010 - Cash on Hand",
                    "1030 - Bank Account / Card Settlement",
                ],
            )
            funding_code = funding_label.split(" - ")[0]

            if st.form_submit_button("Save Product"):
                if not sku or not name or selling_price <= 0:
                    st.error("SKU, Product Name, and a positive Selling Price are required.")
                else:
                    try:
                        add_product_and_initial_stock(
                            sku, name, cost_price, selling_price, tax_rate, initial_qty, funding_code, user["user_id"]
                        )
                        st.success(f"Added '{name}' and posted inventory accounting entry.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error adding product: {e}")

    with tab3:
        st.subheader("Receive New Stock")
        items_df = run_query("SELECT product_id, sku_barcode, name, cost_price, stock_qty FROM products ORDER BY name;")
        if items_df.empty:
            st.info("Add a product first before restocking.")
        else:
            options = {
                f"{row['name']} ({row['sku_barcode']}) — Current Stock: {row['stock_qty']}": row
                for _, row in items_df.iterrows()
            }
            selected_label = st.selectbox("Select Product", list(options.keys()))
            selected_row = options[selected_label]

            with st.form("restock_form"):
                add_qty = st.number_input("Quantity Received", min_value=1, step=1)
                new_cost = st.number_input(
                    "Unit Purchase Cost (KES)", min_value=0.0, value=float(selected_row["cost_price"]), step=1.0
                )
                pay_source = st.selectbox(
                    "Payment Source",
                    [
                        "1010 - Cash on Hand",
                        "1020 - M-Pesa Till / Mobile Money",
                        "1030 - Bank Account / Card Settlement",
                        "3000 - Owner Equity",
                    ],
                )
                pay_code = pay_source.split(" - ")[0]

                if st.form_submit_button("Record Stock Purchase"):
                    try:
                        restock_product(int(selected_row["product_id"]), add_qty, new_cost, pay_code, user["user_id"])
                        st.success(f"Restocked {add_qty} units of {selected_row['name']}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Restock failed: {e}")