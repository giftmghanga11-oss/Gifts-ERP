import streamlit as st
import pandas as pd
from sqlalchemy import text
from db import get_engine, run_query

def record_expense(account_code: str, amount: float, description: str, funding_account_code: str, user_id: int):
    engine = get_engine()
    with engine.begin() as conn:
        je = conn.execute(
            text(
                """
                INSERT INTO journal_entries (reference_type, description, created_by)
                VALUES ('EXPENSE', :desc, :uid)
                RETURNING entry_id
                """
            ),
            {"desc": description.strip(), "uid": user_id},
        )
        entry_id = je.scalar()

        conn.execute(
            text(
                """
                INSERT INTO journal_lines (entry_id, account_id, debit, credit)
                VALUES
                (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = :exp_code), :amt, 0),
                (:eid, (SELECT account_id FROM chart_of_accounts WHERE account_code = :fund_code), 0, :amt)
                """
            ),
            {"eid": entry_id, "exp_code": account_code, "fund_code": funding_account_code, "amt": amount},
        )

def render_accounting_page(user):
    st.header("Accounting & Financial Statements")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "General Journal",
            "Trial Balance",
            "Income Statement (P&L)",
            "Balance Sheet",
            "Record Expense",
        ]
    )

    with tab1:
        st.subheader("General Journal Ledger")
        journal_df = run_query(
            """
            SELECT 
                je.entry_id AS "Entry ID",
                je.entry_date AS "Date",
                je.reference_type AS "Ref Type",
                je.description AS "Description",
                u.username AS "Recorded By",
                COA.account_code AS "Acct Code",
                COA.account_name AS "Account Name",
                jl.debit AS "Debit",
                jl.credit AS "Credit"
            FROM journal_entries je
            JOIN journal_lines jl ON je.entry_id = jl.entry_id
            JOIN chart_of_accounts COA ON jl.account_id = COA.account_id
            LEFT JOIN users u ON je.created_by = u.user_id
            ORDER BY je.entry_id DESC, jl.line_id ASC;
            """
        )
        if journal_df.empty:
            st.info("No journal entries recorded yet.")
        else:
            st.dataframe(journal_df, use_container_width=True, hide_index=True)

    with tab2:
        st.subheader("Trial Balance")
        tb_df = run_query(
            """
            SELECT 
                COA.account_code AS "Account Code",
                COA.account_name AS "Account Name",
                COA.account_type AS "Type",
                SUM(jl.debit) AS "Total Debits",
                SUM(jl.credit) AS "Total Credits"
            FROM chart_of_accounts COA
            LEFT JOIN journal_lines jl ON COA.account_id = jl.account_id
            GROUP BY COA.account_code, COA.account_name, COA.account_type
            ORDER BY COA.account_code;
            """
        )
        tb_df["Total Debits"] = tb_df["Total Debits"].fillna(0.0)
        tb_df["Total Credits"] = tb_df["Total Credits"].fillna(0.0)

        tot_dr = tb_df["Total Debits"].sum()
        tot_cr = tb_df["Total Credits"].sum()

        st.dataframe(tb_df, use_container_width=True, hide_index=True)

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Total Debits", f"KES {tot_dr:,.2f}")
        with col2:
            st.metric("Total Credits", f"KES {tot_cr:,.2f}")

        if round(tot_dr, 2) == round(tot_cr, 2):
            st.success("Trial Balance is Balanced (Debits = Credits).")
        else:
            st.error("Trial Balance is OUT OF BALANCE!")

    with tab3:
        st.subheader("Income Statement (Profit & Loss)")
        pnl_df = run_query(
            """
            SELECT 
                COA.account_code,
                COA.account_name,
                COA.account_type,
                SUM(jl.credit - jl.debit) AS net_balance
            FROM chart_of_accounts COA
            JOIN journal_lines jl ON COA.account_id = jl.account_id
            WHERE COA.account_type IN ('Revenue', 'Expense')
            GROUP BY COA.account_code, COA.account_name, COA.account_type
            ORDER BY COA.account_code;
            """
        )
        if pnl_df.empty:
            st.info("No revenue or expense data recorded yet.")
        else:
            revs = pnl_df[pnl_df["account_type"] == "Revenue"]
            exps = pnl_df[pnl_df["account_type"] == "Expense"].copy()
            exps["display_amount"] = -exps["net_balance"]

            total_revenue = revs["net_balance"].sum() if not revs.empty else 0.0
            total_expense = exps["display_amount"].sum() if not exps.empty else 0.0
            net_profit = total_revenue - total_expense

            st.markdown("#### **Revenue**")
            if not revs.empty:
                st.dataframe(
                    revs[["account_code", "account_name", "net_balance"]].rename(
                        columns={"account_code": "Code", "account_name": "Account", "net_balance": "Amount (KES)"}
                    ),
                    hide_index=True,
                    use_container_width=True,
                )
            st.metric("Total Revenue", f"KES {total_revenue:,.2f}")

            st.markdown("#### **Expenses & COGS**")
            if not exps.empty:
                st.dataframe(
                    exps[["account_code", "account_name", "display_amount"]].rename(
                        columns={"account_code": "Code", "account_name": "Account", "display_amount": "Amount (KES)"}
                    ),
                    hide_index=True,
                    use_container_width=True,
                )
            st.metric("Total Expenses & COGS", f"KES {total_expense:,.2f}")
            st.divider()
            st.metric("Net Profit / (Loss)", f"KES {net_profit:,.2f}")

    with tab4:
        st.subheader("Balance Sheet")
        bs_df = run_query(
            """
            SELECT 
                COA.account_code,
                COA.account_name,
                COA.account_type,
                CASE 
                    WHEN COA.account_type IN ('Asset', 'Expense') THEN SUM(jl.debit - jl.credit)
                    ELSE SUM(jl.credit - jl.debit)
                END AS balance
            FROM chart_of_accounts COA
            JOIN journal_lines jl ON COA.account_id = jl.account_id
            WHERE COA.account_type IN ('Asset', 'Liability', 'Equity')
            GROUP BY COA.account_code, COA.account_name, COA.account_type
            ORDER BY COA.account_code;
            """
        )
        pl = run_query(
            """
            SELECT COALESCE(SUM(jl.credit - jl.debit), 0) AS p
            FROM journal_lines jl
            JOIN chart_of_accounts c ON c.account_id = jl.account_id
            WHERE c.account_type IN ('Revenue', 'Expense')
            """
        )
        current_earnings = float(pl.iloc[0]["p"])

        if bs_df.empty:
            st.info("No balance sheet data available yet.")
        else:
            assets = bs_df[bs_df["account_type"] == "Asset"]
            liabilities = bs_df[bs_df["account_type"] == "Liability"]
            equity = bs_df[bs_df["account_type"] == "Equity"]

            tot_assets = assets["balance"].sum() if not assets.empty else 0.0
            tot_liab = liabilities["balance"].sum() if not liabilities.empty else 0.0
            tot_eq = (equity["balance"].sum() if not equity.empty else 0.0) + current_earnings

            st.markdown("#### **Assets**")
            st.dataframe(
                assets[["account_code", "account_name", "balance"]].rename(
                    columns={"account_code": "Code", "account_name": "Account", "balance": "Balance (KES)"}
                ),
                hide_index=True,
                use_container_width=True,
            )
            st.metric("Total Assets", f"KES {tot_assets:,.2f}")

            st.markdown("#### **Liabilities & Equity**")
            if not liabilities.empty:
                st.dataframe(
                    liabilities[["account_code", "account_name", "balance"]].rename(
                        columns={"account_code": "Code", "account_name": "Account", "balance": "Balance (KES)"}
                    ),
                    hide_index=True,
                    use_container_width=True,
                )
            if not equity.empty:
                st.dataframe(
                    equity[["account_code", "account_name", "balance"]].rename(
                        columns={"account_code": "Code", "account_name": "Account", "balance": "Balance (KES)"}
                    ),
                    hide_index=True,
                    use_container_width=True,
                )
            st.metric("Current Period Earnings", f"KES {current_earnings:,.2f}")
            st.metric("Total Liabilities & Equity", f"KES {(tot_liab + tot_eq):,.2f}")

            if round(tot_assets, 2) == round(tot_liab + tot_eq, 2):
                st.success("Balance Sheet is balanced (Assets = Liabilities + Equity).")
            else:
                st.error("Balance Sheet is OUT OF BALANCE! Check the Trial Balance tab.")

    with tab5:
        st.subheader("Record Operating Expense")
        expense_accounts = run_query(
            "SELECT account_code, account_name FROM chart_of_accounts WHERE account_type = 'Expense' AND account_code != '5000' ORDER BY account_code;"
        )
        with st.form("expense_form"):
            acct_options = {f"{r['account_code']} - {r['account_name']}": r["account_code"] for _, r in expense_accounts.iterrows()}
            selected_acct_label = st.selectbox("Expense Category", list(acct_options.keys()))
            expense_code = acct_options[selected_acct_label]

            amount = st.number_input("Expense Amount (KES)", min_value=1.0, step=100.0)
            description = st.text_input("Description / Memo", placeholder="e.g., November Electricity Bill")

            funding_source = st.selectbox(
                "Paid From Account",
                [
                    "1010 - Cash on Hand",
                    "1020 - M-Pesa Till / Mobile Money",
                    "1030 - Bank Account / Card Settlement",
                ],
            )
            funding_code = funding_source.split(" - ")[0]

            if st.form_submit_button("Post Expense"):
                if not description or amount <= 0:
                    st.error("Description and valid amount are required.")
                else:
                    try:
                        record_expense(expense_code, amount, description, funding_code, user["user_id"])
                        st.success("Expense recorded and balanced journal entry posted successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to record expense: {e}")
