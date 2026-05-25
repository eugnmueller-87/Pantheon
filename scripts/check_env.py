"""
Run on the server to see what's missing from /opt/pantheon/.env
Usage: python3 scripts/check_env.py
"""
REQUIRED = [
    "ANTHROPIC_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "HERMES_API_KEY",
    "IB_HOST",
    "IB_PORT",
    "ZEUS_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

found = {}
with open("/opt/pantheon/.env") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            found[k.strip()] = v.strip()

print("\n=== Pantheon .env audit ===\n")
missing = []
for k in REQUIRED:
    v = found.get(k, "")
    if v:
        print(f"  OK      {k}")
    else:
        print(f"  MISSING {k}")
        missing.append(k)

if missing:
    print(f"\n{len(missing)} key(s) missing. Add them to /opt/pantheon/.env then restart zeus:")
    print("  docker compose -f /opt/pantheon/docker-compose.prod.yml up -d --no-deps zeus")
else:
    print("\nAll keys present.")
