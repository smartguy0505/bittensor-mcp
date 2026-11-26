# Changelog

## 0.1.1 (2026-11-26)

- `subnets` has the pool in every table: `pool_tao` and `pool_alpha` (the pool's reserves, which the `subnet` tool
  calls `tao_in` and `alpha_in`) and `alpha_out` (the subnet's alpha held outside the pool). `sort_by="pool"` ranks
  by `pool_tao`. The result has a `units` note. Checked against the `subnet` tool at one block, for four subnets.
- Changed: root (netuid 0) has null pool columns, because it has no pool. In 0.1.0, `flow_window` results gave root
  a `pool_tao` of its stake (about 5.5M TAO). All other 0.1.0 columns are unchanged: compared at block 9,593,238,
  with and without a window.
- A bad setting stops the server at start with one line on stderr instead of a traceback: `BT_MCP_MAX_CHARS` must
  be a whole number of at least 2000 (smaller values broke the size cap), and `BT_MCP_LOG` one of the five level
  names, now in any case. Empty variables take their defaults.
- README: examples of questions and the calls that answer them; a changelog.

## 0.1.0 (2026-11-26)

First release: the read-only server with `chain_status`, `subnets`, `subnet`, `metagraph`, `neuron`, `account`,
`hotkey` and the catalog tools, the historical-state check, the frozen flow-EMA label, the windowed net flow, the
`bittensor-mcp` command, offline tests and CI.
