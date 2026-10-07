#!/usr/bin/env bash
# 端到端调用示例：登记展品 -> 预演 -> 凭令牌提交 -> 幂等重试 -> 冲突检测 ->
# 提交违规批次（不落记录）-> 令牌失效与重新预演 -> 查询交接链 -> 哈希链审计校验 ->
# 凭证摘要补正（拒绝/冲突/幂等/独立补正链）-> 双链审计 ->
# 已受理批次撤回（拒绝/幂等/独立撤回链/有效状态回退）-> 三链审计 ->
# 车次整车交接（整单确认/拒绝/幂等/冲突/与单件接口共存）。
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; echo; fi; }
# 从响应中抽取状态令牌（有 jq 用 jq，无 jq 退化为 sed）
extract_token() {
  if [ -n "$JQ" ]; then jq -r '.state_token.token'; else
    sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\(stv1\.[^"]*\)".*/\1/p' | head -1; fi
}

EXHIBIT_ID="EX-DEMO-$(date +%s)"
KEY_OK="demo-ok-${EXHIBIT_ID}"
KEY_BAD="demo-bad-${EXHIBIT_ID}"
KEY_RH="demo-rh-${EXHIBIT_ID}"

# 补正用摘要（均为合法 sha256:<64hex>）
D_ORIG="sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"
D_NEW1="sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f00918273645564738291a0b1c2d3e4f50"
D_NEW2="sha256:5566778899aabbccddeeff00112233445566778899aabbccddeeff0011223344"
KEY_C1="demo-corr1-${EXHIBIT_ID}"
KEY_C2="demo-corr2-${EXHIBIT_ID}"

echo "== 0) 健康检查 =="
curl -s "${BASE}/health" | pp

echo; echo "== 1) 登记展品 ${EXHIBIT_ID}（馆藏地/保管人/初始封签为首站校验基准）=="
curl -s -X POST "${BASE}/api/v1/exhibits" \
  -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"name\":\"宋代青瓷莲花尊\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-0001\"}" | pp

echo; echo "== 2) 预演合规批次（只读核验：不落报告、不落记录、不推进链；返回状态令牌）=="
TOKEN="$(curl -s -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d @- <<JSON | extract_token
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON
)"
[ -n "${TOKEN}" ] || { echo "未取到预演令牌，终止"; exit 1; }
echo "已取得状态令牌：${TOKEN:0:40}..."

echo; echo "== 2b) 预演未知展品 -> 404 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"EX-NOT-EXIST","records":[{"location":"x","from_person":"a","to_person":"b","seal_no":"S","occurred_at":"2026-09-01T09:00:00+08:00","voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}]}'

echo; echo "== 3) 携带 Handover-Token 正式提交合规批次（链状态未变，正常受理）=="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON

echo; echo "== 4) 同键同载荷（含令牌）重试：链已推进但仍返回原报告，idempotent_replay=true =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -H "Handover-Token: ${TOKEN}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
     "seal_no":"SEAL-NJ-0001","occurred_at":"2026-09-01T09:00:00+08:00",
     "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"},
    {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
     "seal_no":"SEAL-SH-1002","seal_change_reason":"原封签运输途中磨损，双方馆长书面授权换签（批文号 SH-2026-091）",
     "occurred_at":"2026-09-03T14:30:00+08:00",
     "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"},
    {"location":"上海博物馆临时展厅","from_person":"王强","to_person":"陈晨",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-04T10:00:00+08:00",
     "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
  ]
}
JSON

echo; echo "== 5) 过期令牌再提交（链已推进）-> 409，需重新预演 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H "Handover-Token: ${TOKEN}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 6) 令牌格式无效 -> 400 参数错误 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_RH}" \
  -H 'Handover-Token: not-a-valid-token' \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院交接区","from_person":"陈晨","to_person":"赵磊","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00","voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"}]}'

echo; echo "== 7) 同幂等键但载荷不同 -> 409（令牌计入载荷身份）=="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_OK}" \
  -d "{\"exhibit_id\":\"${EXHIBIT_ID}\",\"records\":[{\"location\":\"x\",\"from_person\":\"a\",\"to_person\":\"b\",\"seal_no\":\"S\",\"occurred_at\":\"2026-09-05T09:00:00+08:00\",\"voucher_digest\":\"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9\"}]}"

echo; echo "== 8) 提交违规批次（不带令牌，人员断裂+时间倒退+无授权换签+凭证非法）-> REJECTED =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_BAD}" \
  -d @- <<JSON | pp
{
  "exhibit_id": "${EXHIBIT_ID}",
  "records": [
    {"location":"上海博物馆临时展厅","from_person":"陈晨","to_person":"赵磊",
     "seal_no":"SEAL-SH-1002","occurred_at":"2026-09-05T09:00:00+08:00",
     "voucher_digest":"sha256:aa11bb22cc33dd44ee55ff660011223344556677889900aabbccddeeff00112233"},
    {"location":"西安博物院交接区","from_person":"孙倩","to_person":"周明",
     "seal_no":"SEAL-XA-2001","occurred_at":"2026-09-05T08:30:00+08:00",
     "voucher_digest":"not-a-valid-digest"}
  ]
}
JSON

echo; echo "== 9) 查询交接链（应有 3 环：409/400/REJECTED 均未写入任何记录）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 10) 哈希链审计校验 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 11) 批次历史（含被驳回批次，供审计追溯；409/400 不产生报告）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batches" | pp

echo; echo "== 12) 补正第 3 环凭证摘要（原摘要 -> 新摘要）：201，锚定原记录 record_hash =="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp

echo; echo "== 13) 同键同载荷重试：返回原补正单（200, idempotent_replay=true）=="
curl -s -D /tmp/corr13.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/corr13.hdr

echo; echo "== 14) 旧预期值再补 -> 409（当前有效摘要已是 ${D_NEW1:0:18}...）=="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-corr-stale-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"再次补正\"}"

echo; echo "== 15) 以当前有效摘要为预期继续补正：201（补正链第 2 环，prev_hash 接第 1 环）=="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C2}" \
  -d "{\"expected_digest\":\"${D_NEW1}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"二次复核再更正（复核单 CR-2026-1002）\"}" | pp

echo; echo "== 16) 拒绝用例：目标不存在(404) / 原因空缺(422) / 新旧相同(422) / 同键异载荷(409) =="
curl -s -o /dev/null -w '目标链序号 999 不存在        -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/999/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-miss-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"x\"}"
curl -s -o /dev/null -w '原因空缺                     -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-empty-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"   \"}"
curl -s -o /dev/null -w '新旧摘要相同                 -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-corr-same-${EXHIBIT_ID}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW2}\",\"reason\":\"x\"}"
curl -s -o /dev/null -w '同键异载荷                   -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_NEW2}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"换一个载荷\"}"

echo; echo "== 17) 交接链查询：原摘要 voucher_digest 不变，current_voucher_digest 为最新，补正轨迹两环 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp

echo; echo "== 18) 单环节查询（原摘要 / 当前有效摘要 / 补正轨迹）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3" | pp

echo; echo "== 19) 补正链列表（可按链序号过滤）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/digest-corrections" | pp

echo; echo "== 20) 双链审计复算（交接链 + 补正链；补正不推进交接链，预演令牌不受影响）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 21) 幂等优先于现状：即便当前有效摘要已是 ${D_NEW2:0:14}... ，用 ${KEY_C1} 原载荷仍重放原单 =="
curl -s -D /tmp/corr21.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3/digest-corrections" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_C1}" \
  -d "{\"expected_digest\":\"${D_ORIG}\",\"new_digest\":\"${D_NEW1}\",\"reason\":\"凭证摘要录入笔误，据馆方复核原件更正（复核单 CR-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/corr21.hdr

# 从 JSON 响应抽取字段（有 jq 用 jq，无 jq 退化为 grep/sed）
json_field() {
  local field="$1"
  if [ -n "${JQ}" ]; then jq -r --arg f "${field}" '.[$f]'; else
    grep -oE "\"${field}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 \
      | sed -E "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/"; fi
}

KEY_W1="demo-wd1-${EXHIBIT_ID}"
KEY_W2="demo-wd2-${EXHIBIT_ID}"

echo; echo "== 22) 再提交一笔已受理批次（陈晨 -> 周敏），作为待撤回的最新批次 =="
BATCH_WD="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-batch-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院库房","from_person":"陈晨","to_person":"周敏","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-06T09:00:00+08:00","voucher_digest":"sha256:3344556677889900112233445566778899001122334455667788990011223344"}]}' \
  | json_field batch_id)"
echo "待撤回批次：${BATCH_WD}"
[ -n "${BATCH_WD}" ] || { echo "未取到批次号，终止"; exit 1; }

echo; echo "== 23) 撤回该批次：201，返回撤回后的有效状态（保管人回退为陈晨，原链历史长度不变）=="
curl -s -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_W1}" \
  -d "{\"batch_id\":\"${BATCH_WD}\",\"reason\":\"交接批次误登记，撤回后重新核对凭证再提交（复核单 WD-2026-1001）\"}" | pp

echo; echo "== 24) 同键同载荷重试：返回首次撤回结果（200, idempotent_replay=true）=="
curl -s -D /tmp/wd24.hdr -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_W1}" \
  -d "{\"batch_id\":\"${BATCH_WD}\",\"reason\":\"交接批次误登记，撤回后重新核对凭证再提交（复核单 WD-2026-1001）\"}" | pp
grep -i '^HTTP/' /tmp/wd24.hdr

echo; echo "== 25) 展品当前状态：保管人/封签按撤回后计算，chain_length（原链历史长度）仍为 4，有效链长 3 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}" | pp

echo; echo "== 26) 撤回拒绝用例：更晚批次(409) / 补正引用(409) / 批次被驳回(409) / 原因空(422) / 同键异载荷(409) =="
# 26a) 被补正引用：第 3 环属于最初的 3 记录批次（已被两笔补正引用），先重新提交一笔再制造更晚批次
BATCH_FRESH="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-fresh-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"西安博物院库房","from_person":"陈晨","to_person":"吴迪","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-07T09:00:00+08:00","voucher_digest":"sha256:4455667788990011223344556677889900aabbccddeeff001122334455667788"}]}' \
  | json_field batch_id)"
# 取最初 3 记录批次号（第 3 环所在批次）
BATCH_CORR="$(curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers/3" | json_field batch_id)"
curl -s -o /dev/null -w '存在更晚已受理批次时撤旧批次 -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-older-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${BATCH_CORR}\",\"reason\":\"试图撤回旧批次\"}"
# 26b) 目标批次被补正引用：先撤回最新批次，再撤被补正引用的旧批次
curl -s -o /dev/null -X POST "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_W2}" \
  -d "{\"batch_id\":\"${BATCH_FRESH}\",\"reason\":\"为演示补正引用拦截先撤回最新批次\"}"
curl -s -o /dev/null -w '批次已被补正单引用           -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-corr-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${BATCH_CORR}\",\"reason\":\"该批次已有补正，应被拒绝\"}"
# 26c) 批次被驳回
REJECTED_BATCH="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-rejb-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"x","from_person":"陌生人","to_person":"b","seal_no":"S","occurred_at":"2026-09-08T09:00:00+08:00","voucher_digest":"bad"}]}' \
  | json_field batch_id)"
curl -s -o /dev/null -w '撤回被驳回批次               -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-rej-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${REJECTED_BATCH}\",\"reason\":\"x\"}"
# 26d) 原因为空
curl -s -o /dev/null -w '撤回原因为空                 -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-empty-${EXHIBIT_ID}" \
  -d "{\"batch_id\":\"${BATCH_CORR}\",\"reason\":\"   \"}"
# 26e) 目标不存在 / 批次号格式非法
curl -s -o /dev/null -w '撤回不存在批次               -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-miss-${EXHIBIT_ID}" \
  -d '{"batch_id":"9f9c646e-0000-0000-0000-000000000000","reason":"x"}'
curl -s -o /dev/null -w '批次号格式非法               -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-wd-badid-${EXHIBIT_ID}" \
  -d '{"batch_id":"not-a-uuid","reason":"x"}'
# 26f) 同键异载荷
curl -s -o /dev/null -w '同键异载荷                   -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_W1}" \
  -d "{\"batch_id\":\"${BATCH_WD}\",\"reason\":\"换一个撤回原因\"}"

echo; echo "== 27) 交接链查询：被撤回批次记录仍完整保留（withdrawn=true），原链历史长度 4、有效链长 3 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers" | pp
echo "--- 仅有效交接记录（?effective_only=true）---"
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/handovers?effective_only=true" | pp

echo; echo "== 28) 撤回链列表（撤回时序，可按批次过滤）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batch-withdrawals" | pp

echo; echo "== 29) 撤回后继续提交：业务校验以撤回后有效状态（陈晨持有）为基准，原链仍物理追加 =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-wd-cont-${EXHIBIT_ID}" \
  -d '{"exhibit_id":"'"${EXHIBIT_ID}"'","records":[{"location":"南京博物院回程交接区","from_person":"陈晨","to_person":"周敏","seal_no":"SEAL-SH-1002","occurred_at":"2026-09-09T09:00:00+08:00","voucher_digest":"sha256:556677889900aabbccddeeff0011223344556677889900aabbccddeeff00112233"}]}' | pp

echo; echo "== 30) 三链审计复算（交接链 + 补正链 + 撤回链；原链历史不删除，有效状态按撤回后计算）=="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/audit/verify" | pp

echo; echo "== 31) 批次历史：已撤回批次带 withdrawn 标记与撤回引用 =="
curl -s "${BASE}/api/v1/exhibits/${EXHIBIT_ID}/batches" | pp

# ---------------------------------------------------------------- 车次整车交接
V1="EX-V1-$(date +%s)"
V2="EX-V2-$(date +%s)"
KEY_VEH="demo-veh-${V1}"
KEY_VEH_BAD="demo-veh-bad-${V1}"

echo; echo "== 32) 登记两件展品 ${V1} / ${V2}，准备同一车次整车交接 =="
curl -s -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${V1}\",\"name\":\"明代青花缠枝莲纹瓶\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-NJ-1001\"}" | pp
curl -s -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${V2}\",\"name\":\"清代掐丝珐琅香炉\",\"home_location\":\"南京博物院库房B区\",\"custodian\":\"王芳\",\"initial_seal\":\"SEAL-NJ-1002\"}" | pp

echo; echo "== 33) 车次整车交接：同一车次两件展品一次确认（全数通过 -> 同一事务各件入链）=="
curl -s -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH}" \
  -d @- <<JSON | pp
{
  "vehicle_id": "TRAIN-2026-1001",
  "items": [
    {"exhibit_id": "${V1}", "records": [
      {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
       "seal_no":"SEAL-NJ-1001","occurred_at":"2026-09-10T09:00:00+08:00",
       "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}
    ]},
    {"exhibit_id": "${V2}", "records": [
      {"location":"南京博物院库房B区","from_person":"王芳","to_person":"赵磊",
       "seal_no":"SEAL-NJ-1002","occurred_at":"2026-09-10T09:30:00+08:00",
       "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"}
    ]}
  ]
}
JSON

echo; echo "== 34) 同键同载荷重试：重放整单原报告（idempotent_replay=true，不重复入链）=="
curl -s -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH}" \
  -d @- <<JSON | pp
{
  "vehicle_id": "TRAIN-2026-1001",
  "items": [
    {"exhibit_id": "${V1}", "records": [
      {"location":"南京博物院库房B区","from_person":"张伟","to_person":"李静",
       "seal_no":"SEAL-NJ-1001","occurred_at":"2026-09-10T09:00:00+08:00",
       "voucher_digest":"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"}
    ]},
    {"exhibit_id": "${V2}", "records": [
      {"location":"南京博物院库房B区","from_person":"王芳","to_person":"赵磊",
       "seal_no":"SEAL-NJ-1002","occurred_at":"2026-09-10T09:30:00+08:00",
       "voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"}
    ]}
  ]
}
JSON

echo; echo "== 35) 同幂等键但载荷不同 -> 409 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1001\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"张伟\",\"to_person\":\"李静\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-10T09:00:00+08:00\",\"voucher_digest\":\"sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9\"}]},{\"exhibit_id\":\"${V2}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"王芳\",\"to_person\":\"钱进\",\"seal_no\":\"SEAL-NJ-1002\",\"occurred_at\":\"2026-09-10T09:30:00+08:00\",\"voucher_digest\":\"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809\"}]}]}"

echo; echo "== 36) 任一件核验失败 -> 整单 REJECTED：保存整单拒绝报告，两件都不入链 =="
curl -s -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_VEH_BAD}" \
  -d @- <<JSON | pp
{
  "vehicle_id": "TRAIN-2026-1002",
  "items": [
    {"exhibit_id": "${V1}", "records": [
      {"location":"上海博物馆交接区","from_person":"李静","to_person":"王强",
       "seal_no":"SEAL-NJ-1001","occurred_at":"2026-09-11T09:00:00+08:00",
       "voucher_digest":"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"}
    ]},
    {"exhibit_id": "${V2}", "records": [
      {"location":"上海博物馆交接区","from_person":"孙倩","to_person":"周明",
       "seal_no":"SEAL-XA-2001","occurred_at":"2026-09-11T09:30:00+08:00",
       "voucher_digest":"not-a-valid-digest"}
    ]}
  ]
}
JSON

echo; echo "== 37) 拒绝后两件展品链状态均未推进（整单要么全入链、要么全不入）=="
curl -s "${BASE}/api/v1/exhibits/${V1}" | pp
curl -s "${BASE}/api/v1/exhibits/${V2}" | pp

echo; echo "== 38) 整车单含不存在展品 -> 404，整单不写入 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-veh-miss-${V1}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1003\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]},{\"exhibit_id\":\"EX-NOT-EXIST\",\"records\":[{\"location\":\"x\",\"from_person\":\"a\",\"to_person\":\"b\",\"seal_no\":\"S\",\"occurred_at\":\"2026-09-12T09:30:00+08:00\",\"voucher_digest\":\"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809\"}]}]}"

echo; echo "== 39) 格式不合法 -> 422（展品重复 / 件数越界），整单不写入 =="
curl -s -o /dev/null -w '同一车次展品重复   -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-veh-dup-${V1}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1004\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]},{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T10:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]}]}"
curl -s -o /dev/null -w '仅 1 件（不足 2 件）-> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-veh-one-${V1}" \
  -d "{\"vehicle_id\":\"TRAIN-2026-1005\",\"items\":[{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"南京博物院库房B区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-12T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]}]}"

echo; echo "== 40) 整车入链后单件接口继续提交（既有接口完全兼容），并做三链审计 =="
curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-veh-cont-${V1}" \
  -d "{\"exhibit_id\":\"${V1}\",\"records\":[{\"location\":\"上海博物馆交接区\",\"from_person\":\"李静\",\"to_person\":\"王强\",\"seal_no\":\"SEAL-NJ-1001\",\"occurred_at\":\"2026-09-11T09:00:00+08:00\",\"voucher_digest\":\"sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506\"}]}" | pp
curl -s "${BASE}/api/v1/exhibits/${V1}/audit/verify" | pp
curl -s "${BASE}/api/v1/exhibits/${V2}/audit/verify" | pp

# ---------------------------------------------------------------- 知悉时刻历史快照
# 独立演示展品：用服务端受理时间作为知悉时刻边界（业务 occurred_at 只用于呈现）。
AID="EX-ASOF-$(date +%s)"
echo; echo "== 41) 登记知悉时刻演示展品 ${AID} =="
REG_JSON="$(curl -s -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${AID}\",\"name\":\"时点快照演示展品\",\"home_location\":\"库房A\",\"custodian\":\"张伟\",\"initial_seal\":\"SEAL-1\"}")"
echo "${REG_JSON}" | pp
REG_AT="$(echo "${REG_JSON}" | json_field created_at)"
echo "登记时间：${REG_AT}"

# 一个稍后用于「早于登记 -> 404」的时刻（2020 年）
echo; echo "== 42) 知悉时刻早于登记 -> 404；未知展品 -> 404 =="
curl -s -o /dev/null -w '早于登记      -> HTTP %{http_code}\n' \
  "${BASE}/api/v1/exhibits/${AID}/state-at?at=2020-01-01T00:00:00Z"
curl -s -o /dev/null -w '未知展品      -> HTTP %{http_code}\n' \
  "${BASE}/api/v1/exhibits/EX-NOT-EXIST/state-at?at=2020-01-01T00:00:00Z"

echo; echo "== 43) 参数校验：缺 at / 无时区 / 非法时间 / 未来时刻 -> 422 =="
curl -s -o /dev/null -w '缺少 at       -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at"
curl -s -o /dev/null -w '无时区        -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at?at=2026-09-10T09:00:00"
curl -s -o /dev/null -w '非法时间      -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at?at=not-a-time"
FUTURE="$(date -u -d '+2 days' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+2d +%Y-%m-%dT%H:%M:%SZ)"
curl -s -o /dev/null -w '未来时刻      -> HTTP %{http_code}\n' "${BASE}/api/v1/exhibits/${AID}/state-at?at=${FUTURE}"

# 业务时间写在 2026-09-01，但批次此刻才受理；先停 2 秒，使「受理前 1 秒」晚于登记时刻
sleep 2
echo; echo "== 44) 提交批次（业务发生时间 2026-09-01，服务端此刻受理）=="
B1="$(curl -s -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-b1-${AID}" \
  -d '{"exhibit_id":"'"${AID}"'","records":[{"location":"库房A","from_person":"张伟","to_person":"李静","seal_no":"SEAL-1","occurred_at":"2026-09-01T09:00:00+08:00","voucher_digest":"sha256:ababababababababababababababababababababababababababababababab"}]}' \
  | json_field batch_id)"
B1_AT="$(curl -s "${BASE}/api/v1/handover-batches/${B1}" | json_field created_at)"
echo "批次 ${B1} 受理时间：${B1_AT}（业务时间虽在 9 月 1 日，但此刻才进入可见链）"

# 由服务端受理时间派生「受理前 1 秒」的知悉时刻（证明业务时间不提前让记录出现）
B1_BEFORE="$(python3 - "${B1_AT}" <<'PY'
import sys, urllib.parse
from datetime import datetime, timedelta, timezone
t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")) - timedelta(seconds=1)
print(urllib.parse.quote(t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"))
PY
)"
echo; echo "== 45) 受理前 1 秒查询：历史交接虽已发生但尚未受理，空链回退登记状态 =="
curl -s "${BASE}/api/v1/exhibits/${AID}/state-at?at=${B1_BEFORE}" | pp

REG_AT_ENC="$(python3 - "${REG_AT}" <<'PY'
import sys, urllib.parse
print(urllib.parse.quote(sys.argv[1]))
PY
)"
echo; echo "== 46) 以登记时刻查询：空链，保管人=张伟、封签=SEAL-1、最近交接时间=null =="
curl -s "${BASE}/api/v1/exhibits/${AID}/state-at?at=${REG_AT_ENC}" | pp

# 第二、三环（第二环随后撤回），再对第一环补正，演示「有效记录 + 当时有效摘要 + 撤回排除」
curl -s -o /dev/null -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-b2-${AID}" \
  -d '{"exhibit_id":"'"${AID}"'","records":[{"location":"展厅B","from_person":"李静","to_person":"王强","seal_no":"SEAL-1","occurred_at":"2026-09-02T09:00:00+08:00","voucher_digest":"sha256:cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"}]}'
curl -s -o /dev/null -X POST "${BASE}/api/v1/exhibits/${AID}/handovers/1/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-c1-${AID}" \
  -d '{"expected_digest":"sha256:ababababababababababababababababababababababababababababababab","new_digest":"sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f00918273645564738291a0b1c2d3e4f50","reason":"凭证摘要录入笔误更正"}'
# 撤回最新批次（第 2 环）
B2="$(curl -s "${BASE}/api/v1/exhibits/${AID}/handovers/2" | json_field batch_id)"
curl -s -o /dev/null -X POST "${BASE}/api/v1/exhibits/${AID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-asof-w1-${AID}" \
  -d "{\"batch_id\":\"${B2}\",\"reason\":\"第二环批次误登记，撤回\"}"

echo; echo "== 47) 当前知悉时刻快照：原链长度/链头仍含被撤回的第 2 环；有效列表仅第 1 环（带补正后摘要）；保管人回退为李静 =="
NOW_ENC="$(python3 - <<'PY'
import urllib.parse
from datetime import datetime, timezone
print(urllib.parse.quote(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"))
PY
)"
curl -s "${BASE}/api/v1/exhibits/${AID}/state-at?at=${NOW_ENC}" | pp

# ---------------------------------------------------------------- 实物封签异常待核事件
# 独立演示展品：先交接两环 -> 登记待核（冻结预演/单件/整车，补正/撤回照常）->
# 待核期间补正旧环、撤回最新环（有效封签回到 SEAL-77）->
# 复核始终按操作时有效封签结案 -> 解锁恢复交接 -> 知悉时刻呈现待核。
SID="EX-SEAL-$(date +%s)"
KEY_SI_R="demo-si-reg-${SID}"
KEY_SI_C="demo-si-close-${SID}"
KEY_SI_H2="demo-si-hand2-${SID}"
D_SI1="sha256:9f2c4a1b7e3d5f608192a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"
D_SI2="sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"
D_SI3="sha256:7f8e9d0c1b2a39485766a5b4c3d2e1f00918273645564738291a0b1c2d3e4f506"

echo; echo "== 48) 登记待核演示展品 ${SID}（初始封签 SEAL-77），并先交接两环（周武->郑涛->何珊，封签保持 SEAL-77）=="
curl -s -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${SID}\",\"name\":\"封签待核演示展品\",\"home_location\":\"库房C\",\"custodian\":\"周武\",\"initial_seal\":\"SEAL-77\"}" | pp
curl -s -o /dev/null -X POST "${BASE}/api/v1/handover-batches" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-si-b1-${SID}" \
  -d '{"exhibit_id":"'"${SID}"'","records":[{"location":"库房C","from_person":"周武","to_person":"郑涛","seal_no":"SEAL-77","occurred_at":"2026-09-18T09:00:00+08:00","voucher_digest":"'"${D_SI1}"'"}]}'
SI_B2="$(curl -s -X POST "${BASE}/api/v1/handover-batches" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-si-b2-${SID}" \
  -d '{"exhibit_id":"'"${SID}"'","records":[{"location":"展厅D","from_person":"郑涛","to_person":"何珊","seal_no":"SEAL-77","occurred_at":"2026-09-19T09:00:00+08:00","voucher_digest":"'"${D_SI2}"'"}]}' \
  | json_field batch_id)"
echo "第 2 环批次：${SI_B2}（当前保管人何珊、当前有效封签 SEAL-77）"

echo; echo "== 49) 登记实物封签异常待核事件：预期=撤回后当前有效封签 SEAL-77，实见=SEAL-99（不同）-> 201 =="
SI_JSON="$(curl -s -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_SI_R}" \
  -d '{"expected_seal":"SEAL-77","observed_seal":"SEAL-99","reason":"开箱实见封签与系统封签不符，登记待核"}')"
echo "${SI_JSON}" | pp
SI_EID="$(echo "${SI_JSON}" | json_field event_id)"
echo "事件编号：${SI_EID}；状态 PENDING；登记时间见 registered_at"

echo; echo "== 50) 事件同键同载荷重试 -> 200 重放原登记（idempotent_replay=true）；同键异载荷 -> 409 =="
curl -s -D /tmp/si50.hdr -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_SI_R}" \
  -d '{"expected_seal":"SEAL-77","observed_seal":"SEAL-99","reason":"开箱实见封签与系统封签不符，登记待核"}' | pp
grep -i '^HTTP/' /tmp/si50.hdr
curl -s -o /dev/null -w '同键异载荷          -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_SI_R}" \
  -d '{"expected_seal":"SEAL-77","observed_seal":"SEAL-98","reason":"换一个实见值"}'

echo; echo "== 51) 登记拒绝用例：重复待核 409 / 预期封签不符 409 / 实见=预期 422 / 封签非法 422 / 未知展品 404 =="
curl -s -o /dev/null -w '重复待核             -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-dup-${SID}" \
  -d '{"expected_seal":"SEAL-77","observed_seal":"SEAL-98","reason":"再登记一件"}'
curl -s -o /dev/null -w '预期封签与现状不符   -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-exp-${SID}" \
  -d '{"expected_seal":"WRONG","observed_seal":"SEAL-98","reason":"r"}'
curl -s -o /dev/null -w '实见与预期相同       -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-same-${SID}" \
  -d '{"expected_seal":"SEAL-77","observed_seal":"SEAL-77","reason":"r"}'
curl -s -o /dev/null -w '封签号含空白(非法)   -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-bad-${SID}" \
  -d '{"expected_seal":"SEAL 77","observed_seal":"SEAL-98","reason":"r"}'
curl -s -o /dev/null -w '未知展品             -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/EX-NOT-EXIST/seal-incidents" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-miss-${SID}" \
  -d '{"expected_seal":"S1","observed_seal":"S2","reason":"r"}'

echo; echo "== 52) 待核冻结：预演 409 且不签发令牌；单件交接 409（不生成批次/记录）=="
curl -s -o /dev/null -w '预演                 -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches/rehearse" \
  -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"'"${SID}"'","records":[{"location":"展厅D","from_person":"何珊","to_person":"郑涛","seal_no":"SEAL-77","occurred_at":"2026-09-21T09:00:00+08:00","voucher_digest":"'"${D_SI3}"'"}]}'
curl -s -o /dev/null -w '单件交接             -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/handover-batches" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-handblock-${SID}" \
  -d '{"exhibit_id":"'"${SID}"'","records":[{"location":"展厅D","from_person":"何珊","to_person":"郑涛","seal_no":"SEAL-77","occurred_at":"2026-09-21T09:00:00+08:00","voucher_digest":"'"${D_SI3}"'"}]}'
echo "冻结期批次历史仍只有登记前两笔（不新增任何交接报告）："
curl -s "${BASE}/api/v1/exhibits/${SID}/batches" | pp

echo; echo "== 53) 含该展品的整车交接整单 409（同车其他件也不落单、不生成整车报告）=="
VO2="EX-VO2-$(date +%s)"
curl -s -o /dev/null -X POST "${BASE}/api/v1/exhibits" -H 'Content-Type: application/json' \
  -d "{\"exhibit_id\":\"${VO2}\",\"name\":\"同车他件\",\"home_location\":\"库房C\",\"custodian\":\"冯力\",\"initial_seal\":\"SEAL-88\"}"
curl -s -o /dev/null -w '含待核件的整车交接   -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/vehicle-handovers" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-veh-${SID}" \
  -d '{"vehicle_id":"TRAIN-SI-2001","items":[{"exhibit_id":"'"${SID}"'","records":[{"location":"展厅D","from_person":"何珊","to_person":"郑涛","seal_no":"SEAL-77","occurred_at":"2026-09-21T09:00:00+08:00","voucher_digest":"'"${D_SI3}"'"}]},{"exhibit_id":"'"${VO2}"'","records":[{"location":"库房C","from_person":"冯力","to_person":"许静","seal_no":"SEAL-88","occurred_at":"2026-09-20T09:30:00+08:00","voucher_digest":"sha256:1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"}]}]}'
echo "同车他件 ${VO2} 交接链（应为空，整单不落任何记录）："
curl -s "${BASE}/api/v1/exhibits/${VO2}/handovers" | pp

echo; echo "== 54) 待核期间补正与撤回照常：对第 1 环补正凭证摘要（201），再撤回最新批次 ${SI_B2}（201）=="
curl -s -o /dev/null -w '待核期补正第 1 环      -> HTTP %{http_code}\n' -X POST \
  "${BASE}/api/v1/exhibits/${SID}/handovers/1/digest-corrections" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-corr-${SID}" \
  -d "{\"expected_digest\":\"${D_SI1}\",\"new_digest\":\"${D_SI3}\",\"reason\":\"待核期间凭证摘要笔误照常补正\"}"
curl -s -X POST "${BASE}/api/v1/exhibits/${SID}/batch-withdrawals" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-wd-${SID}" \
  -d "{\"batch_id\":\"${SI_B2}\",\"reason\":\"待核期间最新批次误登记，照常撤回\"}" | pp
echo "撤回后当前状态（有效封签仍为 SEAL-77，保管人回退为郑涛；待核事件未结，交接仍冻结）："
curl -s "${BASE}/api/v1/exhibits/${SID}" | pp

echo; echo "== 55) 复核结案始终按操作时有效封签：复核值=SEAL-99 -> 409 保持 PENDING；=SEAL-77 -> 200 =="
curl -s -o /dev/null -w '复核值不符(SEAL-99)  -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents/${SI_EID}/close" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-close-bad-${SID}" \
  -d '{"observed_seal":"SEAL-99","note":"复核见99"}'
echo "冲突后事件仍为 PENDING（无复核结论/结案时间）："
curl -s "${BASE}/api/v1/exhibits/${SID}/seal-incidents/${SI_EID}" | pp
curl -s -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents/${SI_EID}/close" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_SI_C}" \
  -d '{"observed_seal":"SEAL-77","note":"复核复验与系统撤回后当前有效封签一致，异常系登记时误读，结案"}' | pp

echo; echo "== 56) 结案拒绝用例：已结案再结案 409 / 空说明 422 / 事件不匹配 404 / 事件号格式非法 422；结案同键重放 200 =="
curl -s -o /dev/null -w '已结案再结案         -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents/${SI_EID}/close" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-reclose-${SID}" \
  -d '{"observed_seal":"SEAL-77","note":"再次结案"}'
curl -s -o /dev/null -w '空说明               -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents/${SI_EID}/close" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-note-${SID}" \
  -d '{"observed_seal":"SEAL-77","note":"   "}'
curl -s -o /dev/null -w '事件不属于该展品     -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${VO2}/seal-incidents/${SI_EID}/close" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-mismatch-${SID}" \
  -d '{"observed_seal":"SEAL-88","note":"n"}'
curl -s -o /dev/null -w '事件号格式非法       -> HTTP %{http_code}\n' -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents/not-a-uuid/close" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-si-badid-${SID}" \
  -d '{"observed_seal":"SEAL-77","note":"n"}'
curl -s -D /tmp/si56.hdr -X POST "${BASE}/api/v1/exhibits/${SID}/seal-incidents/${SI_EID}/close" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: ${KEY_SI_C}" \
  -d '{"observed_seal":"SEAL-77","note":"复核复验与系统撤回后当前有效封签一致，异常系登记时误读，结案"}' | pp
grep -i '^HTTP/' /tmp/si56.hdr

echo; echo "== 57) 解锁后：预演恢复签发令牌，单件交接恢复（以撤回后有效状态：郑涛/SEAL-77 为基准）=="
TOKEN_SI="$(curl -s -X POST "${BASE}/api/v1/handover-batches/rehearse" -H 'Content-Type: application/json' \
  -d '{"exhibit_id":"'"${SID}"'","records":[{"location":"南京博物院回程交接区","from_person":"郑涛","to_person":"周武","seal_no":"SEAL-77","occurred_at":"2026-09-22T09:00:00+08:00","voucher_digest":"'"${D_SI3}"'"}]}' \
  | extract_token)"
[ -n "${TOKEN_SI}" ] && echo "解锁后已取得令牌：${TOKEN_SI:0:36}..."
curl -s -X POST "${BASE}/api/v1/handover-batches" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY_SI_H2}" -H "Handover-Token: ${TOKEN_SI}" \
  -d '{"exhibit_id":"'"${SID}"'","records":[{"location":"南京博物院回程交接区","from_person":"郑涛","to_person":"周武","seal_no":"SEAL-77","occurred_at":"2026-09-22T09:00:00+08:00","voucher_digest":"'"${D_SI3}"'"}]}' | pp
echo "--- 事件链列表（1 件，状态 RESOLVED，带复核结论与结案时间）---"
curl -s "${BASE}/api/v1/exhibits/${SID}/seal-incidents" | pp
echo "--- 四链审计（交接链 + 补正链 + 撤回链 + 待核事件链）---"
curl -s "${BASE}/api/v1/exhibits/${SID}/audit/verify" | pp

echo; echo "== 58) 知悉时刻快照呈现该时点待核状态：以服务端登记/结案时间为界 =="
SI_REG="$(echo "${SI_JSON}" | python3 -c 'import sys,json,urllib.parse; print(urllib.parse.quote(json.load(sys.stdin)["registered_at"]))')"
echo "恰登记时刻（含等号）-> pending_seal_incident 非空："
curl -s "${BASE}/api/v1/exhibits/${SID}/state-at?at=${SI_REG}" \
  | python3 -c 'import sys,json; j=json.load(sys.stdin); print("pending?", j["pending_seal_incident"] is not None, "-", (j["pending_seal_incident"] or {}).get("event_id"))'
NOW_SI_ENC="$(python3 - <<'PY'
import urllib.parse
from datetime import datetime, timezone
print(urllib.parse.quote(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"))
PY
)"
echo "当前时刻（已结案）-> pending_seal_incident 为 null："
curl -s "${BASE}/api/v1/exhibits/${SID}/state-at?at=${NOW_SI_ENC}" \
  | python3 -c 'import sys,json; print("pending?", json.load(sys.stdin)["pending_seal_incident"])'
