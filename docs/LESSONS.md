# Engineering Decisions & Lessons Learned

A running record of the non-obvious decisions made building Pantheon OS — what
we built, what we tore back out, and what each round taught us. Kept honest:
every entry below actually happened and is traceable in the git history.

The theme that recurs: **this is a small system, and most of the mistakes came
from adding infrastructure that a small system didn't need yet.** Removing
things turned out to be as much of the work as adding them.

---

## 1. Removing Kafka — weight without payoff at this scale

**What we did:** Added a Kafka event bus (KRaft mode, its own container, a
`kafka-python` dependency, a `core/kafka_bus.py` module, topic plumbing for
`raw_signals` and `decision_traces`) as "pre-live infrastructure hardening."
Then removed all of it.

**Why we added it:** On paper the case was reasonable — decouple signal
production from consumption, get replay/retention, stop one slow stage from
blocking the pipeline. The original design is preserved (marked **SUPERSEDED**)
in [`docs/HANDOVER_2026-05-23.md`](HANDOVER_2026-05-23.md) Task 6.

**Why we removed it:** None of those risks were real *at this scale*. Pantheon
runs one pipeline cycle every 15 minutes over a handful of signals. The volume
that justifies Kafka — high-throughput, many producers/consumers, genuine
backpressure — simply isn't there. What Kafka actually bought us was:

- one more container to run, monitor, and keep healthy on a 2-vCPU / 4-GB VPS,
- a broker dependency that could fail and take signal flow with it,
- conditional "is Kafka up? else fall back" branches threaded through ZEUS and
  Icarus that existed only to tolerate the thing we'd added.

Signals now flow directly **Supabase → Icarus → ZEUS**. Simpler, fewer moving
parts, one fewer failure domain.

**Lesson:** Match infrastructure to *current* load, not to an imagined future
one. "We might need replay someday" is not a reason to run a message broker
today. The decoupling we genuinely wanted (signal production with its own
failure domain) we got far more cheaply — see the Hermes story below.

---

## 2. Hermes — from external API to an owned, decoupled component

**What we did:** Signal production went through four shapes before it settled.

1. **External Railway API.** Hermes ran as a separate service on Railway;
   Icarus called it over HTTP. (`Wire Icarus directly to live Hermes API`)
2. **Railway → Hetzner.** Moved the service onto our own VPS to co-locate it.
   (`add hermes service to docker-compose — move from Railway to Hetzner`)
3. **Replaced the agent with a local fetcher.** Swapped the whole external
   service for an in-repo EDGAR + Finnhub fetcher writing straight to Supabase.
   (`replace Hermes with local EDGAR + Finnhub fetcher`)
4. **Decoupled into its own container.** Signal production became
   `run_hermes_producer.py` in its own container with its own failure domain —
   a producer crash can't take down the ZEUS pipeline, and vice versa.

**Why:** The external Railway dependency was the weakest link — a separate
host, separate auth, separate failure mode, and network latency on every
signal fetch, all for data we could fetch ourselves. Each step traded an
external dependency for owned, in-repo code.

**Where it landed:** Icarus is **Supabase-only**. The old Hermes HTTP path
still exists but is dead by default — kept behind `HERMES_FALLBACK_ENABLED`
as a deliberate, documented escape hatch ([`agents/icarus.py`](../agents/icarus.py)),
not lingering dead code.

**Lesson:** An external service you don't control is a liability until proven
otherwise. We got the decoupling we wanted (independent failure domains) by
splitting our *own* code into a separate container — not by depending on
someone else's host. And when you retire a path, either delete it or gate it
explicitly; don't leave it ambiguously half-alive.

---

## 3. Long-only execution — and the test that didn't get the memo

**What we did:** Changed Ares to be **long-only** — always BUY on a new signal,
never short. ZEUS can still set `side="SELL"` explicitly to close an existing
long, but the system never opens a short on a stock it doesn't hold.
(`fix(ares): long-only mode — always BUY on new signals, never short`)

**The bug it caused:** A pre-existing test still asserted that a
`SUPPLIER_DISRUPTION` signal produces a `SELL`. That was true under the old
direction-from-category logic; under long-only it's now a BUY. The test wasn't
updated in the same change, so it sat there red.

**Lesson:** A behavior change and its tests belong in the **same commit**. A
stale test isn't a harmless leftover — it either fails (noise) or, worse,
passes for the wrong reason and quietly encodes the old behavior as if it were
intended.

---

## 4. The 346-vs-356 trap — deleting code but not its tests

**What we did (wrong, then fixed):** When Kafka's module was removed,
`tests/test_kafka_bus.py` was left behind. It imported a module that no longer
existed, so it errored on collection — **13 errors + ~8 failures** that had
nothing to do with any real defect.

**Why it was dangerous:** The README badge and docs claimed a passing test
count (346, later edited to 377) that **was not actually true** — the suite
didn't pass clean. A green-looking badge over a red suite is worse than no
badge, because it stops you looking. We only caught it by running a real smoke
test, not by trusting the number.

**How we fixed it:** Deleted the orphaned test file, fixed the one genuinely
stale assertion (see #3), re-ran, and synced every stated count to the real
result: **356 passing, 0 failed, 0 errors.**

**Lesson:** Two of them.
1. **Delete code and its tests together.** An orphaned test of a deleted module
   is dead weight that masquerades as a failure.
2. **Trust the runner, not the badge.** Numbers in docs drift; `pytest` doesn't.
   If a badge claims a count, something should keep it honest — or it shouldn't
   claim a specific number at all.

---

## 5. Layered safety gates are a feature, not an annoyance

**What we observed:** During a smoke test, a deliberately high-conviction
NVIDIA earnings signal was **killed at Pythia** before it ever reached ZEUS:
`Stage SEED: tier 2 not allowed (need [1])`.

This was not a bug. At the **SEED** milestone (€100 starting capital), the
system only permits tier-1 (strongest-conviction) signals. A merely "good"
signal is correctly refused — the gate did exactly its job.

**Why it's worth recording:** It's a concrete demonstration of the design
principle the whole pipeline is built on — **every stage can independently stop
a trade**: compliance (Hades), macro suppression (Artemis), conviction/milestone
tier (Pythia), the LLM director (ZEUS), concentration caps, and a portfolio
drawdown circuit breaker (Argus). A signal has to survive *all* of them. When a
trade *doesn't* happen, that's usually the system working, not failing.

**The biggest gate of all — real money is *earned*, not configured.** Setting
`paper_trading: false` is not enough to risk a cent. There are THREE conditions,
all required (an *AND*, never an *OR*):

1. **The team reached the Senior tier.** Agents start at **Trainee L1** (zero
   experience) and climb four tiers — Trainee → Junior → Intermediate → Senior —
   by producing *successful trades*: 10 wins = +1 level, 10 levels = +1 tier
   (~300 wins to reach Senior). ([`core/seniority.py`](../core/seniority.py))
2. **An operator armed it.** Reaching Senior only *unlocks* real money; it stays
   disarmed until `ARM_REAL_MONEY=true`. A human makes the final call on real
   capital.
3. **The config requested it** (`paper_trading: false`).

If any one is missing, ZEUS forces paper mode at startup — it physically cannot
resolve the live broker port. (This caught a real bug earlier: agents were named
"Senior" *at the floor*, so a 0-experience system looked maxed out and the gate
read backwards. Now "Senior" is the top you earn, not the floor you're born at.)

> **Earlier mistake worth remembering:** the first version named the *floor*
> level "Senior" (level_int 0). A brand-new agent with zero closed trades
> displayed as "Senior" — maximum-sounding title, no experience — and a stubbed
> metric even auto-promoted Hades to "Managing Director" on signal count alone.
> The fix wasn't just gating promotions; it was inverting the ladder so rank is
> *earned upward* from Trainee, and making real-money unlock a tier you reach
> plus a switch a human flips.

**Lesson:** In a system that can move money, "it refused to act" is a success
state, and your tooling/logs should make *why it refused* obvious. The kill
reason being printed plainly is what turned a confusing "nothing happened" into
"oh, it's correctly gated at SEED."

---

## Recurring themes

- **Subtract before you add.** Most of these entries are about removing
  something (Kafka, the external Hermes dependency) that a small system didn't
  need. Restraint scaled better than infrastructure.
- **Match complexity to actual load.** Kafka, a separate signal service — both
  were solutions to problems we didn't have at our volume.
- **Tests and docs must track the code, or they lie.** A stale test and a stale
  badge both cost us real debugging time and credibility.
- **Make refusals legible.** The safest systems say no often; design so it's
  always clear *why*.
