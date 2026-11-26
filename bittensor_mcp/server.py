"""Read-only Bittensor MCP server (stdio).

Wraps the bittensor 11.x SDK's catalog of named chain reads (Subtensor.reads() / Subtensor.read()) and adds a few
curated tools (subnets, subnet, metagraph, neuron, account, hotkey) whose output is sized for an LLM context.

Read-only by design: it never loads a wallet and does not expose the SDK's transactions (Subtensor.tools(),
execute_tool, plan, execute). Nothing here can sign or spend.

Start: the bittensor-mcp command, python -m bittensor_mcp, or python -I server.py from a checkout. It speaks MCP on
stdin/stdout; --version prints the version.

Config (environment):
  BT_MCP_NETWORK    chain for head reads: finney (default), test, local, or a ws:// / wss:// URL
  BT_MCP_ARCHIVE    archive endpoint for historical reads the primary cannot prove (default: archive)
  BT_MCP_MAX_CHARS  hard cap on one tool result, in characters (default 30000, roughly 10k tokens)
  BT_MCP_LOG        log level for stderr (default WARNING)
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import enum
import ipaddress
import json
import logging
import math
import os
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

import bittensor as bt
from bittensor.balance import Balance
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from bittensor_mcp import __version__

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET", "WARN", "FATAL")  # every name logging takes


def _config_error(msg: str) -> None:
    """A bad setting stops the server at start with one line on stderr, which MCP clients show in their logs."""
    sys.exit(f"bittensor-mcp: {msg}")


def _env_int(name: str, default: int, least: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = least - 1
    if value < least:
        _config_error(f"{name} must be a whole number of at least {least}, got {raw!r}")
    return value


_level = os.environ.get("BT_MCP_LOG", "").strip().upper() or "WARNING"
if _level not in LOG_LEVELS:
    _config_error(f"BT_MCP_LOG must be one of {', '.join(LOG_LEVELS)}, got {os.environ.get('BT_MCP_LOG')!r}")
logging.basicConfig(stream=sys.stderr, level=_level)
log = logging.getLogger("bittensor-mcp")

NETWORK = os.environ.get("BT_MCP_NETWORK", "").strip() or "finney"
ARCHIVE = os.environ.get("BT_MCP_ARCHIVE", "").strip() or "archive"
MAX_CHARS = _env_int("BT_MCP_MAX_CHARS", 30000, 2000)  # render needs room for the envelope and its notes
U16 = 65535.0
RAO = 1e9


# --------------------------------------------------------------------------------------------- chain access
class Chain:
    """One lazily connected head client plus an archive client, used by one thread at a time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._st: Any = None
        self._archive: Any = None
        self._catalog: dict[str, dict] | None = None

    @property
    def st(self) -> Any:
        if self._st is None:
            self._st = bt.Subtensor(NETWORK)
        return self._st

    @property
    def archive(self) -> Any:
        if self._archive is None:
            self._archive = bt.Subtensor(ARCHIVE)
        return self._archive

    def _reset(self) -> None:
        for c in (self._st, self._archive):
            try:
                if c is not None:
                    c.close()
            except Exception:  # teardown of a dead socket may fail; the client is dropped anyway
                pass
        self._st = self._archive = None

    def catalog(self) -> dict[str, dict]:
        with self._lock:
            if self._catalog is None:
                self._catalog = {r["name"]: r for r in self.run(lambda st: st.reads())}
            return self._catalog

    def resolve_block(self, block: int | None) -> int | None:
        """None = head. Negative = relative to head (-7200 is about a day ago at 12 s blocks)."""
        if block is None:
            return None
        head = int(self.st.block)
        b = head + block if block < 0 else block
        if b < 0 or b > head:
            raise ToolError(f"block {block} is outside 0..{head} (current head)")
        return b

    @staticmethod
    def _proven(view: Any, block: int) -> bool:
        # A node without state at `block` answers storage DEFAULTS, not an error: System.Number there reads 0.
        n = view.query(["System", "Number"])
        n = n.value if hasattr(n, "value") else n
        return int(n or 0) == block

    def _view(self, block: int | None) -> tuple[Any, int]:
        if block is None:
            return self.st, int(self.st.block)
        snap = self.st.at(block)
        if self._proven(snap, block):
            return snap, block
        snap = self.archive.at(block)
        if self._proven(snap, block):
            return snap, block
        raise ToolError(f"no node reachable here has state at block {block} (checked {NETWORK} and {ARCHIVE}); "
                        "the result would be storage defaults, so nothing was returned")

    def run(self, fn: Callable[[Any], Any], block: int | None = None) -> Any:
        """fn(view) at head or at a proven historical block. Retries once on a fresh connection."""
        return self.run_at(fn, block)[0]

    def run_at(self, fn: Callable[[Any], Any], block: int | None = None) -> tuple[Any, int]:
        with self._lock:
            for attempt in (1, 2):
                try:
                    b = self.resolve_block(block)
                    view, at = self._view(b)
                    return fn(view), at
                except ToolError:
                    raise
                except bt.RpcPolicyError as e:  # the endpoint refused under its traffic policy: a reconnect spends more
                    raise ToolError(_policy_message(e)) from e
                except (ValueError, TypeError, KeyError) as e:  # bad input (SS58 checksum, u16 range, missing param)
                    raise ToolError(f"{type(e).__name__}: {e}") from e
                except Exception as e:  # connection drop, timeout, decode failure: reconnect once
                    if attempt == 2:
                        raise ToolError(f"chain request failed after reconnecting: {type(e).__name__}: {e}") from e
                    log.warning("chain call failed (%s: %s); reconnecting", type(e).__name__, e)
                    self._reset()
        raise AssertionError("unreachable")


def _policy_message(e: Exception) -> str:
    """An endpoint's refusal (rate limit, exhausted history budget), with what the caller can do about it."""
    tags = ", ".join(f"{k}={v}" for k in ("policy", "reason", "retry_after") if (v := getattr(e, k, None)))
    return (f"RpcPolicyError: {e}" + (f" ({tags})" if tags else "") + ". The endpoint refused under its traffic "
            "policy and was not asked again: wait before retrying, and ask for less history (a later block, a "
            "shorter flow_window).")


chain = Chain()


# --------------------------------------------------------------------------------------------- serialization
def jsonable(x: Any, depth: int = 0) -> Any:
    """SDK values -> plain JSON. TAO amounts become numbers in TAO; alpha amounts {"alpha": x, "netuid": n}."""
    if depth > 40:
        return str(x)
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, Balance):
        n = getattr(x, "netuid", 0) or 0
        return x.amount if n == 0 else {"alpha": x.amount, "netuid": n}
    if isinstance(x, enum.Enum):
        return x.name
    if isinstance(x, (bytes, bytearray, memoryview)):
        return "0x" + bytes(x).hex()
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {f.name: jsonable(getattr(x, f.name), depth + 1) for f in dataclasses.fields(x) if f.name != "raw"}
    if isinstance(x, Mapping):
        return {str(k): jsonable(v, depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in x]
    if hasattr(x, "model_dump"):
        return jsonable(x.model_dump(), depth + 1)
    if hasattr(x, "value") and not callable(x.value):
        return jsonable(x.value, depth + 1)
    if hasattr(x, "__dict__"):
        return {k: jsonable(v, depth + 1) for k, v in vars(x).items() if not k.startswith("_")}
    return str(x)


def _dumps(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), ensure_ascii=False)


def _target(obj: Any) -> tuple[str, Any, Any, Any]:
    """(path, parent, key, node) of the collection to shorten: follow the biggest child collection while it holds
    at least half of its parent's size, then cut the node whose size is spread over its items."""
    path, parent, key, node = "", None, None, obj
    for _ in range(8):
        kids = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else []
        colls = [(k, v, len(_dumps(v))) for k, v in kids if isinstance(v, (list, dict)) and len(v) > 1]
        if not colls:
            break
        k, v, size = max(colls, key=lambda t: t[2])
        if size < 0.5 * len(_dumps(node)) and parent is not None:
            break
        path, parent, key, node = (f"{path}.{k}" if path else str(k)), node, k, v
    return path, parent, key, node


def render(obj: Any) -> str:
    """Compact JSON, cut to MAX_CHARS by shortening the biggest list/dict (each cut noted in "truncated")."""
    obj = jsonable(obj)
    if not isinstance(obj, dict):
        obj = {"data": obj}
    prior = obj.get("truncated") if isinstance(obj.get("truncated"), list) else []
    notes: dict[str, tuple[int, int]] = {}

    def dump() -> str:  # the notes count toward the cap, so every size check includes them
        if not notes:
            return _dumps(obj)
        return _dumps({**obj, "truncated": prior + [f"{p}: showing {k} of {t}" for p, (k, t) in notes.items()] + [
            f"result capped at {MAX_CHARS} chars; narrow with limit/offset/filters"]})

    s = dump()
    for _ in range(40):
        if len(s) <= MAX_CHARS:
            return s
        path, parent, key, node = _target(obj)
        if parent is None or not isinstance(node, (list, dict)) or len(node) <= 1:
            break
        keep = max(1, min(len(node) - 1, int(len(node) * MAX_CHARS * 0.85 / len(s))))
        parent[key] = node[:keep] if isinstance(node, list) else dict(list(node.items())[:keep])
        notes[path] = (keep, notes.get(path, (0, len(node)))[1])
        s = dump()
    return s if len(s) <= MAX_CHARS else s[: MAX_CHARS - 80] + f"... [cut at {MAX_CHARS} chars; narrow the request]"


RO = ToolAnnotations(read_only_hint=True, open_world_hint=True)
mcp = MCPServer(
    "bittensor",
    version=__version__,
    instructions=(
        "Read-only access to the Bittensor (TAO) chain. Start with the curated tools (subnets, subnet, metagraph, "
        "neuron, account, hotkey); for anything else, list_reads -> describe_read -> read. Every tool takes an "
        "optional `block` (absolute, or negative = relative to head; 7200 blocks ~ 1 day) and reports the block it "
        "read at. Amounts in the SDK's Balance type come out as plain numbers in TAO, or {\"alpha\": x, \"netuid\": "
        "n} for alpha. Plain integers from `read` (and fields the curated tools mark raw) are chain units: rao = "
        "1e-9 TAO/alpha, u16 fractions /65535, U64F64 /2**64; describe_read documents each read's units. The chain's "
        "TAO-flow EMA (subnets' tao_flow_per_day, the subnet_tao_flows read) can be months old: results carry its age "
        "under tao_flow_ema. For current net TAO flow use subnets(sort_by=\"net_flow\", flow_window=N): stakes "
        "minus unstakes over the last N blocks, a total, not per day. No wallet access, no transactions."
    ),
)

Block = Annotated[int | None, Field(description="Block to read at: absolute number, or negative = blocks before "
                                                "head (-7200 ~ 1 day). Omit for the current head.")]


def _envelope(at: int, **kw: Any) -> dict:
    return {"network": NETWORK, "block": at, **kw}


def _table(rows: list[dict]) -> dict:
    """Rows -> {"columns": [...], "rows": [[...], ...]}: the keys are sent once, not once per row."""
    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    return {"columns": cols, "rows": [[r.get(c) for c in cols] for r in rows]}


# --------------------------------------------------------------------------------------------- generic catalog tools
@mcp.tool(annotations=RO, structured_output=False)
def chain_status() -> str:
    """Current chain head, finalized block, runtime spec version, block time and chain timestamp."""
    def f(st: Any) -> dict:
        return {"network": NETWORK, "endpoint": getattr(st, "endpoint", None), "head": int(st.block),
                "finalized": int(st.finalized_block), "spec_version": st.spec_version,
                "block_time_s": st.read("block_time"), "timestamp": st.read("timestamp")}
    return render(chain.run(f))


@mcp.tool(annotations=RO, structured_output=False)
def list_reads(category: Annotated[str | None, Field(description="Only this category (case-insensitive), e.g. "
                                                                  "Staking, Subnets, Delegation")] = None) -> str:
    """The SDK's catalog of named read-only chain queries (about 100), grouped by category, with their parameters.
    Use describe_read for one read's full documentation, then read to run it."""
    cat = chain.catalog()
    groups: dict[str, list] = {}
    for r in cat.values():
        if category and r.get("category", "").lower() != category.lower():
            continue
        groups.setdefault(r.get("category", "Other"), []).append(
            {"name": r["name"], "params": r.get("params", {}), "summary": r.get("summary", "")})
    if not groups:
        raise ToolError(f"no category {category!r}; categories: {sorted({r.get('category') for r in cat.values()})}")
    return render(groups)


def _unknown_read(name: str) -> ToolError:
    close = difflib.get_close_matches(name, list(chain.catalog()), n=6, cutoff=0.5)
    return ToolError(f"unknown read {name!r}" + (f"; did you mean {close}?" if close else "; see list_reads"))


@mcp.tool(annotations=RO, structured_output=False)
def describe_read(name: Annotated[str, Field(description="Read name from list_reads, e.g. stake_for_coldkey")]) -> str:
    """Full documentation of one named read: description, parameter docs, units and docs URL."""
    r = chain.catalog().get(name)
    if r is None:
        raise _unknown_read(name)
    return render(r)


@mcp.tool(annotations=RO, structured_output=False)
def read(
    name: Annotated[str, Field(description="Read name from list_reads, e.g. subnet_hyperparameters")],
    params: Annotated[dict[str, Any] | None, Field(description="Parameters as listed for that read, e.g. "
                                                               "{\"netuid\": 19}")] = None,
    block: Block = None,
    limit: Annotated[int, Field(ge=1, le=5000, description="Max items kept from a list/dict result")] = 300,
) -> str:
    """Run any named read from the SDK catalog (see list_reads / describe_read). Balance amounts are converted
    (TAO number, or {"alpha", "netuid"}); plain integers are raw chain units (rao = 1e-9, u16 /65535, U64F64
    /2**64) as describe_read documents. Large results are cut to the first `limit` items and to the size cap;
    prefer the curated tools for metagraph-sized data."""
    spec = chain.catalog().get(name)
    if spec is None:
        raise _unknown_read(name)
    params = dict(params or {})
    allowed = set(spec.get("params", {}))
    extra = set(params) - allowed
    if extra:
        raise ToolError(f"{name} takes {sorted(allowed) or 'no parameters'}; unexpected {sorted(extra)}")
    label = name == "subnet_tao_flows"  # the chain's flow EMA, which can be months old: say how old
    (data, ema), at = chain.run_at(lambda v: (v.read(name, **params), _ema_blocks(v) if label else None), block)
    data = jsonable(data)
    total = len(data) if isinstance(data, (list, dict)) else None
    if isinstance(data, list) and len(data) > limit:
        data = data[:limit]
    elif isinstance(data, dict) and len(data) > limit:
        data = dict(list(data.items())[:limit])
    out = _envelope(at, read=name, params=params, **({"tao_flow_ema": _ema_age(ema, at)} if label else {}), data=data)
    if total is not None and total > limit:
        out["truncated"] = [f"data: showing {limit} of {total} (raise limit to see more)"]
    return render(out)


# --------------------------------------------------------------------------------------------- curated tools
FlowWindow = Annotated[int | None, Field(ge=1, le=2_000_000, description=(
    "Also measure net TAO flow over this many blocks ending at `block` (7200 ~ 1 day, 50400 ~ 7 days): TAO staked "
    "minus unstaked, from the chain's per-subnet counter. Adds window_net_flow_tao and window_pool_change_tao "
    "columns. Defaults to 7200 when sort_by is net_flow."))]

SUBNETS_UNITS = ("price: TAO per alpha. burn, pool_tao: TAO. pool_alpha, alpha_out: alpha of that subnet. "
                 "tao_flow_per_day: TAO per day. window_net_flow_tao, window_pool_change_tao: TAO, totals over the "
                 "window. Root (netuid 0) has no pool: its pool columns are null.")


@mcp.tool(annotations=RO, structured_output=False)
def subnets(
    sort_by: Literal["netuid", "price", "pool", "tao_flow", "net_flow", "burn", "neurons"] = "netuid",
    limit: Annotated[int, Field(ge=1, le=1024)] = 200,
    flow_window: FlowWindow = None,
    block: Block = None,
) -> str:
    """Every subnet as one table (columns + rows): name, token symbol, alpha spot price (TAO per alpha), registration burn (TAO),
    neuron count, tempo, the chain's net TAO flow EMA (TAO per day; negative = net outflow), and the pool: pool_tao
    and pool_alpha are its reserves (the subnet tool's tao_in and alpha_in), alpha_out is the subnet's alpha held
    outside the pool. sort_by="pool" ranks by pool_tao; root (netuid 0) has no pool, so it comes last. The chain updates
    that EMA lazily: `tao_flow_ema` in the result gives its last-update block and age, and warns when it is stale
    (on spec 493 it has not moved since block 8,466,530), so check it before reading tao_flow_per_day as current.

    With flow_window (or sort_by="net_flow"), it also measures the real net flow over a window of blocks: the change
    in the chain's stake-minus-unstake counter between the window's two ends, as a total in TAO, not per day. A
    subnet re-registered inside the window, or whose counter may have been reset there, gets null and a reason
    under net_flow.excluded."""
    if sort_by == "net_flow" and flow_window is None:
        flow_window = 7200
    end = block
    if flow_window is not None:
        # Pin the window's end to a number: the live client's reads would drift with the head.
        end = chain.run(lambda st: int(st.block) if block is None else chain.resolve_block(block))
        if end - flow_window <= 0:  # run_at reads a non-positive block as "relative to head": a different window
            raise ToolError(f"flow_window {flow_window} reaches before block 1 (window ends at block {end})")

    def f(v: Any) -> tuple[list[dict], set[int] | str, dict | None]:
        infos, prices = v.read("subnets"), v.read("alpha_prices")
        names, symbols, flows = v.read("subnet_names"), v.read("token_symbols"), v.read("subnet_tao_flows")
        state = _flow_state(v) if flow_window is not None else None
        if state is not None:
            ema: set[int] | str = set(state["ema"].values())
        else:
            ema = _ema_blocks(v)
        tao = state["pool"] if state is not None else _storage_map(v, "SubnetTAO")  # one read per block and item
        alpha_in, alpha_out = _storage_map(v, "SubnetAlphaIn"), _storage_map(v, "SubnetAlphaOut")
        rows = []
        for s in infos:
            n = s.netuid
            flow = flows.get(n)
            pool = n != 0  # root's SubnetTAO is the stake on root, not a pool
            rows.append({"netuid": n, "name": names.get(n), "symbol": symbols.get(n), "price": prices.get(n),
                         "burn": jsonable(s.burn), "neurons": s.neuron_count, "tempo": s.tempo,
                         "tao_flow_per_day": None if flow is None else round(flow / RAO * 7200, 4),
                         "pool_tao": round(tao.get(n, 0) / RAO, 4) if pool else None,
                         "pool_alpha": round(alpha_in.get(n, 0) / RAO, 4) if pool else None,
                         "alpha_out": round(alpha_out.get(n, 0) / RAO, 4) if pool else None})
        return rows, ema, state
    (rows, ema, end_state), at = chain.run_at(f, end)
    extra: dict[str, Any] = {}
    if flow_window is not None:
        start = at - flow_window
        start_state, _ = chain.run_at(_flow_state, start)
        extra["net_flow"] = _window_flow(rows, start_state, end_state, start, at)
    key = {"netuid": "netuid", "price": "price", "pool": "pool_tao", "tao_flow": "tao_flow_per_day",
           "net_flow": "window_net_flow_tao", "burn": "burn", "neurons": "neurons"}[sort_by]
    rows.sort(key=lambda r: (r[key] is None, r[key] if sort_by == "netuid" else -(r[key] or 0)))
    return render(_envelope(at, count=len(rows), sort_by=sort_by, units=SUBNETS_UNITS, tao_flow_ema=_ema_age(ema, at),
                            **extra, subnets=_table(rows[:limit])))


def _storage_map(v: Any, item: str) -> dict:
    """One SubtensorModule storage map at the view's block, as {key: raw value}. Missing keys mean the default."""
    return dict(v.query_map(["SubtensorModule", item]))


def _flow_state(v: Any) -> dict[str, dict]:
    """The storage the windowed net flow is computed from, read at one block. Missing map entries mean 0 / unset."""
    return {"acc": _storage_map(v, "SubnetTaoFlow"), "pool": _storage_map(v, "SubnetTAO"),
            "reg": _storage_map(v, "NetworkRegisteredAt"),
            "ema": {n: val[0] for n, val in _storage_map(v, "SubnetEmaTaoFlow").items()}}


def _window_flow(rows: list[dict], s: dict, e: dict, start: int, end: int) -> dict:
    """Adds window_net_flow_tao and window_pool_change_tao to each row; returns the envelope block.

    SubnetTaoFlow adds every stake and subtracts every unstake (rao). When the runtime updates the EMA it resets the
    counter, so the difference between two blocks is the window's net flow only if the EMA's update block is the same
    at both ends. A re-registered netuid's counter belongs to a different subnet, so that row gets null too.
    Root (netuid 0) has no alpha pool, and its own counter's change can be larger than any subnet's flow.
    So its figure goes in the envelope as root_net_flow_tao and its row stays out of the subnet ranking."""
    excluded: dict[str, str] = {}
    root: float | None = None
    for r in rows:
        n = r["netuid"]
        reg_s, reg_e = s["reg"].get(n), e["reg"].get(n)
        why = None
        if n == 0:
            root = round((e["acc"].get(0, 0) - s["acc"].get(0, 0)) / RAO, 4)
            why = "root: no alpha pool; its own counter's change is root_net_flow_tao, kept out of the subnet ranking"
        elif reg_s != reg_e or (reg_e is not None and reg_e > start):
            why = f"registered at block {reg_e}, inside the window"
        elif s["ema"].get(n) != e["ema"].get(n):
            why = (f"flow EMA updated inside the window (block {s['ema'].get(n)} -> {e['ema'].get(n)}), "
                   "so the counter may have been reset")
        if why:
            excluded[str(n)] = why
            r["window_net_flow_tao"] = r["window_pool_change_tao"] = None
        else:
            r["window_net_flow_tao"] = round((e["acc"].get(n, 0) - s["acc"].get(n, 0)) / RAO, 4)
            r["window_pool_change_tao"] = round((e["pool"].get(n, 0) - s["pool"].get(n, 0)) / RAO, 4)
    return {"window_blocks": end - start, "approx_days": round((end - start) / 7200, 2), "from_block": start,
            "to_block": end,
            "definition": "window_net_flow_tao = change in SubtensorModule.SubnetTaoFlow (TAO staked minus "
                          "unstaked) from from_block to to_block, a total over the window in TAO. "
                          "window_pool_change_tao = change in the pool's SubnetTAO over the same window; it also "
                          "moves with emission and other pool movements and does not reconcile exactly with net flow. "
                          "pool_tao = SubnetTAO at to_block.",
            "root_net_flow_tao": root, "excluded": excluded}


def _ema_blocks(v: Any) -> set[int] | str:
    """The blocks at which SubnetEmaTaoFlow was last updated, or the error as text. It only labels the EMA's age,
    so a runtime without this item, or a refused request, must not cost the result it labels."""
    try:
        return {val[0] for _, val in v.query_map(["SubtensorModule", "SubnetEmaTaoFlow"])}
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:200]


def _ema_age(blocks: set[int] | str, at: int) -> dict:
    """How old the chain's SubnetEmaTaoFlow is. It is stored as (last_update_block, value) and only moves when the
    runtime updates it, so a stale EMA must be labelled or it reads as current flow."""
    if isinstance(blocks, str):
        return {"last_updated_block": None, "error": "could not read the EMA's update block: " + blocks}
    if not blocks:
        return {"last_updated_block": None}
    last = max(blocks)
    age = at - last
    out = {"last_updated_block": last, "age_blocks": age, "age_days": round(age / 7200, 1)}
    if age > 7200:
        out["warning"] = (f"the chain has not updated this EMA for {age:,} blocks (~{age / 7200:.0f} days); "
                          "values taken from it (subnets' tao_flow_per_day, the subnet_tao_flows read) are that "
                          "old, not current flow; for current flow use subnets with sort_by=\"net_flow\"")
    return out


def _ip(axon: dict) -> str | None:
    ip, port = axon.get("ip") or 0, axon.get("port") or 0
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return str(ip)
    return f"[{addr}]:{port}" if addr.version == 6 else f"{addr}:{port}"


SUBNET_RAO_TAO = ("burn", "min_burn", "max_burn", "tao_in", "tao_in_emission")
SUBNET_RAO_ALPHA = ("alpha_in", "alpha_out", "alpha_in_emission", "alpha_out_emission", "pending_alpha_emission")
SUBNET_RAO_UNLABELLED = ("subnet_emission", "pending_root_emission", "subnet_volume")
SUBNET_UNITS = ("burn, min_burn, max_burn, tao_in: TAO. alpha_in, alpha_out, pending_alpha_emission: alpha. "
                "tao_in_emission: TAO per block; alpha_in_emission, alpha_out_emission: alpha per block. "
                "moving_price and spot_price: TAO per alpha. raw_rao_amounts: unconverted integers in rao (1e-9) "
                "whose unit (TAO or alpha) the chain does not label. Other header fields and hyperparameters_raw "
                "are raw chain encodings: u16 fractions /65535, U64F64 /2**64, burns in rao.")


def _subnet_header(mg: dict) -> dict:
    head = {k: v for k, v in mg.items() if not isinstance(v, list)}
    for k in SUBNET_RAO_TAO:
        if isinstance(head.get(k), int):
            head[k] = head[k] / RAO
    for k in SUBNET_RAO_ALPHA:
        if isinstance(head.get(k), int):
            head[k] = {"alpha": head[k] / RAO, "netuid": mg.get("netuid")}
    head["raw_rao_amounts"] = {k: head.pop(k) for k in SUBNET_RAO_UNLABELLED if k in head}
    mp = head.get("moving_price")
    if isinstance(mp, dict) and "bits" in mp:
        head["moving_price"] = mp["bits"] / 2 ** 32
    return head


def _neurons(mg: dict, include_axons: bool) -> list[dict]:
    n = len(mg.get("hotkeys") or [])

    def col(k: str) -> list:
        v = mg.get(k) or []
        return v if len(v) == n else [None] * n

    cols = {k: col(k) for k in ("hotkeys", "coldkeys", "active", "validator_permit", "incentives", "dividends",
                                "consensus", "emission", "alpha_stake", "tao_stake", "total_stake",
                                "block_at_registration", "last_update", "pruning_score", "collateral_locked",
                                "identities", "axons")}
    rows = []
    for uid in range(n):
        g = {k: v[uid] for k, v in cols.items()}
        ident = g["identities"]
        row = {
            "uid": uid, "hotkey": g["hotkeys"], "coldkey": g["coldkeys"],
            "active": g["active"], "validator_permit": g["validator_permit"],
            "incentive": None if g["incentives"] is None else round(g["incentives"] / U16, 6),
            "dividends": None if g["dividends"] is None else round(g["dividends"] / U16, 6),
            "consensus": None if g["consensus"] is None else round(g["consensus"] / U16, 6),
            "emission_alpha_per_epoch": None if g["emission"] is None else g["emission"] / RAO,
            "alpha_stake": None if g["alpha_stake"] is None else g["alpha_stake"] / RAO,
            "tao_stake": None if g["tao_stake"] is None else g["tao_stake"] / RAO,
            "total_stake": None if g["total_stake"] is None else g["total_stake"] / RAO,
            "registered_at_block": g["block_at_registration"], "last_update": g["last_update"],
        }
        if g["pruning_score"] is not None:
            row["pruning_score"] = round(g["pruning_score"] / U16, 6)
        if g["collateral_locked"]:
            row["collateral_locked_alpha"] = g["collateral_locked"] / RAO
        if isinstance(ident, dict) and ident.get("name"):
            row["identity"] = ident.get("name")
        if include_axons and isinstance(g["axons"], dict):
            row["axon"] = _ip(g["axons"])
        rows.append(row)
    return rows


@mcp.tool(annotations=RO, structured_output=False)
def subnet(netuid: Annotated[int, Field(ge=0, le=65535)], block: Block = None) -> str:
    """One subnet's state: identity, owner, pool (tao_in TAO, alpha_in/alpha_out), per-block emission, spot and
    moving price, registration (burn TAO, limits, immunity), weights settings, epoch position, all
    hyperparameters (raw encodings), and neuron counts. The result's "units" field says which fields are converted
    and which are raw chain integers."""
    def f(v: Any) -> dict:
        mg = v.read("metagraph", netuid=netuid)
        out = {"units": SUBNET_UNITS, "subnet": _subnet_header(mg)}
        for key, name in (("spot_price", "alpha_price"), ("epoch", "epoch_status"),
                          ("hyperparameters_raw", "subnet_hyperparameters")):
            try:
                out[key] = v.read(name, netuid=netuid)
            except Exception as e:  # one failed side-read should not hide the rest
                out[key] = f"error: {type(e).__name__}: {e}"
        rows = _neurons(mg, include_axons=False)
        out["neurons"] = {"total": len(rows), "active": sum(1 for r in rows if r["active"]),
                          "validator_permits": sum(1 for r in rows if r["validator_permit"]),
                          "with_incentive": sum(1 for r in rows if (r["incentive"] or 0) > 0)}
        return out
    data, at = chain.run_at(f, block)
    return render(_envelope(at, netuid=netuid, **data))


SORT_KEYS = {"uid": "uid", "incentive": "incentive", "dividends": "dividends", "emission": "emission_alpha_per_epoch",
             "stake": "total_stake", "alpha_stake": "alpha_stake", "registered": "registered_at_block",
             "last_update": "last_update"}


@mcp.tool(annotations=RO, structured_output=False)
def metagraph(
    netuid: Annotated[int, Field(ge=0, le=65535)],
    sort_by: Literal["uid", "incentive", "dividends", "emission", "stake", "alpha_stake", "registered",
                     "last_update"] = "incentive",
    limit: Annotated[int, Field(ge=1, le=1024)] = 64,
    offset: Annotated[int, Field(ge=0)] = 0,
    validators_only: bool = False,
    miners_only: Annotated[bool, Field(description="Exclude neurons holding a validator permit")] = False,
    coldkey: Annotated[str | None, Field(description="Only neurons owned by this coldkey (SS58)")] = None,
    include_axons: Annotated[bool, Field(description="Add each axon's ip:port")] = False,
    block: Block = None,
) -> str:
    """A subnet's neurons as a table (columns + rows), sorted (descending except uid/registered) and paged. Per neuron: uid, hotkey,
    coldkey, active, validator_permit, incentive / dividends / consensus (0..1), emission (alpha per epoch),
    alpha/tao/total stake, registration block, last weight update; optionally axon ip:port."""
    if validators_only and miners_only:
        raise ToolError("validators_only and miners_only exclude each other")
    mg, at = chain.run_at(lambda v: v.read("metagraph", netuid=netuid), block)
    rows = _neurons(mg, include_axons)
    if validators_only:
        rows = [r for r in rows if r["validator_permit"]]
    if miners_only:
        rows = [r for r in rows if not r["validator_permit"]]
    if coldkey:
        rows = [r for r in rows if r["coldkey"] == coldkey]
    key = SORT_KEYS[sort_by]
    asc = sort_by in ("uid", "registered")
    rows.sort(key=lambda r: (r[key] is None, r[key] if asc else -(r[key] or 0)))
    page = rows[offset: offset + limit]
    return render(_envelope(at, netuid=netuid, name=mg.get("name"), matched=len(rows), offset=offset,
                            returned=len(page), sort_by=sort_by, neurons=_table(page)))


@mcp.tool(annotations=RO, structured_output=False)
def neuron(
    netuid: Annotated[int, Field(ge=0, le=65535)],
    uid: Annotated[int | None, Field(ge=0)] = None,
    hotkey: Annotated[str | None, Field(description="Hotkey SS58 (instead of uid)")] = None,
    block: Block = None,
) -> str:
    """One neuron on a subnet by uid or hotkey, with its rank by incentive and by stake among all neurons."""
    if (uid is None) == (hotkey is None):
        raise ToolError("give exactly one of uid or hotkey")
    mg, at = chain.run_at(lambda v: v.read("metagraph", netuid=netuid), block)
    rows = _neurons(mg, include_axons=True)
    match = [r for r in rows if (r["uid"] == uid if uid is not None else r["hotkey"] == hotkey)]
    if not match:
        raise ToolError(f"{'uid ' + str(uid) if uid is not None else hotkey} is not registered on subnet {netuid} "
                        f"at block {at} ({len(rows)} neurons)")
    r = match[0]
    by_inc = sorted(rows, key=lambda x: -(x["incentive"] or 0))
    by_stake = sorted(rows, key=lambda x: -(x["total_stake"] or 0))
    r["rank_by_incentive"] = next(i for i, x in enumerate(by_inc, 1) if x["uid"] == r["uid"])
    r["rank_by_stake"] = next(i for i, x in enumerate(by_stake, 1) if x["uid"] == r["uid"])
    r["of"] = len(rows)
    return render(_envelope(at, netuid=netuid, name=mg.get("name"), neuron=r))


def _side_reads(v: Any, reads: list[tuple[str, str, dict]]) -> dict:
    out = {}
    for key, name, params in reads:
        try:
            out[key] = jsonable(v.read(name, **params))
        except ValueError as e:  # bad SS58 is the caller's error: fail the whole call
            raise ToolError(f"{name}: {e}") from e
        except Exception as e:
            out[key] = f"error: {type(e).__name__}: {e}"
    return out


@mcp.tool(annotations=RO, structured_output=False)
def account(coldkey: Annotated[str, Field(description="Coldkey SS58 address")], block: Block = None) -> str:
    """A coldkey's position: free TAO balance, every stake position (hotkey, subnet, alpha), the stake marked to
    TAO at spot prices, the hotkeys it owns, and its on-chain identity."""
    reads = [("free_balance", "balance", {"coldkey_ss58": coldkey}),
             ("stake_value", "stake_value_for_coldkey", {"coldkey_ss58": coldkey}),
             ("stake_positions", "stake_for_coldkey", {"coldkey_ss58": coldkey}),
             ("owned_hotkeys", "owned_hotkeys", {"coldkey_ss58": coldkey}),
             ("identity", "identity", {"coldkey_ss58": coldkey})]
    data, at = chain.run_at(lambda v: _side_reads(v, reads), block)
    sv = data.get("stake_value")
    if isinstance(sv, dict) and isinstance(sv.get("tao_per_alpha"), dict):  # keep prices of subnets held only
        held = {str(p.get("netuid")) for p in sv.get("positions") or [] if isinstance(p, dict)}
        sv["tao_per_alpha"] = {k: v for k, v in sv["tao_per_alpha"].items() if k in held}
    return render(_envelope(at, coldkey=coldkey, **data))


@mcp.tool(annotations=RO, structured_output=False)
def hotkey(hotkey: Annotated[str, Field(description="Hotkey SS58 address")], block: Block = None) -> str:
    """A hotkey's owner coldkey, the subnets it is registered on (with its uid on each), delegate status and take,
    and identity."""
    def f(v: Any) -> dict:
        out = _side_reads(v, [("owner_coldkey", "hotkey_owner", {"hotkey_ss58": hotkey}),
                              ("netuids", "netuids_for_hotkey", {"hotkey_ss58": hotkey}),
                              ("is_delegate", "is_delegate", {"hotkey_ss58": hotkey}),
                              ("identity", "hotkey_identities", {"hotkey_ss58s": [hotkey]})])
        if isinstance(out.get("identity"), dict):
            out["identity"] = out["identity"].get(hotkey)
        if out.get("is_delegate") is True:
            out.update(_side_reads(v, [("delegate_take", "delegate_take", {"hotkey_ss58": hotkey})]))
        nets = out.get("netuids") if isinstance(out.get("netuids"), list) else []
        out["registrations"] = [{"netuid": n, "uid": jsonable(v.read("uid", hotkey_ss58=hotkey, netuid=n))}
                                for n in nets[:128]]
        return out
    data, at = chain.run_at(f, block)
    return render(_envelope(at, hotkey=hotkey, **data))


def main(argv: list[str] | None = None) -> None:
    """The bittensor-mcp command. Without arguments it serves MCP over stdio until the client closes it."""
    p = argparse.ArgumentParser(
        prog="bittensor-mcp",
        description="Read-only MCP server for the Bittensor chain, over stdio. An MCP client starts it; configure it "
                    "with the BT_MCP_NETWORK, BT_MCP_ARCHIVE, BT_MCP_MAX_CHARS and BT_MCP_LOG environment variables.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.parse_args(argv)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
