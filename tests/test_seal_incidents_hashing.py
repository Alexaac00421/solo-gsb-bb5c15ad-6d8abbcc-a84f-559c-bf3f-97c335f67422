"""实物封签异常待核事件哈希与请求指纹单测（纯标准库，无数据库依赖）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.hashing import (
    SEAL_INCIDENTS_GENESIS_HASH,
    seal_incident_close_request_hash,
    seal_incident_hash,
    seal_incident_request_hash,
)
from app.validation import is_valid_seal_no


EID = uuid.UUID("12345678-abcd-ef01-2345-6789abcdef01")
AT = datetime(2026, 10, 7, 8, 0, 0, tzinfo=timezone.utc)
CLOSED = datetime(2026, 10, 7, 9, 30, 0, tzinfo=timezone.utc)


def _calc(**overrides):
    kw = dict(
        prev_hash=SEAL_INCIDENTS_GENESIS_HASH,
        event_id=EID,
        exhibit_id="EX-0001",
        event_seq=1,
        expected_seal="SEAL-BJ-0001",
        observed_seal="SEAL-X-9001",
        reason="开箱实见封签与系统封签不符",
        status="PENDING",
        review_observed_seal=None,
        review_note=None,
        registered_at=AT,
        closed_at=None,
    )
    kw.update(overrides)
    return seal_incident_hash(**kw)


def test_pending_hash_deterministic_and_hex():
    h1 = _calc()
    h2 = _calc()
    assert h1 == h2
    assert len(h1) == 64 and int(h1, 16) >= 0


def test_next_ring_chains_to_previous():
    first = _calc()
    second = _calc(prev_hash=first, event_seq=2,
                   event_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
    assert first != second


def test_changing_any_register_field_breaks_hash():
    base = _calc()
    assert _calc(exhibit_id="EX-0002") != base
    assert _calc(expected_seal="OTHER") != base
    assert _calc(observed_seal="OTHER2") != base
    assert _calc(reason="另一个原因") != base
    assert _calc(event_seq=9) != base
    assert _calc(event_id=uuid.uuid4()) != base
    assert _calc(prev_hash="c" * 64) != base


def test_close_changes_hash_and_fields_signed():
    pending = _calc()
    resolved = _calc(
        status="RESOLVED",
        review_observed_seal="SEAL-BJ-0001",
        review_note="复核与系统封签一致",
        closed_at=CLOSED,
    )
    assert pending != resolved
    # 复核字段任一变化都进哈希（结案后篡改可定位）
    assert _calc(
        status="RESOLVED", review_observed_seal="SEAL-BJ-0001",
        review_note="另一份说明", closed_at=CLOSED,
    ) != resolved
    assert _calc(
        status="RESOLVED", review_observed_seal="SEAL-TAMPERED",
        review_note="复核与系统封签一致", closed_at=CLOSED,
    ) != resolved
    assert _calc(
        status="RESOLVED", review_observed_seal="SEAL-BJ-0001",
        review_note="复核与系统封签一致",
        closed_at=CLOSED + timedelta(seconds=1),
    ) != resolved
    # 仅状态翻转（无复核内容）也改变哈希
    assert _calc(status="RESOLVED", closed_at=CLOSED) != pending


def test_reason_whitespace_normalized_in_hash():
    assert _calc(reason="  异常原因 ") == _calc(reason="异常原因")


def test_timestamp_timezone_normalized():
    plus8 = datetime(2026, 10, 7, 16, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert plus8.isoformat() != AT.isoformat()
    assert _calc(registered_at=plus8) == _calc(registered_at=AT)


def test_register_request_hash_same_payload_equal():
    a = seal_incident_request_hash("EX-0001", "S1", "S2", "原因")
    b = seal_incident_request_hash("EX-0001", " S1 ", "S2", "原因")
    assert a == b


def test_register_request_hash_different_payload_differs():
    base = seal_incident_request_hash("EX-0001", "S1", "S2", "原因")
    assert seal_incident_request_hash("EX-0002", "S1", "S2", "原因") != base
    assert seal_incident_request_hash("EX-0001", "S1", "S3", "原因") != base
    assert seal_incident_request_hash("EX-0001", "S2", "S2", "原因") != base
    assert seal_incident_request_hash("EX-0001", "S1", "S2", "别的") != base


def test_close_request_hash_independent_and_distinct():
    a = seal_incident_close_request_hash(EID, "S1", "说明")
    b = seal_incident_close_request_hash(EID, " S1 ", "说明")
    assert a == b
    assert seal_incident_close_request_hash(uuid.uuid4(), "S1", "说明") != a
    assert seal_incident_close_request_hash(EID, "S2", "说明") != a
    assert seal_incident_close_request_hash(EID, "S1", "别的说明") != a


def test_is_valid_seal_no():
    assert is_valid_seal_no("SEAL-1")
    assert is_valid_seal_no("SEAL-NJ-0001")
    assert is_valid_seal_no("封签-甲-01")
    assert not is_valid_seal_no("")
    assert not is_valid_seal_no("   ")
    assert not is_valid_seal_no(" SEAL-1")
    assert not is_valid_seal_no("SEAL-1 ")
    assert not is_valid_seal_no("SEAL 1")
    assert not is_valid_seal_no("SEAL\t1")
    assert not is_valid_seal_no("x" * 101)
