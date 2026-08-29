# Competitive scan

Other public repos tackling the same brief — payment-failure recovery. Kept so we
can re-check them as they evolve and decide what is worth adopting.

**Rule for this file:** record what a repo *does* and what is *verifiably absent*,
checked against its code rather than its README. Several of these repos claim
capabilities in prose that do not exist in the source.

Last checked: **29 Aug 2026**.

---

## Scoreboard against the brief's four requirements

> "Don't just identify the problem. Show measured money recovered across a batch,
> with compliant escalation, stopping rules, and an audit trail."

| | Measured recovery | Compliant escalation | Stopping rules | Audit trail |
|---|---|---|---|---|
| **Ours** | ✓ floor + naive + 20-seed sweep | ✓ 12 enforced rules | ✓ logged reasons | ✓ append-only, trigger-enforced |
| RecoverAI | ✗ diagnostic only | ~ approval gate real; caps not enforced | ✗ described in UI only | ✓ model + API + UI |
| prasanna781 | ✗ "potential" only | ✗ | ~ 3-retry rule, unenforced | ✗ |

No competitor scanned so far closes the loop from action to measured money. That
remains our clearest differentiator, and it is the half of the brief that is
explicitly called out.

### Scope for inclusion

On-track means **payment-failure recovery** for this brief. Adjacent
revenue-recovery tools that solve a different problem (B2B pipeline leakage,
churn prediction, dunning-as-a-service) are out of scope for the scoreboard —
they cannot be compared against the four requirements and including them
inflates the field. Note the idea if one has a good one; do not score it.

---

## 1. dhirajmahato67/recoverai-revenue-recovery

<https://github.com/dhirajmahato67/recoverai-revenue-recovery>

**"RecoverAI"** — a full-stack SaaS platform. The most serious competitor scanned.

- **Stack:** FastAPI + SQLAlchemy + Alembic, Next.js/React/Tailwind, Docker,
  Netlify + Render deploy configs. 226 files (140 backend, 59 frontend).
- **Scale:** 15 DB models with a repository layer, multi-tenant throughout
  (`merchant_id` everywhere + a tenant-isolation test), 8 REST endpoint groups,
  17 frontend routes, 86 test functions across 29 files, 6 docs files.
- **Pipeline:** ingest → risk detection (success-rate degradation by bank/method)
  → investigation (collectors, root cause, timeline, impact) → recommendation →
  human approval → recovery batch.

**Ahead of us on:**
- Production engineering — migrations, repositories, multi-tenancy, middleware,
  auth, containerisation, deployment.
- Frontend — a real 17-route app vs our single Streamlit page.
- **Risk *detection*** — they spot degradation as it emerges. We take a batch of
  already-failed payments as given and never detect anything.
- **Investigation depth** — evidence collectors, timeline reconstruction, a case
  file per incident with citations.
- Docs — architecture, API, deployment, demo guide.

**Behind us on:**
- **It never executes and never measures.** Their own validator emits:
  *"Phase 6 is diagnostic only. No financial retries or transfers have been
  executed."* Every AI action carries `can_execute=False`. Seed data has
  `successful_transactions=0` and a literal `expected_success_rate: 0.42`.
- **Guardrails are display strings.** `_build_safety_checks()` in
  `api/v1/recovery.py` returns four hardcoded entries with `passed=True` —
  "Single Retry Bound", "Max Exposure Cap", "Dynamic Rate Limiting", "Failure
  Rate Circuit Breaker". Nothing evaluates them.
- No baselines, no uplift, no robustness testing.

**Worth stealing (highest-value idea found in any repo):**
`services/ai/validator.py` — a `ResponseValidator` that checks LLM output
against a whitelist of real evidence IDs, drops unverifiable references, and
flags text claiming actions that never happened. We validate facts in outbound
*message copy* but have nothing equivalent for classifier output. See
"Candidate adoptions" below.

---

## 2. prasanna781/AI-Revenue-Recovery-Agent

<https://github.com/prasanna781/AI-Revenue-Recovery-Agent>

Streamlit app, same Buildathon. Clean and well-commented; honest about its limits.

- **Stack:** Streamlit + pandas + scikit-learn + OpenAI. ~18 source files.
- **Parts:** a weighted priority score (amount 40%, failure-reason recoverability
  25%, segment 15%, history 10%, retries 10%); a RandomForest predicting recovery
  probability; a rule-based decision agent choosing among 6 actions; an OpenAI
  message generator with a template fallback.
- **Zero tests.**

**Absent:** any measurement of recovered money (`grep` for
`recovered|uplift|baseline` in `app.py` returns nothing), any guardrails
(no opt-out, quiet hours, caps, idempotency, audit). Their pitch script's key
line still contains the placeholder: *"roughly **[X]%** of failed revenue as
recoverable."* Their headline metric is *potential* recovery.

**Already adopted from this repo (Aug 2026):**
1. **Escalation budget + priority ordering.** Their priority score exposed our
   real gap — we capped retries and messages but never human review cost. Now
   `MAX_HUMAN_ESCALATIONS_PER_RUN = 20`, with the queue worked highest-value-first.
2. **`customer_ltv` put to work.** It was generated and never read; it now feeds
   escalation priority (deliberately *not* recoverability).
3. **README.** We had none — a judge saw `CLAUDE.md`, a spec addressed to an AI.

**Deliberately not adopted:** the RandomForest. It trains on
`recovered_after_retry`, which their generator computes from a fixed formula, so
the model can only rediscover that arithmetic. Their README concedes the metrics
"describe internal consistency of the simulation." For us it would mean training
on our own frozen simulator — the exact circularity `SIMULATION_ASSUMPTIONS.md`
exists to prevent.

---

## Scanned and dropped

- **mscbuild/AI-Revenue-Recovery** — checked 29 Aug, removed as out of scope.
  Despite the name it is B2B pipeline-leakage detection from CRM deals and
  support tickets (data model: deals with `stage`, `value`,
  `last_activity_days`), not payment-failure recovery. Nothing to compare
  against the four requirements. One idea was carried over before dropping it —
  see candidate 2 below.

---

## Candidate adoptions — not yet built

Ranked by value to the judging bar. None of these are in the code today.

1. **Evidence grounding on LLM output** (from RecoverAI). Verify that classifier
   rationales reference only real transaction fields, and flag any claim of an
   action that did not occur. Closest thing to a real hallucination guard.
2. **PII redaction before the LLM call.** We send `failure_message` and customer
   context to the classifier unscrubbed. Scrub card-like and email-like patterns
   before anything leaves our process. Cheap, and it pairs well with the
   compliance narrative. (Idea noted from the since-dropped mscbuild repo, whose
   `SecurityGuardrail` regex-redacted PII from tool output before it reached the
   model. The gap in our code is real regardless of where the idea came from.)
3. **Risk detection as an upstream stage** (from RecoverAI). We start from a
   batch of known failures. Detecting *emerging* degradation — success rate
   dropping on a specific bank or method — is a genuinely missing capability,
   but it is a new build rather than an improvement, and out of scope before
   5 Sep.

**Explicitly rejected:** supervised recovery-probability models trained on
simulator-generated labels (see prasanna781 above).

---

## How to re-run this scan

```bash
gh repo clone <owner>/<repo> /tmp/scan -- --depth 1
# Check claims against code, not README:
grep -rniE "recovered|uplift|baseline|measur" <repo>/ --include=*.py   # is recovery measured?
grep -rniE "opt.?out|quiet.?hour|idempot|audit|cap\b|threshold" ...    # are guardrails real?
grep -rc "def test_" <repo>/tests/*.py                                 # test depth
```

The two questions that separate the field: **does it execute?** and **does it
measure against a baseline?**
