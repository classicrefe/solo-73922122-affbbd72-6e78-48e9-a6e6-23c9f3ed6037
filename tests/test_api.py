"""端到端自测：覆盖分页、快照隔离、名额、关闭、到期、连接丢失与重启语义。

运行方式（本地已起好 PostgreSQL 15，见 README）：
    DATABASE_URL=postgresql://actuary:actuary@127.0.0.1:55432/claims \
    SESSION_TTL_SECONDS=2 ENABLE_DEBUG_HOOKS=true \
    python -m unittest tests.test_api -v
"""
from __future__ import annotations

import os
import threading
import time
import unittest

import httpx
import psycopg
import uvicorn

# 本地测试用默认值
os.environ.setdefault(
    "DATABASE_URL", "postgresql://actuary:actuary@127.0.0.1:55432/claims"
)
os.environ.setdefault("SESSION_TTL_SECONDS", "2")
os.environ.setdefault("ENABLE_DEBUG_HOOKS", "true")
os.environ.setdefault("SWEEPER_INTERVAL_SECONDS", "0.1")

from app.config import settings  # noqa: E402
from app.main import app, manager  # noqa: E402
from app.session_manager import SessionError  # noqa: E402

SEED_SQL = os.path.join(os.path.dirname(__file__), "..", "db", "init", "02_seed.sql")


def reset_seed_data() -> None:
    """每个用例前重置演示数据，保证改/删类测试可重复运行。"""
    with open(SEED_SQL, encoding="utf-8") as fh:
        sql = fh.read()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(sql)

TTL = settings.session_ttl_seconds


class ApiTestBase(unittest.TestCase):
    # 整个套件共享一个 uvicorn 实例与一个 manager 单例，
    # 避免多实例争抢同一端口导致请求串到旧进程。
    _server: "uvicorn.Server | None" = None
    _server_thread: threading.Thread | None = None
    client: httpx.Client

    @classmethod
    def setUpClass(cls):
        if ApiTestBase._server is None:
            config = uvicorn.Config(
                "app.main:app", host="127.0.0.1", port=18080,
                log_level="warning",
            )
            ApiTestBase._server = uvicorn.Server(config)
            ApiTestBase._server_thread = threading.Thread(
                target=ApiTestBase._server.run,
                name="test-uvicorn", daemon=True,
            )
            ApiTestBase._server_thread.start()
            for _ in range(100):
                try:
                    httpx.get("http://127.0.0.1:18080/health", timeout=1)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            # 全套件以干净的种子数据开始
            reset_seed_data()
        cls.client = httpx.Client(
            base_url="http://127.0.0.1:18080", timeout=15
        )

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def setUp(self):
        # 每个用例前清掉所有未终结会话，保证相互独立
        for s in manager.list_sessions():
            if s.status in ("open", "closing"):
                try:
                    manager.close(s.session_id)
                except SessionError:
                    pass
        # 写入类用例会改数据，统一在用例内自行安排快照时机，
        # 不在全局 setUp 中重置（否则会清掉同用例刚提交的写入）。

    @classmethod
    def _reset_seed(cls):
        reset_seed_data()

    def open(self, region="华北"):
        r = self.client.post("/sessions", json={"region": region})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def close(self, sid):
        return self.client.post(f"/sessions/{sid}/close")

    def page(self, sid, page=1, page_size=100, headers=None):
        return self.client.get(
            f"/sessions/{sid}/claims",
            params={"page": page, "page_size": page_size},
            headers=headers or {},
        )


class TestPagination(ApiTestBase):
    def test_pages_ordered_and_capped(self):
        s = self.open()
        r1 = self.page(s["session_id"])
        self.assertEqual(r1.status_code, 200)
        body = r1.json()
        self.assertEqual(len(body["rows"]), 100)
        self.assertTrue(body["has_more"])
        ids = [r["claim_id"] for r in body["rows"]]
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(all(r["region"] == "华北" for r in body["rows"]))
        self.assertTrue(all(isinstance(r["amount"], int) for r in body["rows"]))

        r2 = self.page(s["session_id"], page=2)
        self.assertEqual(len(r2.json()["rows"]), 20)
        self.assertFalse(r2.json()["has_more"])
        self.assertGreater(
            r2.json()["rows"][0]["claim_id"], body["rows"][-1]["claim_id"]
        )

        r3 = self.page(s["session_id"], page=3)
        self.assertEqual(r3.json()["rows"], [])
        self.assertFalse(r3.json()["has_more"])

    def test_page_size_forced_to_cap(self):
        s = self.open()
        r = self.page(s["session_id"], page_size=999)
        self.assertEqual(r.json()["page_size"], 100)
        self.assertEqual(len(r.json()["rows"]), 100)

    def test_invalid_pagination_params(self):
        s = self.open()
        self.assertEqual(self.client.get(
            f"/sessions/{s['session_id']}/claims", params={"page": 0}
        ).status_code, 422)
        self.assertEqual(self.client.get(
            f"/sessions/{s['session_id']}/claims", params={"page_size": 0}
        ).status_code, 422)

    def test_repeat_page_same_snapshot(self):
        s = self.open()
        b1 = self.page(s["session_id"]).json()
        b2 = self.page(s["session_id"]).json()
        self.assertEqual(
            [r["claim_id"] for r in b1["rows"]],
            [r["claim_id"] for r in b2["rows"]],
        )
        self.assertEqual(b1["snapshot_time"], b2["snapshot_time"])


class TestSnapshotIsolation(ApiTestBase):
    def test_source_writes_invisible_inside_session(self):
        self._reset_seed()  # 本用例需要干净的种子数据
        s = self.open("华北")
        before = self.page(s["session_id"]).json()
        before_ids = {r["claim_id"] for r in before["rows"]}

        # 源端：新增保单+理赔、改金额、删理赔，均独立提交
        pol = self.client.post("/admin/policies", json={"region": "华北"})
        self.assertEqual(pol.status_code, 201)
        nc = self.client.post(
            "/admin/claims",
            json={"policy_id": pol.json()["id"], "amount": 777},
        )
        self.assertEqual(nc.status_code, 201)
        first_id = before["rows"][0]["claim_id"]
        second_id = before["rows"][1]["claim_id"]
        self.assertEqual(self.client.patch(
            f"/admin/claims/{first_id}", params={"amount": 1}
        ).status_code, 200)
        self.assertEqual(self.client.delete(
            f"/admin/claims/{second_id}"
        ).status_code, 204)

        after = self.page(s["session_id"]).json()
        after_ids = {r["claim_id"] for r in after["rows"]}
        self.assertEqual(before_ids, after_ids)
        self.assertNotIn(nc.json()["id"], after_ids)
        self.assertNotEqual(after["rows"][0]["amount"], 1)
        self.assertIn(second_id, after_ids)

        # 新会话能看到源端最新提交（翻完全部页核对）
        s2 = self.open("华北")
        fresh_ids = set()
        page_no = 1
        while True:
            body = self.page(s2["session_id"], page=page_no).json()
            fresh_ids.update(r["claim_id"] for r in body["rows"])
            if not body["has_more"]:
                break
            page_no += 1
        self.assertIn(nc.json()["id"], fresh_ids)
        self.assertNotIn(second_id, fresh_ids)
        self.close(s2["session_id"])

    def test_snapshot_covers_both_tables(self):
        # 地区来自 policies 表、金额来自 claims 表：JOIN 结果同样固定
        s = self.open("华东")
        b = self.page(s["session_id"]).json()
        self.assertEqual(len(b["rows"]), 100)
        self.assertTrue(all(r["region"] == "华东" for r in b["rows"]))


class TestCapacity(ApiTestBase):
    def test_max_two_sessions_and_release(self):
        s1 = self.open("华东")
        s2 = self.open("华南")
        r = self.client.post("/sessions", json={"region": "华北"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["code"], "capacity_reached")

        # 满额不影响既有会话
        self.assertEqual(self.page(s1["session_id"], page_size=5).status_code, 200)

        self.assertEqual(self.close(s2["session_id"]).json()["status"], "closed")
        s3 = self.open("华北")  # 名额归还后可再开
        self.assertEqual(s3["status"], "open")
        # 另一个会话不受影响
        self.assertEqual(self.page(s1["session_id"], page_size=5).status_code, 200)
        self.close(s1["session_id"])
        self.close(s3["session_id"])

    def test_unknown_session_404(self):
        r = self.client.get("/sessions/nonexistent/claims")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.get("/sessions/nonexistent").status_code, 404)


class TestCloseSemantics(ApiTestBase):
    def test_close_rejects_later_pages_but_keeps_status_queryable(self):
        s = self.open()
        self.close(s["session_id"])
        r = self.page(s["session_id"])
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("rows", r.json())
        info = self.client.get(f"/sessions/{s['session_id']}").json()
        self.assertEqual(info["status"], "closed")

    def test_slow_page_then_close(self):
        s = self.open("华东")
        sid = s["session_id"]
        box = {}

        def slow():
            box["r"] = self.client.get(
                f"/sessions/{sid}/claims",
                params={"page": 1, "page_size": 10},
                headers={"X-Debug-Sleep-Seconds": "3"},
                timeout=15,
            )

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.8)
        r = self.close(sid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "closed")
        t.join()
        self.assertIn(box["r"].status_code, (409, 410))
        self.assertNotIn("rows", box["r"].json())

        # 关闭后名额已归还，可开新会话
        s2 = self.open("华东")
        self.assertEqual(s2["status"], "open")
        self.close(s2["session_id"])

    def test_close_idempotent_and_new_session_unaffected(self):
        s1 = self.open("华东")
        s2 = self.open("华南")
        self.close(s1["session_id"])
        again = self.close(s1["session_id"])
        self.assertEqual(again.json()["status"], "closed")
        # 另一个会话照常工作
        self.assertEqual(self.page(s2["session_id"], page_size=5).status_code, 200)
        self.close(s2["session_id"])


class TestExpiry(ApiTestBase):
    def test_expiry_rejects_pages_and_releases_slot(self):
        s = self.open("华南")
        sid = s["session_id"]
        time.sleep(TTL + 0.6)
        r = self.page(sid)
        self.assertIn(r.status_code, (409, 410))
        self.assertNotIn("rows", r.json())
        info = self.client.get(f"/sessions/{sid}").json()
        self.assertEqual(info["status"], "expired")
        # 名额已释放：能连开两个新会话
        a = self.open("华东")
        b = self.open("华北")
        self.assertEqual(a["status"], "open")
        self.assertEqual(b["status"], "open")
        self.close(a["session_id"])
        self.close(b["session_id"])

    def test_pages_before_expiry_still_work(self):
        s = self.open("华南")
        r = self.page(s["session_id"], page_size=10)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["rows"]), 10)


class TestConnectionLossAndRestart(ApiTestBase):
    """连接丢失（终止会话后端）与服务重启语义。

    本环境禁止 kill 系统调用，故用 pg_terminate_backend 从 SQL 层
    断开会话持有的后端连接，对客户端而言与网络/进程断开等价。
    """

    def _terminate_session_backend(self, sid: str) -> None:
        sess = manager.get_status(sid)
        with psycopg.connect(settings.database_url, autocommit=True) as admin:
            # 按客户端后端 PID 精准终止该会话独占的连接
            admin.execute(
                "SELECT pg_terminate_backend(%s)",
                (sess.conn.info.backend_pid,),
            )
        # 给客户端一点时间感知 TCP 断开
        deadline = time.time() + 5
        while time.time() < deadline:
            if sess.conn.closed:
                return
            time.sleep(0.1)

    def test_connection_loss_invalidates_session_no_snapshot_swap(self):
        s = self.open("华东")
        sid = s["session_id"]
        # 从 SQL 层断开会话持有的后端连接 -> 快照事务随之消失
        self._terminate_session_backend(sid)
        r = self.page(sid)
        # 旧会话直接失败，绝不换连接/换快照续取；
        # 409（清理线程先标记终态）或 503（取页时发现连接断裂）均合规
        self.assertIn(r.status_code, (409, 503))
        self.assertNotIn("rows", r.json())
        # 旧 id 无法续取（失效会话已移出注册表或已为终态）
        info = self.client.get(f"/sessions/{sid}")
        if info.status_code == 200:
            self.assertIn(info.json()["status"], ("closed", "expired"))
            self.assertEqual(self.page(sid).status_code, 409)
        else:
            self.assertEqual(info.status_code, 404)
        # 新会话在池恢复后的连接上正常开启
        s2 = self.open("华东")
        body = self.page(s2["session_id"], page_size=5)
        self.assertEqual(body.status_code, 200)
        self.assertEqual(len(body.json()["rows"]), 5)
        self.close(s2["session_id"])

    @staticmethod
    def _restart_uvicorn():
        """完整重启共享 uvicorn：lifespan 负责 manager.stop()/start()。"""
        ApiTestBase._server.should_exit = True
        ApiTestBase._server_thread.join(timeout=10)
        config = uvicorn.Config(
            "app.main:app", host="127.0.0.1", port=18080, log_level="warning",
        )
        ApiTestBase._server = uvicorn.Server(config)
        ApiTestBase._server_thread = threading.Thread(
            target=ApiTestBase._server.run,
            name="test-uvicorn-restart", daemon=True,
        )
        ApiTestBase._server_thread.start()
        for _ in range(100):
            try:
                if httpx.get(
                    "http://127.0.0.1:18080/health", timeout=1
                ).status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.1)
        raise RuntimeError("uvicorn 重启后健康检查失败")

    def test_service_restart_drops_in_memory_sessions(self):
        s = self.open("华南")
        sid = s["session_id"]

        # 模拟 API 服务重启（新进程语义：内存注册表为空、池与 sweeper 重建）
        self._restart_uvicorn()

        # 旧会话 id 在新进程中不存在，不得换快照续取
        r = self.client.get(f"/sessions/{sid}")
        self.assertEqual(r.status_code, 404)
        r2 = self.page(sid)
        self.assertEqual(r2.status_code, 404)
        s2 = self.open("华南")
        self.assertEqual(s2["status"], "open")
        self.close(s2["session_id"])


class TestCedingReport(ApiTestBase):
    """再保报送清单 API 的端到端覆盖。"""

    def _payload(self, policy_id=1, parties=None, booked=None):
        if parties is None:
            # 默认两家 50/50，核定总额先占位，由用例自行覆盖
            parties = [
                {"party": "A", "ratio_bps": 5000, "approved_total": 0},
                {"party": "B", "ratio_bps": 5000, "approved_total": 0},
            ]
        body = {"policy_id": policy_id, "parties": parties}
        if booked is not None:
            body["booked"] = booked
        return body

    def _report(self, sid, body):
        return self.client.post(f"/sessions/{sid}/ceding-report", json=body)

    def test_happy_path_row_and_column_constraints(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        # 保单 1 在种子数据中有 80 笔理赔；先取其金额总和
        rows = []
        page = 1
        while True:
            b = self.page(sid, page=page, page_size=100).json()
            for r in b["rows"]:
                if r["policy_id"] == 1:
                    rows.append(r)
            if not b["has_more"]:
                break
            page += 1
        total = sum(r["amount"] for r in rows)
        half = total // 2
        # 总额为奇数时让 B 多 1 分
        parties = [
            {"party": "A", "ratio_bps": 5000, "approved_total": half},
            {"party": "B", "ratio_bps": 5000, "approved_total": total - half},
        ]
        r = self._report(sid, self._payload(policy_id=1, parties=parties))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["claim_count"], 80)
        self.assertEqual(len(body["claims"]), 80)
        ids = [c["claim_id"] for c in body["claims"]]
        self.assertEqual(ids, sorted(ids))
        # 行和 + 每格 floor/ceil + 列和
        col = {"A": 0, "B": 0}
        for c in body["claims"]:
            self.assertEqual(sum(sh["amount"] for sh in c["shares"]), c["amount"])
            self.assertEqual([sh["party"] for sh in c["shares"]], ["A", "B"])
            for sh in c["shares"]:
                col[sh["party"]] += sh["amount"]
                floor = c["amount"] * sh["ratio_bps"] // 10000
                ceil = floor + (0 if c["amount"] * sh["ratio_bps"] % 10000 == 0 else 1)
                self.assertGreaterEqual(sh["amount"], floor)
                self.assertLessEqual(sh["amount"], ceil)
        self.assertEqual(col, {"A": half, "B": total - half})
        self.assertEqual(
            body["parties"],
            [
                {"party": "A", "ratio_bps": 5000,
                 "approved_total": half, "allocated_total": half},
                {"party": "B", "ratio_bps": 5000,
                 "approved_total": total - half, "allocated_total": total - half},
            ],
        )
        self.close(sid)

    def test_repeat_report_identical(self):
        reset_seed_data()
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.page(sid, page_size=100).json()["rows"]
        total = sum(r["amount"] for r in rows if r["policy_id"] == 5)
        parties = [
            {"party": "甲", "ratio_bps": 3000, "approved_total": total * 3 // 10},
            {"party": "乙", "ratio_bps": 7000,
             "approved_total": total - total * 3 // 10},
        ]
        body = self._payload(policy_id=5, parties=parties)
        r1 = self._report(sid, body).json()
        r2 = self._report(sid, body).json()
        self.assertEqual(r1, r2)
        self.assertEqual(r1["snapshot_time"], r2["snapshot_time"])
        self.close(sid)

    def test_booked_items_preserved(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        rows = sorted(
            (r for r in self.page(sid, page_size=100).json()["rows"]
             if r["policy_id"] == 1),
            key=lambda r: r["claim_id"],
        )
        # 33.33% 比例下取一笔非整除理赔（种子金额均为 100 的倍数，
        # 100*3333/10000=33.33 非整除），构造一笔合法入账（ceil 给 A）
        ratio_a = 3333
        target = next(
            r for r in rows if r["amount"] * ratio_a % 10000 != 0
        )
        ceil_val = (target["amount"] * ratio_a + 9999) // 10000
        total = sum(r["amount"] for r in rows)
        # A 拿该笔 ceil：A 核定 = 全部 floor 之和 + 该笔多出的 1 分
        floor_sum_a = sum(r["amount"] * ratio_a // 10000 for r in rows)
        approved_a = floor_sum_a + 1
        approved_b = total - approved_a
        parties = [
            {"party": "A", "ratio_bps": ratio_a, "approved_total": approved_a},
            {"party": "B", "ratio_bps": 10000 - ratio_a, "approved_total": approved_b},
        ]
        booked = [{"claim_id": target["claim_id"], "party": "A", "amount": ceil_val}]
        r = self._report(sid, self._payload(1, parties, booked))
        self.assertEqual(r.status_code, 200, r.text)
        claim = next(c for c in r.json()["claims"]
                     if c["claim_id"] == target["claim_id"])
        share_a = next(sh for sh in claim["shares"] if sh["party"] == "A")
        self.assertEqual(share_a["amount"], ceil_val)
        self.assertTrue(share_a["booked"])
        # 列和仍然精确对齐
        for p in r.json()["parties"]:
            self.assertEqual(p["allocated_total"], p["approved_total"])
        self.close(sid)

    def test_infeasible_returns_422_with_no_partial_list(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        rows = self.page(sid, page_size=100).json()["rows"]
        total = sum(r["amount"] for r in rows if r["policy_id"] == 1)
        parties = [
            {"party": "A", "ratio_bps": 5000, "approved_total": total},
            # 故意让总额对不上
            {"party": "B", "ratio_bps": 5000, "approved_total": total + 1},
        ]
        r = self._report(sid, self._payload(1, parties))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "totals_mismatch")
        self.assertNotIn("claims", r.json())
        self.close(sid)

    def test_invalid_ratios_422(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        parties = [
            {"party": "A", "ratio_bps": 4000, "approved_total": 1},
            {"party": "B", "ratio_bps": 5000, "approved_total": 1},
        ]
        # Pydantic model_validator 失败 -> 422
        r = self._report(sid, self._payload(1, parties))
        self.assertEqual(r.status_code, 422)
        self.close(sid)

    def test_wrong_party_count_422(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        body = {
            "policy_id": 1,
            "parties": [
                {"party": "A", "ratio_bps": 10000, "approved_total": 1}
            ],
        }
        self.assertEqual(self._report(sid, body).status_code, 422)
        body6 = {
            "policy_id": 1,
            "parties": [
                {"party": f"P{i}", "ratio_bps": 2000, "approved_total": 0}
                for i in range(5)
            ],
        }
        # 5 家合法比例 -> 不报数量错（这里 5*2000=10000 通过结构校验，
        # 仅总额不匹配）；再构造 6 家确认上限
        body6["parties"].append(
            {"party": "P5", "ratio_bps": 1, "approved_total": 0}
        )
        self.assertEqual(self._report(sid, body6).status_code, 422)
        self.close(sid)

    def test_booked_unknown_claim_422(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        rows = self.page(sid, page_size=100).json()["rows"]
        total = sum(r["amount"] for r in rows if r["policy_id"] == 1)
        parties = [
            {"party": "A", "ratio_bps": 5000, "approved_total": total // 2},
            {"party": "B", "ratio_bps": 5000,
             "approved_total": total - total // 2},
        ]
        booked = [{"claim_id": 999999, "party": "A", "amount": 1}]
        r = self._report(sid, self._payload(1, parties, booked))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "booked_claim_unknown")
        self.close(sid)

    def test_policy_not_in_region_404(self):
        reset_seed_data()
        # 保单 3 属于华东；在华南会话中请求
        s = self.open("华南")
        sid = s["session_id"]
        r = self._report(sid, self._payload(policy_id=3))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "policy_not_found")
        # 不存在的保单同样 404
        r2 = self._report(sid, self._payload(policy_id=999999))
        self.assertEqual(r2.status_code, 404)
        self.close(sid)

    def test_closed_session_rejects_report(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        self.close(sid)
        r = self._report(sid, self._payload(1))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "session_not_open")
        self.assertNotIn("claims", r.json())

    def test_expired_session_rejects_report(self):
        reset_seed_data()
        s = self.open("华南")
        sid = s["session_id"]
        time.sleep(TTL + 0.6)
        r = self._report(sid, self._payload(5))
        self.assertIn(r.status_code, (409, 410))
        self.assertNotIn("claims", r.json())

    def test_unknown_session_404(self):
        r = self.client.post(
            "/sessions/nonexistent/ceding-report", json=self._payload(1)
        )
        self.assertEqual(r.status_code, 404)

    def test_snapshot_isolation_for_report(self):
        reset_seed_data()
        s = self.open("华北")
        sid = s["session_id"]
        before_rows = [
            r for r in self.page(sid, page_size=100).json()["rows"]
            if r["policy_id"] == 1
        ]
        before_total = sum(r["amount"] for r in before_rows)
        # 源端给保单 1 新增一笔理赔并提交
        nc = self.client.post(
            "/admin/claims", json={"policy_id": 1, "amount": 12345}
        )
        self.assertEqual(nc.status_code, 201)
        half = before_total // 2
        parties = [
            {"party": "A", "ratio_bps": 5000, "approved_total": half},
            {"party": "B", "ratio_bps": 5000,
             "approved_total": before_total - half},
        ]
        r = self._report(sid, self._payload(1, parties))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["claim_count"], len(before_rows))
        self.assertNotIn(nc.json()["id"],
                         [c["claim_id"] for c in r.json()["claims"]])
        self.close(sid)

    def test_empty_policy_returns_empty_list(self):
        reset_seed_data()
        # 新建一个华北保单，没有任何理赔
        pol = self.client.post("/admin/policies", json={"region": "华北"})
        self.assertEqual(pol.status_code, 201)
        pid = pol.json()["id"]
        s = self.open("华北")
        sid = s["session_id"]
        parties = [
            {"party": "A", "ratio_bps": 5000, "approved_total": 0},
            {"party": "B", "ratio_bps": 5000, "approved_total": 0},
        ]
        r = self._report(sid, self._payload(pid, parties))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["claims"], [])
        self.assertEqual(r.json()["claim_count"], 0)
        self.close(sid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
