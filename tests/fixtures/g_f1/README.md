# G-F1 Cursor-Fixture JSON Files

These JSON files simulate upstream `/session/{sid}/message` page bodies for
G-F1 tests (cursor-walk conditions).

- `equal_ts_page1.json`: A page containing two messages that share the same
  `info.time.created` timestamp at the ts boundary (both `100`). Used to test
  that `/since/{ts}` includes items with `created >= ts` (boundary inclusive).

- `loop_scenario_pages.json`: *(Not yet used)* — would contain multiple pages
  for the loop-degradation test. Currently the loop test constructs pages inline.

All fixtures are UTF-8 encoded JSON arrays per opencode's shape.
