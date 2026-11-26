"""End-to-end check over a real MCP stdio connection: handshake, tools/list, every tool, error paths, a sweep of
every no-parameter and netuid-only catalog read, and the size cap.

    .venv/bin/python -I test_client.py [--out DIR] [--cmd COMMAND]     # exit 0 = all checks passed

It starts the checkout's server (python -I server.py), or with --cmd an installed command such as bittensor-mcp.
"""
import json
import os
import sys

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
CMD = sys.argv[sys.argv.index("--cmd") + 1] if "--cmd" in sys.argv else None
MAX = int(os.environ.get("BT_MCP_MAX_CHARS", "30000"))
fails: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


async def call(s: ClientSession, name: str, args: dict | None = None) -> tuple[bool, str, dict | None]:
    r = await s.call_tool(name, args or {})
    text = "".join(getattr(c, "text", "") for c in r.content)
    err = bool(getattr(r, "is_error", False) or getattr(r, "isError", False))
    data = None
    if not err:
        try:
            data = json.loads(text)
        except ValueError:
            pass
    if OUT:
        safe = "_".join([name] + [f"{k}-{v}" for k, v in (args or {}).items()])[:120].replace("/", "_")
        with open(os.path.join(OUT, safe + ".json"), "w") as f:
            f.write(text)
    return err, text, data


def rows(table: dict) -> list[dict]:
    return [dict(zip(table["columns"], r)) for r in table["rows"]]


def opaque(text: str) -> bool:
    """A crash: the SDK hides the cause and sends only 'Error executing tool <name>'."""
    return text.strip().rstrip(".").count(":") == 0


def clean(text: str) -> bool:
    return " object at 0x" not in text and "<bittensor." not in text


async def main() -> None:
    if CMD:
        params = StdioServerParameters(command=CMD, args=[], cwd=HERE)
    else:
        params = StdioServerParameters(command=sys.executable, args=["-I", os.path.join(HERE, "server.py")], cwd=HERE)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams[:2]) as s:
            init = await s.initialize()
            check(init.server_info.name == "bittensor", f"initialize: server {init.server_info.name}")
            check("net_flow" in (init.instructions or ""), "instructions point clients to the windowed net flow")
            tools = {t.name: t for t in (await s.list_tools()).tools}
            want = {"chain_status", "list_reads", "describe_read", "read", "subnets", "subnet", "metagraph",
                    "neuron", "account", "hotkey"}
            check(set(tools) == want, f"tools/list: {sorted(tools)}")
            check(all(t.annotations and t.annotations.read_only_hint for t in tools.values()), "all tools read-only")

            err, text, d = await call(s, "chain_status")
            check(not err and d["head"] > 9_000_000 and d["finalized"] <= d["head"], f"chain_status: {text[:160]}")
            head = d["head"]

            err, text, d = await call(s, "list_reads")
            n = sum(len(v) for v in d.values()) if d else 0
            check(not err and n >= 100 and len(text) <= MAX, f"list_reads: {n} reads, {len(text)} chars")
            err, text, _ = await call(s, "list_reads", {"category": "staking"})
            check(not err and "stake_for_coldkey" in text, "list_reads category=staking")
            err, text, _ = await call(s, "list_reads", {"category": "nope"})
            check(err and "categories" in text, f"list_reads bad category -> error: {text[:100]}")
            err, text, _ = await call(s, "describe_read", {"name": "metagrap"})
            check(err and "metagraph" in text, f"describe_read typo -> suggestion: {text[:100]}")
            err, text, d = await call(s, "describe_read", {"name": "stake_for_coldkey"})
            check(not err and d.get("docs_url"), "describe_read stake_for_coldkey")

            err, text, d = await call(s, "read", {"name": "subnet_hyperparameters", "params": {"netuid": 19}})
            check(not err and d["data"].get("tempo") and clean(text), f"read subnet_hyperparameters: {len(text)} chars")
            err, text, d = await call(s, "read", {"name": "subnet_tao_flows"})
            ema = (d or {}).get("tao_flow_ema") or {}
            check(not err and ema.get("last_updated_block") and clean(text),
                  f"read subnet_tao_flows carries tao_flow_ema: {ema}")
            err, text, d = await call(s, "read", {"name": "burn", "params": {"netuid": 19}, "block": -7200})
            check(not err and d["block"] in range(head - 7200 - 5, head - 7200 + 30), f"read burn at -7200: {text[:140]}")
            err, text, _ = await call(s, "read", {"name": "burn", "params": {"netuid": 19, "foo": 1}})
            check(err and "unexpected" in text, f"read extra param -> error: {text[:100]}")
            err, text, _ = await call(s, "read", {"name": "burn"})
            check(err and "netuid" in text, f"read missing param -> error: {text[:100]}")
            err, text, d = await call(s, "read", {"name": "neurons", "params": {"netuid": 19, "lite": True}})
            check(not err and len(text) <= MAX and clean(text), f"read neurons (big): {len(text)} chars, "
                                                               f"truncated={d.get('truncated') if d else None}")
            err, text, _ = await call(s, "read", {"name": "burn", "params": {"netuid": 19}, "block": head + 100})
            check(err and "outside" in text, f"read future block -> error: {text[:100]}")

            err, text, d = await call(s, "subnets", {"sort_by": "price", "limit": 5})
            check(not err and len(d["subnets"]["rows"]) == 5 and d["count"] > 100, f"subnets top-5 by price: "
                  f"{[(r['netuid'], r['name'], r['price']) for r in rows(d['subnets'])]}")
            ema = (d or {}).get("tao_flow_ema") or {}
            check(not err and ema.get("last_updated_block") and ema.get("age_blocks") is not None
                  and ("warning" in ema) == (ema["age_blocks"] > 7200), f"subnets tao_flow_ema: {ema}")
            err, text, d = await call(s, "subnets")
            check(not err and len(text) <= MAX, f"subnets all: {d.get('count') if d else '?'} rows, {len(text)} chars")

            err, text, d = await call(s, "subnet", {"netuid": 19})
            check(not err and d["subnet"].get("name") and clean(text) and len(text) <= MAX,
                  f"subnet 19: {d['subnet'].get('name') if d else text[:200]}, {len(text)} chars")
            owner_ck = d["subnet"].get("owner_coldkey") if d else None
            owner_hk = d["subnet"].get("owner_hotkey") if d else None

            err, text, d = await call(s, "metagraph", {"netuid": 19})
            check(not err and d["returned"] == 64 and clean(text), f"metagraph 19 default: {len(text)} chars, "
                  f"top={rows(d['neurons'])[0]['uid'] if d else '?'} inc={rows(d['neurons'])[0]['incentive'] if d else '?'}")
            top = rows(d["neurons"])[0] if d else {}
            err, text, d = await call(s, "metagraph", {"netuid": 19, "limit": 1024, "include_axons": True})
            check(not err and len(text) <= MAX, f"metagraph 19 limit 1024: {len(text)} chars, "
                                                f"truncated={d.get('truncated') if d else None}")
            err, text, d = await call(s, "metagraph", {"netuid": 19, "validators_only": True, "sort_by": "stake",
                                                       "limit": 5})
            check(not err and all(r["validator_permit"] for r in rows(d["neurons"])), "metagraph validators_only")

            err, text, d = await call(s, "neuron", {"netuid": 19, "uid": top.get("uid", 0)})
            check(not err and d["neuron"]["rank_by_incentive"] == 1, f"neuron by uid: {text[:200]}")
            err, text, d = await call(s, "neuron", {"netuid": 19, "hotkey": top.get("hotkey", "")})
            check(not err and d["neuron"]["uid"] == top.get("uid"), "neuron by hotkey")
            err, text, _ = await call(s, "neuron", {"netuid": 19})
            check(err and "exactly one" in text, "neuron without uid/hotkey -> error")

            err, text, d = await call(s, "account", {"coldkey": owner_ck or top.get("coldkey")})
            check(not err and clean(text) and "free_balance" in (d or {}), f"account (subnet owner): {text[:240]}")
            err, text, _ = await call(s, "account", {"coldkey": "5Fnotarealaddress"})
            check(err and "ss58" in text.lower(), f"account bad SS58 -> clean error: {text[:120]}")
            err, text, d = await call(s, "hotkey", {"hotkey": top.get("hotkey") or owner_hk})
            check(not err and clean(text) and 19 in (d or {}).get("netuids", []), f"hotkey: {text[:240]}")

            err, text, d = await call(s, "metagraph", {"netuid": 19, "block": -3_000_000, "limit": 3})
            check(not err and d["block"] == head - 3_000_000 or (not err and abs(d["block"] - (head - 3_000_000)) < 50),
                  f"historical metagraph -3,000,000: block {d['block'] if d else text[:200]}")

            # windowed net flow (after the deep historical read: two more snapshots against the history budget)
            err, text, d = await call(s, "subnets", {"sort_by": "net_flow", "limit": 1024})
            nf = (d or {}).get("net_flow") or {}
            rs = rows(d["subnets"]) if d else []
            vals = [r["window_net_flow_tao"] for r in rs if r["window_net_flow_tao"] is not None]
            check(not err and nf.get("window_blocks") == 7200 and nf.get("to_block") == d["block"]
                  and nf.get("from_block") == d["block"] - 7200 and len(text) <= MAX
                  and all(r["window_net_flow_tao"] is not None or str(r["netuid"]) in nf["excluded"] for r in rs)
                  and "0" in nf["excluded"] and isinstance(nf.get("root_net_flow_tao"), float)
                  and vals == sorted(vals, reverse=True) and rs[:len(vals)] == [r for r in rs if r["window_net_flow_tao"] is not None],
                  f"subnets net_flow (default 7200 window): {len(vals)} measured, excluded={list(nf.get('excluded', {}))}, "
                  f"top={[(r['netuid'], r['window_net_flow_tao']) for r in rs[:3]]}, {len(text)} chars")
            err, text, _ = await call(s, "subnets", {"flow_window": 5000, "block": 1000})
            check(err and "before block 1" in text, f"subnets window before genesis -> error: {text[:100]}")

            # sweep: every read with no params or only a netuid
            cat = json.loads((await call(s, "list_reads"))[1])
            fill = {"netuid": 19, "mechid": 0, "uid": top.get("uid", 0), "lite": True,
                    "hotkey_ss58": top.get("hotkey"), "coldkey_ss58": top.get("coldkey"),
                    "hotkey_ss58s": [top.get("hotkey")], "coldkey_ss58s": [top.get("coldkey")]}
            sweep = [(r["name"], r["params"]) for grp in cat.values() for r in grp if set(r["params"]) <= set(fill)]
            bad, explained = [], []
            for name, p in sweep:
                err, text, _ = await call(s, "read", {"name": name, "params": {k: fill[k] for k in p}})
                if (err and opaque(text)) or not clean(text) or len(text) > MAX:
                    bad.append(f"{name}: {'ERR ' if err else ''}{text[:160]}")
                elif err:
                    explained.append(f"{name}: {text[:160]}")
            check(not bad, f"sweep of {len(sweep)} catalog reads (params filled from the top neuron): no crash, no raw objects, under the cap"
                  + "".join("\n     " + b for b in bad))
            for e in explained:
                print("INFO explained error (chain/SDK side): " + e)
    print(f"\n{len(fails)} failed" if fails else "\nall checks passed")
    sys.exit(1 if fails else 0)


if OUT:
    os.makedirs(OUT, exist_ok=True)
anyio.run(main)
