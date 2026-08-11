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

- `stock_ledger_writer` / `bin_writer` — the chokepoints become the
  projection writers
- Stock Closing Entry — now the checkpoint/convergence barrier. Before the
  report migration below, Stock Closing Balance must persist fold-resumable
  state (per-key layers and lot detail, not just totals).
- The fuzzing tool and shadow diff — permanent CI gates, not scaffolding

## The v17 breaking changes (decided 2026-08)

1. **All stock reports read Stock Events + Stock Closing Balance** — nearest
   checkpoint plus a folded tail. Running balances are computed on read;
   Stock Ageing takes layers from the fold instead of parsing stock_queue.
   No report queries tabStock Ledger Entry.
2. **The SLE table is removed, not kept as a projection.** The dual table is
   a v16 transitional necessity only. At v17 the facts table absorbs SLE's
   role: one narrow append-only table without qty_after_transaction,
   valuation_rate, stock_value, stock_value_difference, or stock_queue.
   Whether the surviving doctype keeps the name "Stock Ledger Entry"
   (ecosystem familiarity) or "Stock Event" is decided at the v17 boundary;
   the schema is the same. The hourly SLE rename job dies with it.
3. **Read-compat bridge for one deprecation window:** a SQL view named
   `tabStock Ledger Entry` over events + computed effects, so third-party
   SELECTs keep working while writes fail loudly. The view is deleted one
   release later.
4. **Stock/account mismatches are settled once at cutover** — shadow's
   class (a) findings become an explicit opening adjustment (GL adjustment
   or reconciliation). Afterwards GL derives from fold Effects, so
   stock-side drift is structurally impossible and no ongoing sync tooling
   exists.

## Order of operations

Delete in reverse dependency order: repair tools first (they rebuild what no
longer drifts), then reposting, then the legacy fold, then the serial/batch
compat layer. Every deletion lands as its own commit so any surprise can be
reverted narrowly.
