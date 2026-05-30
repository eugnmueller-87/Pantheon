---
name: vps-watchdog
description: SSH into the Hetzner VPS (187.124.14.81), diagnose CPU/memory spikes, identify and kill runaway processes, and report container health. Use when VPS shows 100% CPU, containers are crash-looping, or after deploying code that might have introduced an infinite loop.
tools: Bash
---

# VPS Watchdog — Hetzner srv1638260

Diagnose and fix runaway processes on the Pantheon OS VPS.

**VPS**: `root@187.124.14.81` (Hetzner CX21, Ubuntu 24.04 LTS, 2 vCPU / 4 GB RAM)

## Step 1 — Snapshot current state

Run all diagnostics in one SSH call to minimise round-trips:

```bash
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@187.124.14.81 '
echo "=== TOP CPU PROCESSES ==="
ps aux --sort=-%cpu | head -15

echo ""
echo "=== CONTAINER STATUS ==="
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "=== CPU BY CONTAINER ==="
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"

echo ""
echo "=== ZEUS LOGS (last 30 lines) ==="
docker logs --tail 30 pantheon_zeus 2>&1

echo ""
echo "=== IBGATEWAY LOGS (last 20 lines) ==="
docker logs --tail 20 pantheon_ibgateway 2>&1
'
```

## Step 2 — Interpret results

### CPU culprits and fixes

| What you see | Cause | Fix |
|---|---|---|
| `python` process >80% CPU in host | ib_insync busy-wait / rogue script | `ssh root@187.124.14.81 'docker exec pantheon_zeus pkill -9 -f <script_name>'` |
| `pantheon_zeus` container >80% | Zeus pipeline loop | `ssh root@187.124.14.81 'docker restart pantheon_zeus'` |
| `pantheon_ibgateway` >80% | IBC login loop | `ssh root@187.124.14.81 'docker restart pantheon_ibgateway'` |
| Java process on host | Stale IB Gateway outside Docker | `ssh root@187.124.14.81 'pkill -f ibgateway'` |
| Multiple `python` procs | Old scripts from /tmp left running | `ssh root@187.124.14.81 "pkill -f '/tmp/.*\.py'"` |

### Container states and fixes

| State | Fix |
|---|---|
| `Exited` | `docker start <name>` |
| `Restarting` (crash-loop) | `docker logs <name> --tail 50` to find root cause |
| `Created` but never started | `docker start <name>` |
| Missing from list | `docker compose -f /opt/pantheon/docker-compose.prod.yml up -d <name>` |

## Step 3 — Kill runaway processes

### Kill everything suspicious and restart clean

Only do this if Step 2 confirms runaway processes:

```bash
ssh -o StrictHostKeyChecking=no root@187.124.14.81 '
echo "Killing stray Python scripts in /tmp..."
pkill -f "/tmp/.*\.py" 2>/dev/null && echo "Killed" || echo "None found"

echo "Restarting Zeus container..."
docker restart pantheon_zeus

echo "Waiting 10s for Zeus to stabilise..."
sleep 10

echo "=== POST-RESTART CPU ==="
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
'
```

### Nuclear option — full stack restart

Only if individual restarts don't help:

```bash
ssh -o StrictHostKeyChecking=no root@187.124.14.81 '
cd /opt/pantheon
docker compose -f docker-compose.prod.yml down --remove-orphans
sleep 5
docker compose -f docker-compose.prod.yml up -d
echo "Stack restarted. Waiting 30s..."
sleep 30
docker ps --format "table {{.Names}}\t{{.Status}}"
'
```

## Step 4 — Report

After fixes, report:
- Which process was the culprit (name, PID, CPU%)
- What was done to fix it
- Current CPU after fix (from `docker stats --no-stream`)
- Any container that is still unhealthy and needs attention
- Whether the root cause was a deployed script or a pipeline bug

## Prevention checklist

After every fix, check if the root cause was a script I deployed:
- `/tmp/close_ghost_positions.py` — used `ib.sleep()` which busy-waits. Use `time.sleep()` for delays outside ib_insync event loop.
- Any script that calls `ib_insync` functions must run inside the IB event loop and must call `ib.disconnect()` when done.
- Background threads that call `ib.sleep()` will spin at 100% — always use `time.sleep()` in threads.
