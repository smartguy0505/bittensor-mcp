# Contributing to bittensor-mcp

For people and for coding agents. It says how to get going, where things are,
what must stay true, and what a finished change looks like. What the server
does, and what was checked against the chain, is in [README.md](README.md).

- [Quick start](#quick-start)
- [Where things are](#where-things-are)
- [What must stay true](#what-must-stay-true)
- [Making a change](#making-a-change)
- [Tests](#tests)
- [Pull requests](#pull-requests)
- [For coding agents](#for-coding-agents)
- [What to work on](#what-to-work-on)
- [License](#license)

## Quick start

You need Python 3.10 to 3.14 (what `bittensor` 11.3 accepts) and network access
to the public Bittensor endpoints. The code is developed on 3.12.

```
git clone https://github.com/smartguy0505/bittensor-mcp.git && cd bittensor-mcp
python3 -m venv .venv && .venv/bin/pip install -e .   # the package, editable, with its pinned dependencies
.venv/bin/python -I -m unittest discover -s tests   # offline, about 20 seconds
.venv/bin/python -I test_client.py                  # live chain, a few minutes; exit 0 = all checks passed
```

To use it from Claude Code, see "Use with Claude Code" in the README.

## Where things are

| file | what it is |
|---|---|
| `bittensor_mcp/server.py` | the whole server, one module; `main` is the `bittensor-mcp` command |
| `bittensor_mcp/__init__.py` | `__version__`, the one place the version is set |
| `bittensor_mcp/__main__.py` | `python -m bittensor_mcp` |
| `server.py` | the launcher for a checkout: `python -I server.py` |
| `pyproject.toml` | the package: name, the command, the three pinned dependencies |
| `CHANGELOG.md` | what each release changed |
| `tests/test_offline.py` | offline tests, no network: the helpers, `Chain`, the `subnets` and `read` tools with fakes, and both launch paths |
| `test_client.py` | the end-to-end test: a real stdio MCP session against the live chain |

`bittensor_mcp/server.py`, from the top:

| section | what it does |
|---|---|
| config | `BT_MCP_*` environment variables |
| `Chain` | one lazy head client and one archive client, the historical-state check (`_proven`), `resolve_block`, and `run_at`, which turns errors into `ToolError`: an endpoint's refusal (`RpcPolicyError`) at once, other failures after one reconnect |
| serialization | `jsonable` (SDK objects to JSON), `render` (the size cap, with notes on what was cut), `_table` (columns + rows) |
| generic catalog tools | `chain_status`, `list_reads`, `describe_read`, `read` |
| curated tools | `subnets` (with the flow-EMA age and the windowed net flow), `subnet`, `metagraph`, `neuron`, `account`, `hotkey` |
| `main` | parses `--version` / `--help`, then `mcp.run("stdio")` |

## What must stay true

A change that breaks one of these needs a discussion first.

1. **Read-only.** No wallet, no keys, no signing, no extrinsics. Nothing from
   the SDK's transaction tools (`Subtensor.tools()`) is exposed or called.
2. **No wrong numbers from old blocks.** Some endpoints answer storage
   *defaults* for blocks they no longer hold. Every read goes through
   `Chain.run` or `Chain.run_at`, which checks `System.Number` at the block and
   falls back to the archive endpoint, or fails.
3. **Units are explicit.** SDK `Balance` values become TAO, or
   `{"alpha": x, "netuid": n}`. Raw chain integers stay raw and are labelled as
   raw. A new field says its unit in its name or in a `units` note.
4. **Stale or derived values are labelled.** The chain's TAO-flow EMA has been
   frozen for months, so `subnets` reports its age. A windowed figure says its
   window. A row that can't be measured is null, with the reason in the
   result, never 0.
5. **Errors a caller can act on are `ToolError`,** with a message that says
   what to change. An endpoint's refusal under its traffic policy is returned
   at once, since asking again spends more of its budget. Anything else is
   reported after one reconnect as "chain request failed after reconnecting:
   ...". No tracebacks, and no raw SDK objects (`<bittensor....>`,
   `object at 0x...`) in results.
6. **Results stay under `BT_MCP_MAX_CHARS`.** Lists of rows are columnar
   tables, and what `render` cuts is noted under `truncated`.
7. **Every way of starting keeps working:** the `bittensor-mcp` command and
   `python -m bittensor_mcp` from an installed package, and
   `python -I server.py` from a checkout, which clients were registered with
   before there was a package. `-I` keeps the script's folder off `sys.path`,
   so the launcher puts the checkout first itself. Starting the server opens
   no chain connection; the first tool call does.
8. **Nothing from your machine in tracked files:** no keys, addresses, host
   names, user names or local paths.

## Making a change

For anything that is more than a fix, open an issue first and say what you
want to change, why, and how you will show that it works. Always ask before
you add or remove a tool, rename or remove a field of a result, or change a
default or a dependency (`pyproject.toml` pins exact versions: the server
uses SDK internals that change between releases).

**Releases** are made by the maintainer: raise `__version__` in
`bittensor_mcp/__init__.py`, add an entry to `CHANGELOG.md` (say what changed
in results, not only what was added), commit, build from a clean tree
(`python -m build`), check (`twine check --strict dist/*`), upload, and tag
the commit `vX.Y.Z`. A version number can be published only once, so check
before uploading: the offline tests pass from the unpacked sdist, and the
built command starts.

- **Small steps.** One change per pull request; no clean-ups on the side.
- **Check against the chain.** A new figure is compared at a fixed block with
  a reference computed another way, and the comparison goes in the pull
  request (the README's net-flow section shows one).
- **Docstrings are the documentation that models read.** An MCP client shows
  a tool's docstring and parameter descriptions to the model, so they say
  units, defaults and limits.
- **Style:** lines of up to 120 columns, formatted by hand. Do not run a
  formatter over the file; match what is around you. Comments say why.
- **README.** A change that matters gets a paragraph: what it does, what was
  checked, and what is *not* covered.

## Tests

There are two layers.

`tests/test_offline.py` needs no network and runs in about 20 seconds,
most of it starting the server in subprocesses. It uses only `unittest`, with
fakes for the chain: the retry and refusal paths of `Chain.run_at`,
`resolve_block`, the historical-state check, `jsonable`, `render` under its
cap, the flow-EMA age, every exclusion of the windowed net flow, the
`subnets` tool's pool columns and sorts, the `read` tool's label, that
`python -I server.py` and `main` start and stop as they should, and that a bad
setting stops the server with one clear line. Add to it for any helper you change. CI
(`.github/workflows/tests.yml`) installs the package on Python 3.10, 3.12 and
3.14 for every push and pull request, starts the command and the module from
outside the checkout, and runs these tests.

`test_client.py` starts the server as a subprocess (the checkout's
`python -I server.py`, or with `--cmd` an installed command such as
`bittensor-mcp`), talks to it over stdio, and checks:

- the handshake, the tool list, and the read-only annotations;
- every tool, and its error paths;
- a historical read 3,000,000 blocks back;
- the windowed net flow;
- the pool columns, against the `subnet` tool's `tao_in`, `alpha_in` and
  `alpha_out` at one block;
- a sweep of 94 catalog reads, which may fail only with a clean, explained
  error.

It needs the network and reads live chain state, so CI never runs it, and its
numbers change from run to run. The public endpoints limit historical work:
after many history reads, one refuses with `RpcPolicyError ... budget_exhausted`
for a while (the error doesn't say which endpoint). Run the suite once per
change, not in a loop.

What is expected of tests for a change:

- **A check that fails without it.** Undo the change, or break the line it is
  about, and see the check fail.
- **Error paths too:** bad input gives a `ToolError` that says what to change.

## Pull requests

Before you open one:

- [ ] the offline tests pass, and `test_client.py` passes (say when you ran it)
- [ ] a check fails without the change (say how you checked)
- [ ] nothing under "What must stay true" is broken
- [ ] new figures are compared with a reference at a fixed block
- [ ] README and docstrings say what is true now
- [ ] no keys, addresses, host names or paths of your machine in the diff

The title says what changes, in a few words, in the style of the log
(`git log --oneline`). The text says why, what you ran, and what is still open.
Pushed commits are not rewritten: no force pushes to `main`.

## For coding agents

All of the above applies. Also:

**Read in this order:** the README, the section of `bittensor_mcp/server.py` you work on, the
tool docstrings next to it, `test_client.py`.

**Ask the person you work for before you:** commit, push or open a pull
request, unless they have told you to; add a tool, change a result's fields, a
default or a dependency; delete anything you did not make.

**Do not:**

- load a wallet, read key files, or call anything that signs or submits;
- make a test pass by weakening it: no looser checks, skips or retries. If an
  expectation is wrong, say why in the pull request;
- run the live suite in a loop; the endpoints' history budget is shared;
- report a number you have not read from the chain, or a check you have not
  run;
- put text from the machine you run on into tracked files.

**Before you say it is done:** run both test layers; break your change and see a check
fail; read your own diff once more, as if someone else had written it; grep it
for private text; list what you did not do or could not verify. Commits that an
agent wrote carry a `Co-Authored-By` line.

**Good to know:** a running MCP client keeps the server process it started.
After you change the server, test with a fresh process (`test_client.py`
starts one), or reconnect the client (`/mcp` in Claude Code). A package
installed without `-e` keeps the code it was installed with.

## What to work on

| what | where | how to tell that it works |
|---|---|---|
| why pool change and net flow differ | `_window_flow`, README | a reconciliation that closes at a fixed block |
| releases through PyPI trusted publishing, so no token is needed | a workflow in `.github/workflows`, the PyPI project's settings | pushing a tag publishes, and `pip install bittensor-mcp==X.Y.Z` works |
| the `subnet_convictions` read, which the SDK asks for storage the runtime lacks | upstream SDK | the read returns data |

Found a bug? Open an issue with the commit, the tool call, the block, and what
you saw and expected.

## License

MIT: see [LICENSE](LICENSE). By contributing, you agree that your contributions
are licensed under the same terms.
