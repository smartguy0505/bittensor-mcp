# Changelog

## Unreleased

- The repository is also a Claude Code plugin and a marketplace that lists it (`.claude-plugin/`):
  `/plugin install bittensor-mcp --marketplace smartguy0505/bittensor-mcp`. The plugin runs the published package
  with `uvx bittensor-mcp==0.1.2`; the package itself is unchanged.
- README: what the server runs, sends and fetches, with every host it connects to.
- `metagraph` and `neuron` results include the subnet's `tempo` and a `units` note. Emission is per epoch, and an
  epoch is about `tempo` blocks, which differs between subnets (360 on most, 7200 on subnet 19), so emissions
  from two subnets compare only per day: `emission_alpha_per_epoch x 7200 / tempo`.
- Every tool has a title for clients to show.
- README: the plugin's server reads the `BT_MCP_*` variables from Claude Code's environment; where Claude Code logs
  a failed start; updates need a restart.

## 0.1.2 (2026-11-26)

- Fixed: a result that `render` cut could end up longer than `BT_MCP_MAX_CHARS`, by the length of the `truncated`
  notes added after the cut (since 0.1.0; seen at a 2000 cap: a `net_flow` table of 2,067 chars). The notes now
  count toward the cap.
- Fixed: `BT_MCP_LOG` accepts `NOTSET`, `WARN` and `FATAL` again. 0.1.0 took them; 0.1.1 stopped at start.

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
