"""Offline tests: no network. They cover the pure helpers and the error handling of Chain with fakes, and that the
server starts from a checkout and as a package.

    python -I -m unittest discover -s tests -v

They test the checkout's code, even when the package is installed. The end-to-end test against the live chain is
test_client.py at the root.
"""
import json
import math
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bittensor as bt  # noqa: E402
import bittensor_mcp  # noqa: E402
from bittensor_mcp import server  # noqa: E402
from bittensor.balance import Balance  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


def fake_chain(fn_errors: list[Exception]) -> tuple[server.Chain, dict]:
    """A Chain whose view is a stub; each call of the tool function raises the next error, then returns "ok"."""
    c = server.Chain()
    seen = {"calls": 0, "resets": 0}
    c.resolve_block = lambda b: b
    c._view = lambda b: (object(), 1000 if b is None else b)

    def reset() -> None:
        seen["resets"] += 1
    c._reset = reset

    def fn(view: object) -> str:
        seen["calls"] += 1
        if fn_errors:
            raise fn_errors.pop(0)
        return "ok"
    seen["fn"] = fn
    return c, seen


class RunAt(unittest.TestCase):
    def test_success(self):
        c, seen = fake_chain([])
        self.assertEqual(c.run_at(seen["fn"], None), ("ok", 1000))
        self.assertEqual(c.run(seen["fn"], 7), "ok")

    def test_policy_refusal_is_not_retried(self):
        # A reconnect would spend more of a budget the endpoint already says is exhausted.
        err = bt.RpcPolicyError("Historical work rate limit exceeded", policy="historical_work",
                                reason="budget_exhausted", retry_after="30")
        c, seen = fake_chain([err])
        with self.assertRaises(ToolError) as cm:
            c.run_at(seen["fn"], 5)
        self.assertEqual((seen["calls"], seen["resets"]), (1, 0))
        msg = str(cm.exception)
        for part in ("RpcPolicyError", "budget_exhausted", "30"):
            self.assertIn(part, msg)

    def test_transport_error_reconnects_once(self):
        c, seen = fake_chain([RuntimeError("socket closed")])
        self.assertEqual(c.run_at(seen["fn"], None), ("ok", 1000))
        self.assertEqual((seen["calls"], seen["resets"]), (2, 1))

    def test_transport_error_twice_is_a_tool_error(self):
        c, seen = fake_chain([RuntimeError("a"), RuntimeError("b")])
        with self.assertRaises(ToolError) as cm:
            c.run_at(seen["fn"], None)
        self.assertIn("after reconnecting", str(cm.exception))
        self.assertEqual((seen["calls"], seen["resets"]), (2, 1))

    def test_bad_input_is_not_retried(self):
        c, seen = fake_chain([ValueError("invalid ss58 address")])
        with self.assertRaises(ToolError) as cm:
            c.run_at(seen["fn"], None)
        self.assertIn("ValueError: invalid ss58 address", str(cm.exception))
        self.assertEqual((seen["calls"], seen["resets"]), (1, 0))


class Blocks(unittest.TestCase):
    def setUp(self):
        self.c = server.Chain()
        self.c._st = SimpleNamespace(block=1000)

    def test_resolve(self):
        r = self.c.resolve_block
        self.assertEqual((r(None), r(-100), r(500), r(1000), r(0)), (None, 900, 500, 1000, 0))
        for bad in (1001, -1001):
            with self.assertRaises(ToolError):
                r(bad)

    def test_proven(self):
        view = lambda n: SimpleNamespace(query=lambda path: n)  # noqa: E731
        self.assertTrue(server.Chain._proven(view(42), 42))
        self.assertFalse(server.Chain._proven(view(0), 42))      # a node without state answers the default, 0
        self.assertFalse(server.Chain._proven(view(None), 42))
        self.assertTrue(server.Chain._proven(view(SimpleNamespace(value=42)), 42))


class Serialization(unittest.TestCase):
    def test_jsonable(self):
        import dataclasses
        import enum

        class Color(enum.Enum):
            RED = 1

        @dataclasses.dataclass
        class Info:
            name: str
            raw: dict

        j = server.jsonable
        self.assertEqual(j(Balance.from_tao(1.5)), 1.5)
        self.assertEqual(j(Balance.from_rao(2_000_000_000, 19)), {"alpha": 2.0, "netuid": 19})
        self.assertEqual(j(Info("x", {"big": 1})), {"name": "x"})          # 'raw' is dropped
        self.assertEqual(j({1: b"\x01\xff", 2: Color.RED}), {"1": "0x01ff", "2": "RED"})
        self.assertEqual(j([math.nan, math.inf, 0.5]), [None, None, 0.5])
        self.assertEqual(j((1, {2})), [1, [2]])

    def test_table(self):
        t = server._table([{"a": 1, "b": 2}, {"a": 3, "c": 4}])
        self.assertEqual(t, {"columns": ["a", "b", "c"], "rows": [[1, 2, None], [3, None, 4]]})

    def test_render_caps_and_notes(self):
        old = server.MAX_CHARS
        server.MAX_CHARS = 2000
        try:
            out = server.render({"block": 1, "rows": [{"i": i, "pad": "x" * 40} for i in range(500)]})
        finally:
            server.MAX_CHARS = old
        self.assertLessEqual(len(out), 2000)
        d = json.loads(out)
        self.assertEqual(d["block"], 1)
        self.assertEqual(d["rows"][0]["i"], 0)                               # the head of the list is kept
        self.assertTrue(any(n.startswith("rows: showing ") and n.endswith(" of 500") for n in d["truncated"]))

    def test_render_notes_count_toward_the_cap(self):
        # The cut can land just under the cap; the truncated notes added after it must not push the result over.
        old = server.MAX_CHARS
        server.MAX_CHARS = 2000
        try:
            for pad in range(0, 1600, 23):
                out = server.render({"block": 1, "note": "n" * pad, "rows": [[i, "x" * 30] for i in range(300)]})
                self.assertLessEqual(len(out), 2000, f"envelope padding {pad}")
                self.assertIn("truncated", json.loads(out))
        finally:
            server.MAX_CHARS = old

    def test_render_wraps_non_dict(self):
        self.assertEqual(json.loads(server.render([1, 2])), {"data": [1, 2]})


class FlowEma(unittest.TestCase):
    def test_age(self):
        a = server._ema_age
        self.assertEqual(a({990, 995}, 1000), {"last_updated_block": 995, "age_blocks": 5, "age_days": 0.0})
        stale = a({100}, 100 + 7201)
        self.assertEqual(stale["age_blocks"], 7201)
        self.assertIn("not current flow", stale["warning"])
        self.assertEqual(a(set(), 1000), {"last_updated_block": None})
        self.assertIn("StorageFunctionNotFound", a("StorageFunctionNotFound: x", 1000)["error"])


def state(acc: dict, pool: dict, reg: dict, ema: dict) -> dict:
    return {"acc": acc, "pool": pool, "reg": reg, "ema": ema}


class WindowFlow(unittest.TestCase):
    """_window_flow: the counter difference, and each reason a row is left out."""
    START, END = 1000, 2000
    R = int(server.RAO)

    def run_flow(self):
        rows = [{"netuid": n} for n in (0, 1, 2, 3, 4, 5)]
        s = state(acc={0: 5 * self.R, 1: 10 * self.R, 2: 0, 3: 0, 4: 0},
                  pool={1: 100 * self.R, 2: 50 * self.R, 4: 7 * self.R},
                  reg={1: 10, 2: 20, 3: 30, 4: 40},
                  ema={1: 500, 2: 500, 3: 500, 4: 500})
        e = state(acc={0: 2 * self.R, 1: 13 * self.R, 2: 9 * self.R, 3: 1 * self.R, 4: 4 * self.R, 5: 6 * self.R},
                  pool={1: 104 * self.R, 2: 60 * self.R, 4: 9 * self.R, 5: 3 * self.R},
                  reg={1: 10, 2: 1500, 3: 30, 4: 40, 5: 1800},
                  ema={1: 500, 2: 500, 3: 1999, 4: 500})
        out = server._window_flow(rows, s, e, self.START, self.END)
        return {r["netuid"]: r for r in rows}, out

    def test_measured_row(self):
        rows, out = self.run_flow()
        self.assertEqual((rows[1]["window_net_flow_tao"], rows[1]["window_pool_change_tao"]), (3.0, 4.0))
        self.assertNotIn("pool_tao", rows[1])                         # the subnets tool sets it, from one read
        self.assertEqual(rows[4]["window_net_flow_tao"], 4.0)        # no start entry for a counter means 0
        self.assertEqual((out["from_block"], out["to_block"], out["window_blocks"]), (1000, 2000, 1000))

    def test_exclusions(self):
        rows, out = self.run_flow()
        ex = out["excluded"]
        self.assertEqual(set(ex), {"0", "2", "3", "5"})
        self.assertTrue(ex["0"].startswith("root"))
        self.assertIn("registered at block 1500", ex["2"])
        self.assertIn("registered at block 1800", ex["5"])
        self.assertIn("EMA updated inside the window", ex["3"])
        for n in (0, 2, 3, 5):
            self.assertIsNone(rows[n]["window_net_flow_tao"])
            self.assertIsNone(rows[n]["window_pool_change_tao"])
        self.assertEqual(out["root_net_flow_tao"], -3.0)


class FakeView:
    """Enough of a SyncSnapshot for the subnets tool."""

    def __init__(self, block: int, acc: dict, ema_block: int):
        self.block, self.acc, self.ema_block = block, acc, ema_block

    def read(self, name: str, **params):
        if name == "burn":
            return Balance.from_tao(0.5)
        infos = [SimpleNamespace(netuid=n, burn=Balance.from_tao(0.5), neuron_count=256, tempo=360) for n in (0, 1, 2, 3)]
        return {"subnets": infos, "alpha_prices": {0: 1.0, 1: 0.1, 2: 0.2, 3: 0.3},
                "subnet_names": {1: "one", 2: "two", 3: "three"}, "token_symbols": {},
                "subnet_tao_flows": {1: 1e9, 2: 2e9}}[name]

    def query_map(self, path: list):
        item = path[1]
        if item == "SubnetEmaTaoFlow":
            return [(n, (self.ema_block, {"bits": 0})) for n in (1, 2, 3)]
        r = int(server.RAO)
        return list({"SubnetTaoFlow": self.acc, "NetworkRegisteredAt": {1: 1, 2: 1, 3: 1},
                     "SubnetTAO": {0: 5_000_000 * r, 1: 10 * r, 2: 30 * r, 3: 20 * r},   # root's is its stake
                     "SubnetAlphaIn": {1: 100 * r, 2: 300 * r, 3: 200 * r},
                     "SubnetAlphaOut": {1: 50 * r, 2: 60 * r, 3: 70 * r}}[item].items())


class FakeChain:
    def __init__(self, head: int, views: dict):
        self.head, self.views, self.blocks = head, views, []

    def catalog(self):
        return {"subnet_tao_flows": {"params": {}}, "burn": {"params": {"netuid": "int"}}}

    def run(self, fn, block=None):
        return fn(SimpleNamespace(block=self.head))

    def resolve_block(self, block):
        return block if block >= 0 else self.head + block

    def run_at(self, fn, block=None):
        b = self.head if block is None else self.resolve_block(block)
        self.blocks.append(b)
        return fn(self.views[b]), b


class SubnetsTool(unittest.TestCase):
    R = int(server.RAO)

    def call(self, **kw):
        old = server.chain
        server.chain = FakeChain(10_000, {
            10_000: FakeView(10_000, {0: 9 * self.R, 1: 5 * self.R, 2: 1 * self.R, 3: 2 * self.R}, ema_block=50),
            2_800: FakeView(2_800, {0: 0, 1: 4 * self.R, 2: 3 * self.R, 3: 0}, ema_block=50)})
        try:
            return json.loads(server.subnets(**kw)), server.chain.blocks
        finally:
            server.chain = old

    def test_net_flow_sort(self):
        d, blocks = self.call(sort_by="net_flow")
        self.assertEqual(blocks, [10_000, 2_800])                    # end pinned to the head, then start = end - 7200
        rows = [dict(zip(d["subnets"]["columns"], r)) for r in d["subnets"]["rows"]]
        self.assertEqual([(r["netuid"], r["window_net_flow_tao"]) for r in rows], [(3, 2.0), (1, 1.0), (2, -2.0), (0, None)])
        self.assertEqual(d["net_flow"]["root_net_flow_tao"], 9.0)
        self.assertIn("warning", d["tao_flow_ema"])                  # the EMA is 9,950 blocks old

    def test_pool_columns_and_sort(self):
        d, _ = self.call(sort_by="pool")
        rows = [dict(zip(d["subnets"]["columns"], r)) for r in d["subnets"]["rows"]]
        self.assertEqual([(r["netuid"], r["pool_tao"], r["pool_alpha"], r["alpha_out"]) for r in rows],
                         [(2, 30.0, 300.0, 60.0), (3, 20.0, 200.0, 70.0), (1, 10.0, 100.0, 50.0), (0, None, None, None)])
        self.assertIn("pool_alpha, alpha_out: alpha", d["units"])

    def test_pool_tao_same_with_window(self):
        plain, _ = self.call(sort_by="netuid")
        window, _ = self.call(sort_by="netuid", flow_window=7200)
        col = lambda d: [r[d["subnets"]["columns"].index("pool_tao")] for r in d["subnets"]["rows"]]  # noqa: E731
        self.assertEqual(col(plain), col(window))
        self.assertEqual(col(window), [None, 10.0, 30.0, 20.0])

    def test_no_window_no_flow_columns(self):
        d, blocks = self.call(sort_by="price", limit=2)
        self.assertEqual(blocks, [10_000])
        self.assertNotIn("net_flow", d)
        self.assertNotIn("window_net_flow_tao", d["subnets"]["columns"])
        self.assertEqual([r[0] for r in d["subnets"]["rows"]], [0, 3])

    def test_window_before_genesis(self):
        with self.assertRaises(ToolError):
            self.call(flow_window=5000, block=1000)


class ReadTool(unittest.TestCase):
    """The read tool labels the flow EMA's age on subnet_tao_flows, and only there."""

    def call(self, **kw):
        old = server.chain
        server.chain = FakeChain(10_000, {10_000: FakeView(10_000, {}, ema_block=50)})
        try:
            return json.loads(server.read(**kw))
        finally:
            server.chain = old

    def test_label_on_flow_read(self):
        d = self.call(name="subnet_tao_flows")
        self.assertEqual(d["tao_flow_ema"]["last_updated_block"], 50)
        self.assertIn("not current flow", d["tao_flow_ema"]["warning"])
        self.assertEqual(d["data"], {"1": 1e9, "2": 2e9})

    def test_no_label_elsewhere(self):
        d = self.call(name="burn", params={"netuid": 1})
        self.assertNotIn("tao_flow_ema", d)
        self.assertEqual(d["data"], 0.5)



class Plugin(unittest.TestCase):
    """The Claude Code plugin in .claude-plugin: pinned to a published release, and every host it reaches declared."""

    def load(self, name: str) -> dict:
        with open(os.path.join(ROOT, ".claude-plugin", name)) as f:
            return json.load(f)

    def test_pinned_to_its_own_version(self):
        p = self.load("plugin.json")
        self.assertEqual(p["mcpServers"]["bittensor"], {"command": "uvx", "args": [f"bittensor-mcp=={p['version']}"]})
        as_tuple = lambda v: tuple(int(x) for x in v.split("."))  # noqa: E731
        self.assertLessEqual(as_tuple(p["version"]), as_tuple(bittensor_mcp.__version__))  # never ahead of the package

    def test_marketplace_lists_it(self):
        p, m = self.load("plugin.json"), self.load("marketplace.json")
        self.assertEqual([(e["name"], e["source"]) for e in m["plugins"]], [(p["name"], "./")])

    def test_hosts_declared(self):
        from urllib.parse import urlparse
        from bittensor import settings
        urls = [settings.NETWORKS[n] for n in (server.NETWORK, server.ARCHIVE)]
        for pool in (settings.FALLBACK_ENDPOINTS, settings.ARCHIVE_ENDPOINTS):
            for n in (server.NETWORK, server.ARCHIVE):
                urls += pool.get(n, [])
        chain = {urlparse(u).hostname for u in urls}
        hosts = set(self.load("plugin.json")["network"]["hosts"])
        self.assertEqual(hosts - {"pypi.org", "files.pythonhosted.org"}, chain)
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        self.assertEqual([h for h in sorted(hosts) if f"`{h}`" not in readme], [])

    def test_icon(self):
        icon = self.load("plugin.json")["icon"]
        with open(os.path.join(ROOT, icon), "rb") as f:
            head = f.read(24)
        width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(width == height and 512 <= width <= 2048, (width, height))


class Launch(unittest.TestCase):
    """How clients start it. --version exits before the stdio loop, so these need no client and no network."""

    def start(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", *args, "--version"], capture_output=True, text=True, timeout=120,
                              cwd=ROOT, env={**os.environ, **env})

    def version(self, *args: str) -> str:
        r = self.start(*args)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return r.stdout.strip()

    def test_bad_config_is_one_clear_line(self):
        launcher = os.path.join(ROOT, "server.py")
        for env, text in (({"BT_MCP_MAX_CHARS": "abc"}, "BT_MCP_MAX_CHARS must be a whole number of at least 2000"),
                          ({"BT_MCP_MAX_CHARS": "100"}, "BT_MCP_MAX_CHARS must be a whole number of at least 2000"),
                          ({"BT_MCP_LOG": "loud"}, "BT_MCP_LOG must be one of DEBUG, INFO, WARNING")):
            r = self.start(launcher, **env)
            lines = r.stderr.strip().splitlines()
            self.assertEqual((r.returncode, len(lines)), (1, 1), r.stderr[-500:])     # one line, no traceback
            self.assertTrue(lines[0].startswith(f"bittensor-mcp: {text}"), lines[0])
        for env in ({"BT_MCP_LOG": "info"}, {"BT_MCP_LOG": "WARN"}, {"BT_MCP_LOG": "fatal"}, {"BT_MCP_LOG": "NOTSET"},
                    {"BT_MCP_MAX_CHARS": "2000"}, {"BT_MCP_MAX_CHARS": ""}):
            self.assertEqual(self.start(launcher, **env).returncode, 0, env)

    def test_checkout_launcher(self):
        # Registered clients run "python -I server.py". -I keeps the checkout off sys.path; the launcher adds it.
        self.assertEqual(self.version(os.path.join(ROOT, "server.py")), f"bittensor-mcp {bittensor_mcp.__version__}")

    def test_launcher_runs_the_checkout_copy(self):
        # Even with the package installed (as in CI), the launcher must run the checkout's code, not site-packages'.
        code = ("import runpy, sys; runpy.run_path(sys.argv[1], run_name='launcher'); "
                "print(sys.modules['bittensor_mcp'].__file__)")
        r = subprocess.run([sys.executable, "-I", "-c", code, os.path.join(ROOT, "server.py")], capture_output=True,
                           text=True, timeout=120, cwd=os.path.dirname(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertEqual(r.stdout.strip(), os.path.join(ROOT, "bittensor_mcp", "__init__.py"))

    def test_main_rejects_unknown_arguments(self):
        with self.assertRaises(SystemExit) as cm:
            server.main(["--nope"])
        self.assertEqual(cm.exception.code, 2)

    def test_server_reports_version(self):
        self.assertEqual(server.mcp.version, bittensor_mcp.__version__)


if __name__ == "__main__":
    unittest.main()
