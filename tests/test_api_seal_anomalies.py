"""封签异常待核事件端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 登记：201 成功（返回事件编号/状态/登记时间）、预期封签不符 409、实见=预期 422、
        空原因/空封签 422、未知展品 404、重复待核 409、幂等重放 200、同键异载荷 409、
        并发登记仅一笔 201
- 待核期间：预演 409 不签发令牌；单件交接 409 不落报告/记录；整车交接整单 409 不写入；
        已落单同键同载荷重试优先返回原结果；补正与撤回照常办理
- 结案：复核封签不符 409 保持待核、相符 201、结案幂等重放 200、同键异载荷 409、
        重复结案 409、未知事件 404、空说明/空封签 422、并发同键结案 201+200；
        结案始终复核操作时有效封签（登记后撤回改变有效封签的情形）
- 解锁后：预演/提交恢复；旧令牌仅按原链状态规则判断；可再登记新事件
- 查询与知悉时刻：事件列表/单事件查询（含 at 时点视图）、state-at 的
        pending_seal_anomaly 以服务端登记/结案时间为界

数据库不可达时整个模块自动 skip：

    DATABASE_URL='postgresql://app@localhost:5432/seals' python3 -m pytest tests/test_api_seal_anomalies.py
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid

import psycopg

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dev 依赖缺失
    TestClient = None

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/seals"
)


def _db_available() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32
D3 = "sha256:" + "ef" * 32


def _rec(exhibit_id: str, frm: str, to: str, digest, when, seal="SEAL-1",
         home=None, reason=None):
    body = {
        "location": home or "展厅", "from_person": frm, "to_person": to,
        "seal_no": seal, "occurred_at": when, "voucher_digest": digest,
    }
    if reason:
        body["seal_change_reason"] = reason
    return {"exhibit_id": exhibit_id, "records": [body]}


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestSealAnomaliesApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app.db as db
        db.DATABASE_URL = DATABASE_URL
        import app.main as main
        main.DATABASE_URL = DATABASE_URL
        cls.main = main
        cls.client_cm = TestClient(main.app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def setUp(self):
        self.ex_id = f"EX-SA-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id, "name": "封签异常测试展品",
            "home_location": "库房A", "custodian": "张伟",
            "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)
        # 一环交接：张伟->李静（封签保持 SEAL-1）；当前有效封签 = SEAL-1
        self.b1 = self._submit(
            _rec(self.ex_id, "张伟", "李静", D1, "2026-09-01T09:00:00+08:00", home="库房A")
        )

    # -- 辅助 ----------------------------------------------------------------

    def _submit(self, body, key=None):
        r = self.client.post(
            "/api/v1/handover-batches", json=body,
            headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED", r.text)
        return r.json()["batch_id"]

    def _register(self, expected="SEAL-1", observed="SEAL-9",
                  reason="实物封签与系统不符，待复核", key=None, exhibit=None):
        return self.client.post(
            f"/api/v1/exhibits/{exhibit or self.ex_id}/seal-anomaly-events",
            json={"expected_seal": expected, "observed_seal": observed,
                  "reason": reason},
            headers={"Idempotency-Key": key or f"sa-{uuid.uuid4()}"},
        )

    def _close(self, event_id, observed="SEAL-1", note="复核无误，系统封签正确",
               key=None):
        return self.client.post(
            f"/api/v1/seal-anomaly-events/{event_id}/close",
            json={"observed_seal": observed, "note": note},
            headers={"Idempotency-Key": key or f"sc-{uuid.uuid4()}"},
        )

    def _events(self, exhibit=None, **params):
        return self.client.get(
            f"/api/v1/exhibits/{exhibit or self.ex_id}/seal-anomaly-events",
            params=params,
        )

    def _state(self):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _batches(self):
        r = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _state_at(self, at):
        return self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at", params={"at": at}
        )

    # -- 登记 ----------------------------------------------------------------

    def test_register_success_201(self):
        r = self._register()
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        # 返回事件编号、状态和登记时间
        self.assertTrue(body["event_id"])
        self.assertEqual(body["status"], "PENDING")
        self.assertTrue(body["created_at"])
        self.assertEqual(body["exhibit_id"], self.ex_id)
        self.assertEqual(body["expected_seal"], "SEAL-1")
        self.assertEqual(body["observed_seal"], "SEAL-9")
        self.assertFalse(body["idempotent_replay"])
        self.assertIsNone(body["closed_at"])
        self.assertIsNone(body["close_observed_seal"])
        self.assertIsNone(body["close_note"])
        # 登记不改变展品链上状态
        st = self._state()
        self.assertEqual(st["chain_length"], 1)
        self.assertEqual(st["custodian"], "李静")
        self.assertEqual(st["current_seal"], "SEAL-1")
        # 事件列表可见
        events = self._events().json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], body["event_id"])

    def test_register_expected_mismatch_409(self):
        r = self._register(expected="SEAL-2")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("SEAL-1", r.json()["detail"])
        self.assertEqual(self._events().json(), [])

    def test_register_observed_equals_expected_422(self):
        r = self._register(expected="SEAL-1", observed="SEAL-1")
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self._events().json(), [])

    def test_register_empty_fields_422(self):
        for bad in ["", "   ", "\t\n"]:
            r = self._register(reason=bad)
            self.assertEqual(r.status_code, 422, f"reason={bad!r}")
            r = self._register(expected=bad)
            self.assertEqual(r.status_code, 422, f"expected={bad!r}")
            r = self._register(observed=bad)
            self.assertEqual(r.status_code, 422, f"observed={bad!r}")
        self.assertEqual(self._events().json(), [])

    def test_register_unknown_exhibit_404(self):
        r = self._register(exhibit="EX-NOPE")
        self.assertEqual(r.status_code, 404)

    def test_register_duplicate_pending_409(self):
        self.assertEqual(self._register().status_code, 201)
        r = self._register(observed="SEAL-8")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("未结案", r.json()["detail"])
        self.assertEqual(len(self._events().json()), 1)

    def test_register_idempotent_replay_and_conflict(self):
        key = f"sa-{uuid.uuid4()}"
        first = self._register(key=key)
        self.assertEqual(first.status_code, 201)
        replay = self._register(key=key)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["event_id"], first.json()["event_id"])
        self.assertEqual(len(self._events().json()), 1)
        # 同键异载荷 -> 409
        r = self._register(observed="SEAL-8", key=key)
        self.assertEqual(r.status_code, 409)

    def test_register_replay_precedence_after_close(self):
        # 结案后再用原键原载荷登记：仍重放首次事件，不新建事件
        key = f"sa-{uuid.uuid4()}"
        first = self._register(key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self._close(first.json()["event_id"]).status_code, 201)
        replay = self._register(key=key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["event_id"], first.json()["event_id"])
        self.assertEqual(len(self._events().json()), 1)

    def test_register_concurrent_only_one_pending(self):
        barrier = threading.Barrier(2)
        results = []

        def worker(key, out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/exhibits/{self.ex_id}/seal-anomaly-events",
                json={"expected_seal": "SEAL-1", "observed_seal": "SEAL-9",
                      "reason": "并发登记"},
                headers={"Idempotency-Key": key},
            )
            out.append(r.status_code)
            client.close()

        t1 = threading.Thread(target=worker, args=(f"sac-{uuid.uuid4()}", results))
        t2 = threading.Thread(target=worker, args=(f"sac-{uuid.uuid4()}", results))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertCountEqual(results, [201, 409])
        self.assertEqual(len(self._events().json()), 1)

    # -- 待核期间的交接限制 ----------------------------------------------------

    def test_rehearse_conflicts_during_pending(self):
        self.assertEqual(self._register().status_code, 201)
        r = self.client.post(
            "/api/v1/handover-batches/rehearse",
            json=_rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotIn("state_token", r.json())

    def test_submit_conflicts_during_pending_and_writes_nothing(self):
        self.assertEqual(self._register().status_code, 201)
        before_batches = len(self._batches())
        r = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 409, r.text)
        # 不生成新交接报告或记录
        self.assertEqual(len(self._batches()), before_batches)
        self.assertEqual(self._state()["chain_length"], 1)

    def test_submit_replay_precedence_during_pending(self):
        # 先落单（键 K），再登记待核；同键同载荷重试仍优先返回原报告
        key = f"k-{uuid.uuid4()}"
        body = _rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00")
        first = self.client.post(
            "/api/v1/handover-batches", json=body,
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "ACCEPTED")
        self.assertEqual(self._register().status_code, 201)
        replay = self.client.post(
            "/api/v1/handover-batches", json=body,
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["batch_id"], first.json()["batch_id"])

    def test_vehicle_order_conflicts_during_pending(self):
        # 第二件正常展品
        other = f"EX-SB-{uuid.uuid4().hex[:10]}"
        self.assertEqual(self.client.post("/api/v1/exhibits", json={
            "exhibit_id": other, "name": "另一展品", "home_location": "库房B",
            "custodian": "王芳", "initial_seal": "SEAL-2",
        }).status_code, 201)
        self.assertEqual(self._register().status_code, 201)
        key = f"veh-{uuid.uuid4()}"
        body = {
            "vehicle_id": "TRAIN-2026-9001",
            "items": [
                {"exhibit_id": self.ex_id, "records": [
                    {"location": "展厅", "from_person": "李静", "to_person": "王强",
                     "seal_no": "SEAL-1", "occurred_at": "2026-09-02T09:00:00+08:00",
                     "voucher_digest": D2}]},
                {"exhibit_id": other, "records": [
                    {"location": "库房B", "from_person": "王芳", "to_person": "赵磊",
                     "seal_no": "SEAL-2", "occurred_at": "2026-09-02T09:30:00+08:00",
                     "voucher_digest": D3}]},
            ],
        }
        r = self.client.post("/api/v1/vehicle-handovers", json=body,
                             headers={"Idempotency-Key": key})
        self.assertEqual(r.status_code, 409, r.text)
        # 整单不写入：两件展品链状态与批次数均不变
        self.assertEqual(self._state()["chain_length"], 1)
        self.assertEqual(
            self.client.get(f"/api/v1/exhibits/{other}").json()["chain_length"], 0)
        # 结案后同键同载荷可正常办理（证明 409 时未落任何整单报告）
        event_id = self._events().json()[0]["event_id"]
        self.assertEqual(self._close(event_id).status_code, 201)
        r2 = self.client.post("/api/v1/vehicle-handovers", json=body,
                              headers={"Idempotency-Key": key})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["status"], "ACCEPTED")
        self.assertFalse(r2.json()["idempotent_replay"])

    def test_correction_and_withdrawal_allowed_during_pending(self):
        # 再提交一环（李静->王强），作为可撤回的最新批次
        b2 = self._submit(
            _rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00")
        )
        self.assertEqual(self._register().status_code, 201)
        # 补正照常办理（补正第 1 环，不影响撤回最新的 b2）
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/1/digest-corrections",
            json={"expected_digest": D1, "new_digest": D3, "reason": "摘要录入笔误"},
            headers={"Idempotency-Key": f"c-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        # 撤回照常办理（撤回后有效状态回退到 b2 之前）
        w = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b2, "reason": "批次误登记"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(w.status_code, 201, w.text)
        # 审计三链仍完整
        audit = self.client.get(f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertTrue(audit["valid"], audit["detail"])

    # -- 结案 ----------------------------------------------------------------

    def test_close_wrong_seal_keeps_pending_409(self):
        event_id = self._register().json()["event_id"]
        r = self._close(event_id, observed="SEAL-8")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("保持待核", r.json()["detail"])
        # 事件仍为待核：预演仍冲突
        events = self._events().json()
        self.assertEqual(events[0]["status"], "PENDING")
        self.assertIsNone(events[0]["closed_at"])
        r2 = self.client.post(
            "/api/v1/handover-batches/rehearse",
            json=_rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        )
        self.assertEqual(r2.status_code, 409)

    def test_close_success_201(self):
        event_id = self._register().json()["event_id"]
        r = self._close(event_id)
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["event_id"], event_id)
        self.assertEqual(body["status"], "CLOSED")
        self.assertEqual(body["close_observed_seal"], "SEAL-1")
        self.assertEqual(body["close_note"], "复核无误，系统封签正确")
        self.assertTrue(body["closed_at"])
        self.assertFalse(body["idempotent_replay"])
        # 解锁后预演恢复签发令牌
        rehe = self.client.post(
            "/api/v1/handover-batches/rehearse",
            json=_rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        )
        self.assertEqual(rehe.status_code, 200, rehe.text)
        self.assertTrue(rehe.json()["state_token"]["token"])

    def test_close_idempotent_replay_and_conflict(self):
        event_id = self._register().json()["event_id"]
        key = f"sc-{uuid.uuid4()}"
        first = self._close(event_id, key=key)
        self.assertEqual(first.status_code, 201)
        replay = self._close(event_id, key=key)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["event_id"], event_id)
        # 同键异载荷 -> 409
        r = self._close(event_id, note="另一说明", key=key)
        self.assertEqual(r.status_code, 409)

    def test_close_twice_different_keys_409(self):
        event_id = self._register().json()["event_id"]
        self.assertEqual(self._close(event_id).status_code, 201)
        r = self._close(event_id)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("已结案", r.json()["detail"])

    def test_close_unknown_event_404(self):
        r = self._close(str(uuid.uuid4()))
        self.assertEqual(r.status_code, 404)

    def test_close_malformed_event_id_422(self):
        r = self._close("not-a-uuid")
        self.assertEqual(r.status_code, 422)
        r = self.client.get("/api/v1/seal-anomaly-events/not-a-uuid")
        self.assertEqual(r.status_code, 422)

    def test_close_empty_note_or_seal_422(self):
        event_id = self._register().json()["event_id"]
        for bad in ["", "   "]:
            r = self._close(event_id, note=bad)
            self.assertEqual(r.status_code, 422, f"note={bad!r}")
            r = self._close(event_id, observed=bad)
            self.assertEqual(r.status_code, 422, f"observed={bad!r}")
        # 事件仍待核
        self.assertEqual(self._events().json()[0]["status"], "PENDING")

    def test_close_concurrent_same_key_one_201_one_200(self):
        event_id = self._register().json()["event_id"]
        barrier = threading.Barrier(2)
        results = []
        key = f"scc-{uuid.uuid4()}"

        def worker(out):
            client = TestClient(self.main.app)
            barrier.wait()
            r = client.post(
                f"/api/v1/seal-anomaly-events/{event_id}/close",
                json={"observed_seal": "SEAL-1", "note": "并发结案"},
                headers={"Idempotency-Key": key},
            )
            out.append(r.status_code)
            client.close()

        t1 = threading.Thread(target=worker, args=(results,))
        t2 = threading.Thread(target=worker, args=(results,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertCountEqual(results, [200, 201])
        self.assertEqual(self._events().json()[0]["status"], "CLOSED")

    def test_close_compares_effective_seal_at_close_time(self):
        # 第二环换签为 SEAL-2，当前有效封签 = SEAL-2
        b2 = self._submit(_rec(
            self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00",
            seal="SEAL-2", reason="授权换签",
        ))
        event_id = self._register(expected="SEAL-2").json()["event_id"]
        # 待核期间撤回该批次：有效封签回退为 SEAL-1
        w = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b2, "reason": "批次误登记"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(w.status_code, 201, w.text)
        # 结案始终复核操作时有效封签：SEAL-2 已失效 -> 409 保持待核
        r = self._close(event_id, observed="SEAL-2")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._events().json()[0]["status"], "PENDING")
        # 以当前有效封签 SEAL-1 复核 -> 201
        self.assertEqual(self._close(event_id, observed="SEAL-1").status_code, 201)

    def test_register_expected_must_match_effective_seal_after_withdrawal(self):
        b2 = self._submit(_rec(
            self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00",
            seal="SEAL-2", reason="授权换签",
        ))
        w = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b2, "reason": "批次误登记"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(w.status_code, 201)
        # 撤回后有效封签为 SEAL-1：以 SEAL-2 为预期 -> 409
        self.assertEqual(self._register(expected="SEAL-2").status_code, 409)
        self.assertEqual(self._register(expected="SEAL-1").status_code, 201)

    # -- 解锁后行为 ------------------------------------------------------------

    def test_old_token_judged_by_chain_rules_after_close(self):
        # 预演取令牌 -> 登记待核（提交 409）-> 结案 -> 旧令牌仅按原链状态规则判断
        token = self.client.post(
            "/api/v1/handover-batches/rehearse",
            json=_rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
        ).json()["state_token"]["token"]
        event_id = self._register().json()["event_id"]
        blocked = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}",
                     "Handover-Token": token},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self._close(event_id).status_code, 201)
        # 链状态未变，旧令牌仍有效
        r = self.client.post(
            "/api/v1/handover-batches",
            json=_rec(self.ex_id, "李静", "王强", D2, "2026-09-02T09:00:00+08:00"),
            headers={"Idempotency-Key": f"k-{uuid.uuid4()}",
                     "Handover-Token": token},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")

    def test_register_new_event_after_close(self):
        event_id = self._register().json()["event_id"]
        self.assertEqual(self._close(event_id).status_code, 201)
        r = self._register(observed="SEAL-7")
        self.assertEqual(r.status_code, 201, r.text)
        events = self._events().json()
        self.assertEqual(len(events), 2)
        self.assertEqual([e["status"] for e in events], ["CLOSED", "PENDING"])

    # -- 查询与知悉时刻 ----------------------------------------------------------

    def test_event_queries_and_filters(self):
        event_id = self._register().json()["event_id"]
        # 单事件查询
        one = self.client.get(f"/api/v1/seal-anomaly-events/{event_id}")
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["status"], "PENDING")
        # 状态过滤
        self.assertEqual(len(self._events(status="PENDING").json()), 1)
        self.assertEqual(self._events(status="CLOSED").json(), [])
        # 结案后过滤翻转
        self.assertEqual(self._close(event_id).status_code, 201)
        self.assertEqual(self._events(status="PENDING").json(), [])
        self.assertEqual(len(self._events(status="CLOSED").json()), 1)
        closed = self.client.get(f"/api/v1/seal-anomaly-events/{event_id}").json()
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(closed["close_observed_seal"], "SEAL-1")
        # 未知展品 / 未知事件
        self.assertEqual(self._events(exhibit="EX-NOPE").status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/v1/seal-anomaly-events/{uuid.uuid4()}").status_code,
            404,
        )

    def test_event_query_as_of(self):
        registered_at = self._state()["created_at"]
        event_id = self._register().json()["event_id"]
        created_at = self._events().json()[0]["created_at"]
        closed_at = self._close(event_id).json()["closed_at"]

        # 登记前时刻：列表为空；单事件 404
        self.assertEqual(self._events(at=registered_at).json(), [])
        r = self.client.get(f"/api/v1/seal-anomaly-events/{event_id}",
                            params={"at": registered_at})
        self.assertEqual(r.status_code, 404)
        # 登记时刻（边界含等号）：待核，结案字段不提前出现
        at_register = self._events(at=created_at).json()
        self.assertEqual(len(at_register), 1)
        self.assertEqual(at_register[0]["status"], "PENDING")
        self.assertIsNone(at_register[0]["closed_at"])
        self.assertIsNone(at_register[0]["close_observed_seal"])
        one = self.client.get(f"/api/v1/seal-anomaly-events/{event_id}",
                              params={"at": created_at})
        self.assertEqual(one.json()["status"], "PENDING")
        # 结案时刻（边界含等号）：已结案，结案字段呈现
        at_close = self._events(at=closed_at).json()
        self.assertEqual(at_close[0]["status"], "CLOSED")
        self.assertEqual(at_close[0]["closed_at"], closed_at)
        self.assertEqual(at_close[0]["close_observed_seal"], "SEAL-1")
        # at 参数校验：无时区/非法/未来 -> 422
        self.assertEqual(self._events(at="2026-09-01T09:00:00").status_code, 422)
        self.assertEqual(self._events(at="not-a-time").status_code, 422)
        self.assertEqual(self._events(at="2999-01-01T00:00:00Z").status_code, 422)

    def test_state_at_presents_pending_seal_anomaly(self):
        registered_at = self._state()["created_at"]
        event_id = self._register(observed="SEAL-9").json()["event_id"]
        created_at = self._events().json()[0]["created_at"]

        # 登记前：无待核事件
        r = self._state_at(registered_at)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["pending_seal_anomaly"])
        # 登记时刻：待核事件出现（边界含等号）
        r = self._state_at(created_at)
        self.assertEqual(r.status_code, 200, r.text)
        pending = r.json()["pending_seal_anomaly"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["event_id"], event_id)
        self.assertEqual(pending["expected_seal"], "SEAL-1")
        self.assertEqual(pending["observed_seal"], "SEAL-9")
        # 结案时刻：不再是待核（结案边界含等号）
        closed_at = self._close(event_id).json()["closed_at"]
        r = self._state_at(closed_at)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["pending_seal_anomaly"])
        # 三链快照字段不受事件影响
        self.assertEqual(r.json()["chain_length"], 1)
        self.assertEqual(r.json()["custodian"], "李静")


if __name__ == "__main__":
    unittest.main()
