import json
import os
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2


DB_HOST = os.getenv("DB_HOST", "db")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "paysim")
DB_USER = os.getenv("DB_USER", "paysim")
DB_PASSWORD = os.getenv("DB_PASSWORD", "paysim")

HOST = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_HTTP_PORT", "8765"))

TOOLS = [
    {
        "name": "get_account_kpi",
        "description": "KPI d'un compte (volume, nb_tx_in/out, fraud_out) avec filtre optionnel step_from/step_to.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "step_from": {"type": "integer"},
                "step_to": {"type": "integer"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "detect_suspicious",
        "description": "Détecte des transferts sortants suspects (règles simples).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "min_amount": {"type": "number"},
                "window_steps": {"type": "integer"},
                "max_rows": {"type": "integer"},
            },
            "required": ["name"],
        },
    },
]


def db_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


def jsonrpc_ok(_id, result):
    return {"jsonrpc": "2.0", "id": _id, "result": result}


def jsonrpc_err(_id, code, message):
    return {"jsonrpc": "2.0", "id": _id, "error": {"code": code, "message": message}}


def resource_read(uri: str):
    if uri.startswith("account/"):
        name = uri.split("/", 1)[1]
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE name_orig=%s) AS nb_out,
                  COALESCE(SUM(amount) FILTER (WHERE name_orig=%s), 0) AS total_out,
                  COUNT(*) FILTER (WHERE name_dest=%s) AS nb_in,
                  COALESCE(SUM(amount) FILTER (WHERE name_dest=%s), 0) AS total_in,
                  COUNT(*) FILTER (WHERE name_orig=%s AND is_fraud) AS fraud_out
                FROM transactions
                """,
                (name, name, name, name, name),
            )
            nb_out, total_out, nb_in, total_in, fraud_out = cur.fetchone()
        return {
            "name": name,
            "nb_out": int(nb_out),
            "total_out": float(total_out),
            "nb_in": int(nb_in),
            "total_in": float(total_in),
            "fraud_out": int(fraud_out),
        }

    if uri.startswith("transaction/"):
        tx_id = int(uri.split("/", 1)[1])
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  id,
                  step,
                  type,
                  amount::float8 AS amount,
                  name_orig,
                  oldbalance_org::float8 AS oldbalance_org,
                  newbalance_org::float8 AS newbalance_org,
                  name_dest,
                  oldbalance_dest::float8 AS oldbalance_dest,
                  newbalance_dest::float8 AS newbalance_dest,
                  is_fraud,
                  is_flagged_fraud
                FROM transactions
                WHERE id=%s
                """,
                (tx_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"id": tx_id, "found": False}
        keys = ["id","step","type","amount","name_orig","oldbalance_org","newbalance_org",
                "name_dest","oldbalance_dest","newbalance_dest","is_fraud","is_flagged_fraud"]
        out = dict(zip(keys, row))
        return out

    return {"uri": uri, "error": "unknown resource"}


def tool_get_account_kpi(name: str, step_from=None, step_to=None):
    step_from = int(step_from) if step_from is not None else 1
    step_to = int(step_to) if step_to is not None else 200

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE name_orig=%s AND step BETWEEN %s AND %s) AS nb_out,
              COALESCE(SUM(amount) FILTER (WHERE name_orig=%s AND step BETWEEN %s AND %s),0) AS total_out,
              COUNT(*) FILTER (WHERE name_dest=%s AND step BETWEEN %s AND %s) AS nb_in,
              COALESCE(SUM(amount) FILTER (WHERE name_dest=%s AND step BETWEEN %s AND %s),0) AS total_in,
              COUNT(*) FILTER (WHERE name_orig=%s AND is_fraud AND step BETWEEN %s AND %s) AS fraud_out
            FROM transactions
            """,
            (name, step_from, step_to, name, step_from, step_to, name, step_from, step_to, name, step_from, step_to, name, step_from, step_to),
        )
        nb_out, total_out, nb_in, total_in, fraud_out = cur.fetchone()

        cur.execute(
            """
            SELECT type, COUNT(*) as cnt
            FROM transactions
            WHERE name_orig=%s AND step BETWEEN %s AND %s
            GROUP BY type
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (name, step_from, step_to),
        )
        top_types = [{"type": t, "cnt": int(c)} for (t, c) in cur.fetchall()]

    avg_out = (float(total_out) / int(nb_out)) if nb_out else 0.0
    avg_in = (float(total_in) / int(nb_in)) if nb_in else 0.0

    return {
        "name": name,
        "step_from": step_from,
        "step_to": step_to,
        "out": {"nb_out": int(nb_out), "total_out": float(total_out), "avg_out_amount": avg_out, "fraud_out": int(fraud_out)},
        "in": {"nb_in": int(nb_in), "total_in": float(total_in), "avg_in_amount": avg_in},
        "top_out_types": top_types,
    }


def tool_detect_suspicious(name: str, min_amount=200000, window_steps=10, max_rows=10):
    min_amount = float(min_amount) if min_amount is not None else 200000.0
    window_steps = int(window_steps) if window_steps is not None else 10
    max_rows = int(max_rows) if max_rows is not None else 10

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, step, type, amount, name_orig, name_dest, is_fraud
            FROM transactions
            WHERE name_orig=%s
              AND amount >= %s
              AND type IN ('TRANSFER','CASH_OUT')
            ORDER BY amount DESC
            LIMIT %s
            """,
            (name, min_amount, max_rows),
        )
        rows = cur.fetchall()

    matches = []
    for r in rows:
        matches.append({
            "id": int(r[0]),
            "step": int(r[1]),
            "type": r[2],
            "amount": float(r[3]),
            "name_orig": r[4],
            "name_dest": r[5],
            "is_fraud": bool(r[6]),
        })

    return {
        "name": name,
        "min_amount": min_amount,
        "window_steps": window_steps,
        "max_rows": max_rows,
        "matches": matches,
        "note": "Règles simples de démo (pas de ML).",
    }



def _json_default(o):
    # Convert Postgres NUMERIC (Decimal) to JSON number
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        data = json.dumps(obj, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/rpc":
            return self._send({"error": "not found"}, 404)

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            req = json.loads(raw)
        except Exception:
            return self._send({"error": "invalid json"}, 400)

        _id = req.get("id", 1)
        method = req.get("method")
        params = req.get("params", {}) or {}

        try:
            if method == "initialize":
                return self._send(jsonrpc_ok(_id, {"server": "mcp-paysim-demo", "version": "0.1", "time": time.strftime("%Y-%m-%dT%H:%M:%S")}))

            if method == "tools/list":
                return self._send(jsonrpc_ok(_id, {"tools": TOOLS}))

            if method == "resources/read":
                uri = params.get("uri") or params.get("resource")
                if not uri:
                    return self._send(jsonrpc_err(_id, -32602, "missing uri"), 200)
                return self._send(jsonrpc_ok(_id, resource_read(uri)))

            if method == "tools/call":
                name = params.get("name") or params.get("tool")
                args = params.get("arguments") or params.get("params") or {}
                if name == "get_account_kpi":
                    return self._send(jsonrpc_ok(_id, tool_get_account_kpi(**args)))
                if name == "detect_suspicious":
                    return self._send(jsonrpc_ok(_id, tool_detect_suspicious(**args)))
                return self._send(jsonrpc_err(_id, -32601, f"unknown tool: {name}"), 200)

            return self._send(jsonrpc_err(_id, -32601, f"unknown method: {method}"), 200)

        except Exception as e:
            return self._send(jsonrpc_err(_id, -32000, str(e)), 200)


def main():
    print(f"[MCP_HTTP] starting on {HOST}:{PORT} (db={DB_HOST}:{DB_PORT}/{DB_NAME})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
