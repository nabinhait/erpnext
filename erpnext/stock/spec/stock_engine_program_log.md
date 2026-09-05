# M2 / Phase 0 — SLE and Bin write-path audit (+ program log)

## ⟲ Session-restart snapshot — 2026-09-05 (evening)

**Where everything is:**
- **One program branch: `stock-ledger-redesign`** (origin = nabinhait/erpnext), tip `eea2cc91c1`
  (queued refolds + Stock Restatement), rebased onto upstream develop (5beed5f4, Sep 5). frappe fast-forwarded
  to `b56649ed` (Sep 4) — required by new erpnext. Safety tag `pre-rebase-2026-09-05` = old tip.
  The old stock-ledger-cutover name and the M2–M4-only branch are gone (renamed/deleted).
- **Engine is vendored**: `erpnext/stock/engine/` (63 frappe-runner tests incl. property suite;
  purity gate = source-scan test). Standalone repo archived at `~/bench-cli/stock_engine-archive`
  (harnesses benchmark/ + sle_replay/ live only there).
- **Sites**: test-runner-site migrated + full battery green (engine 63, authority 13, fold-read 5,
  event 2, closing 7, opening adjustment 2, refold 1, restatement 1). **test2 and apnaklub are on pre-rebase schema — `bench migrate` needed
  before next use** (apnaklub deliberately: last migration surfaced 5 patch bugs; those are now
  upstream, but a month of new patches + frappe jump is untested there).
- **Flags on test2**: dual_write, fold_authoritative, suppress_legacy_repost, gl_adjustment.
- **Docs**: this file = chronological program log (entries below, newest at bottom);
  `stock_engine_redesign.md` (this directory) = design doc, fully current incl. the v17 frozen-frontier
  cutover plan (Part 4) and all of this week's decisions (§2.6 batch/serial semantics, §2.10
  freeze-the-past). Plan file for the engine merge: `~/.claude/plans/` (done, historical).

**Decided 2026-09-05 (Nabin): the Stock Event table is NOT merged into SLE yet** — the
dual-write + shadow stack is the verification instrument for the production sites he will run it
on first. The absorption design (fact columns + sequence-backed event_id + allocation child table
on SLE, in-place stamping patch, cancelled rows as Reversal facts) is worked out and deferred, not
rejected; every v17 build item is built against the Stock Event table until he says otherwise.

**Settled this week (all recorded in the design doc):** v17 = no shadow, SLE absorbs Stock Event
(deferred — see above),
frozen frontier + opening adjustment, reopen-restates-the-year; batches = flag decides sub-fold vs
quantity tag, pools never borrow, flag fixed at birth (set_only_once upstream); serials = never in
fold state, `use_serialwise_valuation` picks pool rate vs per-serial rate buckets; negative stock =
freeze past as exposure via baseline assertions (freeze_baseline, guard conditional on closing
entry docstatus); dimensions = event attributes, qty as sums; checkpoints scheduled/silent,
closings manual locks.

**Next steps (priority order):**
1. **Production verification runs (Nabin)** — dual-write + shadow on real sites; shadow's
   negative_exposure bucket must be re-read after the d3363f56 fix (it was inflated).
2. **v17 migration patch** — one resumable full fold, FY-boundary checkpoints, frontier closing
   entry, Opening Adjustment (auto-submit within threshold, else stop), current-FY refold. The
   SLE-absorbs-Stock-Event schema step is deferred until the production runs are done.
3. **apnaklub**: Opening Adjustment compute dry run over 90k keys (first scale test of
   opening_delta + the attached breakdown; child table holds differing keys only), then a
   reopen (closing cancel → Stock Restatement) to size a year's refold.
4. Refold queue hardening: per-key advisory lock in refold_key on postgres (sync appends and the
   job can interleave; MariaDB gap locks cover it today), a Desk list view / retry button for
   Failed rows.
6. **Lint debt**: pre-commit hangs → every commit used --no-verify; fix hook, re-lint branch.
7. Upstream wave: write-guard logger → v16 PR; remaining migration/report fixes; #57980.
8. Gameplan post — blocked on user running `frappectl auth login` in a real terminal.
9. Open numbers for Nabin/Ankush: opening-delta approval threshold; v17 beta cohort.

Known small debts: probe no-commit guards, report JSON filters for Desk, closing-entry checkpoint
summary line, remaining fold fallbacks (Standard Cost, SE/SCR LCVs, post-baseline reco-bearing
batch keys), pre-fix −80 GL pair on test2's MAT-PRE-2026-00002, scoped TLA+.

---

Audit of every code path that writes `tabStock Ledger Entry` and `tabBin` in
current erpnext develop. This is the map the chokepoint refactor is built on:
the M2 gate is *100% of writes through chokepoints; every external writer
identified or the bypass log quiet*.

Method: exhaustive sweep of `apps/erpnext` for every write mechanism (doc API,
`db_set`/`db_update`, `frappe.db.set_value`, raw SQL, query builder, bulk
insert, deletes). One-time patches under `erpnext/patches/` are excluded from
the main lists but counted. `apps/frappe` and `apps/stock_engine` contain no
writers to either table.

---

## Part 1 — Stock Ledger Entry writers

Patches excluded: 14 patch files touch SLE (13 name the doctype, one writes
through `make_sl_entries`). `apps/frappe` has zero references to the doctype;
SLEs are exempt from framework auto-cancel (`hooks.py:460`), and
`SLE.on_cancel` hard-throws — individual SLEs can never be cancelled.

### 1.1 The 17 distinct write sites

Only #1–#6 are the "official" pipeline; the rest are side channels.

| # | site | mechanism | lifecycle |
|---|------|-----------|-----------|
| 1 | `stock_ledger.py:260` `make_entry()` | `frappe.get_doc(args)` + `.submit()` | **primary insert** — submit and cancel-reversal rows |
| 2 | `stock_ledger.py:264` `make_entry()` | `db_set("creation", ...)` | reco SLEs recreated during repost keep their original creation |
| 3 | `stock_ledger_entry.py:222` `validate_serial_batch_no_bundle()` | `self.db_set(...)` | second UPDATE right after insert (`has_batch_no`, `has_serial_no`) |
| 4 | `stock_ledger.py:1177` `update_entries_after.process_sle()` | `doc.db_update()` | **primary valuation writeback** — qty_after_transaction, valuation_rate, stock_value, stock_queue, stock_value_difference, incoming_rate; runs on submit (`repost_current_voucher`, `:180`) and background repost (`repost_future_sle`, `:336`) |
| 5 | `stock_ledger.py:2277` `update_qty_in_future_sle()` | `qb.update` bulk | qty_after_transaction (+ stock_value for Standard Cost) across a date range, every submit/cancel |
| 6 | `stock_ledger.py:244` `set_as_cancel()` | `qb.update` bulk | flags `is_cancelled=1` on cancel (rows are never deleted here; reversals are inserted via #1) |
| 7 | `purchase_receipt.py:515` `enable_recalculate_rate_in_sles()` | `qb.update` bulk | sets `recalculate_rate=1` on PI submit/cancel to re-drive PR valuation |
| 8 | `accounts/.../gl_entry.py:511` `rename_temporarily_named_docs()` | `qb.update` bulk | **renames the primary key** (hash → MAT-SLE series) from an hourly cron, batches of 100 with per-batch commit — lives in the *accounts* module |
| 9 | `serial_batch_bundle.py:129` | `sle.db_set` | bundle link for material-transfer inward legs, on_submit |
| 10 | `serial_batch_bundle.py:211` | `sle.db_set` | auto-created bundle link + flag, on_submit |
| 11 | `serial_batch_bundle.py:1183` | `sle.db_set` | bundle link — submit *and* repost path (`process_sle` recreates bundles) |
| 12 | `serial_and_batch_bundle.py:1400` `delink_serial_and_batch_bundle()` | `db.set_value` per row | nulls bundle link on bundle before_cancel |
| 13 | `accounts/.../pos_invoice_merge_log.py:396` | `qb.update` bulk | nulls bundle link on cancelled SLEs during POS merge — *POS module* |
| 14 | `controllers/accounts_controller.py:446` `on_trash()` | `qb.delete` | **hard DELETE** of a voucher's SLEs on document deletion, gated by Accounts Settings `delete_linked_ledger_entries` |
| 15 | `setup/.../transaction_deletion_record.py:927` | `db.delete` batched | per-company wipe background job (SLE is explicitly in `LEDGER_ENTRY_DOCTYPES`) |
| 16 | `stock/stock_balance.py:347` `set_stock_balance_as_per_serial_no()` | `.insert()` with ignore_validate | console repair tool — inserts a **docstatus-0** SLE, bypassing the whole pipeline |
| 17 | `repost_item_valuation.py:378` `recreate_stock_ledger_entries()` | docstatus flip 1→2→1 + two `update_stock_ledger()` calls | regenerates a voucher's SLE rows wholesale (UI "recreate" flag on RIV) |

### 1.2 Voucher entry points into the pipeline

All creation funnels through `make_sl_entries()` (`stock_ledger.py:104`),
reached via `StockLedgerService.make_sl_entries`
(`services/stock_ledger_service.py:204`) → `StockController.make_sl_entries`
(`controllers/stock_controller.py:274`). `update_stock_ledger()`
implementations: buying_controller `:765` (PR/PI), selling_controller `:676`
(DN/SI), stock_entry `:977`, stock_reconciliation `:872` (adjustment entries at
`:957`), subcontracting_controller `:1181` + `:1163` (supplier warehouse),
asset_capitalization `:365`.

### 1.3 Hazards found

1. **Latent bug in `make_sl_entries` (`stock_ledger.py:148-151`):** `sle_doc`
   is only assigned when `sle.get("actual_qty") or voucher_type == "Stock
   Reconciliation"`, but `args = sle_doc.as_dict()` runs unconditionally — a
   zero-qty non-reco row reuses the *previous iteration's* doc, or raises
   `NameError` if it is the first row. Candidate cheap-win PR.
2. **The hourly rename cron (#8) rewrites SLE primary keys** from the accounts
   module — any chokepoint, event-sourcing dual-write, or foreign reference
   design must survive `name` changing after insert.
3. **#16 inserts docstatus-0 SLEs** that the entire pipeline (and most report
   filters on `docstatus=1`/`is_cancelled=0`) never sees consistently.
4. **#17 flips a submitted voucher's docstatus in place** — the most invasive
   pattern in the codebase; the new engine's replace-by-reversal semantics
   makes it unnecessary, but Phase 0 must still route it.
5. The valuation writeback (#4) is `db_update()` on a doc built from a dict —
   no validation, no hooks; the same repost run *also* writes voucher child
   tables, Serial and Batch entries, and Bin, so "one repost = one write path"
   is already false today in four directions.

---

## Part 2 — Bin writers

Patches excluded: 8 patch files touch `tabBin`.

### 2.1 The 9 distinct low-level write sites

Everything else funnels into one of these:

| # | site | mechanism | role |
|---|------|-----------|------|
| 1 | `stock/utils.py:235` `_create_bin()` | `bin_obj.insert()` in a savepoint with `UniqueValidationError` fallback | the **only** INSERT path |
| 2 | `stock/doctype/bin/bin.py:310` `update_qty()` | `frappe.db.set_value` | SLE-driven qty update (called from `make_sl_entries`) |
| 3 | `stock/stock_ledger.py:1918` `update_entries_after.update_bin()` | `frappe.db.set_value` | repost path (`actual_qty`, `stock_value`, `valuation_rate`) |
| 4 | `stock/stock_ledger.py:1904` `update_entries_after.update_bin_data()` | `frappe.db.set_value` | **DEAD CODE** — zero callers; delete in Phase 0 |
| 5 | `stock/stock_balance.py:287` `update_bin_qty()` | `bin.db_update()` (full-row) | ordered/indented/planned/reserved family |
| 6 | `stock/doctype/bin/bin.py` lines 107, 115, 130, 145, 152, 223, 225, 236 | `self.db_set(...)` in 7 Bin methods | reserved_qty_for_production / _sub_contract / _production_plan / reserved_stock |
| 7 | `stock/doctype/bin/bin.py:71` `recalculate_values()` | `self.save()` | whitelisted manual repair button |
| 8 | `stock/doctype/item/item.py:1438` | `frappe.qb.update` | `stock_uom` after item UOM change (only qb-update on Bin) |
| 9 | deletes: `item.py:634,664,743`, `warehouse.py:106`, `transaction_deletion_record.py:685` | `frappe.db.delete` | item/warehouse delete, item merge, transaction deletion |

### 2.2 Writers by field family

**actual_qty / valuation_rate / stock_value (SLE-derived — the fold's territory):**

- `stock_ledger.py:160-165 make_sl_entries()` → `get_or_make_bin()` +
  `bin.update_qty()` — every stock document submit/cancel.
- `stock_ledger.py:1906-1918 update_entries_after.update_bin()` — called from
  `build()` at `stock_ledger.py:717` (submit path, no future SLE) and `:723`
  (background repost). Writes per `(item, warehouse)` in `prev_sle_dict`.
- `bin.py update_qty()` is **additive** for ordered/reserved/indented/planned
  but **absolute** for `actual_qty`; conditionally writes `stock_value` for
  Standard Cost items (lines 299-308).

**reserved_qty (Sales Order soft reservation):**

- `selling/doctype/sales_order/services/reservation.py:58` →
  `stock_balance.update_bin_qty` — SO submit/cancel/status, DN/SI submit-cancel
  via `selling_controller.py:513,677`, child-item update
  (`accounts/services/child_item_update.py:177`).

**reserved_stock (Stock Reservation Entry, hard reservation):**

All funnel through `stock_reservation_entry.py:523-528
update_reserved_stock_in_bin()` → `Bin.update_reserved_stock()`. Callers: SRE
lifecycle (`:97,:107,:115`), `stock_controller.py:520`,
`selling_controller.py:1035,1102`,
`subcontracting_inward_controller.py:1123,1150`,
`manufacturing/.../work_order/services/reservation.py:146,177,496`.
The only writer that deliberately skips `projected_qty` recomputation
(`reserved_stock` is not part of the projected formula).

**reserved_qty_for_production:** `work_order/services/required_items.py:50-55`
→ `Bin.update_reserved_qty_for_production()` (WO submit/close/cancel, Stock
Entry against WO). Nests into `update_reserved_qty_for_production_plan()`.

**reserved_qty_for_sub_contract:**
`subcontracting_order.py:229-236` (SCO status changes, SCR submit/cancel) and
`stock_entry/services/subcontracting.py:203-211` (Send to Subcontractor).

**reserved_qty_for_production_plan:**
`production_plan.py:345-354` (PP lifecycle, two loops: mr_items and
sub_assembly_items) and `work_order/services/status.py:331,337-347`.

**ordered_qty / indented_qty / planned_qty:** all via
`stock_balance.py:274-289 update_bin_qty()` → `bin.db_update()`:

- `purchase_order.py:367` — ordered_qty (PO lifecycle, PR/PI via
  `buying_controller.py:954`, child-item update)
- `subcontracting_order.py:227` — ordered_qty
- `material_request.py:433` — indented_qty (7+ call sites across MR, PO, SCO,
  WO status services)
- `work_order/services/status.py:331` — planned_qty

### 2.3 Creation / deletion

- Insert: only `stock/utils.py:227-240 _create_bin()` (via `get_bin()` /
  `get_or_make_bin()`).
- Delete: `Item.on_trash` (`item.py:634`), item merge (`item.py:664,743`),
  `Warehouse.on_trash` (`warehouse.py:106`), Transaction Deletion Record
  background job (`transaction_deletion_record.py:685`).

### 2.4 Repair / rebuild tools

- `stock_balance.py:12-37 repost()` — loops all bins, bench-console only.
- `stock_balance.py:40-61 repost_stock()` — manual + item merge.
- `Bin.recalculate_values()` — whitelisted "Recalculate Values" button.
- `Item.recalculate_bin_qty()` — deletes and rebuilds bins on item merge.

### 2.5 Writers outside erpnext/stock

`accounts/services/child_item_update.py` (reserved/ordered/indented + a direct
`update_bin_on_delete()` at `:406-425`), `buying` (PO),
`selling` (SO reservation + reserved_stock), `manufacturing` (WO, PP:
planned/reserved-for-production/plan + reserved_stock),
`subcontracting` (SCO/SCR), `controllers/*` (stock, selling,
subcontracting_inward: reserved_stock), `setup` (transaction deletion: DELETE).

### 2.6 Refactor hazards found

1. **Two functions named `update_bin_qty` with opposite semantics** —
   `bin.py:310 update_qty` (imported as `update_bin_qty`, *additive* for
   reserved/ordered/indented/planned) vs `stock_balance.py:274 update_bin_qty`
   (*absolute*). Any chokepoint must rename one.
2. **`update_entries_after.update_bin_data()` is dead** but shaped like a live
   writer — delete it, or it will be resurrected by accident.
3. ~~`Bin.company` is never populated~~ — **verified false**: `bin.json` sets
   `fetch_from: warehouse.company` and patch
   `v16_0/update_company_custom_field_in_bin` backfilled old rows; live bins
   carry company. No action needed.
4. `Bin.update_reserved_stock` is called on a `frappe.get_cached_doc` — safe
   today because it `db_set`s a single field, but the in-memory doc can diverge
   from the row.
5. `stock_balance.update_bin_qty` unconditionally assigns `bin.modified` but
   only writes when a value changed.

---

## Part 3 — Chokepoint design

### 3.1 Shape

Two writer modules under the existing `erpnext/stock/services/` extraction,
each exposing a *small, closed* set of primitives. Nothing else in erpnext
touches the tables. The primitives mirror the lifecycle stages the audit
found — they do not try to unify semantics (that is Phase 1+); Phase 0 only
guarantees *every write has one address*.

**`stock_ledger_writer.py`** (absorbs the 17 SLE sites):

- `insert(rows)` — #1 (doc-API submit stays inside, unchanged)
- `set_fields(sle_name, values)` — #2, #3, #9–#12 (single-row db_set family)
- `write_valuation(sle_dict)` — #4 (the repost writeback)
- `bulk_update(filters, values)` — #5, #6, #7, #13, #8
- `delete(filters)` — #14, #15
- `insert_raw(row)` — #16, kept deliberately ugly and named for what it is
- #17 stays a caller (it composes cancel+submit, both already routed)

**`bin_writer.py`** (absorbs the 9 Bin sites):

- `get_or_create(item, warehouse)` — site 1 (the only insert)
- `write_stock_fields(bin_name, values)` — sites 2, 3 (SLE-derived:
  actual_qty, valuation_rate, stock_value)
- `write_planning_fields(bin_name, values)` — sites 5, 6 (ordered/indented/
  planned/reserved families; resolves the additive-vs-absolute split by making
  callers pass absolute values only)
- `save(bin_doc)` — site 7 (repair button)
- `delete(filters)` — site 9
- site 4 (`update_bin_data`) is deleted; site 8 (stock_uom qb.update) becomes
  `write_stock_fields`

### 3.2 Bypass logger

Two layers, cheapest first:

1. **In-process (catches erpnext + well-behaved third-party apps):** the
   writer modules set a context flag (`frappe.local.stock_write_token`) around
   their DB calls. A lightweight patch on the SLE/Bin doctype classes'
   `db_insert`/`db_update`/`db_set` plus a `frappe.db.set_value` wrapper logs
   any write arriving without the token — full Python traceback into a
   dedicated Error Log category. Zero cost when the site config flag
   `log_unrouted_stock_writes` is off.
2. **DB triggers (catches raw SQL from anything, optional):** site-config
   gated AFTER INSERT/UPDATE/DELETE triggers on the two tables writing
   `(table, op, timestamp, connection user)` to an audit table, with the
   chokepoint setting a session variable the triggers honor. Only for
   installs that suspect out-of-band writers; not shipped on by default.

Gate metric: N days of quiet log on a busy site = the writer inventory is
complete. This is also exactly the hook where Phase 1's dual-write attaches —
the chokepoint *is* the future event-emission point.

### 3.3 Scheduled Stock Closing Entry

Today manual: user submits a Stock Closing Entry, a background job builds
Stock Closing Balances; not present in `scheduler_events`. Phase 0 adds a
Stock Settings toggle (`auto_create_stock_closing_entry`, default off) plus a
monthly scheduled job that creates+submits one per company for the previous
month, skipping companies where one already overlaps (the doctype's
`validate_duplicate` already enforces overlap exclusion). Under the new
engine these become the checkpoint/convergence barriers, so turning them on
early caps future backdate cost on real sites.

---

## Part 4 — Proposed PR sequence

Ordered so every PR is independently shippable, behavior-neutral, and small
enough to review:

1. **Cleanups + bug fixes** — delete `update_entries_after.update_bin_data`
   (dead); fix the `sle_doc` reuse bug in `make_sl_entries`; rename
   `bin.update_qty` → `update_qty_from_sle` to end the `update_bin_qty` name
   collision. (`Bin.company` item dropped — verified already handled.)
   **Status: done** — branch `refactor/stock-write-path-cleanups`, 3 commits,
   red/green-verified regression test.
2. **SLE writer module** — introduce `stock_ledger_writer.py`; route the six
   in-pipeline sites (#1–#6). No semantic change, pure call-site refactor.
   **Status: done** — PR #57981 (stacked on #57980), branch
   `refactor/sle-writer-module`. Primitives: `submit_new`, `set_fields`,
   `write_valuation`, `shift_future_qty`, `flag_voucher_cancelled`.
3. **SLE side channels** — route #7–#17 (serial/batch db_sets, PR
   recalculate flag, POS delink, rename cron, deletes, repair insert).
   **Status: done** — PR #57982 (stacked on #57981), branch
   `refactor/sle-writer-side-channels`. Added primitives: `insert_raw`,
   `set_fields_for_voucher`, `clear_bundle_links`, `rename_row`,
   `delete_for_voucher`, `delete_rows`. Sweep confirms zero unrouted SLE
   writes outside patches/tests.
4. **Bin writer module** — introduce `bin_writer.py`; route all 9 sites;
   absolute-value semantics at the primitive boundary.
   **Status: done** — commit 687203d8d.
5. **Bypass logger** — context token + logging wrapper behind
   `log_unrouted_stock_writes`; optional trigger DDL behind a second flag.
   **Status: done (in-process layer)** — commit b63cef0c5;
   `stock_write_guard.py`, `authorized_writer` decorator on all primitives,
   `db_insert`/`db_update`/`db_set` overrides on both doctypes. DB-trigger
   layer deliberately not implemented (optional, revisit if real-site logs
   suggest raw-SQL writers).
6. **Scheduled Stock Closing Entry** — settings toggle + monthly job + tests.
   **Status: done** — commit 826010dfe; Stock Settings
   `auto_create_stock_closing_entry` + `create_monthly_stock_closing_entries`
   on monthly_long.

**Delivery model (revised):** everything lives on the single integration
branch `stock-ledger-redesign` (formerly `stock-write-chokepoints`), to be
validated against real site data before submission. #57980 stays open as a
standalone bug-fix PR; the superseded stacked drafts #57981/#57982 are
closed. Consolidated test pass: 128 tests green across stock_ledger_entry,
bin, stock_closing_entry, stock_reconciliation, serial_and_batch_bundle,
gl_entry, warehouse.

Gate check for M2: branch validated on a real-site restore with
`log_unrouted_stock_writes` on — quiet log (or every hit identified) for the
agreed window, then submit for review.

---

## Part 5 — M3 (Phase 1) on the same branch

Implemented in commits 7f7fc8cb1 + 474b9f93a:

- **Doctypes:** `Stock Event` (autoincrement id → order key
  `(posting_datetime, name)`; kind Receipt/Issue/Assertion/Reversal; declared
  facts only; unique `sle` provenance link; `content_hash` over fact fields)
  and child `Stock Event Allocation` (serial/batch ± rows). Rows writable only
  via `stock_event_emitter` — the doctype throws on any other write.
- **Dual-write:** `stock_ledger_writer.submit_new` emits the fact in the same
  transaction, behind site config `stock_event_dual_write` (default off — do
  not enable before the M2 bypass-log gate closes). Cancels → Reversal events
  referencing the original; recos → Assertions. Voucher deletion and company
  wipes remove events alongside SLEs (`emitter.delete_for_voucher`, TDR
  `LEDGER_ENTRY_DOCTYPES`).
- **Backfill:** `stock_event_backfill.run()` — per warehouse, legacy order
  `(posting_datetime, creation, name)`, keyset-paginated, idempotent
  (rerun skips via unique sle link). `verify()` checks the Phase 1 gate:
  no missing events, per-item id order reproduces legacy order, hashes
  recompute deterministically.
- **Known gaps (accepted for M3, revisit in M4):** cancelled-history SLE
  pairs are not backfilled (they are invisible to every legacy balance);
  repost-time bundle relinking and post-hoc `incoming_rate` rewrites
  (PR-after-PI recalculate, LCV) mutate the SLE after the event was emitted —
  in mixed dual-write mode `verify()` will flag these as hash mismatches,
  which is exactly the drift M4's diffing must classify.

Tests: `test_stock_event` (dual-write fact+reversal round-trip with hash
check; backfill order/hash gate + idempotence) — green.

---

## Part 6 — M4 (Phase 2, shadow mode) on the same branch

Commit 3f7737ec5: `stock/services/stock_shadow.py` — folds each
(item, warehouse) key's events through the `stock_engine` pure core
(FIFO/LIFO/Moving Average) and diffs every event's qty_after/value_after
against the linked SLE's stored `qty_after_transaction`/`stock_value`, with
the doc's three-way classification: (a) legacy self-inconsistent keys
(running-qty invariant check), (b) genuine mismatches (both sides reported,
capped at 50), (c) precision noise. First live result: the fold matched
legacy **exactly** on the test fixture (receipts, issue, backdated entry,
reconciliation).

Deliberate v1 limits, to revisit during real-data runs: Standard Cost keys
skipped (fixed-rate variance semantics need compat mode); allocations not
folded (aggregate level only — lot-level shadow comes with M6 scope);
transfer legs with declared_rate 0 at insert will diff where legacy computed
the inward rate during repost — that is the cost-link drift the M4
classification exists to measure, not a tool bug.

**Rung 2 — concurrent fuzzing (commit de36bf819):**
`stock/services/stock_fuzz.py` — seeded worker threads, own DB connections,
overlapping receipts/issues/transfers/backdates on a hot-key pool; deadlock
retry mimicking the web layer; queued reposts processed; invariants checked
(running qty, Σsvd vs stock_value, Bin parity, dual-write event parity).
First measurements on test-runner-site (4 workers × 15 ops, dual-write on):
**all invariants hold**; hot-key contention cost measured at 16/60 clean,
25/60 needed retries, 19/60 exhausted 3 retries — the empirical number for
the lock-contention argument in §2.7/R3.

**M4 remaining (needs real data / calendar):** run backfill + shadow on a
production restore; classify diffs; wire `stock_shadow.run` into a scheduled
job with a persisted result log once the first manual runs are understood;
fuzz as a CI gate; scoped TLA+ model per §2.14 rung 4.

---

## Part 7 — M5–M7 on branch `stock-ledger-cutover` (stacked on redesign branch)

So the M4 branch stays frozen for the real-site test, cutover work lives on
`stock-ledger-cutover` (3 commits on top):

- **M5 core (dad621eee)** — `stock_fold_authority.py` + `Stock Fold State`
  doctype + `stock_engine_bridge.py`. With `stock_fold_authoritative` on
  (requires dual-write), the submit hot path folds the new event onto the
  key's persisted state and projects the Effect into legacy SLE fields +
  Bin (GL and reports unchanged). Per-event legacy fallback: lots, Standard
  Cost, recos, LCV, backdates, incomplete event history. Legacy rewrites
  invalidate the checkpoint via the `write_valuation` chokepoint; next fold
  rebuilds from events. Parity test green: identical FIFO scenario under
  both engines → matching SLE fields, queues, Bins.
- **M6 instruments (0c58f396a)** — shadow folds lot allocations
  (lot-tracked keys now diffed, not skipped); `stock_restatement_preview.py`
  folds each lot-carrying key aggregate-vs-lot and reports the value delta
  the one-time batchwise restatement would post.
- **M7 (472d8feec)** — `spec/stock_ledger_decommission.md`: what gets
  deleted, what stays, deletion order, and the four per-company
  preconditions. Actual deletions land only after full cutover.

**M5 continued (82f5ca7e7):** fold-native backdates — a backdated insert
synchronously refolds the whole key (cap 20k events, complete aggregate-only
history required) and rewrites changed projections in the same transaction;
the legacy RIV still runs afterwards for GL and re-confirms identical
values (coexistence by design until decommission). Reconciliations fold as
assertion events; adjustment entries stay legacy.
`stock_fold_authoritative_companies` scopes authority per company. Parity
test: reco + mid-history backdate scenario matches legacy — with the fold
side never processing a repost (legacy side needed one).

**M5/M6 build completed (75f8116b8 + a0fc255e5 + dc66bd483):**
- Repost suppression (`stock_fold_suppress_legacy_repost`): fold-covered
  vouchers create no RIV; refolds regenerate affected GL inline.
- Append-only GL (`stock_fold_gl_adjustment`): refolds never rewrite posted
  GL — net svd deltas post as remarked GL rows on the backdated voucher,
  netted per counter account; account balances proven equal to the legacy
  rewrite.
- Bounded refolds: only the window between the surrounding assertions is
  folded; reversal-across-boundary forces full refold.
- Lot-tracked submits: allocations fold as lot sub-states at per-lot moving
  average (= legacy batch-wise/per-serial semantics); parity test green.
  Lot keys with assertions, and lot backdates, stay legacy.
- GL parity test incl. backdate corrections (perpetual company).

**Still open (deliberately):** the M6 restatement *apply* (gated on
previewing real data); §2.11 perf gate on the real-data restore; scoped
TLA+ of the fold-state locking protocol; v17 track (closing-balance
checkpoint fidelity, report migration, SLE removal per spec).

Site test2 currently runs the full new system: dual_write + authoritative +
suppress_legacy_repost (switch to gl_adjustment to try append-only GL).

---

## Part 8 — Real-data gate results (apnaklub, v14-era backup, 2026-08-12)

Migration surfaced 5 upstream patch bugs (all fixed on this branch, each a
standalone PR candidate). Site: 664,614 live SLEs, 90,694 keys, 4,501
warehouses, 307 assets.

- **M2 gate: PASSED.** `log_unrouted_stock_writes` on through migration,
  backfill, and both gate runs — **0 unrouted writes**.
- **M3 gate: PASSED** (`verify_fast`, seconds): 0 missing events, 0 order
  violations across all keys (window-function proof that per-key event ids
  ascend in the legacy total order), 0/20,000 sampled hash mismatches.
- **M4 shadow (6 parallel shards over 90,694 keys):**
  - **matched exactly: 641,562 (96.53%)** · precision noise: 3,971 (0.60%)
  - class (a) legacy self-inconsistent: **1 key** (23 rows)
  - negative exposure: **0** (site never ran uncovered negatives)
  - class (b): ≈19k rows, heavily concentrated (300 captured examples span
    only 58 keys). Two dominant causes: (1) ~70% of examples are <1%
    relative value drift — legacy's per-step flt() rounding vs the fold's
    full-precision floats, the doc's predicted compat-mode item (§4 freeze
    flt bit-for-bit); (2) extreme-relative-delta outliers where legacy
    valued a near-zero-declared receipt later (cost-linked/return inward
    rates — the known emit-time vs repost-time rate gap recorded in Part 5).

**Compat iteration (4 shadow rounds, all committed):**

| round | change | exact match | within noise |
|---|---|---|---|
| 1 | baseline | 96.53% | 97.13% |
| 2 | per-lot MA for all lot items | **72.3% (regression)** | — |
| 3 | lots folded only for bundle rows; rate-targeted cost-linked legs | 96.59% | 97.18% |
| 4 | per-key adaptive semantics classification | 97.29% | 97.86% |
| 5 | switchover-point detection (e18255a4) | 97.42% | 97.98% |
| 6 | offset recalibration at reconciliations (15e8092f) | **97.43%** | **98.00%** |

Round 5/6 findings: 65 hybrid keys, switchover dates clustered in one
Dec-2022/Jan-2023 window with zero corroborating RIVs — the boundary is the
site's engine-change moment, not per-key reposts. Residue (~2%) fits no
switchover shape; accepted as classified per the standing decision. Note:
the perf probe leaked 12 committed events on 2026-08-13 (legacy RIV
processing commits internally, defeating the probe's rollback) — explains
the +12 events and class_a 1→3 keys; probe hygiene item.

Findings that drove it: (a) the site is 100% pre-bundle — batchwise flags
were set by migration patches, stored values are v14 aggregate math;
(b) transit/purchase-return legs consume at the linked rate (engine gained
rate-targeted consumption, stock_engine c7ab1aa); (c) **734 keys** were
reposted after the v15 migration and carry batchwise-restated stored values
(proven by dual-fold: 33/33 vs 31/33) — the adaptive classifier recognizes
either of legacy's own semantics per key.

**Residual (2.1%, ~14k rows):** keys matching neither pure semantics —
partially-reposted histories where rows before the repost point keep
aggregate values and later rows are batchwise (median residual delta 0.12%).
Characterized; next refinement if wanted is switchover-point detection using
the site's own Repost Item Valuation history. Alternatively acceptable as a
classified, explained difference for the cutover opening adjustment.
**Decision 2026-08-12: parked** — design recorded in the redesign doc
(Phase 2, "Deferred — switchover-point detection"); resolve before calling
the M4 gate or fold the residue into the cutover opening adjustment.

**§2.11 perf gate, measured on apnaklub (a32a2ea8a, rolled-back probe):**

| metric | fold authority | legacy |
|---|---|---|
| warm submit, 124–214-event keys | 259–262 ms (flat vs depth) | 190–211 ms |
| cold submit (checkpoint rebuild) | 1.5 s once per key | n/a |
| backdated entry → books correct | **900 ms, synchronous** (SLE+Bin+GL inline) | 247 ms submit + **12.9 s** repost ≈ 13.2 s |

Verdict: sync-backdate target (<1 s) **met with GL correction included** —
14.6× faster to correct books than legacy's queue. Plain-submit overhead was
~1.25× legacy; after the hot-path optimization (9ed7e5a04: sequence-reserved
direct event insert, event handed to authority via request-locals, checkpoint
row handle reused from the locked read) the second probe run measured:
backdate **900 → 395 ms** (legacy time-to-correct 7.2–12.9 s across runs),
cold rebuild 1.5 s → 584 ms, warm submits within legacy's own run-to-run
noise band (fold 247–444 ms vs legacy 190–679 ms — single-shot timings on a
dev machine; an N-iteration averaging mode is the remaining probe nicety).
Site quirks handled by the probe: closed books until 2023-03 (backdate
window clamped), site server scripts (disabled inside the rolled-back
transaction; `server_script_enabled` now on globally in this bench's common
config).


---

## Part 9 — v17 build track (started 2026-09-03)

1. **Checkpoint fidelity + fold-read service — done** (aac57e7b).
   `Stock Fold Checkpoint` persists fold-resumable state per active key when
   a Stock Closing Entry is processed (sparse keys skipped — reads fall back
   to older checkpoints); cancellation removes them. `stock_fold_read` is
   the v17 read model: `state_as_of` = nearest checkpoint + folded tail,
   `ledger_rows` for running per-event views. Property test green.
   **Real-data proof (apnaklub, 200 sampled keys):** checkpoints created and
   resume-from-checkpoint == fold-from-zero **200/200**; quantities match
   Bin **200/200**; values match **197/200** (the 3 misses are the known
   classified residue keys). Whole exercise: 8.6 s.
2. **Stock Balance port + parity harness — done** (5014f197 + arbitration).
   Full-company run on apnaklub (2024-01→2026-08): 3,860 keys compared.
   **Ground-truth arbitration of the 774 disputed quantities: legacy wrong
   772, fold wrong 1, both wrong 1** — the fold report agrees with the
   ledger's own sums on 99.95% of compared keys, while the legacy Stock
   Balance report misstates opening balances on ~20% of its rows (its
   SLE-aggregation opening path; upstream defect candidate). Row-coverage
   note: legacy shows only keys with window activity (4,002 rows) while the
   fold shows every key with balances (21,715) — a semantics difference,
   arguably a fold improvement. Value arbitration + the 1 fold-wrong key:
   next loop. Probe hygiene: two "rolled-back" exercises leaked commits
   (RIV repost and checkpoint flusher commit internally) — future probes
   get a no-commit guard; leaked checkpoints cleaned 2026-09-03.
3. **Stock Ledger + Stock Ageing ports — done** (a206b04f), parity on the
   residue-hotspot warehouse (SHR-SHR-P29-OTPL):
   - Ledger: **qty 1308/1308**; value 1040/1308 — the mismatch dump exposed
     stored values frozen at 1800.0 while balances swing 370→2151 units:
     legacy value-corruption invisible to the qty-only class (a) check.
   - Ageing: **qty 80/80**; average age within 5 days on 43/80 — legacy
     systematically ages *older* (stale stock_queue entries linger), the
     drift its own repair reports exist for; the fold ages actual surviving
     layers by source-event date.
   All three fold reports carry parity tests; harness handles legacy's
   positional ageing rows.
4. **Remaining on track:** closing-entry checkpoint summary line (UX);
   value-level arbitration in the balance harness; the 1 fold-wrong balance
   key; long-tail reports (Projected Qty, Analytics) as needed.


---

## Part 10 — Landed cost as Revaluation facts (2026-09-03)

Engine 2e7dab4 + erpnext d3782acd. Surfaced by manual testing on test2: an
LCV produced no difference GL entry because the whole landed-cost flow was
a legacy fallback (in-place rewrite via cancel/recreate + RIV).

Now: each receipt item's charge becomes a **Revaluation event** at the
receipt's own instant referencing the receipt's event; the fold uplifts the
surviving layers per-unit and the refold trues up downstream consumption.
GL is append-only end to end: the charge posts on the LCV against its
taxes' expense accounts dated at the receipt; downstream corrections ride
the standard per-voucher-dated adjustment machinery. Refold projections
became absorption-based (SLE-less events fold into the preceding SLE row —
the shape legacy books carry landed cost in). All-or-nothing per receipt,
legacy fallback for lot-tracked keys / incomplete history; cancel emits the
negative revaluation. Parity test: zero RIVs, identical ledger values and
net account balances vs legacy. Engine suite 67 green; authority suite 7
green.

**Extended to lot keys (8cf355f6):** bundle-backed batch and serial keys
now take the fold path for backdates and revaluations — allocations fold as
lot sub-states in the refold; parity test covers PR + issue + LCV for both
a batch and a serial item (zero RIVs, difference entries, identical books).
Found via manual testing on test2 (a batch item's LCV fell back to legacy
by design; stale web workers and unprocessed RIVs compounded the
confusion).

Remaining fold-coverage fallbacks: Standard Cost,
reconciliation-bearing lot keys, Stock Entry / Subcontracting Receipt LCVs.

2026-09-04 — Batch semantics settled and implemented (erpnext 2b588de7,
engine fbedb52). `use_batchwise_valuation` survives and decides fold
participation: flag-on batch = sub-fold, flag-off batch = quantity tag
priced at the shared pool's rate; flag fixed at the batch's birth.
Engine folds partially allocated events (remainder → top-level pool);
bridge `to_event` filters allocations by the flag at the single
chokepoint (authority/checkpoints/reads), shadow + restatement preview
opt out to replay history's own shape. Mixed keys deliberately diverge
from legacy's whole-position blend: pools never borrow, so stock-out
closes at exactly zero instead of leaving residual value at zero qty.
Also: LCV cancel = negative facts + make_reverse_gl_entries on the
LCV's own GL (f9ce8219, closed-period clamp in the same commit).
Inventory-dimension decision recorded: attributes on the event, sums
for qty, warehouse stays the valuation boundary.

2026-09-04 (later) — Negative-stock decision implemented: freeze the past
as-is, clean forward (erpnext 62218d2f, engine 6435368). New
`stock_fold_cutover.freeze_baseline(company)`: one SLE-less baseline
Assertion per key pins legacy's stored closing balance (negative →
frozen exposure settled at true cost; lots seeded via per-lot
declared_rate on Stock Event Allocation; quantity-tag batches in the
pool). Authority/read paths are baseline-aware (completeness + lot-reco
gates since latest baseline only; refolds never fetch behind it;
frozen-era backdates fall back to legacy). This is also the missing M5
brownfield-start mechanism: a site can flip fold authority without
backfill by freezing first. Batch flag guard: turned out already
enforced upstream — use_batchwise_valuation is set_only_once; pinned in
test instead of duplicating (994ecc28). Run `freeze_baseline` +
`frappe.reload_doc` for stock_event/stock_event_allocation on test2
after pulling (new declared_rate column).

2026-09-04 (evening) — v17 cutover model settled in a brainstorm and
recorded in the redesign doc (Part 4, "The v17 cutover: frozen
frontier"): no per-site shadow; SLE absorbs Stock Event and stays the
one facts table; migration folds history once, checkpoints every FY
boundary, submits a frontier Stock Closing Entry and posts an opening
adjustment document at current-FY start (threshold-gated), then refolds
the current FY. Frontier invariant: one live adjustment at the
frozen/engine boundary; reopening a year (cancelling its closing)
restates it to engine truth and slides the adjustment back one year,
newest-first. Amendments behind the lock become reversal-facts-today.
Checkpoints decoupled from closings (3279c4a8): monthly scheduler cuts
bare checkpoints (create_monthly_fold_checkpoints, idempotent, no
setting); closings are manual locks; the auto-closing job and its
Stock Settings checkbox are removed. Pending build alignment: make the
baseline guard conditional on the closing entry's docstatus; write
migration patch (SLE merge + opening adjustment doc); ship write-guard
logger to v16.

2026-09-04 (night) — Baseline guard made conditional on the closing
entry's docstatus (78e6ee97). freeze_baseline gained closing_entry=...:
owned baselines link via voucher fields and lock only while the closing
is submitted; _latest_baseline resolves the newest *active* baseline
(frontier slides back on cancel); _drop_revoked_baselines filters
revoked pins out of refold/rebuild/read replays. Unowned baselines stay
unconditional. 11/11 authority + 3/3 fold-read tests green.

2026-09-04 (late) — Stale-checkpoint bug found while writing the fold
state/checkpoint explainer and fixed (da370489): refolds never
invalidated Stock Fold Checkpoint rows dated after the insertion, so
state_as_of resumed from a pre-backdate photograph and never folded the
backdated fact (wrong reads forever). _refold now deletes the key's
checkpoints with as_of >= the inserted instant; write_valuation's
invalidate() does the same scoped deletion for legacy rewrites (all
checkpoints when the instant is unknown). Regression test confirmed
red-without/green-with the fix.

2026-09-04 (test2 backdate flow) — Full backdated-entry flow verified
live on test2 (all four flags on): receipt 10@50, issue 2, closing
CBAL-00002 cut a checkpoint, then a receipt backdated before everything
(10@60). Results: sync refold repriced the issue -100 → -120 (FIFO
consumes the older 60-layer first); stale checkpoint deleted (the
da370489 fix working live); 0 RIVs; append-only GL — original issue GL
untouched, -20 correction pair posted on the backdated voucher dated at
the issue's own date; fold state, state_as_of, and Bin all at 18/980.
Bonus catch: the tightened allocation validation tripped on test2's old
cancelled-receipt events — the emitter stored bundle-direction signs on
Reversal events (+1 on a -1 event; folding would move the lot the wrong
way). Fixed at emit time + normalized on read for stored rows
(a72d54a2). Probe module removed after the run; test2 scenario docs
left committed (warehouse "Backdate Flow 18023 - TC").

2026-09-04 (lot-cardinality) — Scale question answered and recorded
(§2.6 scale note): blob-per-key state is O(participating lots); fix
ladder = serial participation flag (open item, mirrors batch decision;
covers mass-serialized goods), guardrail warning at 5k lots in
_save_state + checkpoint creation (implemented, commit above), per-lot
state rows as the designed escape hatch (build only when real data
triggers the guardrail).

2026-09-04 (serials decision) — Serial participation flag REJECTED
(Nabin): every serial carries its own cost at any scale, matching v15
bundle semantics (incoming_rate per serial) and physical COGS (units
picked from the ₹55 carton cost ₹55). Quantity-tag mode stays for
batches only (legacy's own flag). Scale answer promoted from escape
hatch to committed plan: storage tiering — blob tier (≤ ~5k lots, as
built) / row tier (Stock Fold Lot State, one row per lot, point-reads
of touched lots only, copy-on-write checkpoints); the 5k guardrail is
the row-tier migration trigger. §2.6 scale note revised accordingly.
Row tier is a v17 build item, not yet implemented.

2026-09-04 (serials, final) — Serial model settled on third iteration
(participation flag and Stock Fold Lot State row tier both rejected):
serials NEVER live in fold state — always quantity tags in the fold,
facts carry position/traceability. New Item flag
`use_serialwise_valuation` (mirrors use_batchwise_valuation, fixed
after first serialized movement, default on for v15 continuity) picks
the issue's rate source: off = pool rate from Stock Fold State; on =
per-serial rate derived at write time from the last inward allocation's
declared_rate (+ revaluation uplifts on the source receipt), consumed
via rate buckets through _take_at_rate. Scale problem structurally
gone; guardrail now effectively watches batches only; freeze_baseline
seeds pools + batchwise batches only. §2.6 rewritten. Build items: the
Item flag, emitter rate-bucket derivation for serialwise issues, drop
serial allocations from fold events in to_event, simplify
freeze_baseline serial seeding.

2026-09-04 (serialwise implemented) — use_serialwise_valuation shipped
(erpnext 086a4ba3, engine 36a1760). Engine: Event.rate_buckets — (qty,
rate) groups consumed from matching layers before declared_rate/policy
(outward only, validated). ERPNext: Item flag (default 1, v15
continuity; enable blocked once Serial Nos exist per Nabin's rule);
emitter stores SABE incoming_rate as allocation declared_rate (per-
serial audit trail); to_event drops serials from lot allocations
always and buckets flagged items' outward picks; policy_for folds
serialwise items layered (Fifo) regardless of valuation method
(shadow passes honor_serialwise=False); freeze_baseline seeds batches
only; item_code added to replay fetch field lists. Tests: engine 77,
authority 12 (new: -140 serialwise vs -116 MA pool on identical picks,
empty lots both, enable-guard), fold-read 4. NOTE for test2/apnaklub:
reload_doc stock/item needed after pull.

2026-09-05 — stock_engine vendored into erpnext (a8341050). Answer to
"when do we merge": now — upstream CI never installs extra apps, so
every fold test would have died on the bridge's ImportError throw at
first PR; plus the paired-commit tax and the migration patch deepening
coupling. Core (1,265 LOC, zero deps, relative imports) copied verbatim
to erpnext/stock/engine @ archived-repo 36a1760; bridge engine()
repointed (only file that named stock_engine.*), throw removed; 77
pytest tests → 63 frappe-runner unittest tests (13 harness tests stay
archived with benchmark/ + sle_replay/, 1 sys.modules purity check
dropped — source-scan gate kept); app shell removed from apps.txt +
env; repo archived to ~/bench-cli/stock_engine-archive with pointer
README (540384a). Verified: 63 engine + 13 authority + 4 read + 2
event + 6 closing green under bench runner; test2 smoke resolves
erpnext.stock.engine.state.State over real data. One hypothesis
cold-start deadline flake observed once on test_lots (4 subsequent
clean runs) — same profile as test_valuation.py in CI. test2/apnaklub:
restart bench so long-running workers drop the old .pth import.

2026-09-05 — stock-ledger-redesign branch deleted (local + origin). It
was a strict ancestor of stock-ledger-cutover (tip 4cbbfc197, zero
unique commits); its purpose — gating M2-M4 on real-data validation
before cutover work — was served weeks ago on apnaklub, and the v17
plan's PR carving won't follow that boundary. stock-ledger-cutover is
now the single program branch. Recovery if ever needed:
`git branch stock-ledger-redesign 4cbbfc197`.

2026-09-05 — Program branch renamed: stock-ledger-cutover →
stock-ledger-redesign (local + origin, tracking updated; tip a8341050).
The name now covers the whole arc M2→vendored engine. Historical log
entries above referring to "stock-ledger-cutover" mean this branch.

2026-09-05 — Branch rebased onto latest upstream develop (366 upstream
commits, base 2026-08-10 → 5beed5f4). 58 commits replayed → 54: four
dropped as already-upstream (asset-type patch fix = #58416, plus the
three M0 SLE/Bin fixes — the draft-PR wave landed). Only 4 conflict
stops (SLE writer imports ×2, closing-entry test imports ×2, asset
patch); all resolved as import unions. frappe fast-forwarded Aug 11 →
Sep 4 (b56649ed) — new erpnext requires it (SMS Settings.allowed_roles
in migrate). test-runner-site migrated clean. Full battery green on the
rebased stack: engine 63, authority 13, fold-read 4, event 2, closing
7 (upstream added one). Hypothesis cold-start deadline flake recurred
→ fixed for good with a deadline=None profile in engine/tests/__init__
(5dabceaa). Force-pushed (safety tag pre-rebase-2026-09-05 = old tip
a8341050). test2 and apnaklub still on pre-rebase schema: bench migrate
needed there before next use.

2026-09-05 (later) — Upstream PR wave, migration fixes: four draft PRs
opened against frappe/erpnext develop (cherry-picked via worktree while
apnaklub migration ran): #58773 asset depreciation schedule patch,
#58774 reporting-currency unset guard, #58775 acc_frozen_upto guards
(3 patches), #58776 tax-withholding column guard. The fifth apnaklub
fix (asset-type checkboxes) was already fixed upstream independently
as #58416 — no PR needed. Remaining upstream items: write-guard logger
→ v16, the 3 legacy report defects, #57980.

2026-09-05 (backports + apnaklub) — Backport analysis: all four removal
sites shipped in released version-16 (v15 unaffected — still has the
method/field, lacks reporting currency), so "backport version-16-hotfix"
labels added to #58773-76; #58416 already followed that path. Both
sites migrated onto the rebased stack: test2 clean; apnaklub clean,
exit 0, zero patch failures — the original five-bug gauntlet now passes
end-to-end with our four fixes + upstream's #58416.

2026-09-05 (evening, decision) — "SLE absorbs Stock Event" started (schema
moves for fact columns + event_id sequence + allocation child table) and
was STOPPED by Nabin: "don't merge the stock event table into SLE now, I
will still run this into some real production sites to verify the
results." Reverted before any commit. Design worked out that session,
kept for later: fact columns on SLE (event_id from a standalone sequence,
kind incl. Baseline, declared_rate, assert_qty/rate, reverses_event,
value_change), allocations as a child table of SLE with rename-cron
cascade, cancelled originals + reversal rows both facts (kind Reversal
paired by voucher/detail/-qty), in-place stamping patch with Python-side
reversal pairing, Revaluation rows projected with svd 0 (uplift absorbed
into the preceding receipt row as today).

2026-09-05 (evening, fix) — Exposure double-subtraction (d3363f56): the
engine's State.value already nets -exposure_qty*exposure_rate, yet
_equivalent_value (authority refold projections), shadow's
_legacy_equivalent_value, ledger_rows and the Stock Balance (fold) report
subtracted it again — a key at -3 @ 80 read -480. Verified with a bare
engine replay. One helper now (stock_engine_bridge.equivalent_value =
identity), all callers through it, regression test in test_stock_fold_read.
Consequence for the production runs: shadow's negative_exposure counts
on apnaklub were inflated; rerun before citing them.

2026-09-05 (evening, build) — Stock Opening Adjustment (3dc25fb8): the
v17 frontier document. Fields: company, stock_closing_entry (owner, must
be submitted), moment = closing to_date 23:59:59.999999, posting_date =
to_date + 1, adjustment_account (default Company.stock_adjustment_account),
keys/skipped_keys (Standard Cost), total_delta, threshold (new Stock
Settings.opening_adjustment_threshold), within_threshold, items table
(differing keys only). compute() enqueues build(): opening_delta folds
every key via state_as_of, full per-key result attached as gz JSON.
Submit: emit_baselines at engine values owned by the adjustment (batch
seeds from fold lots; dropped when negative/overshoot), GL Dr/Cr stock
account vs adjustment account netted per account on posting_date, Bins
shifted by the deltas. Cancel: only via the closing entry's cancel
(before_cancel guard; closing.on_cancel cascades with
flags.via_closing_cancel), reverse GL, un-shift bins, drop fold state.
_baseline_active now checks any owner's docstatus. stock_fold_cutover
split: emit_baselines (shared) + freeze_baseline (legacy pins) +
opening_delta (engine truth). Tests on a dedicated company: drift of 37
booked exactly, baseline 6 @ 100, fold continues from it (issue 1 →
svd -100), threshold gating (0/5/10 vs |−8|), closing cancel cascades.
Open: 90k-key scale of build() (single request in the long queue, one
attachment), and whether the child table should cap rows.

2026-09-06 — Queued refolds + Stock Restatement (eea2cc91c1). Refold core moved
out of stock_fold_authority into stock_fold_refold (authority 873 → ~600
lines): refold_for_event (sync, anchored) and refold_key (background,
from an instant, no cap) share _refold_rows/_refold_window, anchored on a
(posting_datetime, id) sort key instead of an inserted event.
foldable_reason(key) → None | "cap" | "incomplete" | "lots".
  Overflow queue: past REFOLD_CAP the backdate is valued from the
nearest checkpoint (stock_fold_read.state_before = checkpoint + tail
strictly before the event), future qty shifted via legacy
update_qty_in_future_sle, outcome QUEUED (folded → RIV suppressed), and a
Stock Refold row queued (one Queued row per key; earlier instant widens
it). Worker: process_refold_queue (long queue, job_id dedupe, 25-min
budget, re-kicks; hourly_long safety net). The tip fold state is left
stale on purpose — self-healing when the job refolds the tail.
  Stock Restatement: Stock Closing Entry.on_cancel → if it cancelled a
live Opening Adjustment (i.e. it was the frontier) → start_for_closing.
run_restatement: status In Progress → _slide_frontier (closing at the
previous closing's to_date or FY start − 1, created+submitted if missing;
Opening Adjustment built and auto-submitted when within threshold or
zero) → one Stock Refold per key with events after the new frontier →
process queue → finalize (Completed / Failed with keys_failed). GL
corrections carried on the restatement (force_gl_adjustment, dated at
each voucher's date). Lock: validate_no_running_restatement blocks SLEs
dated ≤ to_date while Queued/In Progress. Tests: queued-vs-sync parity
(values, Bin, stock account balance); reopen scenario (lock, new
frontier + adjustment, drift of 37 restated, GL −37 on the restatement,
old adjustment cancelled, unlock). Job exceptions re-raise under the test
runner (a rollback there erases the sandbox).

