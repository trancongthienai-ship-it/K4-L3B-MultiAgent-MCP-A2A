# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi case chạy trong một scope độc lập. Coordinator chỉ chuyển sang điều tra domain sau khi
Entity Agent xác nhận một order hợp lệ bằng `get_order`. Candidate không có định dạng order ID
được loại tại client để không lãng phí MCP call; candidate hợp lệ được thử tuần tự và có giới hạn.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

Trước khi mở MCP session, CLI gọi Competition API để create/resume run của team và xác nhận
`variant_id` cùng `case_set_version` khớp bộ input local. Bước này là bắt buộc vì tool discovery
không cần active run nhưng mọi evidence tool cần run scope hợp lệ.

Specialist trả evidence envelope, không trả kết luận tự do. Coordinator tạo output xác định
(deterministic) từ evidence; entity IDs và provenance luôn được lấy từ payload đã validate.
Verifier ghi trạng thái cuối, còn CLI validate output bằng JSON Schema trước khi ghi atomically.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case, candidates, customer hint | resolve order, lấy lịch sử customer | `get_order`, `get_customer_history` | resolved/rejected IDs và related orders |
| Coordinator | case và specialist results | lập kế hoạch, giới hạn call, assemble output | không gọi domain tool trực tiếp | task/handoff và draft output |
| Order/product | resolved order | items, seller và product context | `get_order_items`, `get_product_context` | item/seller entities |
| Shipment | resolved order | timeline và trách nhiệm giao hàng | `get_shipment_summary` | shipment verdict, late sellers |
| Payment/refund | resolved order | captured/refunded/refundable totals | `get_payment_timeline`, `get_refund_timeline` | payment verdict và totals |
| Policy | policy version | lấy policy đúng version/case | `get_policy` | policy evidence |
| Conflict resolver | draft và evidence | precedence, conflicts, semantic review | không gọi MCP | bounded corrections |
| Verifier | output draft | invariant và provenance checks | không gọi MCP | `verification_completed` |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- Claimed order đứng đầu danh sách, sau đó mới tới candidate, và được de-duplicate giữ thứ tự.
- Candidate sai định dạng Olist 32-hex bị reject tại client. Candidate đúng định dạng chỉ được chấp
  nhận nếu `get_order` trả record tồn tại và `order_id` khớp.
- Mọi MCP request luôn mang `case_id`. Mọi message observable dùng cùng `case_id`, actor, target
  và decision code; không ghi prompt hoặc chain-of-thought.
- Handoff chỉ xảy ra sau kết quả evidence hoặc failure hữu hạn. Không actor nào tự giao việc ngược
  lại cho chính nó, do đó không tạo vòng lặp.
- Mỗi tool chỉ được gọi tối đa một lần cho resolved order. Gateway dùng timeout transport cấu hình
  sẵn; workflow không retry mù vì failed call cũng bị tính vào efficiency.

## 4. Evidence và conflict lifecycle

`EvidenceGateway` validate mọi response bằng `mcp-evidence-response-v1` trước khi workflow nhìn
thấy dữ liệu. Evidence được giữ trong map cục bộ của case theo tool name; cache chỉ áp dụng cho
danh sách tool, tuyệt đối không cache evidence giữa case. Mỗi response thực sự dùng sẽ emit
`tool_result_consumed` với đúng ref và ref đó mới được đưa vào output.

Nguồn lifecycle chuyên biệt (`shipment`, `payment`, `refund`) được ưu tiên hơn base order row cho
trạng thái tương ứng. Warning có dấu hiệu conflict được biểu diễn trong `data_conflicts`, ghi cả hai
source, selected source và resolution code. Claim shipment chỉ liên kết shipment/item refs; claim
refund liên kết refund/policy/payment refs để tránh evidence không liên quan.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/tool error | 0 automatic retry | dừng run để tránh output thiếu provenance và call thừa | CLI error |
| Entity not found/ambiguous | mỗi candidate hợp lệ 1 call | không gọi specialist, trả `not_found` | `ENTITY_NOT_FOUND` |
| Source conflict | 0 MCP retry | chọn authoritative domain source, giảm confidence | `POLICY_AND_CONFLICTS_RESOLVED` |
| Invalid specialist result | 0 | gateway reject envelope và dừng run | CLI error |

Query budget tối đa thông thường là 8 calls/case: order, items, shipment, payment timeline, refund
timeline, policy, customer history và product context. Refund timeline là optional vì gateway trả
tool error khi order chưa từng có refund; lần gọi đã audit vẫn chứng minh workflow đã kiểm tra.
`get_order_payments` và `get_sellers` không gọi vì dữ liệu đã nằm trong timeline/items. Tool
discovery được cache ở gateway. Missing evidence luôn tạo giá trị null/insufficient thay vì tự dựng
evidence ref hoặc số tiền.

## 6. Verification invariants

- `case_id` ở output phải trùng input; chỉ order đã resolve mới xuất hiện trong affected entities.
- Candidate sai/không tồn tại nằm trong rejected set và không được dùng cho domain calls.
- Evidence ref chỉ lấy nguyên văn từ MCP response trong cùng case; claim refs là tập con output refs.
- Shipment seller delay phải liên kết seller; logistics delay liên kết shipment/provider nếu có.
- Tiền dùng `Decimal`, làm tròn hai chữ số, không âm; refundable mặc định bằng captured trừ refunded.
- Refund đã hoàn thành/pending không tạo lệnh refund trùng; no-action không tạo refund line.
- Lifecycle source thắng base row khi source mâu thuẫn và conflict làm giảm confidence.
- Confidence luôn trong `[0, 1]`; danh sách ID/ref được unique và giới hạn theo schema.
- CLI chạy JSON Schema validation trước khi replace file output và chỉ sau đó emit `case_finalized`.

## 7. Reproducibility

- Runtime: Python 3.11+, dependency ranges trong `pyproject.toml`, xử lý case tuần tự và MCP calls
  tuần tự trên một session. Không dùng random cho quyết định nghiệp vụ.
- Workflow chạy deterministic từ structured MCP evidence, không phụ thuộc model ngoài và không
  gửi dữ liệu case hoặc credential sang dịch vụ LLM khác.
- Lệnh chuẩn: `pytest -q`, `ruff check .`, `day09 validate-inputs`, `day09 run`, `day09 validate`.
- Trace event IDs/timestamps không deterministic nhưng không ảnh hưởng quyết định hoặc totals.
- API key chỉ đọc từ `.env`, không được đưa vào prompt, output, trace hoặc submission ZIP.
