import os
import time
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

CSV_PATH = os.getenv("CSV_PATH", "/data/paysim_small.csv")
MAX_ROWS = int(os.getenv("MAX_ROWS", "50000"))

DB_HOST = os.getenv("DB_HOST", "db")   # docker service name
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "paysim")
DB_USER = os.getenv("DB_USER", "paysim")
DB_PASSWORD = os.getenv("DB_PASSWORD", "paysim")


def connect_with_retry(max_wait_s: int = 90):
    deadline = time.time() + max_wait_s
    last_err = None
    while time.time() < deadline:
        try:
            return psycopg2.connect(
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
            )
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"DB not reachable after {max_wait_s}s (host={DB_HOST}): {last_err}")


def main():
    print("Loading CSV subset...")
    print(f"CSV_PATH resolved to: {CSV_PATH}")
    print(f"DB target: {DB_HOST}:{DB_PORT}/{DB_NAME}")

    # Read only what we need + explicit dtypes to keep memory low.
    # Works for both original PaySim columns (camelCase) and already-normalized snake_case.
    preferred_cols_sets = [
        ["step","type","amount","nameOrig","oldbalanceOrg","newbalanceOrig","nameDest","oldbalanceDest","newbalanceDest","isFraud","isFlaggedFraud"],
        ["step","type","amount","name_orig","oldbalance_org","newbalance_org","name_dest","oldbalance_dest","newbalance_dest","is_fraud","is_flagged_fraud"],
    ]

    dtype_guess = {
        "step": "int64",
        "type": "string",
        "amount": "float64",
        "nameOrig": "string",
        "nameDest": "string",
        "name_orig": "string",
        "name_dest": "string",
        "oldbalanceOrg": "float64",
        "newbalanceOrig": "float64",
        "oldbalanceDest": "float64",
        "newbalanceDest": "float64",
        "oldbalance_org": "float64",
        "newbalance_org": "float64",
        "oldbalance_dest": "float64",
        "newbalance_dest": "float64",
        "isFraud": "int64",
        "isFlaggedFraud": "int64",
        "is_fraud": "int64",
        "is_flagged_fraud": "int64",
    }

    df = None
    last_err = None
    for usecols in preferred_cols_sets:
        try:
            df = pd.read_csv(CSV_PATH, nrows=MAX_ROWS, usecols=usecols, dtype={k: v for k, v in dtype_guess.items() if k in usecols})
            break
        except Exception as e:
            last_err = e
            df = None

    if df is None:
        # Fall back to reading without usecols (handles unexpected column layouts)
        print(f"WARN: could not read with predefined columns (will fallback). Reason: {last_err}")
        df = pd.read_csv(CSV_PATH, nrows=MAX_ROWS)

    # Normalize column names to snake_case (no-op if already normalized)
    df = df.rename(
        columns={
            "nameOrig": "name_orig",
            "oldbalanceOrg": "oldbalance_org",
            "newbalanceOrig": "newbalance_org",
            "nameDest": "name_dest",
            "oldbalanceDest": "oldbalance_dest",
            "newbalanceDest": "newbalance_dest",
            "isFraud": "is_fraud",
            "isFlaggedFraud": "is_flagged_fraud",
        },
        errors="ignore",
    )

    cols = [
        "step","type","amount",
        "name_orig","oldbalance_org","newbalance_org",
        "name_dest","oldbalance_dest","newbalance_dest",
        "is_fraud","is_flagged_fraud",
    ]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise RuntimeError(f"CSV missing columns: {missing}. Found: {list(df.columns)}")

    def _to_bool(s: pd.Series) -> pd.Series:
        # Accept 0/1, True/False, and string variants.
        if s.dtype == bool:
            return s
        if str(s.dtype).startswith("int") or str(s.dtype).startswith("float"):
            return s.fillna(0).astype(int).astype(bool)
        return s.astype(str).str.lower().isin(["1", "true", "t", "yes", "y"])

    df["is_fraud"] = _to_bool(df["is_fraud"])
    df["is_flagged_fraud"] = _to_bool(df["is_flagged_fraud"])

    print(f"Loaded {len(df)} rows")
    print("Connecting to Postgres (with retry)...")

    conn = connect_with_retry()
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM transactions;")
    existing = cur.fetchone()[0]
    if existing and existing > 0:
        print(f"Table already has {existing} rows. Skipping load.")
        conn.commit()
        cur.close()
        conn.close()
        print("DONE")
        return

    print("Inserting rows into Postgres (bulk)...")
    rows = [tuple(x) for x in df[cols].to_numpy()]

    execute_values(
        cur,
        """
        INSERT INTO transactions (
            step,type,amount,name_orig,oldbalance_org,newbalance_org,
            name_dest,oldbalance_dest,newbalance_dest,is_fraud,is_flagged_fraud
        ) VALUES %s
        """,
        rows,
        page_size=5000,
    )
    conn.commit()
    cur.close()
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
