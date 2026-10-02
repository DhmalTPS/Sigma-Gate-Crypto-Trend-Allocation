# Change Control During the Live Period (Oct 4 – Oct 17)

The organizer allows redeploying during the 14 days but requires **"a consistent and
traceable commit history"** and **"no traces of manually called APIs"**. With 14 days
of noisy data, the bigger risk is *us* fitting noise. So every change is classified:

| Tier | Type | Allowed? | Evidence required |
|---|---|---|---|
| 1 | Infrastructure (restart, logging, monitoring) | always | none |
| 2 | Bug fix (wrong behaviour vs. documented design) | always | a failing test or log excerpt |
| 3 | Robustness improvement (e.g. better fill handling) | yes | backtest + shadow-book evidence |
| 4 | Parameter change | rarely | must survive `scripts/run_backtest.py` perturbation table; never on < 3 days of live data |
| 5 | Strategy replacement | only with very strong evidence | full research write-up |

## Procedure (no exceptions)

1. Write the change + test on a branch; run `pytest` and `scripts/run_backtest.py`.
2. Append an entry to `CHANGELOG.md`: id, hypothesis, change, evidence, expected effect,
   rollback condition.
3. Bump `version:` in `config/strategy.yaml` (it is stamped into every decision log line).
4. Commit, push, then on EC2: `git pull && sudo systemctl restart t105-bot`.
5. The bot re-reads positions from the exchange on start, so a restart never duplicates orders.

## Never

* Never call trading endpoints by hand with the **deployment** keys. `scripts/smoke_test.py`
  is hard-wired to the testing profile for this reason.
* Never edit code directly on the EC2 host — every running line must exist in git.
* Never change parameters because of one bad day.
