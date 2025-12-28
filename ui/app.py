import os
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import streamlit as st
import psycopg2


# Config

MCP_HTTP_URL = os.getenv("MCP_HTTP_URL", "http://localhost:8765/rpc")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "paysim")
DB_USER = os.getenv("DB_USER", "paysim")
DB_PASSWORD = os.getenv("DB_PASSWORD", "paysim")

# Helpers

def mcp_call(method: str, params: Optional[dict] = None, _id: int = 1, timeout: int = 10) -> Dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    r = requests.post(MCP_HTTP_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def wait_mcp(max_wait_s: float = 8.0) -> bool:
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            out = mcp_call("initialize", {}, _id=1, timeout=3)
            if "result" in out and "error" not in out:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def db_conn():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=5,
    )


@st.cache_data(ttl=30)
def list_accounts(limit: int = 500) -> List[str]:
    """Sample of accounts for the dropdown (read-only)."""
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name
                FROM (
                  SELECT name_orig AS name FROM transactions
                  UNION
                  SELECT name_dest AS name FROM transactions
                ) t
                WHERE name IS NOT NULL AND name <> ''
                ORDER BY name
                LIMIT %s;
                """,
                (limit,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


@st.cache_data(ttl=15)
def global_overview() -> Dict[str, Any]:
    """Small global overview using SQL (fast)."""
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transactions;")
            n = int(cur.fetchone()[0])

            cur.execute("SELECT COUNT(*) FROM transactions WHERE is_fraud = TRUE;")
            n_fraud = int(cur.fetchone()[0])

            cur.execute("SELECT MIN(step), MAX(step) FROM transactions;")
            step_min, step_max = cur.fetchone()

            cur.execute(
                """
                SELECT type, COUNT(*) AS cnt
                FROM transactions
                GROUP BY type
                ORDER BY cnt DESC
                LIMIT 10;
                """
            )
            top_types = [{"type": t, "cnt": int(c)} for (t, c) in cur.fetchall()]

        return {
            "n": n,
            "n_fraud": n_fraud,
            "fraud_rate": (n_fraud / n) if n else 0.0,
            "step_min": int(step_min) if step_min is not None else None,
            "step_max": int(step_max) if step_max is not None else None,
            "top_types": top_types,
        }
    finally:
        conn.close()


# --- Auto-tune detection params per account ---
@st.cache_data(ttl=60)
def suggest_detection_params(account: str) -> Dict[str, Any]:
    """Heuristics to propose good detection filters for a given account.

    Goal: avoid the user having to guess min_amount/window_steps. Uses fast SQL on the local sample.
    """
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            # Outgoing activity distribution for this account
            cur.execute(
                """
                SELECT
                    COUNT(*)::int AS nb_out,
                    COALESCE(AVG(amount), 0)::float8 AS avg_amt,
                    COALESCE(MAX(amount), 0)::float8 AS max_amt,
                    COALESCE(MIN(step), 0)::int AS step_min,
                    COALESCE(MAX(step), 0)::int AS step_max,
                    COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY amount), 0)::float8 AS p95
                FROM transactions
                WHERE name_orig = %s;
                """,
                (account,),
            )
            nb_out, avg_amt, max_amt, step_min, step_max, p95 = cur.fetchone()

        nb_out = int(nb_out or 0)
        avg_amt = float(avg_amt or 0.0)
        max_amt = float(max_amt or 0.0)
        p95 = float(p95 or 0.0)
        step_min = int(step_min or 0)
        step_max = int(step_max or 0)

        # --- Heuristics ---
        # min_amount: start from the 95th percentile of outgoing amounts, with sensible floors
        base = max(p95, avg_amt * 2.0, 50_000.0)
        if max_amt > 0:
            base = min(base, max_amt)  # don't propose above max

        # Round to nearest 1k for nicer UX
        min_amount = float(int(base / 1000.0) * 1000)
        if min_amount <= 0:
            min_amount = 50_000.0

        # window_steps: denser accounts => smaller window, sparse accounts => larger window
        if nb_out >= 30:
            window_steps = 5
        elif nb_out >= 10:
            window_steps = 10
        else:
            window_steps = 20

        # Provide context for UI
        span = max(0, step_max - step_min)
        density = (nb_out / span) if span > 0 else (float(nb_out) if nb_out else 0.0)

        return {
            "nb_out": nb_out,
            "avg_amt": avg_amt,
            "max_amt": max_amt,
            "p95": p95,
            "min_amount": min_amount,
            "window_steps": int(window_steps),
            "step_span": span,
            "density": float(density),
        }
    finally:
        conn.close()

# --- Helper: stats on risky outgoing operations for diagnostics ---
@st.cache_data(ttl=60)
def risky_out_stats(account: str) -> Dict[str, Any]:
    """Stats sur les opérations sortantes 'à risque' (TRANSFER/CASH_OUT) pour expliquer pourquoi il y a (ou non) des matchs."""
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)::int AS nb_risky_out,
                    COALESCE(MAX(amount), 0)::float8 AS max_risky_amount,
                    COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY amount), 0)::float8 AS p95_risky_amount
                FROM transactions
                WHERE name_orig = %s
                  AND type IN ('TRANSFER', 'CASH_OUT');
                """,
                (account,),
            )
            nb_risky_out, max_risky_amount, p95_risky_amount = cur.fetchone()

        return {
            "nb_risky_out": int(nb_risky_out or 0),
            "max_risky_amount": float(max_risky_amount or 0.0),
            "p95_risky_amount": float(p95_risky_amount or 0.0),
        }
    finally:
        conn.close()



def risk_badge(n_matches: int, max_amt: float) -> str:
    # simple + lisible pour une démo client
    if n_matches == 0:
        return "🟢 Faible"
    if n_matches >= 3 or max_amt >= 1_000_000:
        return "🔴 Élevé"
    return "🟠 Moyen"


# ----------------- Insights helpers -----------------

def fmt_eur(x: float) -> str:
    try:
        return f"{float(x):,.2f} €".replace(",", " ").replace(".", ",")
    except Exception:
        return str(x)


def risk_label(score: int) -> str:
    if score >= 80:
        return "Élevé"
    if score >= 50:
        return "Modéré"
    return "Faible"


def build_insights(kpi: dict, suspicious: dict, tx: Optional[dict] = None) -> Dict[str, Any]:
    """Insights automatiques explicables (règles simples).

    Retourne aussi un breakdown du score (règle -> points) pour une démo client.
    """

    out_ = (kpi or {}).get("out", {}) or {}
    in_ = (kpi or {}).get("in", {}) or {}

    nb_out = int(out_.get("nb_out", 0) or 0)
    total_out = float(out_.get("total_out", 0.0) or 0.0)
    avg_out = float(out_.get("avg_out_amount", 0.0) or 0.0)
    fraud_out = int(out_.get("fraud_out", 0) or 0)

    nb_in = int(in_.get("nb_in", 0) or 0)
    total_in = float(in_.get("total_in", 0.0) or 0.0)

    matches = (suspicious or {}).get("matches", []) or []
    min_amount = float((suspicious or {}).get("min_amount", 200000) or 200000)
    window_steps = int((suspicious or {}).get("window_steps", 10) or 10)

    # --- score (0-100) basé sur règles simples
    breakdown: List[Dict[str, Any]] = []
    score = 0

    def add_rule(rule: str, pts: int, triggered: bool, why: str) -> None:
        nonlocal score
        if triggered:
            score += pts
        breakdown.append({"rule": rule, "points": pts if triggered else 0, "triggered": triggered, "why": why})

    # R1 - Volume sortant très concentré
    add_rule(
        "Concentration des sorties",
        25,
        (nb_out <= 2 and total_out >= min_amount),
        f"nb_out ≤ 2 et total_out ≥ seuil ({fmt_eur(min_amount)})",
    )

    # R2 - Montant moyen sortant élevé
    add_rule(
        "Montant moyen sortant élevé",
        20,
        (avg_out >= min_amount and nb_out > 0),
        f"avg_out ≥ seuil ({fmt_eur(min_amount)})",
    )

    # R3 - Détections règles (matches)
    add_rule(
        "Au moins 1 match détecté",
        25,
        (len(matches) >= 1),
        f"≥ 1 transaction sortante ≥ {fmt_eur(min_amount)} dans une fenêtre de {window_steps} steps",
    )

    add_rule(
        "Plusieurs matchs (≥ 3)",
        10,
        (len(matches) >= 3),
        "≥ 3 transactions détectées avec les paramètres de détection",
    )

    # R4 - Fraude connue (label dataset)
    add_rule(
        "Fraude labellisée (dataset)",
        25,
        (fraud_out > 0),
        "Au moins une transaction sortante est marquée is_fraud=True (donnée simulée PaySim)",
    )

    # R5 - Déséquilibre entrants/sortants
    add_rule(
        "Déséquilibre (aucune entrée)",
        10,
        (total_in == 0 and total_out > 0),
        "total_in = 0 alors que total_out > 0",
    )

    # R6 - Signal transaction consultée (optionnel)
    tx_points = 0
    tx_reason = []
    if tx:
        ttype = (tx.get("type") or "").upper()
        amt = float(tx.get("amount", 0.0) or 0.0)
        is_fraud = bool(tx.get("is_fraud", False))
        oldb = float(tx.get("oldbalance_org", 0.0) or 0.0)
        newb = float(tx.get("newbalance_org", 0.0) or 0.0)

        if is_fraud:
            tx_points += 15
            tx_reason.append("transaction labellisée frauduleuse")
        if ttype in ("TRANSFER", "CASH_OUT") and oldb > 0 and newb == 0:
            tx_points += 10
            tx_reason.append("solde vidé sur TRANSFER/CASH_OUT")
        if amt >= min_amount:
            tx_points += 10
            tx_reason.append("montant ≥ seuil")

    add_rule(
        "Signal sur la transaction consultée",
        min(tx_points, 20),
        bool(tx and tx_points > 0),
        "; ".join(tx_reason) if tx_reason else "",
    )

    score = min(score, 100)

    bullets: List[str] = []
    bullets.append(
        f"Transactions sortantes: **{nb_out}** pour un total de **{fmt_eur(total_out)}** (moyenne: {fmt_eur(avg_out)})."
    )
    bullets.append(f"Transactions entrantes: **{nb_in}** pour un total de **{fmt_eur(total_in)}**.")

    if fraud_out > 0:
        bullets.append(f"⚠️ Fraude sortante observée (label dataset): **{fraud_out}** transaction(s).")

    if len(matches) == 0:
        bullets.append(
            f"Aucun transfert sortant > **{fmt_eur(min_amount)}** détecté dans une fenêtre de **{window_steps} steps**."
        )
    else:
        bullets.append(
            f"⚠️ **{len(matches)}** transfert(s) suspect(s) détecté(s) (seuil: {fmt_eur(min_amount)}, fenêtre: {window_steps} steps)."
        )

    # Micro insight transaction si on l'a
    if tx:
        ttype = tx.get("type")
        amt = float(tx.get("amount", 0.0) or 0.0)
        is_fraud = bool(tx.get("is_fraud", False))
        oldb = float(tx.get("oldbalance_org", 0.0) or 0.0)
        newb = float(tx.get("newbalance_org", 0.0) or 0.0)

        if is_fraud:
            bullets.append("🔴 La transaction consultée est **labellisée frauduleuse** dans le dataset (simulation).")
        if (ttype or "").upper() in ("TRANSFER", "CASH_OUT") and oldb > 0 and newb == 0:
            bullets.append("Pattern: **solde vidé** sur une opération à risque (TRANSFER/CASH_OUT).")
        if amt >= min_amount:
            bullets.append("Pattern: **montant très élevé** par rapport au seuil de détection.")

    next_actions: List[str] = []
    if score >= 80:
        next_actions = [
            "Mettre le compte en **revue prioritaire** (contrôle manuel).",
            "Vérifier la **cohérence des soldes** (old/new) sur les transferts détectés.",
            "Analyser les **contreparties fréquentes** (name_dest) et la concentration temporelle (steps).",
        ]
    elif score >= 50:
        next_actions = [
            "Contrôle ciblé des transferts > seuil et de la **fenêtre de steps**.",
            "Comparer ce compte à des comptes similaires (même type/volumes).",
        ]
    else:
        next_actions = [
            "Aucun signal fort : surveiller via alerting simple.",
            "Affiner le seuil si tu veux être plus sensible (mais + faux positifs).",
        ]

    title = f"Risque global: **{risk_label(score)}** (score {score}/100)"

    return {
        "score": score,
        "title": title,
        "bullets": bullets,
        "next_actions": next_actions,
        "breakdown": breakdown,
        "note": "Règles simples de démo (pas de ML) : seuils + fenêtres + patterns explicables.",
    }

# UI

st.set_page_config(page_title="PaySim • Fraud Monitoring (MCP Demo)", page_icon="🕵️", layout="wide")

st.title("🕵️ PaySim — Fraud Monitoring (démo client)")
st.caption("Interface Streamlit qui pilote le serveur MCP (HTTP) pour explorer comptes, transactions et signaux suspects.")

with st.sidebar:
    st.header("⚙️ Connexions")
    st.write(f"**MCP**: `{MCP_HTTP_URL}`")
    st.write(f"**DB**: `{DB_HOST}:{DB_PORT}/{DB_NAME}`")

    st.divider()
    st.header("🧭 Comment lire l’interface")
    st.markdown(
        """
- **Overview** : contexte global (volume, fraude, types).
- **KPI Compte** : ce que fait un compte (sorties/entrées, types, période).
- **Détection** : règles simples (montant min + fenêtre de steps).
- **Lookup Tx** : retrouver une transaction par ID.
        """.strip()
    )
    st.divider()
    st.header("🧭 Navigation")
    page = st.radio(
        "Aller à",
        ["📌 Overview", "📊 KPI Compte", "🚨 Détection", "🔎 Lookup Tx"],
        index=st.session_state.get("page_index", 0),
        key="page_radio",
    )
    st.session_state["page_index"] = ["📌 Overview", "📊 KPI Compte", "🚨 Détection", "🔎 Lookup Tx"].index(page)

ready = wait_mcp()
if not ready:
    st.error("Impossible de joindre le MCP. Vérifie `docker compose ps` et que le port 8765 est up.")
    st.stop()

# Top metrics
ov = global_overview()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Transactions (échantillon)", f"{ov['n']:,}".replace(",", " "))
c2.metric("Fraudes", f"{ov['n_fraud']:,}".replace(",", " "))
c3.metric("Taux fraude", f"{ov['fraud_rate']*100:.2f}%")
c4.metric("Steps", f"{ov['step_min']} → {ov['step_max']}")

st.divider()

# --- Navigation logic ---
PAGES = ["📌 Overview", "📊 KPI Compte", "🚨 Détection", "🔎 Lookup Tx"]
page = st.session_state.get("page_radio", PAGES[0])

# --- Overview page ---
if page == "📌 Overview":
    st.subheader("📌 Overview — Contexte & Objectifs")

    st.markdown(
        """
### 🎯 Objectif de la démo

Cette application est une **démo de monitoring fraude bancaire**, pensée comme un **outil de présentation client**.  
Elle montre comment, à partir de données de transactions brutes, on peut :

- explorer l’activité des comptes,
- détecter des comportements suspects,
- expliquer clairement les résultats (insights),
- sans modèle de Machine Learning complexe.

L’objectif n’est **pas** la performance algorithmique, mais la **lisibilité métier** et la **capacité d’analyse**.
"""
    )

    st.markdown(
        """
### 📊 Source des données

Les données proviennent du dataset **PaySim** (Kaggle) :
- Données **synthétiques** simulant des transactions financières réelles,
- Générées à partir de comportements observés dans des systèmes bancaires,
- Utilisées très fréquemment pour des démonstrations en **fraude / AML**.

⚠️ Il ne s’agit **pas de données réelles** : la fraude est *labellisée* dans le dataset.
"""
    )

    st.markdown(
        """
### ✂️ Pourquoi un échantillon de 50 000 lignes ?

Le dataset PaySim complet contient plusieurs **millions de transactions**.  
Pour cette démo, nous avons volontairement réduit le volume à **50 000 lignes** afin de :

- garantir une exécution fluide sur un **ordinateur personnel (Mac)**,
- éviter les temps de chargement longs dans PostgreSQL,
- conserver une **interface Streamlit réactive**,
- rester focalisé sur l’analyse plutôt que sur l’infrastructure lourde.

👉 Les raisonnements restent **exactement les mêmes** qu’à grande échelle.
"""
    )

    st.markdown(
        """
### 🧱 Architecture technique (simple mais réaliste)

Cette démo repose sur une architecture volontairement proche d’un contexte professionnel :

- **Docker**  
  → Isole chaque composant (base de données, serveur MCP)  
  → Garantit la reproductibilité de l'environnement

- **PostgreSQL**  
  → Stockage structuré des transactions  
  → Ajout d’un **ID technique** pour faciliter les recherches transactionnelles

- **Loader Python**  
  → Chargement contrôlé du CSV vers la base  
  → Transformation minimale (logique *ELT*)

- **Serveur MCP (HTTP / JSON-RPC)**  
  → Expose des capacités analytiques sous forme d’API  
  → KPI compte, détection de règles, lecture transaction

- **Streamlit**  
  → Interface orientée **utilisateur métier**  
  → Filtres simples, résultats lisibles, insights automatiques

👉 Cette séparation **UI / API / DB** est exactement ce qu’on retrouve en entreprise.
"""
    )

    st.markdown(
        """
### 🧭 Comment utiliser l’application

- **Overview**  
  → Comprendre le périmètre, le volume et les types de transactions

- **KPI Compte**  
  → Analyser le comportement global d’un compte (entrées / sorties)

- **Détection**  
  → Identifier des transactions suspectes via des règles simples

- **Lookup Tx**  
  → Analyser une transaction précise et son contexte compte
"""
    )

    st.divider()
    st.subheader("📈 Répartition des types de transactions")

    df_types = pd.DataFrame(ov["top_types"])
    st.dataframe(df_types, use_container_width=True, hide_index=True)

    st.info(
        "PaySim est une donnée **simulée**. Les règles de détection sont volontairement simples afin d’être compréhensibles par un public non technique.",
        icon="ℹ️",
    )

# --- KPI Compte page ---
elif page == "📊 KPI Compte":
    st.subheader("📊 KPI Compte")
    accounts = list_accounts(limit=800)
    if not accounts:
        st.warning("Aucun compte trouvé (table vide ?).")
        st.info("Astuce: vérifie que le loader a bien inséré des lignes (ex: `SELECT COUNT(*) FROM transactions;`).")
        # Ne pas bloquer le reste de l'app: les autres onglets doivent rester accessibles.
    else:
        colA, colB, colC = st.columns([2, 1, 1])
        with colA:
            name = st.selectbox("Compte", accounts, index=0)
        with colB:
            step_from = st.number_input("step_from", min_value=0, value=1, step=1)
        with colC:
            step_to = st.number_input("step_to", min_value=0, value=200, step=1)

        args = {"name": name, "step_from": int(step_from), "step_to": int(step_to)}
        res = mcp_call("tools/call", {"name": "get_account_kpi", "arguments": args}, _id=10)

        if "error" in res:
            st.error(res["error"]["message"])
        else:
            r = res["result"]
            out = r["out"]
            inn = r["in"]

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Sorties (#)", out["nb_out"])
            m2.metric("Sorties (total)", fmt_eur(out["total_out"]))
            m3.metric("Entrées (#)", inn["nb_in"])
            m4.metric("Entrées (total)", fmt_eur(inn["total_in"]))

            st.write("Répartition des types (sortants) :")
            df_top = pd.DataFrame(r.get("top_out_types", []))
            st.dataframe(df_top, use_container_width=True, hide_index=True)

            # Insights (basés uniquement sur les KPI)
            ins = build_insights(r, {"matches": [], "min_amount": 200000.0, "window_steps": 10}, tx=None)
            st.markdown("---")
            st.subheader("🧠 Insights automatiques")
            st.markdown(ins["title"])

            if ins["score"] >= 80:
                st.error("Risque ÉLEVÉ — intervention recommandée")
            elif ins["score"] >= 50:
                st.warning("Risque MODÉRÉ — contrôle conseillé")
            else:
                st.success("Risque FAIBLE — surveillance standard")

            st.markdown("### Ce que ça signifie")
            for b in ins["bullets"]:
                st.markdown(f"- {b}")

            st.markdown("### Actions recommandées")
            for a in ins["next_actions"]:
                st.markdown(f"- {a}")

            with st.expander("🧾 Détail du score (règles)", expanded=False):
                st.caption(ins.get("note", ""))
                df_b = pd.DataFrame(ins.get("breakdown", []))
                if not df_b.empty:
                    st.dataframe(df_b, use_container_width=True, hide_index=True)

# --- Détection page ---
elif page == "🚨 Détection":
    st.subheader("🚨 Détection (règles simples)")
    accounts = list_accounts(limit=800)
    if not accounts:
        st.warning("Aucun compte disponible pour la détection (table vide ?).")
        st.info("Va sur l'onglet Overview pour vérifier le volume, ou relance le loader.")
    else:
        # --- State init (avoid Streamlit warning: setting widget value after instantiation) ---
        if "det_min_amount" not in st.session_state:
            st.session_state["det_min_amount"] = 200_000.0
        if "det_window_steps" not in st.session_state:
            st.session_state["det_window_steps"] = 10
        if "det_apply_pending" not in st.session_state:
            st.session_state["det_apply_pending"] = False

        def request_apply_suggestions() -> None:
            # We only set a flag here; the actual update happens BEFORE widgets are rendered on the next rerun.
            st.session_state["det_apply_pending"] = True

        colA, colB, colC, colD = st.columns([2, 1, 1, 1])
        with colA:
            name = st.selectbox(
                "Compte à scanner",
                accounts,
                index=0,
                key="scan_name",
                on_change=request_apply_suggestions,
            )

        # Apply suggestions BEFORE rendering inputs (on the rerun right after a change/click)
        if st.session_state.get("det_apply_pending"):
            s = suggest_detection_params(st.session_state.get("scan_name", name))
            st.session_state["det_min_amount"] = float(s.get("min_amount", 200_000.0))
            st.session_state["det_window_steps"] = int(s.get("window_steps", 10))
            st.session_state["det_apply_pending"] = False

        with colB:
            min_amount = st.number_input(
                "Montant minimum",
                min_value=0.0,
                step=10_000.0,
                key="det_min_amount",
            )
        with colC:
            window_steps = st.number_input(
                "Fenêtre (steps)",
                min_value=1,
                step=1,
                key="det_window_steps",
            )
        with colD:
            st.button("⚡ Auto-ajuster", on_click=request_apply_suggestions)

        res = mcp_call(
            "tools/call",
            {"name": "detect_suspicious", "arguments": {"name": name, "min_amount": float(min_amount), "window_steps": int(window_steps)}},
            _id=20,
        )

        if "error" in res:
            st.error(res["error"]["message"])
        else:
            r = res["result"]
            matches = r.get("matches", [])
            df = pd.DataFrame(matches)

            if df.empty:
                st.success("Aucun match avec ces paramètres.")

                # Diagnostic expliqué (évite un message générique qui n'aide pas)
                stats = risky_out_stats(name)
                nb_risky = int(stats.get("nb_risky_out", 0))
                max_risky = float(stats.get("max_risky_amount", 0.0))
                p95_risky = float(stats.get("p95_risky_amount", 0.0))

                st.markdown("### Pourquoi aucun match ?")

                if nb_risky == 0:
                    st.info(
                        "Ce compte n'a **aucune** opération sortante de type **TRANSFER** ou **CASH_OUT** (les types les plus ciblés par les règles).\n\n"
                        "➡️ Dans ce cas, **aucun match n'est possible**, quel que soit le seuil ou la fenêtre.",
                        icon="ℹ️",
                    )
                else:
                    st.write(
                        f"Dans cet échantillon, ce compte a **{nb_risky}** opération(s) sortante(s) de type TRANSFER/CASH_OUT. "
                        f"Le **montant max** sur ces opérations est **{fmt_eur(max_risky)}** (p95 ≈ {fmt_eur(p95_risky)})."
                    )

                    if float(min_amount) > max_risky and max_risky > 0:
                        st.warning(
                            f"Ton **Montant minimum** ({fmt_eur(float(min_amount))}) est **au-dessus** du maximum observé ({fmt_eur(max_risky)}).\n\n"
                            f"➡️ Baisse le seuil (ex: {fmt_eur(max(0.0, p95_risky))} ou moins) pour obtenir des matchs.",
                            icon="⚠️",
                        )
                    else:
                        st.info(
                            "Le seuil semble compatible, donc l'absence de match vient probablement du **pattern** recherché par la règle "
                            "(ex: transferts concentrés dans une fenêtre) ou d'une fenêtre trop courte.\n\n"
                            "➡️ Essaie d'augmenter la **Fenêtre (steps)** (ex: 20 → 50) ou de baisser légèrement le seuil.",
                            icon="ℹ️"
                        )
            else:
                max_amt = float(df["amount"].max()) if "amount" in df else 0.0
                badge = risk_badge(len(df), max_amt)

                c1, c2, c3 = st.columns(3)
                c1.metric("Matches", len(df))
                c2.metric("Max amount", f"{max_amt:.2f}")
                c3.metric("Risque", badge)

                st.dataframe(df, use_container_width=True, hide_index=True)

            # Insights (KPI + Détection) — même si aucun match
            kpi_res = mcp_call(
                "tools/call",
                {"name": "get_account_kpi", "arguments": {"name": name, "step_from": 1, "step_to": 200}},
                _id=21,
            )
            if "error" not in kpi_res:
                ins = build_insights(kpi_res["result"], r, tx=None)
                st.markdown("---")
                st.subheader("🧠 Insights automatiques")
                st.markdown(ins["title"])

                # Add score explanation expander
                with st.expander("❓ Comment le score est calculé ?", expanded=False):
                    st.markdown(
                        """
Le score (**0 à 100**) est un **score explicable** construit par **addition de points**.

- Chaque règle a un nombre de points.
- Si la règle est déclenchée, ses points sont ajoutés.
- Le score final est **borné à 100**.

Le tableau ci-dessous montre **quelles règles ont été déclenchées** et **combien de points** elles ont apporté.
                        """.strip()
                    )
                    df_b = pd.DataFrame(ins.get("breakdown", []))
                    if not df_b.empty:
                        st.dataframe(df_b, use_container_width=True, hide_index=True)

                if ins["score"] >= 80:
                    st.error("Risque ÉLEVÉ — intervention recommandée")
                elif ins["score"] >= 50:
                    st.warning("Risque MODÉRÉ — contrôle conseillé")
                else:
                    st.success("Risque FAIBLE — surveillance standard")

                st.markdown("### Ce que ça signifie")
                for b in ins["bullets"]:
                    st.markdown(f"- {b}")

                st.markdown("### Actions recommandées")
                for a in ins["next_actions"]:
                    st.markdown(f"- {a}")

                with st.expander("🧾 Détail du score (règles)", expanded=False):
                    df_b = pd.DataFrame(ins.get("breakdown", []))
                    if not df_b.empty:
                        st.dataframe(df_b, use_container_width=True, hide_index=True)
            else:
                st.info("Impossible de calculer KPI pour ce compte (erreur MCP).")

# --- Lookup Tx page ---
else:
    st.subheader("🔎 Lookup Transaction")

    # --- Explanation: why we have an ID in DB even if CSV doesn't ---
    with st.expander("ℹ️ D'où vient l'ID de transaction ? (important)", expanded=True):
        st.markdown(
            """
Dans le fichier **PaySim (CSV Kaggle)**, il n'y a **pas** de colonne `id`.

👉 Pour la démo, on charge le CSV dans **PostgreSQL** et on ajoute un **ID technique** (aussi appelé *surrogate key*) :
- Dans la table `transactions`, la colonne `id` est un **auto-increment** (`SERIAL` / `IDENTITY`).
- À chaque insertion, Postgres attribue automatiquement un numéro : **1, 2, 3, ...**

Pourquoi c'est utile ?
- Ça permet d'avoir une **référence stable** pour lire une transaction via l'API MCP : `transaction/<id>`.
- C'est plus simple qu'une clé composée (ex: `step + name_orig + name_dest + amount + type`), qui peut être lourde et pas toujours unique.

⚠️ Attention :
- Si tu changes l'échantillon (ex: `paysim_small.csv`) ou si tu fais `docker compose down -v`, tu recrées la base → les IDs peuvent changer.
- Selon la façon dont l'échantillon est construit / inséré, les IDs peuvent aussi être **non continus** (il peut manquer des numéros).
            """
        )

    # Help the user pick a valid ID (IDs may not be continuous in a sample)
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(id), MAX(id) FROM transactions;")
            id_min, id_max = cur.fetchone()
            id_min = int(id_min) if id_min is not None else 1
            id_max = int(id_max) if id_max is not None else 1
    finally:
        conn.close()

    st.caption(
        f"Astuce : dans cet échantillon, les IDs existants sont généralement entre **{id_min}** et **{id_max}** (mais peuvent être non continus)."
    )

    # --- Lookup page: session_state initialization for lookup_tx_id
    if "lookup_tx_id" not in st.session_state:
        # Initialize from DB min id if possible
        try:
            conn = db_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT MIN(id) FROM transactions;")
                row = cur.fetchone()
                st.session_state["lookup_tx_id"] = int(row[0]) if row and row[0] is not None else 1
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # --- Helper for picking a random transaction id
    def pick_random_tx_id() -> None:
        """Pick an existing transaction id and store it in session_state."""
        try:
            conn = db_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM transactions ORDER BY random() LIMIT 1;")
                row = cur.fetchone()
                if row and row[0] is not None:
                    st.session_state["lookup_tx_id"] = int(row[0])
        finally:
            try:
                conn.close()
            except Exception:
                pass

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        st.number_input(
            "Transaction ID",
            min_value=1,
            step=1,
            key="lookup_tx_id",
        )
    with c2:
        st.button("🎲 ID au hasard", on_click=pick_random_tx_id)
    with c3:
        run_lookup = st.button("🔍 Lire la transaction", type="primary")

    if run_lookup:
        try:
            res = mcp_call("resources/read", {"uri": f"transaction/{int(st.session_state['lookup_tx_id'])}"}, _id=30)
        except Exception as e:
            st.error(f"Erreur lors de l'appel MCP: {e}")
            res = None

        if not res:
            st.info("Réessaie dans quelques secondes (le serveur MCP peut être en cours de démarrage).")
        elif "error" in res:
            st.warning(
                "Transaction introuvable pour cet ID. Essaie un autre ID (les IDs ne sont pas forcément continus)."
            )
            st.json(res)
        else:
            tx = res["result"]
            st.success("Transaction chargée.")
            st.json(tx)

            # Contextual deep-dive: KPI + detection on the origin account
            account = tx.get("name_orig")
            if account:
                kpi_res = mcp_call(
                    "tools/call",
                    {"name": "get_account_kpi", "arguments": {"name": account, "step_from": 1, "step_to": 200}},
                    _id=31,
                )
                det_res = mcp_call(
                    "tools/call",
                    {"name": "detect_suspicious", "arguments": {"name": account, "min_amount": 200000.0, "window_steps": 10, "max_rows": 10}},
                    _id=32,
                )

                if "error" not in kpi_res and "error" not in det_res:
                    ins = build_insights(kpi_res["result"], det_res["result"], tx=tx)
                    st.markdown("---")
                    st.subheader("🧠 Insights automatiques")
                    st.markdown(ins["title"])

                    if ins["score"] >= 80:
                        st.error("Risque ÉLEVÉ — intervention recommandée")
                    elif ins["score"] >= 50:
                        st.warning("Risque MODÉRÉ — contrôle conseillé")
                    else:
                        st.success("Risque FAIBLE — surveillance standard")

                    st.markdown("### Ce que ça signifie")
                    for b in ins["bullets"]:
                        st.markdown(f"- {b}")

                    st.markdown("### Actions recommandées")
                    for a in ins["next_actions"]:
                        st.markdown(f"- {a}")
                    with st.expander("🧾 Détail du score (règles)", expanded=False):
                        st.caption(ins.get("note", ""))
                        df_b = pd.DataFrame(ins.get("breakdown", []))
                        if not df_b.empty:
                            st.dataframe(df_b, use_container_width=True, hide_index=True)
                else:
                    st.info("Impossible de calculer KPI/Détection pour cette transaction (erreur MCP).")
                # Add breakdown expander after next_actions
                    with st.expander("🧾 Détail du score (règles)", expanded=False):
                        st.caption(ins.get("note", ""))
                        df_b = pd.DataFrame(ins.get("breakdown", []))
                        if not df_b.empty:
                            st.dataframe(df_b, use_container_width=True, hide_index=True)
            else:
                st.info("Pas de compte d'origine (name_orig) sur cette transaction.")

    else:
        st.info("Entre un ID puis clique sur **Lire la transaction**. Tu peux aussi cliquer sur **ID au hasard**.")