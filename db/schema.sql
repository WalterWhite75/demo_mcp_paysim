CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    step INTEGER,
    type TEXT,
    amount NUMERIC,
    name_orig TEXT,
    oldbalance_org NUMERIC,
    newbalance_org NUMERIC,
    name_dest TEXT,
    oldbalance_dest NUMERIC,
    newbalance_dest NUMERIC,
    is_fraud BOOLEAN,
    is_flagged_fraud BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_tx_name_orig ON transactions(name_orig);
CREATE INDEX IF NOT EXISTS idx_tx_name_dest ON transactions(name_dest);
CREATE INDEX IF NOT EXISTS idx_tx_step ON transactions(step);
CREATE INDEX IF NOT EXISTS idx_tx_fraud ON transactions(is_fraud);
