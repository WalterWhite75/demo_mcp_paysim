import pandas as pd

INPUT = "data/raw/paysim.csv"
OUTPUT = "data/raw/paysim_small.csv"

CHUNK_SIZE = 200_000
MAX_ROWS = 50_000

rows = []

for chunk in pd.read_csv(INPUT, chunksize=CHUNK_SIZE):
    # On garde :
    # - montants élevés
    # - fraudes
    # - types intéressants
    filtered = chunk[
        (chunk["amount"] > 100_000)
        | (chunk["isFraud"] == 1)
        | (chunk["type"].isin(["TRANSFER", "CASH_OUT"]))
    ]

    rows.append(filtered)

    if sum(len(r) for r in rows) >= MAX_ROWS:
        break

df = pd.concat(rows).head(MAX_ROWS)

df.to_csv(OUTPUT, index=False)

print(f"✅ Fichier réduit créé : {OUTPUT}")
print(f"➡️ {len(df)} lignes")
print(df[["type", "amount", "isFraud"]].value_counts().head())