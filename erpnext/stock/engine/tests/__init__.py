from hypothesis import settings

# The engine's property tests are pure CPU; hypothesis's per-example deadline
# only ever fires on cold-start compilation of the fold, which makes the first
# run after a worker boot flaky. Wall-clock safety comes from the test runner.
settings.register_profile("stock_engine", deadline=None)
settings.load_profile("stock_engine")
