"""数据可信度回归测试 —— 钉死三个「静默失真」缺陷。

这三条都不是崩溃型 bug，而是「程序照常跑完、卡片照常发出、结论是错的」，
所以必须用断言锁住，否则下次重构很容易被顺手改回去：

1. `fetch_stock_history` 任何情况下都不能返回随机生成的假 K 线；
2. 退市 / 长期停牌的股票不能进入选股池（否则用几年前的 K 线算今天的信号）；
3. 推送档期必须按「目标档期小时」归并，迟到十几小时的运行不能吃掉当天档期。

全部只依赖标准库 + pandas（不联网、不读真实数据库）。
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
import unittest.mock
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BJ = timezone(timedelta(hours=8))


def read_source(rel_path: str) -> str:
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _make_df(latest: str):
    """构造一个「最新一根 K 线在最前面」的最小 DataFrame 替身。

    CI 只装 PyYAML（不装 pandas/matplotlib），所以这里不引入 pandas：
    `universe_guard` 只用到 `.empty` 和 `.iloc[0]["date"]` 两个属性，
    替身只要提供这两样就足以验证逻辑。本地装了 pandas 时，下面另有
    `TestUniverseGuardWithRealPandas` 做一次真实对象的对照。
    """
    class _ILoc:
        def __init__(self, row):
            self._row = row

        def __getitem__(self, _idx):
            return self._row

    class _FakeDF:
        empty = False

        def __init__(self, value):
            self.iloc = _ILoc({"date": value})

    return _FakeDF(latest)


class TestNoMockKline(unittest.TestCase):
    """假数据一旦入库就永久留在库里，之后所有策略都在它上面跑。"""

    def setUp(self):
        self.src = read_source("utils/stock_data_fetcher.py")

    def test_mock_generator_removed(self):
        # 允许注释里提到这段历史（说明为什么删掉），但不能有定义或调用
        self.assertNotIn(
            "def _generate_mock_data", self.src,
            "随机行情生成器又被加回来了：失败时必须返回 None，不能编数据",
        )
        self.assertNotIn(
            "self._generate_mock_data(", self.src,
            "还在调用随机行情生成器：失败时必须返回 None，不能编数据",
        )

    def test_fetch_history_returns_none_on_total_failure(self):
        tree = ast.parse(self.src)
        func = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "fetch_stock_history"),
            None,
        )
        self.assertIsNotNone(func, "fetch_stock_history 不存在")

        returns_none = any(
            isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is None
            for n in ast.walk(func)
        )
        self.assertTrue(returns_none, "fetch_stock_history 在所有数据源失败时应显式 return None")


class TestUniverseGuard(unittest.TestCase):
    """退市股 / 停牌股不得参与选股。"""

    def setUp(self):
        from utils.universe_guard import split_stale

        self.split_stale = split_stale

    def test_delisted_stock_is_dropped(self):
        batch = {
            "600519": _make_df("2026-11-13"),   # 正常
            "600001": _make_df("2009-12-15"),   # 邯郸钢铁，早已退市
            "600002": _make_df("2006-04-06"),   # 齐鲁石化
            "000001": _make_df("2026-11-12"),   # 正常（滞后 1 天）
        }
        fresh, stats = self.split_stale(batch)

        self.assertEqual(stats["reference"], date(2026, 11, 13))
        self.assertNotIn("600001", fresh, "退市股不能进入选股池")
        self.assertNotIn("600002", fresh, "退市股不能进入选股池")
        self.assertIn("600519", fresh)
        self.assertIn("000001", fresh, "滞后 1 天属于正常数据延迟，不该被剔")
        self.assertEqual(stats["stale"], 2)
        self.assertEqual(stats["fresh"], 2)

    def test_reference_uses_mode_not_max(self):
        """个别脏数据（未来日期）不能把整池判成陈旧。"""
        batch = {f"60{i:04d}": _make_df("2026-11-13") for i in range(10)}
        batch["999999"] = _make_df("2030-01-01")   # 脏数据
        fresh, stats = self.split_stale(batch)
        self.assertEqual(stats["reference"], date(2026, 11, 13))
        self.assertEqual(len(fresh), 10)

    def test_unreadable_date_is_counted_not_crashed(self):
        batch = {"600519": _make_df("2026-11-13"), "600000": _make_df("坏日期")}
        fresh, stats = self.split_stale(batch)
        self.assertEqual(stats["no_date"], 1)
        self.assertNotIn("600000", fresh)

    def test_empty_frame_counts_as_no_date(self):
        batch = {"600519": _make_df("2026-11-13"), "600000": _make_df(None)}

        class _Empty:
            empty = True
            iloc = ()

        batch["600000"] = _Empty()
        fresh, stats = self.split_stale(batch)
        self.assertEqual(stats["no_date"], 1)
        self.assertNotIn("600000", fresh)


class TestUniverseGuardWithRealPandas(unittest.TestCase):
    """本地有 pandas 时做一次真实 DataFrame 对照（CI 跳过）。"""

    def test_real_dataframe(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("未安装 pandas")

        from utils.universe_guard import split_stale

        def frame(latest):
            return pd.DataFrame({"date": [latest, "2026-11-12"], "close": [1.0, 2.0]})

        fresh, stats = split_stale({
            "600519": frame("2026-11-13"),
            "600001": frame("2009-12-15"),
        })
        self.assertEqual(stats["reference"], date(2026, 11, 13))
        self.assertIn("600519", fresh)
        self.assertNotIn("600001", fresh)


class TestEditionWindow(unittest.TestCase):
    """档期归并：迟到十几小时的运行必须归到它原本的档期。"""

    def setUp(self):
        from utils.push_guard import edition_date

        self.edition_date = edition_date

    def test_evening_on_time(self):
        t = datetime(2026, 11, 14, 22, 13, tzinfo=BJ)
        self.assertEqual(self.edition_date(t, slot_hour=22), date(2026, 11, 14))

    def test_evening_late_same_day(self):
        t = datetime(2026, 11, 14, 23, 59, tzinfo=BJ)
        self.assertEqual(self.edition_date(t, slot_hour=22), date(2026, 11, 14))

    def test_evening_late_next_morning(self):
        """真实事故：11-13 晚间的任务，11-14 11:20 才跑到，不能算成 11-14 的档期。"""
        t = datetime(2026, 11, 14, 11, 20, tzinfo=BJ)
        self.assertEqual(
            self.edition_date(t, slot_hour=22), date(2026, 11, 13),
            "迟到档期被归到了当天，会导致当天晚上 22:13 的推送被误判为「已推送」而静默漏推",
        )

    def test_evening_late_early_morning(self):
        t = datetime(2026, 11, 14, 5, 0, tzinfo=BJ)
        self.assertEqual(self.edition_date(t, slot_hour=22), date(2026, 11, 13))

    def test_close_slot_next_day(self):
        t = datetime(2026, 11, 15, 1, 25, tzinfo=BJ)
        self.assertEqual(self.edition_date(t, slot_hour=15), date(2026, 11, 14))

    def test_morning_slot_keeps_same_day(self):
        t = datetime(2026, 11, 14, 8, 43, tzinfo=BJ)
        self.assertEqual(self.edition_date(t, slot_hour=8), date(2026, 11, 14))


class TestPollutedLedgerSelfHeals(unittest.TestCase):
    """已写入的脏记录不能继续吃掉后面的档期。

    现实场景：修复上线前，旧逻辑已经把「11-13 晚间档、11-14 11:20 才跑到」
    记成 `evening-news.yml / 2026-11-14`。只改归并算法是没用的 —— 这条记录
    仍然会让 11-14 22:13 的真档期被跳过。守卫必须自己认出它是脏记录。
    """

    def setUp(self):
        from utils import push_guard

        self.pg = push_guard

    def _records(self):
        return [{
            "workflow": "evening-news.yml",
            "edition": "2026-11-14",          # 旧逻辑写错的档期
            "run_id": "43551048729",
            "ts": "2026-11-14 11:20:00",      # 实际是 11-13 晚间档迟到的
        }]

    def test_polluted_record_does_not_block_tonight(self):
        tonight = datetime(2026, 11, 14, 22, 13, tzinfo=BJ)
        with unittest.mock.patch.object(self.pg, "_read_ledger",
                                        return_value=(self._records(), "sha", True)):
            skip = self.pg.should_skip("evening-news.yml", now=tonight, slot_hour=22)
        self.assertFalse(skip, "脏记录仍然把今晚的晚间新闻判成「已推送」")

    def test_legit_record_still_blocks(self):
        ok = [{
            "workflow": "evening-news.yml",
            "edition": "2026-11-14",
            "run_id": "1",
            "ts": "2026-11-14 22:14:05",
        }]
        tonight = datetime(2026, 11, 14, 23, 30, tzinfo=BJ)
        with unittest.mock.patch.object(self.pg, "_read_ledger",
                                        return_value=(ok, "sha", True)):
            skip = self.pg.should_skip("evening-news.yml", now=tonight, slot_hour=22)
        self.assertTrue(skip, "正常记录应该继续生效（避免重复推送）")


class TestCallSitesPassSlotHour(unittest.TestCase):
    """光改 push_guard 不够：调用方必须真的把目标档期小时传进去。"""

    CASES = [
        ("evening_news.py", 21),
        ("daily_news.py", 8),
        ("intraday_selection.py", 12),
    ]

    def test_slot_hour_wired(self):
        for path, hour in self.CASES:
            src = read_source(path)
            self.assertIn(f"EDITION_SLOT_HOUR = {hour}", src,
                          f"{path} 缺少目标档期小时常量")
            self.assertIn("slot_hour=EDITION_SLOT_HOUR", src,
                          f"{path} 的 should_skip/mark_pushed 没传 slot_hour，迟到会串档")

    def test_selection_wired(self):
        src = read_source("main.py")
        self.assertIn("SELECTION_SLOT_HOUR", src)
        self.assertIn("slot_hour=SELECTION_SLOT_HOUR", src)


class TestSelectionPathUsesGuard(unittest.TestCase):
    """选股热路径必须过时效守卫，否则退市股会参与计算。"""

    def test_select_stocks_calls_split_stale(self):
        src = read_source("main.py")
        self.assertIn("split_stale", src, "select_stocks 不再做数据时效过滤")
        self.assertIn("last_universe_stats", src,
                      "缺失时效统计，卡片无法标注数据基准日")

    def test_no_silent_except_in_strategy_loop(self):
        """选股热路径里不能有 `except: pass`：策略抛异常会被算成「今天没信号」。"""
        tree = ast.parse(read_source("main.py"))
        select = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "select_stocks"),
            None,
        )
        self.assertIsNotNone(select, "main.py 找不到 select_stocks")
        for node in ast.walk(select):
            if isinstance(node, ast.ExceptHandler):
                body = node.body
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    self.fail(
                        f"select_stocks:{node.lineno} 还有 `except: pass`："
                        "策略异常会被吞掉，和「今天没信号」无法区分"
                    )


class TestWebServerLocalOnly(unittest.TestCase):
    """Web 后台没有登录态，默认必须只听本机。"""

    def test_default_bind_is_loopback(self):
        self.assertIn("def run_web_server(host='127.0.0.1'", read_source("web_server.py"),
                      "Web 服务默认绑定地址必须是 127.0.0.1")

    def test_cli_default_is_loopback(self):
        src = read_source("main.py")
        self.assertIn("default='127.0.0.1'", src,
                      "main.py web 的 --host 默认值必须是 127.0.0.1")

    def test_request_guard_exists(self):
        src = read_source("web_server.py")
        self.assertIn("@app.before_request", src)
        self.assertIn("WEB_ALLOWED_HOSTS", src)

    def test_socketio_not_wildcard(self):
        src = read_source("web_server.py")
        self.assertNotIn('cors_allowed_origins="*"', src,
                         'SocketIO 不能放开所有来源')


class TestHostNormalization(unittest.TestCase):
    """`_host_only` 是 Host/Origin 白名单的唯一入口，归一错了整道门就形同虚设。

    这里把 web_server.py 里这两个函数抽出来单独执行（不导入 Flask），
    验证 `127.0.0.1:5000`、`[::1]:5000`、`http://host:port/path` 都能正确归一。
    """

    @classmethod
    def setUpClass(cls):
        src = read_source("web_server.py")
        tree = ast.parse(src)
        want = {"_host_only", "allowed_hosts", "_LOOPBACK_HOSTS"}
        chunks = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in want:
                chunks.append(ast.get_source_segment(src, node))
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any(t in want for t in targets):
                    chunks.append(ast.get_source_segment(src, node))
        assert len(chunks) >= 3, f"web_server.py 里找不到守卫函数，实际抓到 {len(chunks)} 段"

        cls.ns = {"os": os}
        exec("\n\n".join(chunks), cls.ns)  # noqa: S102 - 测的就是这段源码

    def test_plain_host(self):
        self.assertEqual(self.ns["_host_only"]("127.0.0.1"), "127.0.0.1")

    def test_host_with_port(self):
        self.assertEqual(self.ns["_host_only"]("127.0.0.1:5000"), "127.0.0.1")
        self.assertEqual(self.ns["_host_only"]("localhost:5000"), "localhost")

    def test_ipv6_host(self):
        self.assertEqual(self.ns["_host_only"]("[::1]:5000"), "::1")
        self.assertEqual(self.ns["_host_only"]("http://[::1]:5000/x"), "::1")

    def test_origin_url(self):
        self.assertEqual(self.ns["_host_only"]("http://127.0.0.1:5000"), "127.0.0.1")
        self.assertEqual(self.ns["_host_only"]("https://evil.example.com"), "evil.example.com")

    def test_null_origin_is_not_localhost(self):
        """file:// 页面发请求时 Origin 是字符串 "null"，绝不能被当成放行。"""
        self.assertNotIn(self.ns["_host_only"]("null"), self.ns["allowed_hosts"]())

    def test_env_extends_allowlist(self):
        old = os.environ.get("WEB_ALLOWED_HOSTS")
        try:
            os.environ["WEB_ALLOWED_HOSTS"] = "192.168.1.10:5000, nas.local"
            allowed = self.ns["allowed_hosts"]()
            self.assertIn("192.168.1.10:5000", allowed)
            self.assertIn("nas.local", allowed)
            self.assertIn("127.0.0.1", allowed, "本机必须始终在放行列表里")
        finally:
            if old is None:
                os.environ.pop("WEB_ALLOWED_HOSTS", None)
            else:
                os.environ["WEB_ALLOWED_HOSTS"] = old


class TestNoSilentFailure(unittest.TestCase):
    """「跑完了、绿了、但没有消息 / 功能永远用不了」这一类缺陷。

    这批问题的共同点是**不会崩**：异常被 except 吞成一句日志，或者返回值解包错位
    只在空库时才触发。用源码级断言钉住，避免下次重构又改回去。
    """

    def test_select_stocks_early_returns_keep_arity(self):
        """早退分支必须按 return_data 返回 2 或 3 个值。

        历史上两处 `return {}, {}` 在 `return_data=True` 时被
        `results, stock_names, stock_data_dict = ...` 解包，
        于是「库是空的」这条可执行提示变成一句
        `ValueError: not enough values to unpack (expected 3, got 2)`。
        """
        tree = ast.parse(read_source("main.py"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "select_stocks"
        )
        early = [
            n for n in fn.body
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
            and len(n.value.elts) == 2
            and all(isinstance(e, ast.Dict) and not e.keys for e in n.value.elts)
        ]
        self.assertEqual(early, [], "select_stocks 里仍有固定返回二元组的早退分支")

    def test_run_command_fails_when_result_is_empty(self):
        """`main.py run` 拿到空结果必须非零退出，不能绿着什么都不发。"""
        src = read_source("main.py")
        self.assertIn("选股结果为空", src)
        # 空结果分支后面必须紧跟 sys.exit(1)
        idx = src.find("选股结果为空")
        self.assertGreater(idx, 0)
        tail = src[idx:idx + 900]
        self.assertIn("sys.exit(1)", tail, "空结果分支没有让进程失败")

    def test_trading_routes_imports_stdlib_it_uses(self):
        """routes.py 用到 json / datetime，就必须在模块顶层导入。

        缺导入时异常被 `except Exception` 吞掉：批量任务列表永远空、
        执行方案导入导出永远失败，表现都像「没有数据」而不是「代码坏了」。
        """
        tree = ast.parse(read_source("trading/routes.py"))
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
        for required in ("json", "datetime"):
            self.assertIn(required, names, f"trading/routes.py 缺少 `import {required}`")

    def test_api_update_does_not_use_commented_out_object(self):
        """`quant_system` 已被注释掉，/api/update 再直接引用它就必然失败。"""
        src = read_source("web_server.py")
        self.assertNotIn("quant_system.update_data", src)
        self.assertIn('QuantSystem("config/config.yaml").update_data', src)

    def test_no_leftover_javascript_in_python(self):
        """Python 里不该出现 Array.isArray 这种 JS 残留表达式。"""
        for rel in ("trading/routes.py", "web_server.py"):
            self.assertNotIn("Array.isArray", read_source(rel), f"{rel} 含 JS 残留代码")

    def test_pool_probe_looks_back_to_previous_trading_day(self):
        """涨停池探针必须回看最近交易日，否则周一 09:00 的自检固定假故障。"""
        src = read_source("utils/biga_sources.py")
        self.assertIn("_pool_probe", src)
        self.assertIn("回看", src)

    def test_nested_template_copy_is_gone(self):
        """web/static/static、web/templates/templates 是不可达的陈旧副本。

        它们曾经真实存在：改了 web/static/js 不生效时，很难想到还有第二份。
        """
        for dead in ("web/static/static", "web/templates/templates"):
            self.assertFalse(
                os.path.exists(os.path.join(ROOT, dead)),
                f"{dead} 是不可达的重复副本，不应存在",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
