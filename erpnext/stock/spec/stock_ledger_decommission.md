# Stock Ledger Decommission (Phase 5)

The final phase of the stock ledger redesign: deleting the legacy valuation
machinery once the fold engine is authoritative everywhere. This document is
the checklist — nothing here may be deleted until its precondition holds.

## Preconditions (all of them, per company)

1. Fold authority covers every flow — the per-event fallbacks in
   `stock_fold_authority.try_fold` (lot-tracked rows, Standard Cost,
   reconciliations, landed cost, backdated inserts) have been replaced by
   fold-native handling, and the fallback counter is zero over the
   observation window.
2. Shadow diff (`stock_shadow.run`) has reported zero class (a)/(b)
   mismatches for the agreed window on every cut-over company, and GL
   reconciliation passes.
3. The Phase 4 lot restatement (`stock_restatement_preview.run` reviewed and
   applied) is complete — serial/batch valuation runs on allocations, not on
   `deprecated_serial_batch.py`.
4. The bypass log (`log_unrouted_stock_writes`) has stayed quiet long enough
   to trust that no third-party writer depends on legacy internals.

## What gets deleted

- `update_entries_after` and the repost machinery in `stock_ledger.py`
  (`repost_future_sle`, `repost_stock_ledger_entry`, `repost_stock_ledgers`,
  `get_reposting_data` and the gz checkpoint files)
- Repost Item Valuation doctype, its cron entries (`run_parallel_reposting`,
  `repost_entries`), and `spec/reposting.md`
- `deprecated_serial_batch.py`
- The drift/repair surface: `stock_ledger_invariant_check`,
  `stock_ledger_variance`, `incorrect_balance_qty_after_transaction`,
  `incorrect_stock_value_report`, `incorrect_serial_no_valuation`,
  `stock_and_account_value_comparison` repair buttons, and the remaining
  eleven-report drift family (each report is deleted only when the invariant
  it checks is enforced by the engine instead of reported on)
- `stock_balance.repost()` / `repost_stock()` console repair tools
- The hourly `rename_gle_sle_docs` SLE branch (facts don't rename)

## What stays

- The SLE table itself — as a read projection until every report and
  integration reads events/fold state directly (its own migration track)
- `stock_ledger_writer` / `bin_writer` — the chokepoints become the
  projection writers
- Stock Closing Entry — now the checkpoint/convergence barrier
- The fuzzing tool and shadow diff — permanent CI gates, not scaffolding

## Order of operations

Delete in reverse dependency order: repair tools first (they rebuild what no
longer drifts), then reposting, then the legacy fold, then the serial/batch
compat layer. Every deletion lands as its own commit so any surprise can be
reverted narrowly.
