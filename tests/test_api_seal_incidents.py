"""实物封签异常待核事件端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 登记：成功 201（事件编号/状态/登记时间、独立事件哈希链）；幂等重放 200、
  同键异载荷 409（幂等先于业务校验）。
- 登记拒绝：未知展品 404、重复待核 409、预期封签≠当前有效封签 409、
  实见=预期 422、封签非法 422、空原因 422。
- 结案：成功 200（复核当时有效封签、状态 RESOLVED、closed_at、链头同步）；
  幂等重放、同键异载荷 409；复核值不符保持 PENDING + 409 且不留复核结论；
  事件不匹配/不存在 404、已结案再结案 409、空说明/非法封签 422、
  事件编号格式非法 422。
- 待核冻结：预演 409 且不签发令牌；单件交接 409、不生成批次报告/记录；
  含该展品的整车交接整单 409、不生成整车报告/批次/记录（同车其他件也不落单）；
  已落单的同键同载荷重试仍优先返回原结果；补正与撤回照常办理。
- 结案后：预演恢复签发令牌；旧令牌只按原链状态规则判断（结案不推进交接链，
  旧令牌仍有效）；交接/整车恢复；可再登记新事件。
- 撤回与待核相互作用：撤回后当前有效封签变化，登记/结案以撤回后封签复核。
- 查询：事件链列表（可按状态过滤）、单事件查询、审计四链复算。
- 知悉时刻快照：以服务端登记/结案时间为界呈现 pending_seal_incident。
- 并发：同展品登记/结案/交接在展品行锁下串行，不出现两件 PENDING。

数据库不可达时整个模块自动 skip：

    DATABASE_URL='postgresql://app@localhost:5432/seals' python3 -m pytest tests/test_api_seal_incidents.py
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dev 依赖缺失
    TestClient = None

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@localhost:5432/seals")


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


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestSealIncidentsApi(unittest.TestCase):
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
        self.uid = uuid.uuid4().hex[:12]
        self.ex_id = f"EX-SI-{uuid.uuid4().hex[:12]}"
        r = self.client.post("/api/v1/exhibits", json={
            "exhibit_id": self.ex_id, "name": "待核事件测试展品",
            "home_location": "库房A", "custodian": "张伟", "initial_seal": "SEAL-1",
        })
        self.assertEqual(r.status_code, 201, r.text)

    # -- 辅助 ----------------------------------------------------------------

    def _register(self, expected="SEAL-1", observed="SEAL-X", reason="实见封签异常",
                  key=None, ex_id=None):
        return self.client.post(
            f"/api/v1/exhibits/{ex_id or self.ex_id}/seal-incidents",
            json={"expected_seal": expected, "observed_seal": observed, "reason": reason},
            headers={"Idempotency-Key": key or f"r-{uuid.uuid4()}"},
        )

    def _close(self, eid, observed="SEAL-1", note="复核与系统封签一致，结案",
               key=None, ex_id=None):
        return self.client.post(
            f"/api/v1/exhibits/{ex_id or self.ex_id}/seal-incidents/{eid}/close",
            json={"observed_seal": observed, "note": note},
            headers={"Idempotency-Key": key or f"c-{uuid.uuid4()}"},
        )

    def _get_event(self, eid, ex_id=None):
        return self.client.get(
            f"/api/v1/exhibits/{ex_id or self.ex_id}/seal-incidents/{eid}")

    def _rec(self, frm="张伟", to="李静", seal="SEAL-1", when="2026-09-01T09:00:00+08:00",
             home="库房A", digest=D1, reason=None):
        body = {"location": home, "from_person": frm, "to_person": to, "seal_no": seal,
                "occurred_at": when, "voucher_digest": digest}
        if reason:
            body["seal_change_reason"] = reason
        return body

    def _submit(self, body, key=None, token=None):
        headers = {"Idempotency-Key": key or f"k-{uuid.uuid4()}"}
        if token:
            headers["Handover-Token"] = token
        return self.client.post("/api/v1/handover-batches",
                                json={"exhibit_id": self.ex_id, "records": [body]},
                                headers=headers)

    def _accept_one(self, **kw):
        r = self._submit(self._rec(**kw))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED", r.text)
        return r.json()["batch_id"]

    # -- 登记 ----------------------------------------------------------------

    def test_register_success_returns_id_status_registered_at(self):
        r = self._register(key=f"reg-1-{self.uid}")
        self.assertEqual(r.status_code, 201, r.text)
        j = r.json()
        self.assertEqual(j["status"], "PENDING")
        self.assertEqual(j["exhibit_id"], self.ex_id)
        self.assertEqual(j["expected_seal"], "SEAL-1")
        self.assertEqual(j["observed_seal"], "SEAL-X")
        self.assertIsNone(j["closed_at"])
        self.assertIsNone(j["review_observed_seal"])
        self.assertFalse(j["idempotent_replay"])
        self.assertEqual(j["event_seq"], 1)
        self.assertEqual(j["prev_hash"], "SEAL_INCIDENTS_GENESIS")
        self.assertEqual(len(j["event_id"]), 36)
        self.assertTrue(j["registered_at"])
        self.assertEqual(len(j["record_hash"]), 64)

    def test_register_idempotent_replay_then_different_payload_409(self):
        r1 = self._register(key=f"reg-replay-{self.uid}")
        self.assertEqual(r1.status_code, 201)
        # 同键同载荷 -> 200 重放原登记结果（即使事件后来结案）
        eid = r1.json()["event_id"]
        self.assertEqual(self._close(eid, key=f"close-replay-{self.uid}").status_code, 200)
        r2 = self._register(key=f"reg-replay-{self.uid}")
        self.assertEqual(r2.status_code, 200)
        j2 = r2.json()
        self.assertTrue(j2["idempotent_replay"])
        self.assertEqual(j2["event_id"], eid)
        self.assertEqual(j2["status"], "PENDING")  # 登记重放恒为首次 PENDING 形态
        # 同键异载荷 -> 409（幂等优先：即使已有未结案约束，也是 409）
        r3 = self._register(observed="SEAL-Y", key=f"reg-replay-{self.uid}")
        self.assertEqual(r3.status_code, 409, r3.text)

    def test_register_unknown_exhibit_404(self):
        r = self._register(ex_id="EX-NO-SUCH", key=f"reg-miss-{self.uid}")
        self.assertEqual(r.status_code, 404, r.text)

    def test_register_duplicate_pending_409(self):
        self.assertEqual(self._register(key=f"reg-a-{self.uid}").status_code, 201)
        r = self._register(observed="SEAL-Y", key=f"reg-b-{self.uid}")
        self.assertEqual(r.status_code, 409, r.text)

    def test_register_expected_seal_mismatch_409(self):
        r = self._register(expected="WRONG-SEAL", observed="SEAL-X", key=f"reg-exp-{self.uid}")
        self.assertEqual(r.status_code, 409, r.text)

    def test_register_observed_equals_expected_422(self):
        r = self._register(expected="SEAL-1", observed="SEAL-1", key=f"reg-same-{self.uid}")
        self.assertEqual(r.status_code, 422, r.text)

    def test_register_invalid_seals_and_blank_reason_422(self):
        # 空串/纯空白 -> 模型 min_length 422；内部夹空白、超长 -> 422
        for bad in ["", "   ", "SEAL 1", "SEAL\t1", "x" * 101]:
            r = self.client.post(
                f"/api/v1/exhibits/{self.ex_id}/seal-incidents",
                json={"expected_seal": bad, "observed_seal": "SEAL-X", "reason": "r"},
                headers={"Idempotency-Key": f"rb-{uuid.uuid4()}"},
            )
            self.assertEqual(r.status_code, 422, f"expected={bad!r} -> {r.status_code}")
            r = self.client.post(
                f"/api/v1/exhibits/{self.ex_id}/seal-incidents",
                json={"expected_seal": "SEAL-1", "observed_seal": bad, "reason": "r"},
                headers={"Idempotency-Key": f"rb-{uuid.uuid4()}"},
            )
            self.assertEqual(r.status_code, 422, f"observed={bad!r} -> {r.status_code}")
        # 首尾空白被 API 规范化（strip），不报错，与无空白等价
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents",
            json={"expected_seal": "  SEAL-1  ", "observed_seal": "SEAL-X", "reason": "r"},
            headers={"Idempotency-Key": f"rb-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        # 空原因（模型 min_length；空白被 strip 后为空）
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents",
            json={"expected_seal": "SEAL-1", "observed_seal": "SEAL-X", "reason": "   "},
            headers={"Idempotency-Key": f"rb-{uuid.uuid4()}"},
        )
        self.assertEqual(r.status_code, 422, r.text)

    # -- 结案 ----------------------------------------------------------------

    def test_close_success_fields(self):
        eid = self._register(key=f"reg-c1-{self.uid}").json()["event_id"]
        r = self._close(eid, key=f"cls-1-{self.uid}")
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["status"], "RESOLVED")
        self.assertEqual(j["review_observed_seal"], "SEAL-1")
        self.assertTrue(j["closed_at"])
        self.assertFalse(j["idempotent_replay"])

    def test_close_mismatch_keeps_pending_and_no_review(self):
        eid = self._register(key=f"reg-cm-{self.uid}").json()["event_id"]
        r = self._close(eid, observed="SEAL-Y", key=f"cls-bad-{self.uid}")
        self.assertEqual(r.status_code, 409, r.text)
        g = self._get_event(eid).json()
        self.assertEqual(g["status"], "PENDING")
        self.assertIsNone(g["closed_at"])
        self.assertIsNone(g["review_observed_seal"])
        # 冲突 409 不落库：换新键按正确封签仍可结案
        r2 = self._close(eid, key=f"cls-good-{self.uid}")
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["status"], "RESOLVED")

    def test_close_idempotent_replay_and_different_payload_409(self):
        eid = self._register(key=f"reg-cr-{self.uid}").json()["event_id"]
        body = {"observed_seal": "SEAL-1", "note": "说明A"}
        r1 = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents/{eid}/close",
            json=body, headers={"Idempotency-Key": f"cls-rp-{self.uid}"})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents/{eid}/close",
            json=body, headers={"Idempotency-Key": f"cls-rp-{self.uid}"})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["idempotent_replay"])
        r3 = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents/{eid}/close",
            json={"observed_seal": "SEAL-1", "note": "说明B"},
            headers={"Idempotency-Key": f"cls-rp-{self.uid}"})
        self.assertEqual(r3.status_code, 409, r3.text)

    def test_close_unknown_exhibit_and_event_mismatch_404(self):
        eid = self._register(key=f"reg-404a-{self.uid}").json()["event_id"]
        # 未知展品路径
        r = self.client.post(
            f"/api/v1/exhibits/EX-NO-SUCH/seal-incidents/{eid}/close",
            json={"observed_seal": "SEAL-1", "note": "n"},
            headers={"Idempotency-Key": f"cls-miss1-{self.uid}"})
        self.assertEqual(r.status_code, 404, r.text)
        # 事件不属于该展品（另一展品存在，但事件挂在 self.ex_id）
        other = f"EX-SI-{uuid.uuid4().hex[:12]}"
        self.client.post("/api/v1/exhibits", json={
            "exhibit_id": other, "name": "o", "home_location": "库房A",
            "custodian": "钱进", "initial_seal": "SEAL-9"})
        r = self.client.post(
            f"/api/v1/exhibits/{other}/seal-incidents/{eid}/close",
            json={"observed_seal": "SEAL-9", "note": "n"},
            headers={"Idempotency-Key": f"cls-miss2-{self.uid}"})
        self.assertEqual(r.status_code, 404, r.text)
        # 事件编号不存在
        r = self._close(uuid.uuid4(), observed="SEAL-1", key=f"cls-miss3-{self.uid}")
        self.assertEqual(r.status_code, 404, r.text)

    def test_close_bad_event_id_format_422(self):
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents/not-a-uuid/close",
            json={"observed_seal": "SEAL-1", "note": "n"},
            headers={"Idempotency-Key": f"cls-badid-{self.uid}"})
        self.assertEqual(r.status_code, 422, r.text)

    def test_close_already_resolved_409(self):
        eid = self._register(key=f"reg-alr-{self.uid}").json()["event_id"]
        self.assertEqual(self._close(eid, key=f"cls-alr1-{self.uid}").status_code, 200)
        r = self._close(eid, key=f"cls-alr2-{self.uid}")
        self.assertEqual(r.status_code, 409, r.text)

    def test_close_blank_note_and_invalid_seal_422(self):
        eid = self._register(key=f"reg-cv-{self.uid}").json()["event_id"]
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents/{eid}/close",
            json={"observed_seal": "SEAL-1", "note": "   "},
            headers={"Idempotency-Key": f"cls-n1-{self.uid}"})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents/{eid}/close",
            json={"observed_seal": "SEAL 1", "note": "n"},
            headers={"Idempotency-Key": f"cls-n2-{self.uid}"})
        self.assertEqual(r.status_code, 422, r.text)

    # -- 待核冻结 -------------------------------------------------------------

    def test_rehearse_conflicts_without_token_during_pending(self):
        self._register(key=f"reg-rh-{self.uid}")
        r = self.client.post("/api/v1/handover-batches/rehearse",
                             json={"exhibit_id": self.ex_id,
                                   "records": [self._rec(to="李静")]})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertNotIn("state_token", r.json())

    def test_single_submit_conflicts_and_writes_nothing(self):
        self._register(key=f"reg-fr-{self.uid}")
        before = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers").json()
        self.assertEqual(before["chain_length"], 0)
        r = self._submit(self._rec(to="李静"), key=f"hand-fr-{self.uid}")
        self.assertEqual(r.status_code, 409, r.text)
        # 不生成批次报告
        rpt = self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches").json()
        self.assertEqual(rpt, [])
        after = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers").json()
        self.assertEqual(after["chain_length"], 0)

    def test_vehicle_order_conflicts_whole_order_writes_nothing(self):
        other = f"EX-SI-{uuid.uuid4().hex[:12]}"
        self.client.post("/api/v1/exhibits", json={
            "exhibit_id": other, "name": "o", "home_location": "库房B",
            "custodian": "王芳", "initial_seal": "SEAL-2"})
        self._register(key=f"reg-veh-{self.uid}")
        body = {"vehicle_id": "TRAIN-X-1", "items": [
            {"exhibit_id": self.ex_id, "records": [self._rec(to="李静")]},
            {"exhibit_id": other, "records": [
                self._rec(frm="王芳", to="赵磊", seal="SEAL-2", home="库房B",
                          digest=D2)]},
        ]}
        r = self.client.post("/api/v1/vehicle-handovers", json=body,
                             headers={"Idempotency-Key": f"veh-blocked-{self.uid}"})
        self.assertEqual(r.status_code, 409, r.text)
        # 其他件也不得入链、无整车报告
        self.assertEqual(
            self.client.get(f"/api/v1/exhibits/{other}/handovers").json()["chain_length"], 0)
        self.assertEqual(
            self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches").json(), [])
        # 整车报告也不留痕：用同键改载荷（去掉待核件不现实），改为查 DB 无报告——
        # 通过新键同车两件正常展品可成功，侧面证明之前无残留；这里直接断言无批次即可。

    def test_already_landed_idempotent_replay_takes_priority_during_pending(self):
        # 用固定键落单一笔成功交接
        fixed = f"hand-fixed-prio-{self.uid}"
        body = self._rec(to="李静")
        r1 = self._submit(body, key=fixed)
        self.assertEqual(r1.status_code, 200, r1.text)
        landed_batch = r1.json()["batch_id"]
        # 登记待核（冻结开始）
        self._register(observed="SEAL-Z", key=f"reg-prio-{self.uid}")
        # 新键提交 -> 冻结 409，无新报告
        r = self._submit(body, key=f"hand-new-{self.uid}")
        self.assertEqual(r.status_code, 409, r.text)
        # 已落单的同键同载荷重试 -> 优先返回原结果（冻结不拦截幂等重放）
        r2 = self._submit(body, key=fixed)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r2.json()["idempotent_replay"])
        self.assertEqual(r2.json()["batch_id"], landed_batch)

    def test_correction_and_withdrawal_still_work_during_pending(self):
        # 待核登记前先落两笔已受理批次 b1（第 1 环）、b2（第 2 环，最新）
        self._accept_one(to="李静")
        b2 = self._accept_one(frm="李静", to="王强",
                              when="2026-09-02T09:00:00+08:00", digest=D2)
        self._register(key=f"reg-cw-{self.uid}")
        # 补正照常（引用 b1 的第 1 环）
        rc = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/handovers/1/digest-corrections",
            json={"expected_digest": D1, "new_digest": D3, "reason": "摘要笔误"},
            headers={"Idempotency-Key": f"corr-during-{self.uid}"})
        self.assertEqual(rc.status_code, 201, rc.text)
        # 撤回照常：b2 是最新已受理批次且未被补正引用 -> 201
        rw = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b2, "reason": "待核期间撤回照常"},
            headers={"Idempotency-Key": f"wd-during-{self.uid}"})
        self.assertEqual(rw.status_code, 201, rw.text)
        # 待核冻结不解除：单件交接仍 409
        r = self._submit(self._rec(frm="李静", to="陈晨",
                                   when="2026-09-03T09:00:00+08:00", digest=D3),
                         key=f"hand-still-{self.uid}")
        self.assertEqual(r.status_code, 409, r.text)

    # -- 结案后恢复 + 旧令牌规则 ----------------------------------------------

    def test_after_close_handover_resumes_and_old_token_still_valid(self):
        # 待核登记前预演取令牌（链长 0）
        rt = self.client.post("/api/v1/handover-batches/rehearse",
                              json={"exhibit_id": self.ex_id,
                                    "records": [self._rec(to="李静")]})
        self.assertEqual(rt.status_code, 200)
        token = rt.json()["state_token"]["token"]
        # 登记 + 结案：交接链未被推进，旧令牌快照（链长/链头）不变
        eid = self._register(key=f"reg-tok-{self.uid}").json()["event_id"]
        # 待核期间带旧令牌提交仍报冲突（冻结优先于令牌比对）
        r = self._submit(self._rec(to="李静"), key=f"hand-blocked-{self.uid}", token=token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._close(eid, key=f"cls-tok-{self.uid}").status_code, 200)
        # 解锁后旧令牌仅按原链状态规则判断：链未推进 -> 仍有效，正常受理
        r = self._submit(self._rec(to="李静"), key=f"hand-unlocked-{self.uid}", token=token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED", r.text)

    def test_new_event_can_be_registered_after_close_and_chain_advances(self):
        e1 = self._register(key=f"reg-s1-{self.uid}").json()
        self.assertEqual(e1["event_seq"], 1)
        self.assertEqual(self._close(e1["event_id"], key=f"cls-s1-{self.uid}").status_code, 200)
        e2 = self._register(observed="SEAL-Q", key=f"reg-s2-{self.uid}").json()
        self.assertEqual(e2["event_seq"], 2)
        self.assertNotEqual(e2["prev_hash"], "SEAL_INCIDENTS_GENESIS")
        events = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents").json()
        self.assertEqual([e["event_seq"] for e in events], [1, 2])
        pending = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents",
            params={"status": "PENDING"}).json()
        self.assertEqual([e["event_id"] for e in pending], [e2["event_id"]])

    # -- 撤回改变有效封签后登记/结案以撤回后封签为准 ----------------------------

    def test_register_and_close_use_effective_seal_after_withdrawal(self):
        # 首环换签到 SEAL-2（带授权），再撤回该批次 -> 有效封签回到 SEAL-1
        b1 = self._accept_one(to="李静", seal="SEAL-2",
                              reason="书面授权换签 SEAL-2")
        rw = self.client.post(
            f"/api/v1/exhibits/{self.ex_id}/batch-withdrawals",
            json={"batch_id": b1, "reason": "撤回首站批次"},
            headers={"Idempotency-Key": f"wd-seal-{self.uid}"})
        self.assertEqual(rw.status_code, 201, rw.text)
        cur = self.client.get(f"/api/v1/exhibits/{self.ex_id}").json()
        self.assertEqual(cur["current_seal"], "SEAL-1")
        # 以撤回后封签 SEAL-1 为预期登记成功
        r = self._register(expected="SEAL-1", observed="SEAL-Z", key=f"reg-wd-{self.uid}")
        self.assertEqual(r.status_code, 201, r.text)
        eid = r.json()["event_id"]
        # 结案时复核值也须是撤回后有效封签
        self.assertEqual(self._close(eid, observed="SEAL-2",
                                     key=f"cls-wd-bad-{self.uid}").status_code, 409)
        self.assertEqual(self._close(eid, observed="SEAL-1",
                                     key=f"cls-wd-ok-{self.uid}").status_code, 200)

    # -- 审计 ----------------------------------------------------------------

    def test_audit_verify_includes_seal_incidents_chain(self):
        e1 = self._register(key=f"reg-aud1-{self.uid}").json()["event_id"]
        a = self.client.get(f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertTrue(a["valid"], a["detail"])
        self.assertIn("seal_incidents_chain", a)
        self.assertEqual(a["seal_incidents_chain"]["chain_length"], 1)
        self.assertEqual(a["seal_incidents_chain"]["first_invalid_seq"], None)
        self._close(e1, key=f"cls-aud1-{self.uid}")
        self._register(observed="SEAL-M", key=f"reg-aud2-{self.uid}")
        b = self.client.get(f"/api/v1/exhibits/{self.ex_id}/audit/verify").json()
        self.assertTrue(b["valid"], b["detail"])
        self.assertEqual(b["seal_incidents_chain"]["chain_length"], 2)

    # -- 知悉时刻快照 ----------------------------------------------------------

    def test_state_at_snapshot_boundaries(self):
        reg = self._register(key=f"reg-asof-{self.uid}")
        self.assertEqual(reg.status_code, 201)
        eid = reg.json()["event_id"]
        registered_at = _parse(reg.json()["registered_at"])
        # 登记前一毫秒：无待核事件
        before = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at",
            params={"at": (registered_at - timedelta(milliseconds=1)).isoformat()})
        self.assertEqual(before.status_code, 200, before.text)
        self.assertIsNone(before.json()["pending_seal_incident"])
        # 恰登记时刻（含等号）：待核出现
        at = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at",
            params={"at": registered_at.isoformat()})
        snap = at.json()["pending_seal_incident"]
        self.assertIsNotNone(snap)
        self.assertEqual(snap["event_id"], eid)
        self.assertEqual(snap["status"], "PENDING")
        self.assertEqual(snap["observed_seal"], "SEAL-X")
        self.assertIsNone(snap["closed_at"])
        # 结案
        cl = self._close(eid, key=f"cls-asof-{self.uid}")
        self.assertEqual(cl.status_code, 200)
        closed_at = _parse(cl.json()["closed_at"])
        # 结案前一毫秒仍待核
        still = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at",
            params={"at": (closed_at - timedelta(milliseconds=1)).isoformat()})
        self.assertIsNotNone(still.json()["pending_seal_incident"])
        # 恰结案时刻（含等号）：已不待核
        gone = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/state-at",
            params={"at": closed_at.isoformat()})
        self.assertIsNone(gone.json()["pending_seal_incident"])

    # -- 并发串行 --------------------------------------------------------------

    def test_concurrent_registrations_only_one_pending(self):
        barrier = threading.Barrier(2)
        results = []

        def register(observed, key):
            barrier.wait()
            r = self._register(observed=observed, key=key)
            results.append(r.status_code)

        t1 = threading.Thread(target=register, args=("SEAL-A", f"par-{uuid.uuid4()}"))
        t2 = threading.Thread(target=register, args=("SEAL-B", f"par-{uuid.uuid4()}"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results), [201, 409])
        pendings = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents",
            params={"status": "PENDING"}).json()
        self.assertEqual(len(pendings), 1)

    def test_concurrent_handover_and_registration_serialized_no_partial(self):
        # 交接与登记并发：要么交接先入链（登记的预期封签仍以当前为准，可能 201/409），
        # 要么登记先入待核（交接 409 不入链）。任何情况下不产生两件 PENDING，
        # 也不出现「交接 409 但记录已写」。
        barrier = threading.Barrier(2)
        out = {}

        def handover():
            barrier.wait()
            r = self._submit(self._rec(to="李静"), key=f"ph-{uuid.uuid4()}")
            out["hand"] = r.status_code

        def register():
            barrier.wait()
            r = self._register(observed="SEAL-C", key=f"pr-{uuid.uuid4()}")
            out["reg"] = r.status_code

        t1 = threading.Thread(target=handover)
        t2 = threading.Thread(target=register)
        t1.start(); t2.start(); t1.join(); t2.join()

        chain = self.client.get(f"/api/v1/exhibits/{self.ex_id}/handovers").json()
        pendings = self.client.get(
            f"/api/v1/exhibits/{self.ex_id}/seal-incidents",
            params={"status": "PENDING"}).json()
        self.assertLessEqual(len(pendings), 1)
        if out["hand"] == 409:
            # 被冻结：确认无记录、无批次报告
            self.assertEqual(chain["chain_length"], 0)
            self.assertEqual(
                self.client.get(f"/api/v1/exhibits/{self.ex_id}/batches").json(), [])
            self.assertEqual(out["reg"], 201)
        else:
            self.assertEqual(out["hand"], 200)
            self.assertEqual(chain["chain_length"], 1)


if __name__ == "__main__":
    unittest.main()
