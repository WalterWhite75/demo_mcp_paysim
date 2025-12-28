import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

import psycopg2


DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "paysim")
DB_USER = os.getenv("DB_USER", "paysim")
DB_PASSWORD = os.getenv("DB_PASSWORD", "paysim")

# When reloading with a smaller CSV, the loader may take a bit; keep this configurable.
WAIT_S = int(os.getenv("WAIT_S", "180"))

MCP_HTTP_URL = os.getenv("MCP_HTTP_URL", "http://localhost:8765/rpc")


def pick_demo_values(wait_s: int = 120):
    """Pick (name, tx_id) from Postgres on the host machine. Wait until data exists."""

    deadline = time.time() + wait_s

    # 1) Wait until Postgres accepts connections (after docker compose up).
    conn = None
    last_err = None
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                connect_timeout=3,
            )
            break
        except Exception as e:
            last_err = e
            time.sleep(1)

    if conn is None:
        raise RuntimeError(
            "Impossible de se connecter à Postgres.\n"
            f"Cible: {DB_HOST}:{DB_PORT}/{DB_NAME} (user={DB_USER})\n"
            f"Dernière erreur: {last_err}\n\n"
            "Vérifie que le service db est UP et que le port 5432 est publié:\n"
            "  docker compose ps\n"
            "  docker logs paysim_db\n"
        )

    cur = conn.cursor()

    # 2) Wait until the loader has inserted rows.
    n = 0
    while time.time() < deadline:
        cur.execute("SELECT COUNT(*) FROM transactions;")
        n = cur.fetchone()[0]
        if n and n > 0:
            break
        time.sleep(2)

    if not n or n == 0:
        cur.close()
        conn.close()
        raise RuntimeError(
            "La table transactions est vide (le loader n'a pas fini ou a échoué).\n"
            "Vérifie:\n"
            "  docker compose ps\n"
            "  docker logs -f paysim_loader\n"
            "Puis relance proprement si besoin:\n"
            "  docker compose down -v\n"
            "  docker compose up -d\n"
        )

    cur.execute("SELECT MIN(id) FROM transactions;")
    tx_id = cur.fetchone()[0]
    if tx_id is None:
        cur.close()
        conn.close()
        raise RuntimeError("Aucun id trouvé dans transactions (table vide ou chargement incomplet).")

    cur.execute(
        """
        SELECT name_orig
        FROM transactions
        WHERE name_orig IS NOT NULL AND name_orig <> ''
        GROUP BY name_orig
        ORDER BY COUNT(*) DESC
        LIMIT 1;
        """
    )
    row = cur.fetchone()
    name = row[0] if row else None

    if not name:
        cur.execute("SELECT name_orig FROM transactions WHERE name_orig IS NOT NULL AND name_orig <> '' LIMIT 1;")
        row = cur.fetchone()
        name = row[0] if row else None

    if not name:
        cur.execute("SELECT name_dest FROM transactions WHERE name_dest IS NOT NULL AND name_dest <> '' LIMIT 1;")
        row = cur.fetchone()
        name = row[0] if row else None

    cur.close()
    conn.close()
    return name, tx_id


class HttpMcpClient:
    def __init__(self, url: str):
        self.url = url
        self._id = 0

    def call(self, method: str, params: dict):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
        last_err = None
        for _ in range(20):
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    raw = resp.read().decode("utf-8")
                return json.loads(raw)
            except (urllib.error.URLError, ConnectionResetError, TimeoutError) as e:
                last_err = e
                time.sleep(0.5)

        raise RuntimeError(
            f"Impossible de joindre le MCP server en HTTP ({self.url}).\n"
            f"Dernière erreur: {last_err}\n"
            "Vérifie:\n"
            "  docker compose ps (paysim_mcp_server doit être Up/healthy)\n"
            "  docker logs paysim_mcp_server\n"
        )


class StdioMcpClient:
    def __init__(self, cmd):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id = 0

    def call(self, method: str, params: dict):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"Le serveur MCP (stdio) a quitté.\nSTDERR:\n{err}")
        return json.loads(line)

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    name, tx_id = pick_demo_values(wait_s=WAIT_S)
    print(f"(demo picks) name={name} tx_id={tx_id}\n")

    use_http = os.getenv("MCP_HTTP", "0") == "1"
    if use_http:
        p = HttpMcpClient(MCP_HTTP_URL)
    else:
        p = StdioMcpClient([sys.executable, "server/mcp_server_paysim.py"])

    print("1) initialize")
    print(p.call("initialize", {}))
    print("\n2) tools/list")
    print(p.call("tools/list", {}))
    print(f"\n3) resources/read account/{name}")
    print(p.call("resources/read", {"uri": f"account/{name}"}))
    print(f"\n4) resources/read transaction/{tx_id}")
    print(p.call("resources/read", {"uri": f"transaction/{tx_id}"}))
    print(f"\n5) tools/call get_account_kpi({name})")
    print(p.call("tools/call", {"name": "get_account_kpi", "arguments": {"name": name}}))
    print(f"\n6) tools/call detect_suspicious({name})")
    print(
        p.call(
            "tools/call",
            {"name": "detect_suspicious", "arguments": {"name": name, "min_amount": 200000, "window_steps": 10, "max_rows": 10}},
        )
    )

    if not use_http:
        p.close()


if __name__ == "__main__":
    main()
