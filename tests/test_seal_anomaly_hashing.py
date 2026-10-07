"""封签异常待核事件请求指纹单测（纯标准库，无数据库依赖）。"""
from __future__ import annotations

import uuid

from app.hashing import (
    seal_anomaly_close_request_hash,
    seal_anomaly_request_hash,
)

EID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def test_register_request_hash_same_payload_equal():
    a = seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-9", "实物封签破损")
    b = seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-9", "实物封签破损")
    assert a == b
    assert len(a) == 64 and int(a, 16) >= 0


def test_register_request_hash_different_payload_differs():
    base = seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-9", "原因")
    assert seal_anomaly_request_hash("EX-0002", "SEAL-1", "SEAL-9", "原因") != base
    assert seal_anomaly_request_hash("EX-0001", "SEAL-2", "SEAL-9", "原因") != base
    assert seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-8", "原因") != base
    assert seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-9", "别的原因") != base


def test_register_request_hash_reason_whitespace_normalized():
    assert seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-9", " 原因 ") == \
        seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-9", "原因")


def test_close_request_hash_same_payload_equal():
    a = seal_anomaly_close_request_hash(EID, "SEAL-1", "复核无误")
    b = seal_anomaly_close_request_hash(EID, "SEAL-1", "复核无误")
    assert a == b


def test_close_request_hash_different_payload_differs():
    base = seal_anomaly_close_request_hash(EID, "SEAL-1", "复核无误")
    assert seal_anomaly_close_request_hash(uuid.uuid4(), "SEAL-1", "复核无误") != base
    assert seal_anomaly_close_request_hash(EID, "SEAL-2", "复核无误") != base
    assert seal_anomaly_close_request_hash(EID, "SEAL-1", "另一说明") != base


def test_close_request_hash_note_whitespace_normalized():
    assert seal_anomaly_close_request_hash(EID, "SEAL-1", " 复核无误 ") == \
        seal_anomaly_close_request_hash(EID, "SEAL-1", "复核无误")


def test_register_and_close_hashes_do_not_collide():
    # 登记与结案指纹作用于不同幂等键空间，但载荷结构相似时也不应相同
    a = seal_anomaly_request_hash("EX-0001", "SEAL-1", "SEAL-9", "原因")
    b = seal_anomaly_close_request_hash(EID, "SEAL-1", "原因")
    assert a != b
