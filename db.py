import streamlit as st
from sqlalchemy import create_engine, text
import pandas as pd

@st.cache_resource
def get_engine():
    db_url = st.secrets["connections"]["supabase"]["url"]
    return create_engine(db_url, pool_pre_ping=True)

def run_query(query, params=None):
    engine = get_engine()
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn, params=params)

def execute_write(query, params=None):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(query), params or {})