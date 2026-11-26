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
        self.assertEqual((rows[1]["window_net_flow_tao"], rows[1]["window_pool_change_tao"], rows[1]["pool_tao"]),
                         (3.0, 4.0, 104.0))
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
        return list({"SubnetTaoFlow": self.acc, "SubnetTAO": {n: 10 * int(server.RAO) for n in (0, 1, 2, 3)},
                     "NetworkRegisteredAt": {1: 1, 2: 1, 3: 1}}[item].items())


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



class Launch(unittest.TestCase):
    """How clients start it. --version exits before the stdio loop, so these need no client and no network."""

    def version(self, *args: str) -> str:
        r = subprocess.run([sys.executable, "-I", *args, "--version"], capture_output=True, text=True, timeout=120,
                           cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return r.stdout.strip()

    def test_checkout_launcher(self):
        # Registered clients run "python -I server.py". -I keeps the checkout off sys.path; the launcher adds it.
        self.assertEqual(self.version(os.path.join(ROOT, "server.py")), f"bittensor-mcp {bittensor_mcp.__version__}")

    def test_main_rejects_unknown_arguments(self):
        with self.assertRaises(SystemExit) as cm:
            server.main(["--nope"])
        self.assertEqual(cm.exception.code, 2)

    def test_server_reports_version(self):
        self.assertEqual(server.mcp.version, bittensor_mcp.__version__)


if __name__ == "__main__":
    unittest.main()
