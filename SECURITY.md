# Pantheon OS — Security Protocol

> **This file is binding for all agents and contributors.**
> Every rule here exists because a real violation could be catastrophic.
> This system manages real capital. A leaked IB password = real money at risk.

---

## The one rule that matters most

**Credentials never go in source code. Ever.**

Not in `docker-compose.prod.yml`. Not in Python source. Not in SQL seeds.
Not in Grafana dashboard JSON. Not even temporarily. Not even for testing.

All secrets live in `/opt/pantheon/.env` on the VPS and are injected at
container start via `env_file`. The `.env` file is `.gitignore`d and never
committed.

---

## Credential map — where every secret lives

| Credential | Where it lives | How services access it |
|---|---|---|
| `ANTHROPIC_API_KEY` | `/opt/pantheon/.env` | Injected into `pantheon_zeus` via `env_file` |
| `SUPABASE_URL` | `/opt/pantheon/.env` | Injected into `pantheon_zeus`, `pantheon_hermes`, `pantheon_dashboard` |
| `SUPABASE_SERVICE_ROLE_KEY` | `/opt/pantheon/.env` | Backend only — Zeus, Dashboard, Hermes. Never in frontend. |
| `SUPABASE_ANON_KEY` | `/opt/pantheon/.env` | Grafana datasource only (read-only) |
| `SUPABASE_DB_PASSWORD` | `/opt/pantheon/.env` | Never in source — injected into any direct-DB service |
| `IB_PASSWORD` | `/opt/pantheon/.env` | Injected into `pantheon_ibgateway` as `TWS_PASSWORD` |
| `IBC_TOTP_SECRET` | `/opt/pantheon/.env` | Injected into `pantheon_ibgateway` as `TWS_2FA_SECRET_XML` |
| `ZEUS_API_KEY` | `/opt/pantheon/.env` | HTTP bearer for `/run`, `/halt`, `/alert` endpoints |
| `HERMES_API_KEY` | `/opt/pantheon/.env` + Railway env vars | Icarus → Hermes authentication |
| `TELEGRAM_BOT_TOKEN` | `/opt/pantheon/.env` | Argus alert bot — never log this |
| `TELEGRAM_CHAT_ID` | `/opt/pantheon/.env` | Target chat — treat as sensitive (can be scraped) |
| `UPSTASH_REDIS_REST_TOKEN` | `/opt/pantheon/.env` + Railway env vars | Shared Zeus/Hermes Redis cache |
| `GRAFANA_ADMIN_PASSWORD` | `/opt/pantheon/.env` | Grafana admin login |
| `CLOUDFLARE_API_TOKEN` | `/opt/pantheon/.env` | DNS + CDN management |
| `FRED_API_KEY` | `/opt/pantheon/.env` | Macro data fetches |
| `IB_USERNAME` | `/opt/pantheon/.env` | IB account login — not a password but treat as PII |

**Nothing in this table belongs in a `.py`, `.yml`, `.json`, `.sql`, or `.md` source file.**

---

## Rules for `docker-compose.prod.yml`

The production compose file lives at `infra/hetzner/docker-compose.prod.yml`
and is committed. It must never contain real credential values.

```yaml
# ✅ CORRECT — reference from env_file
services:
  ibgateway:
    env_file: /opt/pantheon/.env
    environment:
      - TWS_USERID=${IB_USERNAME}
      - TWS_PASSWORD=${IB_PASSWORD}

# ❌ WRONG — hardcoded value in compose file
services:
  ibgateway:
    environment:
      - TWS_PASSWORD=MyRealPassword123
```

The `env_file` directive reads from `/opt/pantheon/.env` **on the server**.
The local `.env` at repo root is for local development only and is also
`.gitignore`d.

---

## Rules for Python source code

```python
# ✅ CORRECT — read from environment at runtime
import os
api_key = os.environ["ANTHROPIC_API_KEY"]
ib_password = os.getenv("IB_PASSWORD")

# ❌ WRONG — will be caught by pre-commit hook
api_key = "sk-ant-api03-..."
ib_password = "MyRealPassword"
```

The `SUPABASE_SERVICE_ROLE_KEY` is backend-only. It must never appear in:
- Dashboard frontend code (`dashboard/frontend/`)
- Any API response body
- Log output (mask it: `key[:8]...`)

---

## Rules for Supabase SQL migrations

Migration files in `infra/supabase/` are committed. They must never contain:
- Real API keys or tokens
- Seed data with real user PII
- Hardcoded passwords in function bodies

```sql
-- ✅ CORRECT — no credentials, references only role names
GRANT SELECT, INSERT ON public.signals TO service_role;

-- ❌ WRONG — would expose a real token in git history
INSERT INTO config (key, value) VALUES ('api_key', 'sk-ant-api03-...');
```

### New table boilerplate (mandatory from Oct 30 2026)

Every `CREATE TABLE` needs explicit GRANTs or PostgREST returns 403.
Use `infra/supabase/TEMPLATE_migration_NNN.sql` as your starting point.

---

## IB-specific rules (highest risk)

Interactive Brokers credentials control **real money**. Extra caution:

1. **`IB_PASSWORD` and `IBC_TOTP_SECRET` are never logged** — mask them everywhere
2. **Paper vs live ports** — Zeus connects to port `4004` (paper). Live port `4003`
   is wired but disabled. Never change `IB_PORT` to live in prod without an
   explicit Vault unlock + human confirmation
3. **`mock_execution`** — when `true` in `config/settings.json`, Ares skips
   the actual IBKR order submission. Always verify this flag before pushing
   config changes
4. **Vault money** never moves autonomously — Zeus is hard-blocked from touching
   the Vault account regardless of what any signal or LLM says

---

## Rotation procedure

When a credential is compromised or needs rotation:

### 1. API keys (Anthropic, FRED, Cloudflare, Upstash)

```bash
# Generate at the service provider, then update the VPS:
ssh root@187.124.14.81 "nano /opt/pantheon/.env"
# Edit the key, save, then restart affected services:
docker compose -f /opt/pantheon/docker-compose.prod.yml up -d --no-deps zeus hermes
```

### 2. Supabase service role key

```bash
# 1. Rotate in Supabase Dashboard → Settings → API → Rotate service_role key
# 2. Update VPS:
ssh root@187.124.14.81 "nano /opt/pantheon/.env"   # update SUPABASE_SERVICE_ROLE_KEY
# 3. Update Railway env vars for Hermes (Railway Dashboard → Service → Variables)
# 4. Restart zeus + hermes:
docker compose -f /opt/pantheon/docker-compose.prod.yml up -d --no-deps zeus hermes dashboard
```

### 3. IB password

```bash
# 1. Change at IBKR Account Management
# 2. Update VPS:
ssh root@187.124.14.81 "nano /opt/pantheon/.env"   # update IB_PASSWORD
# 3. Restart ibgateway (will re-authenticate with new password):
docker compose -f /opt/pantheon/docker-compose.prod.yml up -d --no-deps ibgateway
```

### 4. ZEUS_API_KEY

```bash
# Generate new key:
python3 -c "import secrets; print(secrets.token_hex(32))"
# Update VPS .env, then restart zeus:
ssh root@187.124.14.81 "nano /opt/pantheon/.env"
docker compose -f /opt/pantheon/docker-compose.prod.yml up -d --no-deps zeus
# Update n8n workflow HTTP node headers to use new key
# Update local .env (dev only) — never commit
```

### 5. If a credential is accidentally committed

**Treat it as compromised immediately. Rotate first, purge second.**

```bash
# 1. Rotate the credential NOW (see above) — this makes the exposed value useless
# 2. Purge from git history:
pip install git-filter-repo
git filter-repo --replace-text <(echo "EXPOSED_VALUE==>REDACTED") --force
git push origin --force --all
# 3. Force all collaborators to re-clone or hard-reset:
git fetch origin && git reset --hard origin/main
```

---

## Automated enforcement

### Pre-commit hook

Installed at `.git/hooks/pre-commit`. Install after cloning:

```bash
bash scripts/install-hooks.sh
```

**Blocks commits containing:**
- Anthropic API keys (`sk-ant-api03-`)
- Supabase service role keys (`sb_secret_`)
- IB/IBKR passwords (pattern-matched against known format)
- Upstash tokens (`AAIg` prefix, base64 Redis tokens)
- Telegram bot tokens (`\d+:AAH` pattern)
- TOTP secrets (base32 strings > 20 chars in credential context)
- Real `.env` files (allows `.env.example`)

### Env audit script

Run on the server to verify all required keys are present before deploying:

```bash
python3 scripts/check_env.py
```

---

## What agents must do

If you are an AI agent (Zeus, Icarus, Claude, etc.) working on this codebase:

1. **Never write a credential value** into any tracked file. Use `os.environ`
   references in Python, `${VAR_NAME}` substitution in compose files.

2. **Never log credential values.** If you must log a key for debugging,
   log only the first 8 characters: `key[:8] + "..."`.

3. **Never move Vault money.** The Vault account is read-only to all
   automated agents. There is no override.

4. **Never change `IB_PORT` to a live port** without explicit human
   confirmation in the chat. Paper = 4002/4004. Live = 4001/4003.

5. **Before pushing**, run `bash scripts/install-hooks.sh` once, then the
   pre-commit hook runs automatically on every `git commit`.

6. **If you generate a new credential** (API key, token, secret), write it to:
   - `/opt/pantheon/.env` on the VPS (via SSH)
   - Railway env vars (via dashboard) for Hermes/Railway services
   Never to any file that gets committed.

---

## Current credential status

| Credential | Status | Last rotated |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ Active | — |
| `SUPABASE_SERVICE_ROLE_KEY` | ✅ Active (sb_ format) | — |
| `IB_PASSWORD` | ✅ Active | — |
| `IBC_TOTP_SECRET` | ✅ Active | — |
| `ZEUS_API_KEY` | ✅ Active | — |
| `HERMES_API_KEY` | ✅ Active | — |
| `TELEGRAM_BOT_TOKEN` | ✅ Active | — |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ Active | — |
| `CLOUDFLARE_API_TOKEN` | ✅ Active | — |
