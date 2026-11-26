# bittensor-mcp

[![tests](https://github.com/smartguy0505/bittensor-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/smartguy0505/bittensor-mcp/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/smartguy0505/bittensor-mcp/blob/main/LICENSE)

A read-only MCP server for the Bittensor chain. It runs over stdio and is built on the official `mcp` Python SDK (2.3)
and the `bittensor` 11.3 SDK.

The server never loads a wallet and does not expose any transaction. It cannot sign, stake, transfer or register.

## Install
Python 3.10 to 3.14. Install it into its own environment; it pins exact versions of its dependencies.
```bash
python3 -m venv ~/bittensor-mcp-env
~/bittensor-mcp-env/bin/pip install bittensor-mcp
~/bittensor-mcp-env/bin/bittensor-mcp --version
```
The latest code from GitHub instead: `pip install git+https://github.com/smartguy0505/bittensor-mcp.git`.
`bittensor-mcp` is the server. An MCP client starts it and talks to it over stdin/stdout; it takes no arguments
besides `--version` and `--help`, and is configured with environment variables (see Config).

## Tools
| tool | what it returns |
|---|---|
| `chain_status` | Head block, finalized block, runtime spec version, block time, chain timestamp |
| `subnets` | Every subnet as one table: name, symbol, alpha spot price, burn, neurons, tempo, the chain's TAO flow EMA per day (with its age; see below), and the pool (`pool_tao`, `pool_alpha`, `alpha_out`). Sortable, by pool size too. `flow_window` (in blocks) adds the real net TAO flow over that window, and `sort_by="net_flow"` ranks by it (default window 7200). |
| `subnet` | One subnet: identity, owner, pool (tao_in, alpha_in, alpha_out), spot and moving price, registration settings, epoch position, all hyperparameters, neuron counts |
| `metagraph` | A subnet's neurons as a columns + rows table: incentive, dividends, emission, stake, registration, last update, optional axon. Sort, page, filter by validators/miners/coldkey. |
| `neuron` | One neuron by uid or hotkey, with its rank by incentive and by stake |
| `account` | For a coldkey: free balance, stake positions, stake valued in TAO, owned hotkeys, identity |
| `hotkey` | Owner coldkey, subnets and uids, delegate status and take, identity |
| `list_reads` / `describe_read` / `read` | The SDK's full catalog of about 100 named read-only queries (staking, delegation, locks, crowdloans, weights, commitments…), runnable by name |

Every tool takes an optional `block`: an absolute block number, or a negative number counted back from the head
(`-7200` is about one day).

## Examples
What a model calls for some common questions:

| question | call |
|---|---|
| Which subnets have the most TAO in their pools? | `subnets(sort_by="pool", limit=10)` |
| Which subnets took in the most TAO over the last 7 days? | `subnets(sort_by="net_flow", flow_window=50400, limit=10)` |
| Which subnets' alpha is priced highest? | `subnets(sort_by="price", limit=11)` (root, netuid 0, is first at 1.0) |
| Top miners on subnet 19 by incentive | `metagraph(netuid=19, miners_only=True, sort_by="incentive", limit=10)` |
| Top validators on subnet 19 by stake | `metagraph(netuid=19, validators_only=True, sort_by="stake", limit=10)` |
| Subnet 19's registration burn a week ago | `read(name="burn", params={"netuid": 19}, block=-50400)` |

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
| `BT_MCP_LOG` | `WARNING` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case. Logs go to stderr. |

An unset or empty variable takes its default. A bad value (`BT_MCP_MAX_CHARS` that is not a whole number of at least
2000, an unknown log level) stops the server at start with one line on stderr naming the variable.

## From a checkout, and tests
```bash
git clone https://github.com/smartguy0505/bittensor-mcp.git && cd bittensor-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -I -m unittest discover -s tests   # offline, no network
.venv/bin/python -I test_client.py                  # against the live chain
```
In a checkout, `.venv/bin/python -I server.py` also starts the server, without the package being installed (its
dependencies are). `test_client.py --cmd ~/bittensor-mcp-env/bin/bittensor-mcp` runs the live checks against an
installed command instead.
`tests/test_offline.py` checks the helpers, and `Chain` and the `subnets` tool with fakes, without a network.

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
claude mcp add bittensor -- ~/bittensor-mcp-env/bin/bittensor-mcp
# every session
claude mcp add -s user bittensor -- ~/bittensor-mcp-env/bin/bittensor-mcp
# undo (run in the same folder, or use -s user)
claude mcp remove bittensor -s local
```
From a checkout, register `/path/to/bittensor-mcp/.venv/bin/python -I /path/to/bittensor-mcp/server.py` instead.
Registrations made that way before the package existed keep working.
The tools appear as `mcp__bittensor__<tool>`. Each session starts its own server process, about 88 MB idle. It opens
no chain connection until a tool is called.

## Known chain/SDK gaps
**The TAO flow EMA is frozen.** On spec 493 (checked 2026-11-23 at block ~9,569,876), no subnet's
`SubtensorModule.SubnetEmaTaoFlow` had been updated after block 8,466,530, about 153 days earlier. For netuids 51 and 64,
the value was the same at each daily checkpoint across the 7 days before that check.
- The `subnets` tool's `tao_flow_per_day`, and the catalog read `subnet_tao_flows`, return that old value.
- The `subnets` result, and the result of `read("subnet_tao_flows")`, carry `tao_flow_ema` with the last-update
  block and age, and a warning when the EMA is more than a day old. The server's instructions tell clients the same,
  and point them to the windowed net flow.
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

`pool_tao`, the pool's TAO at the end block, is in every `subnets` table since 0.1.1.

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
the counter means. A window that starts beyond finney's retention needs the archive endpoint. The public
endpoints rate-limit historical work, so long windows can fail with `RpcPolicyError`; the server returns that at
once, without asking again. A 1,200,000-block window (start before
the freeze) measured no rows, as intended: 104 were excluded for the EMA reason, the rest as root or re-registered.

## License
MIT: see [LICENSE](https://github.com/smartguy0505/bittensor-mcp/blob/main/LICENSE). How to contribute: [CONTRIBUTING.md](https://github.com/smartguy0505/bittensor-mcp/blob/main/CONTRIBUTING.md).
