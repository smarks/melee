# Engine tests — and the regression safety net

Most tests here pin one rule or one method. Two files are different in kind: they
assert the *game stays correct*, not merely that the code ran.

## The safety net (#231)

- **`engine/invariants.py`** — `assert_state_invariants(state, profile, *, context, phase)`
  and `assert_log_truthful(results, *, context)`. One source of truth for what must
  NEVER happen in a fight: no same-side damage, a truthful combat log (hit-word ⟺
  `result.hit`; an auto-hit narrated "unavoidable" with no bogus roll; a claimed hit
  the dice deny is caught), no double action, legal positions/facing/pools, no
  dangling `_pending`, and missile sanity (a reloading weapon never has a queued shot).

- **`engine/tests/test_soak.py`** — plays many randomized AI-vs-AI full games across
  BOTH rule profiles (Classic + Tarmar) and varied team counts/sizes, checking the
  invariants after every action and the log after every combat phase. CI runs a
  bounded count (default 40 games, ~1s); `MELEE_SOAK=500 pytest` runs a larger local
  sweep, as does `pytest -m slow`. On a break it prints the seed so the exact game
  replays. The same file also pins, by name, every bug class we already shipped green
  (missile friendly fire, auto-hit narration, connects-on-a-miss, the resolve-gate
  `must_attack ⇒ queueable` relation, no-wasted-shot, seed determinism).

## How the net grows — the rule

**Every future bug adds either a new invariant in `engine/invariants.py` or a
failing-case test in `test_soak.py` (or a sibling).** A fix without one of those two
is incomplete: it proves the symptom is gone today, not that it can't silently come
back. Reproduce the bug as a red check first, then fix until it is green.
