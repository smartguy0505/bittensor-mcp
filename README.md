# bittensor-mcp

A read-only MCP server for the Bittensor chain. It runs over stdio and is built on the official `mcp` Python SDK (2.3)
and the `bittensor` 11.3 SDK.

The server never loads a wallet and does not expose any transaction. It cannot sign, stake, transfer or register.

## Tools
| tool | what it returns |
|---|---|
| `chain_status` | Head block, finalized block, runtime spec version, block time, chain timestamp |
| `subnets` | Every subnet as one table: name, symbol, alpha spot price, burn, neurons, tempo, the chain's TAO flow EMA per day (with its age; see below). Sortable. `flow_window` (in blocks) adds the real net TAO flow over that window, and `sort_by="net_flow"` ranks by it (default window 7200). |
| `subnet` | One subnet: identity, owner, pool (tao_in, alpha_in, alpha_out), spot and moving price, registration settings, epoch position, all hyperparameters, neuron counts |
| `metagraph` | A subnet's neurons as a columns + rows table: incentive, dividends, emission, stake, registration, last update, optional axon. Sort, page, filter by validators/miners/coldkey. |
| `neuron` | One neuron by uid or hotkey, with its rank by incentive and by stake |
| `account` | For a coldkey: free balance, stake positions, stake valued in TAO, owned hotkeys, identity |
| `hotkey` | Owner coldkey, subnets and uids, delegate status and take, identity |
| `list_reads` / `describe_read` / `read` | The SDK's full catalog of about 100 named read-only queries (staking, delegation, locks, crowdloans, weights, commitments…), runnable by name |

Every tool takes an optional `block`: an absolute block number, or a negative number counted back from the head
(`-7200` is about one day).

Units:
- TAO amounts are plain numbers in TAO.
- Alpha amounts are written `{"alpha": x, "netuid": n}`.
- The metagraph's incentive, dividends and consensus are fractions from 0 to 1.
- The `subnet` result has a `units` field naming which fields were converted. Fields whose unit the chain doesn't
  label stay raw, in `raw_rao_amounts`.
- Plain integers returned by `read` are raw chain units: rao = 1e-9 TAO or alpha, u16 fractions /65535,
  U64F64 /2**64. `describe_read` documents each one.

## Historical reads are checked
Some public finney backends keep only recent state. Asked about an older block, they return storage *defaults*
instead of an error. So before any historical read, the server checks that `System.Number` at that block equals the
block.
- If the head endpoint fails the check, the server retries on the archive endpoint.
- If that also fails, the tool returns an error instead of wrong numbers.

## Config (environment)
| var | default | meaning |
|---|---|---|
| `BT_MCP_NETWORK` | `finney` | Head endpoint: `finney`, `test`, `local`, or a `ws://` / `wss://` URL |
| `BT_MCP_ARCHIVE` | `archive` | Archive endpoint used for historical reads |
| `BT_MCP_MAX_CHARS` | `30000` | Cap on one result. Larger results are shortened and the cut is noted under `truncated`. |
| `BT_MCP_LOG` | `WARNING` | Log level. Logs go to stderr. |

## Install and test
```bash
git clone https://github.com/smartguy0505/bittensor-mcp.git && cd bittensor-mcp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -I test_client.py
```
`test_client.py` opens a real stdio MCP session and checks:
- the handshake;
- every tool;
- the error paths;
- a historical read 3,000,000 blocks back;
- windowed net flow: window bounds, every row measured or excluded with a reason, sort order, the root split, and
  the error for a window before genesis;
- a sweep of 94 of the 102 catalog reads.

## Use with Claude Code
```bash
# sessions started in the current folder
claude mcp add bittensor -- /path/to/bittensor-mcp/.venv/bin/python -I /path/to/bittensor-mcp/server.py
# every session
claude mcp add -s user bittensor -- /path/to/bittensor-mcp/.venv/bin/python -I /path/to/bittensor-mcp/server.py
# undo (run in the same folder, or use -s user)
claude mcp remove bittensor -s local
```
The tools appear as `mcp__bittensor__<tool>`. Each session starts its own server process, about 88 MB idle. It opens
no chain connection until a tool is called.

## Known chain/SDK gaps
**The TAO flow EMA is frozen.** On spec 493 (checked 2026-11-23 at block ~9,569,876), no subnet's
`SubtensorModule.SubnetEmaTaoFlow` had been updated after block 8,466,530, about 153 days earlier. For netuids 51 and 64,
the value was the same at each daily checkpoint across the 7 days before that check.
- The `subnets` tool's `tao_flow_per_day`, and the catalog read `subnet_tao_flows`, return that old value.
- The `subnets` result now carries `tao_flow_ema` with the last-update block and age, and a warning when the EMA is
  more than a day old. The catalog read has no such label.
- The per-block accumulator `SubnetTaoFlow` (stakes minus unstakes, in rao) is still written. For netuids 51 and
  64 it showed no reset across those 7 days. Its change between two blocks is the real net flow over that window. `subnets(flow_window=N)` reports
  it; see below.
- That accumulator change does not equal the change in the pool's `SubnetTAO`, and the gap is unexplained.

On spec 493 (2026-11-23), the catalog read `subnet_convictions` fails with `StorageFunctionNotFound`. It named
`SubtensorModule.DecayingOwnerLock` in one run and `SubtensorModule.OwnerLock` in the next: the SDK asks for storage
the live runtime doesn't have. The tool returns that message as a normal error.

## Windowed net flow (`subnets` with `flow_window`)
The tool reads `SubnetTaoFlow`, `SubnetTAO`, `NetworkRegisteredAt` and `SubnetEmaTaoFlow` at both ends of the
window. The end is pinned to one block number, so head reads don't drift. The start goes through the same
`System.Number` check as other historical reads.

New columns:
- `window_net_flow_tao`: the counter's change, a **total over the window**, not per day.
- `window_pool_change_tao`: the change in the pool's TAO over the same window.
- `pool_tao`: the pool's TAO at the end block.

The envelope's `net_flow` gives the window, `from_block`, `to_block`, a definition, and `excluded`, which maps each
null row to its reason:
- **Root (netuid 0):** it has no alpha pool, and its own counter's change can dwarf any subnet's (+24,051 TAO over
  the 7 days to block 9,569,858). So its figure is `root_net_flow_tao`, and its row stays out of the ranking.
- **Re-registered:** the subnet registered inside the window, so the counter belongs to a different subnet.
- **Counter may have been reset:** the EMA's update block changed inside the window. The runtime appears to
  reset the counter when it updates the EMA. This is inferred from four sampled archive blocks (8,466,520, 8,466,521,
  8,466,522 and 8,466,529): the counters of netuids 51, 64 and 19 read 0 at each, and netuid 51's EMA had been updated
  in that same block. If the chain starts updating the EMA again, most rows will show this, and
  `tao_flow_per_day` becomes the measure to use.

Check (2026-11-23): at block 9,569,858 with 7,200- and 50,400-block windows, all 128 subnet rows matched an
earlier SDK script. That script reads the same counter with the same formula, so this checks the tool's code, not what
the counter means. A window that starts beyond finney's retention needs the archive endpoint, which
rate-limits historical work, so long windows can fail with `RpcPolicyError`. A 1,200,000-block window (start before
the freeze) measured no rows, as intended: 104 were excluded for the EMA reason, the rest as root or re-registered.

## License
MIT: see [LICENSE](LICENSE). How to contribute: [CONTRIBUTING.md](CONTRIBUTING.md).
