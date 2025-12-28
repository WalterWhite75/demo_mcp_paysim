# Demo MCP – PaySim (Fraud Analysis)

## 1. Contexte du projet

Ce projet est une **démo technique de bout en bout** autour d’un cas de **détection de fraude financière**, basée sur le dataset **PaySim** (Kaggle).

L’objectif n’est **pas** de faire du machine learning avancé, mais de montrer :
- comment structurer une pipeline data réaliste,
- comment exposer des données et des analyses via une API,
- comment construire une interface claire pour un utilisateur métier.

Le projet est conçu pour être :
- reproductible,
- compréhensible,
- exploitable en démo client ou académique.

---

## 2. Source des données

**Dataset d’origine :**
- PaySim – Financial Fraud Detection  
- Source : https://www.kaggle.com/datasets/ealaxi/paysim1

Le dataset original contient **plus de 6 millions de transactions**, ce qui est trop lourd pour une démo locale.

👉 Pour cette raison, nous utilisons :
- un **sous-ensemble de 50 000 lignes** (`paysim_small.csv`)
- sélectionné pour conserver :
  - des transactions frauduleuses,
  - plusieurs types d’opérations,
  - des comportements variés de comptes.

---

## 3. Description rapide des données

Chaque ligne représente **une transaction**.

Colonnes principales :
- `step` : pas de temps simulé (temps discret)
- `type` : type d’opération (`TRANSFER`, `PAYMENT`, etc.)
- `amount` : montant de la transaction
- `name_orig` : compte émetteur (sortie d’argent)
- `name_dest` : compte bénéficiaire (entrée d’argent)
- `oldbalance_org`, `newbalance_org` : solde avant / après (émetteur)
- `oldbalance_dest`, `newbalance_dest` : solde avant / après (destinataire)
- `is_fraud` : fraude avérée (ground truth)
- `is_flagged_fraud` : flag automatique (très rare dans PaySim)

⚠️ Le dataset original **ne contient pas d’identifiant de transaction**.  
Un `id` est généré lors de l’insertion en base pour permettre :
- le lookup transaction,
- les démonstrations API/UI.

---

## 4. Architecture du projet

```text
demo_mcp_paysim/
├── docker-compose.yml
├── README.md
├── .gitignore
├── db/
│   └── schema.sql
├── data/
│   └── paysim_small.csv
├── loader/
│   ├── load_paysim.py
│   └── reduce_paysim.py
├── server/
│   └── mcp_server_paysim.py
├── ui/
│   └── app.py
├── scripts/
│   └── test_calls.py
└── output/
    └── mcp_server.log
'''
---
## 5. Outils utilisés et leur rôle

### Docker & Docker Compose
- Orchestration complète du projet
- Permet de lancer :
  - PostgreSQL
  - le loader de données
  - le serveur MCP
- Assure la reproductibilité sur n’importe quelle machine

### PostgreSQL
- Stockage structuré des transactions
- Indexation pour :
  - lookup transaction
  - agrégations par compte
  - détection rapide

### Python
- Ingestion des données (`load_paysim.py`)
- API MCP (`mcp_server_paysim.py`)
- Tests automatisés (`test_calls.py`)
- Interface Streamlit (`ui/app.py`)

### MCP (Model Context Protocol)
- Exposition des données via :
  - resources (`account`, `transaction`)
  - tools (KPI, détection)
- Séparation claire :
  - données
  - logique métier
  - interface utilisateur

### Streamlit
- Interface utilisateur
- Navigation par onglets :
  - Overview
  - Account KPI
  - Fraud Detection
  - Lookup Transaction

---

## 6. Détection de fraude (important)

⚠️ **La détection repose sur des règles simples** (pas de ML).

Pourquoi ?
- Objectif pédagogique et démonstratif
- Transparence totale sur les critères
- Interprétable par un utilisateur non technique

Exemples de règles :
- montant sortant élevé,
- fréquence rapprochée des transferts,
- incohérences de soldes.

👉 Le score affiché est un **score heuristique**, basé sur :
- le montant,
- le contexte du compte,
- la temporalité des opérations.

---

## 7. Lancer le projet

### Prérequis
- Docker
- Docker Compose

### Lancement
```bash
docker compose down -v
docker compose up -d
