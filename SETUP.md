# Pantheon OS — Setup Guide

Everything you need to create accounts, get API keys, and deploy.
Code is already written. You just fill in the blanks.

---

## What you need to set up

| Account | Cost | Time | What it does |
|---------|------|------|-------------|
| Supabase | Free | 5 min | Database — all trades, signals, audit trail |
| Telegram Bot | Free | 3 min | Alerts — milestone crossings, kill switch, daily report |
| Anthropic | Pay-per-use | 2 min | ZEUS LLM reasoning (Claude Haiku ~$0.001/signal) |
| Hetzner CX21 | €6/mo | 10 min | Server — runs ZEUS 24/7 |
| GitHub Secrets | Free | 5 min | CI/CD — auto-deploys on every push |
| Cloudflare Pages | Free | 5 min | Hosts the React dashboard |

**Total time: ~30 minutes**

---

## Step 1 — Supabase (Database)

1. Go to **supabase.com** → Sign in with your existing account
2. Click **New Project**
   - Name: `pantheon-os`
   - Database password: choose a strong one, **save it** — you'll need it
   - Region: `eu-central-1` (Frankfurt — same region as Hetzner)
3. Wait ~2 minutes for the project to create
4. Go to **SQL Editor** → **New query**
5. Paste and run `infra/supabase/001_schema.sql` (creates all 11 tables)
6. Paste and run `infra/supabase/002_seed_ticker_map.sql` (seeds 43 DAX/NYSE tickers)
7. Paste and run `infra/supabase/003_rpc.sql` (creates analytics functions for Grafana)
8. Go to **Settings → API** and copy:
   - `URL` → `SUPABASE_URL` in your `.env`
   - `service_role` key → `SUPABASE_SERVICE_ROLE_KEY` (keep this secret — full DB access)
   - `anon` key → `SUPABASE_ANON_KEY` (safe for frontend)
9. Go to **Settings → Database → Connection string → URI**
   - Extract: host, port, database, user, password → fill in `SUPABASE_DB_*` in `.env`

---

## Step 2 — Telegram Bot (Alerts)

1. Open Telegram → search for **@BotFather**
2. Send `/newbot`
3. Name: `Pantheon OS` — username: `pantheon_yourname_bot`
4. BotFather gives you a token like `7123456789:AAH...` → `TELEGRAM_BOT_TOKEN`
5. Start a chat with your new bot (just send `/start`)
6. Go to: `https://api.telegram.org/bot{YOUR_TOKEN}/getUpdates`
7. Find `"chat":{"id":YOUR_ID}` → `TELEGRAM_CHAT_ID`

You will receive alerts for:
- Every milestone crossing with vault transfer amount
- Emergency halt (kill switch triggered)
- Agent failure + auto-recovery
- Daily QuantStats performance report

---

## Step 3 — Anthropic API (ZEUS Reasoning)

1. Go to **console.anthropic.com** → API Keys → Create key
2. Copy key → `ANTHROPIC_API_KEY` in `.env`
3. Add a credit balance (start with $5 — Claude Haiku costs ~$0.001 per signal decision)

ZEUS uses Claude Haiku for every trade decision. At SEED stage with minimal trades,
$5 lasts months.

---

## Step 4 — Hetzner Server (24/7 Hosting)

1. Go to **hetzner.com/cloud** → Create account
2. Create a new project: `Pantheon`
3. Add server:
   - Location: **Nuremberg** or **Falkenstein** (EU, close to XETRA)
   - Image: **Ubuntu 24.04**
   - Type: **CX21** (2 vCPU, 4GB RAM) — €6.29/mo
   - SSH Key: add your public key (`~/.ssh/id_rsa.pub`)
4. Note your server IP → `HETZNER_SERVER_IP` in `.env`
5. SSH into the server and run the bootstrap script:
   ```bash
   ssh root@YOUR_SERVER_IP
   curl -fsSL https://raw.githubusercontent.com/eugnmueller-87/Pantheon/main/infra/hetzner/setup.sh | bash
   ```
6. Copy your `.env` to the server:
   ```bash
   scp .env root@YOUR_SERVER_IP:/opt/pantheon/.env
   scp infra/hetzner/docker-compose.prod.yml root@YOUR_SERVER_IP:/opt/pantheon/
   scp infra/hetzner/nginx.prod.conf root@YOUR_SERVER_IP:/opt/pantheon/
   ```
7. Copy Grafana config:
   ```bash
   scp -r infra/hetzner/grafana root@YOUR_SERVER_IP:/opt/pantheon/
   ```
8. Start everything:
   ```bash
   ssh root@YOUR_SERVER_IP
   systemctl start pantheon
   docker compose -f /opt/pantheon/docker-compose.prod.yml logs -f
   ```

---

## Step 5 — GitHub Secrets (CI/CD Auto-Deploy)

Go to your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these 8 secrets:

| Secret name | Where to get it |
|-------------|----------------|
| `HETZNER_SERVER_IP` | Your Hetzner server IP |
| `HETZNER_SSH_KEY` | Contents of `~/.ssh/id_rsa` (private key) |
| `VITE_SUPABASE_URL` | Supabase → Settings → API → URL |
| `VITE_SUPABASE_ANON_KEY` | Supabase → Settings → API → anon key |
| `VITE_WS_URL` | `wss://pantheon.yourdomain.com/ws` |
| `VITE_API_URL` | `https://pantheon.yourdomain.com/api` |
| `CLOUDFLARE_API_TOKEN` | Cloudflare → Profile → API Tokens → Create Token |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare → Dashboard → right sidebar |

After setting secrets: push any commit to `main` → CI runs tests → deploys automatically.

---

## Step 6 — Cloudflare Pages (Dashboard Hosting)

1. Go to **dash.cloudflare.com** → Pages → Create a project
2. Connect to GitHub → select `eugnmueller-87/Pantheon`
3. Build settings:
   - Framework preset: `None`
   - Build command: `cd dashboard/frontend && npm ci && npm run build`
   - Build output directory: `dashboard/frontend/dist`
4. Environment variables (add in Cloudflare Pages settings):
   - `VITE_SUPABASE_URL` = your Supabase URL
   - `VITE_SUPABASE_ANON_KEY` = your Supabase anon key
   - `VITE_WS_URL` = `wss://pantheon.yourdomain.com/ws`
   - `VITE_API_URL` = `https://pantheon.yourdomain.com/api`
5. Deploy → get your `.pages.dev` URL

---

## Step 7 — Accessing Grafana

Once the server is running:

- Grafana: `https://pantheon.yourdomain.com/grafana/`
- Login: `admin` / the password you set in `GRAFANA_ADMIN_PASSWORD`
- Dashboard auto-loads: **Pantheon OS — Trading Overview**

What you see immediately:
- Live equity curve (updates every 10s)
- Current drawdown gauge (red when approaching kill switch)
- Win rate by signal category (which signal types actually work)
- Agent health table (all 8 agents, green/yellow/red)
- Last 50 trades with P&L
- Monthly returns bar chart

---

## Step 8 — First Run Check

After everything is running, verify:

```bash
# Check all containers are healthy
ssh root@YOUR_SERVER_IP
docker compose -f /opt/pantheon/docker-compose.prod.yml ps

# Watch live logs
docker compose -f /opt/pantheon/docker-compose.prod.yml logs -f zeus

# Test ZEUS health endpoint
curl https://pantheon.yourdomain.com/health

# Test dashboard API
curl https://pantheon.yourdomain.com/api/status

# Check Supabase data is flowing
# → Supabase Dashboard → Table Editor → agent_health
# → Should see rows appearing every 60 seconds
```

---

## What happens automatically once live

| Schedule | What runs |
|----------|-----------|
| Every pipeline cycle | Trades → Supabase `trades` table |
| Every 5 seconds | Argus equity snapshot → `portfolio_state` |
| Every 60 seconds | Watchdog → `agent_health` |
| Every pipeline run | Decision trace → `decision_traces` |
| Daily (Apollo cycle) | QuantStats report → Telegram |
| Every milestone crossing | Vault alert → Telegram with `/confirm_vault` |

---

## Two-Account Bank Setup (Vault Architecture)

This is important. Set up before going live with real money.

**Engine Account (IBKR):**
- Open Interactive Brokers account: ibkr.com
- Start with **Paper Trading** (free, no real money)
- When ready for live: fund with your starting capital
- ZEUS has full automated access to this account

**Vault Account (N26 or Revolut):**
- Open a separate free account — N26 (Germany) or Revolut
- This is physically separate from IBKR
- ZEUS **never** has access to this account
- When Telegram sends a vault alert: manually transfer the amount
- Never transfer money back to the Engine account

This two-account structure is the Iron Law in practice.
Even if ZEUS makes a catastrophic error, the Vault is physically unreachable.

---

## Cost Summary (Once Live)

| Service | Monthly cost |
|---------|-------------|
| Hetzner CX21 | €6 |
| Supabase Free | €0 (upgrade to Pro €25 when going live) |
| Cloudflare Pages | €0 |
| Anthropic (Claude Haiku) | ~€1–5 depending on signal volume |
| Telegram | €0 |
| **Total development** | **€7–11/mo** |
| **Total live trading** | **€32–36/mo** |

At 5% monthly return on €100 starting capital: €5/mo return vs €7/mo costs.
You break even infrastructure-wise around €200 engine equity.
This is why the SEED stage focuses on learning, not profit — the system pays for itself at SPRINT.
