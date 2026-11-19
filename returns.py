import re
import streamlit as st
from sqlalchemy import text
from db import get_engine, run_query
from common import CURRENCY, PAYMENT_METHODS, account_id, post_entry, show_df


def process_return(entry_id: int, qty_by_item: dict, refund_method, reason: str, user_id: int):
    """Reverse part or all of a sale in one transaction.

    Stock goes back, Sales/VAT/COGS are reversed at the ORIGINAL amounts and cost,
    and the money goes back as a cash/M-Pesa/bank refund or a credit on the
    customer's account (if the sale was on credit).
    """
    with get_engine().begin() as conn:
        orig = conn.execute(
            text("SELECT customer_id FROM journal_entries WHERE entry_id = :e AND reference_type = 'SALE'"),
            {"e": entry_id},
        ).fetchone()
        if orig is None:
            raise ValueError("Sale not found.")
        customer_id = orig[0]

        ar_acc = account_id(conn, "ar")
        sales_acc = account_id(conn, "sales")
        vat_acc = account_id(conn, "vat", required=False)

        on_credit = conn.execute(
            text("SELECT 1 FROM journal_lines WHERE entry_id = :e AND account_id = :a AND debit > 0"),
            {"e": entry_id, "a": ar_acc},
        ).fetchone() is not None
        vat_was_split = bool(vat_acc) and conn.execute(
            text("SELECT 1 FROM journal_lines WHERE entry_id = :e AND account_id = :a AND credit > 0"),
            {"e": entry_id, "a": vat_acc},
        ).fetchone() is not None

        net_total = vat_total = cost_total = 0.0
        returned_lines = []
        for item_id, r in qty_by_item.items():
            if r <= 0:
                continue
            row = conn.execute(
                text(
                    """
                    SELECT product_id, product_name, qty, returned_qty, unit_cost, net_amount, vat_amount
                    FROM pos_sale_items WHERE item_id = :i AND entry_id = :e FOR UPDATE
                    """
                ),
                {"i": item_id, "e": entry_id},
            ).fetchone()
            if row is None:
                raise ValueError("Item does not belong to this sale.")
            pid, pname, qty, already, unit_cost, net, vat = row
            if r > qty - already:
                raise ValueError(f"Cannot return {r} x {pname}: only {qty - already} left to return.")

            net_total += round(float(net) * r / qty, 2)
            vat_total += round(float(vat) * r / qty, 2)
            cost_total += float(unit_cost) * r
            returned_lines.append(f"{r}x {pname}")

            conn.execute(
                text("UPDATE pos_sale_items SET returned_qty = returned_qty + :r WHERE item_id = :i"),
                {"r": r, "i": item_id},
            )
            conn.execute(
                text("UPDATE products SET stock_qty = stock_qty + :r WHERE product_id = :p"),
                {"r": r, "p": pid},
            )

        if not returned_lines:
            raise ValueError("Enter a quantity to return for at least one item.")

        net_total, vat_total, cost_total = round(net_total, 2), round(vat_total, 2), round(cost_total, 2)
        gross = round(net_total + vat_total, 2)

        if on_credit:
            credit_acc, how = ar_acc, "credited to customer account"
        else:
            credit_acc, how = account_id(conn, PAYMENT_METHODS[refund_method]), f"refunded via {refund_method}"

        lines = []
        if vat_was_split and vat_total > 0:
            lines += [(sales_acc, net_total, 0), (vat_acc, vat_total, 0)]
        else:
            lines += [(sales_acc, gross, 0)]
        lines += [(credit_acc, 0, gross)]
        if cost_total > 0:
            lines += [(account_id(conn, "inventory"), cost_total, 0), (account_id(conn, "cogs"), 0, cost_total)]

        desc = f"Return of POS-{entry_id:06d}: {', '.join(returned_lines)} - {how}. Reason: {reason.strip()}"
        cn_id = post_entry(
            conn,
            "SALE_RETURN",
            desc,
            user_id,
            lines,
            customer_id=int(customer_id) if (on_credit and customer_id) else None,
            ref_id=entry_id,
        )
    return {"credit_note": cn_id, "amount": gross, "how": how, "on_credit": on_credit}


def render_returns_page(user):
    st.header("Sales Returns")
    st.caption("Returned goods go back into stock and the sale is reversed in the ledger.")

    flash = st.session_state.pop("ret_flash", None)
    if flash:
        st.success(flash)

    tab1, tab2 = st.tabs(["Process Return", "Return History"])

    with tab1:
        q = st.text_input("Find sale: receipt number, customer name or product")
        q_clean = re.sub(r"(?i)^\s*pos-?0*", "", q.strip()) if q else ""
        sales = run_query(
            """
            SELECT je.entry_id, CAST(je.entry_date AS date) AS d, je.description,
                   CAST(SUM(si.total_amount) AS FLOAT) AS total
            FROM journal_entries je
            JOIN pos_sale_items si ON si.entry_id = je.entry_id
            WHERE je.reference_type = 'SALE'
              AND (:q = '' OR CAST(je.entry_id AS TEXT) LIKE :like
                   OR je.description ILIKE :like OR si.product_name ILIKE :like)
            GROUP BY je.entry_id, je.entry_date, je.description
            ORDER BY je.entry_id DESC LIMIT 30;
            """,
            {"q": q_clean, "like": f"%{q_clean}%"},
        )
        if sales.empty:
            st.info(
                "No matching sales. Only sales made after the returns upgrade can be returned here; "
                "older sales need a manual adjusting entry."
            )
        else:
            options = {
                f"POS-{int(r['entry_id']):06d} | {r['d']} | {CURRENCY} {r['total']:,.2f} | {r['description']}": int(r["entry_id"])
                for _, r in sales.iterrows()
            }
            label = st.selectbox("Sale", list(options.keys()))
            entry_id = options[label]

            items = run_query(
                """
                SELECT item_id, product_name, qty, returned_qty, unit_price
                FROM pos_sale_items WHERE entry_id = :e ORDER BY item_id;
                """,
                {"e": entry_id},
            )
            returnable = items[items["qty"] > items["returned_qty"]]
            if returnable.empty:
                st.info("Everything on this sale has already been returned.")
            else:
                with st.form(f"return_form_{entry_id}"):
                    qty_by_item = {}
                    for _, r in returnable.iterrows():
                        left = int(r["qty"] - r["returned_qty"])
                        c = st.columns([4, 2])
                        c[0].write(f"**{r['product_name']}**  \nSold {int(r['qty'])} | already returned {int(r['returned_qty'])} | {CURRENCY} {float(r['unit_price']):,.2f} each")
                        qty_by_item[int(r["item_id"])] = c[1].number_input(
                            "Return qty", min_value=0, max_value=left, value=0, step=1, key=f"rq_{entry_id}_{int(r['item_id'])}"
                        )
                    method = st.selectbox(
                        "Refund via (ignored if the sale was on credit: the customer account is credited)",
                        list(PAYMENT_METHODS.keys()),
                    )
                    reason = st.text_input("Reason for return (required)", placeholder="e.g., wrong colour, damaged tin")
                    if st.form_submit_button("Process return"):
                        if not reason.strip():
                            st.error("Please give a reason.")
                        else:
                            try:
                                res = process_return(entry_id, qty_by_item, method, reason, user["user_id"])
                                st.session_state["ret_flash"] = (
                                    f"Return recorded (ref {res['credit_note']}): {CURRENCY} {res['amount']:,.2f} {res['how']}."
                                )
                                st.rerun()
                            except Exception as e:
                                st.error(f"Return not recorded: {e}")

    with tab2:
        hist = run_query(
            """
            SELECT je.entry_id AS "Ref", CAST(je.entry_date AS date) AS "Date",
                   je.description AS "Details", u.username AS "Processed by"
            FROM journal_entries je LEFT JOIN users u ON u.user_id = je.created_by
            WHERE je.reference_type = 'SALE_RETURN'
            ORDER BY je.entry_id DESC LIMIT 200;
            """
        )
        if hist.empty:
            st.info("No returns recorded yet.")
        else:
            show_df(hist)
