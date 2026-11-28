# 服装批次争议售后办理服务

本服务把**投诉受理 → 批次质量处置 → 仓库履行 → 售后赔付与复核**串成可重复办理的 HTTP 后端。
所有人员、批次、实物、订单、检测报告与外部流水均为合成资料，**不构成真实产品安全结论**。

- Python 3.11，仅标准库
- SQLite 持久化；重启用同一状态文件即可继续查询
- 每次写操作留审计事件；投诉原始线索与检测报告原文不可被商家改写

## 启动

```sh
# 导入 data/catalog.json（可重复）并启动
python3 -m service.server --port 8000 --state aftercare.sqlite3

# 不导入资料
python3 -m service.server --port 8000 --state aftercare.sqlite3 --no-seed

# 连接合成协作方回放器（另开一个终端）
python3 partner/server.py --port 8765 --state partner-state.sqlite3
python3 -m service.server --port 8000 --state aftercare.sqlite3 --partner-url http://127.0.0.1:8765
```

启动后第一行输出实际端口；`GET /health` 不需要身份。业务接口需要 `X-Actor` 头，值取自
`data/catalog.json` 的 `principals`（例如 `quality-A`、`warehouse-A`、`reviewer-A`、`customer-A`、`merchant-A`）。
该头只用于演练归属，**不是认证凭据**。

角色映射（`principals` 的值可以是角色，也可以是该身份所属商家）：

| 身份 | 角色 | 能确认的事实 |
| --- | --- | --- |
| `customer-A` | consumer | 自己的投诉、补证、本人选择、查询 |
| `merchant-A` | merchant（`shop-A`） | 受理投诉线索、追加待查商品；不能代顾客确认选择 |
| `warehouse-A` | warehouse | 出库、交运、运单查询、退货接收与去向复核 |
| `quality-A` / `quality-B` | quality | 报告、停供/调查/不合格、解除某项、范围确认 |
| `reviewer-A` | independent-aftercare | 通知、履行、赔付复核、分项结案、更正影响复核 |

可选请求头 `Idempotency-Key`：同键同内容只执行一次并返回首次结果（带 `idempotent_replay: true`）；
同键不同内容返回 409。

## 接口一览

### 投诉与待查范围

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /complaints` | consumer/merchant/quality/warehouse/reviewer | 受理投诉；可来自订单，也可只有吊牌/商家/购买时间 |
| `GET /complaints?status=&order=&batch=` | 任意 | 列表 |
| `GET /complaints/{id}` | 任意 | 原始线索、补证、分项、通知、选择、履行、赔付、待办 |
| `GET /complaints/{id}/case` | 任意 | 按件解释已完成/待办 + 顾客可继续补证与查询 |
| `GET /complaints/{id}/items/{item}` | 任意 | 单件解释 |
| `POST /complaints/query` | 任意 | 用线索查候选批次；结果永远标记 `confirmed: false` |
| `POST /complaints/{id}/evidence` | 除 system | 顾客补证（可附 `item` 定位实物 → 批次） |
| `POST /complaints/{id}/targets` | merchant/warehouse/quality/reviewer | 人工追加待查分项（不合并整单） |
| `POST /complaints/{id}/confirm-scope` | quality | 确认受影响批次范围；未确认前不能据此停供或通知 |
| `POST /complaints/{id}/notify` | reviewer/quality | 按已确认范围通知，记录通知对象与批次 |
| `POST /complaints/{id}/choices` | consumer（确认）/merchant（仅登记） | 记录退款/换货/保留选择 |
| `POST /complaints/{id}/exchanges` | reviewer/warehouse | 为**已确认**换货保留替换库存 |
| `POST /complaints/{id}/exchange-advance` | reviewer/warehouse | `dispatch` / `complete` / `cancel` |
| `POST /complaints/{id}/resolve-item` | reviewer | 按件结案；同订单其他分项保持未结 |

### 批次、报告、质量处置

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /batches`、`GET /batches/{id}` | 任意 | 批次、实物、分装映射、当前限制 |
| `GET /orders`、`GET /orders/{id}` | 任意 | 订单分项、批次、赔付与投诉 |
| `GET /cases?status=&order_id=` | 任意 | 案件总览（按件状态与待办） |
| `GET /warehouse/stock` | 任意 | 与 inventory 相同的仓库汇总 |
| `POST /batches` | warehouse/quality | 登记批次（款号/色号/供应商分别保存） |
| `POST /batch-mappings`、`GET /batch-mappings` | warehouse/quality | 分装/子批次关系；必须引用 `evidence_ref`，禁止环 |
| `GET /repacks`、`GET /repacks/{id}` | 任意 | 混箱资料：同箱多批次，不改变每件实物所属批次 |
| `POST /reports` | quality | 新检测报告（`digest` 为 64 位文档摘要） |
| `GET /reports/{id}` | 任意 | 含更正链；原文与撤回信息保留 |
| `GET /reports/{id}/impact?include_mapped=&include_supplied=` | 任意 | **按一份证据**列出真正受影响的批次、实物、出库单、履行 |
| `POST /reports/{id}/withdraw` | quality | 撤回；其他仍有效处置不随之消失 |
| `POST /reports/{id}/correct` | quality | 更正生成新报告，旧报告转 `superseded` 且仍可查 |
| `POST /dispositions` | quality | `stop_supply` / `investigate` / `not_conforming` |
| `GET /dispositions`、`GET /dispositions/{id}` | 任意 | 适用范围、生效批次、库存影响 |
| `GET /dispositions/{id}/impact` | 任意 | 当前实物与履行分布 |
| `POST /dispositions/{id}/lift` | quality | 解除一项处置；仅当批次无其他有效限制时恢复库存 |

范围解析规则（`service/scope.py`）：

- 报告直接列明批次；报告的 `scope.batch_ids`；处置适用批次；**经质量确认的投诉范围**。
- 款号单独出现会被拒绝（`insufficient_scope`），必须给颜色/供应商/批次，或用 `all_colors=true` 显式扩大。
- `include_mapped` 沿分装/子批次关系双向扩展；`include_supplied` 才按同供应商扩展。
- 款号相同、颜色相同、同箱都不是批次合并依据。

### 仓库

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /warehouse/inventory?batch=&state=` | 任意 | 实物状态、去向、限制、`available` |
| `GET /warehouse/items/{id}` | 任意 | 单件：批次、限制、出库单、退货、履行 |
| `POST /warehouse/shipments` | warehouse | 记录出库；`hand_over=true` 表示已交承运人 |
| `GET /warehouse/shipments?phase=&item_id=&batch_id=` | 任意 | 出库单与事件 |
| `POST /warehouse/shipments/{id}/handover` | warehouse | 交运；批次受限制时 409 |
| `POST /warehouse/shipments/{id}/phase` | warehouse/quality | 显式推进阶段（如 `intercepting`） |
| `POST /warehouse/shipments/{id}/tracking` | warehouse/quality/reviewer | 运单查询结果（含 `carrier_revision`） |
| `POST /warehouse/returns` | warehouse/quality | 退货接收；先按该件批次与当前处置定去向 |
| `GET /warehouse/returns?item=&complaint=` | 任意 | 退货记录 |
| `POST /warehouse/returns/{id}/decide` | warehouse/quality | `resell` / `quarantine` / `scrap`；受限制批次禁止 `resell` |

实物状态：`in_stock`、`picked`（已分拣未交运）、`shipped`、`intercepted`、`returned`、`lost`。
去向：`quarantine`、`intercepting`、`intercept_failed`、`intercept_succeeded`、`resell`、`scrap`、
`awaiting_disposition`、`exchange_reserved`。**退款不会自动把退货转为可售。**

### 协作方（承运人 / 支付）

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /partners/commands` | warehouse/reviewer/quality | 登记并发起一次流水；504/202 只表示受理情况 |
| `GET /partners/commands/{id}` | 同上 | 读取下一条观测并落到业务记录；重复内容不重复应用 |
| `POST /partners/commands/{id}/observation` | 同上 | 离线演练：直接写入一条合成观测 |
| `POST /partners/commands/{id}/replay` | 同上 | 按 `observations` 列表逐条复算（对应回放器逐次查询） |

- `intercepting` 是待查，不等于已拦截；只有 `state=intercepted` 才记录拦截成功，且仍受质量处置限制。
- `lost` 之后可用 `/tracking` 继续查询；较早 `carrier_revision` 的迟到记录只留存，不覆盖当前阶段。
- 支付 `state=paid` 才解除未结责任；`pending`/未知期间该笔保持待查，且不能重复退款。

### 赔付与更正

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /payments` | reviewer | 复核后退款/换货退款/补偿（金额单位为分） |
| `GET /payments?order_id=&item_id=&state=`、`GET /payments/{id}` | 任意 | 含 `entitlement`（成交、已退、待查、可退余额） |
| `POST /payments/{id}/submit` | reviewer | 登记支付侧流水；保留待查责任 |
| `POST /corrections` | quality/reviewer | 报告更正影响：应通知、应补付复核、转人工清单 |
| `GET /corrections?report=` | 任意 | 历史评估 |
| `POST /issues`、`GET /issues?status=`、`POST /issues/{id}/resolve` | 相关角色 | 人工待办 |

赔付约束：

- 不得超过该订单分项 `paid`；
- 已有 `pending_payment` 或 `paid` 退款时，不能再按“退款 + 换货退款”各付一遍（409 `duplicate_payment`）；
- 商品不必先退回，但 `not_returned=true` 时必须提供 `item_disposition` 与 `customer_notice`；
- 赔偿之外的额外补偿需独立 `review_note`；
- 更正不自动向顾客扣回已退款，`accepted_refunds_are_not_clawed_back` 恒为 `true`。

## 状态与去向对照

| 层 | 字段 | 取值 |
| --- | --- | --- |
| 实物位置 | `garments.state` | `in_stock`、`picked`、`shipped`、`intercepted`、`returned`、`lost` |
| 实物去向 | `garments.current_outcome` | `quarantine`、`intercepting`、`intercept_failed`、`intercept_succeeded`、`resell`、`scrap`、`awaiting_disposition`、`exchange_reserved` |
| 出库单阶段 | `shipments.phase` | `dispatch_pending`、`carrier_pending`、`with_carrier`、`intercepting`、`intercepted`、`delivered`、`returned`、`cancelled`、`lost` |
| 质量处置 | `dispositions.type` / `status` | `stop_supply`、`investigate`、`not_conforming` / `active`、`lifted` |
| 投诉 | `complaints.status` | `open`、`investigating`、`notified`、`choice_made`、`in_fulfillment`、`pending_payment`、`resolved` |
| 分项 | `complaint_targets.status` | `open`、`resolved` |
| 赔付 | `payments.state` | `pending_payment`（待查）、`paid`、`failed`、`cancelled`、`reversed` |

同一批次可同时受多项有效处置覆盖；`GET /dispositions/{id}` 的
`inventory_still_restricted` 展示每批次当前仍生效的全部依据 ID。`available=true` 需要同时满足
实物在库、批次无有效限制、且去向不是隔离/报废/保留。

## 重启与迁移

- 服务启动时执行 `CREATE TABLE IF NOT EXISTS` 与轻量列迁移，早期状态文件可直接继续使用。
- 读请求使用独立连接，写请求用 `BEGIN IMMEDIATE`，并发提交相同投诉会得到 409 而不是重复记录。
- `--no-seed` 可用于只读复核既有状态文件。

## 数据与持久化

业务表在 `service/store.py` 的 `SCHEMA` 中定义；主要表：

- `batches` / `garments` / `order_lines` / `batch_mappings` / `repacks`
- `reports` / `report_batches` / `dispositions` / `disposition_batches`
- `complaints` / `complaint_inputs` / `complaint_targets` / `complaint_scopes` / `evidence`
- `shipments` / `shipment_events` / `returns`
- `choices` / `fulfillments` / `reservations` / `notifications`
- `payments` / `partners` / `issues` / `corrections`
- `audit` / `case_actions` / `idempotency`

重启后，所有列表与详情从 SQLite 重建；协作方观测进度由 `partners.state`/`last_observation_json` 保存。

## 自动检查

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_materials.py`：合成资料引用完整性与协作方回放器行为。
- `tests/test_service.py`：44 项端到端检查，覆盖
  批次隔离、款号不能推断批次、报告撤回不连带、解除一项依据、已分拣/在途/拦截、
  退货不得因退款再售、商家不能代替顾客确认、换货保留遇新停供、按件结案、
  支付未知不得重复、更正不扣回、重启后继续查询。

## 本地复算示例

```sh
# 1. 质量按报告查出真正受影响批次
curl -s localhost:8000/reports/report-B/impact -H 'X-Actor: quality-A'

# 2. 临时停供（只作用于 batch-B）
curl -s localhost:8000/dispositions -H 'X-Actor: quality-A' -H 'Content-Type: application/json' \
  -d '{"id":"disp-B","type":"stop_supply","report":"report-B","reason":"合成演练"}'

# 3. 仓库看到实物去向
curl -s localhost:8000/warehouse/inventory -H 'X-Actor: warehouse-A'

# 4. 售后按件解释
curl -s localhost:8000/complaints/complaint-A/case -H 'X-Actor: reviewer-A'

# 5. 赔付复核（商品未退回时须写明去向与顾客告知）
curl -s localhost:8000/payments -H 'X-Actor: reviewer-A' -H 'Content-Type: application/json' \
  -d '{"id":"pay-B","order":"order-A","item":"item-B","kind":"refund","amount":8900,
       "complaint":"complaint-A","basis":["report-B"],"reason":"合成结论",
       "not_returned":true,"item_disposition":"顾客留用","customer_notice":"已书面告知"}'
```

## 明确不做的事

- 不根据款号、颜色或同箱关系推断批次；
- 不把 202/504 当成成功或失败的业务结论；
- 不自动恢复库存、不自动扣回已退款；
- 不把商家代填当作顾客同意；
- 不给未确认批次的顾客强加一个批次选择；
- 不对真实产品安全作任何结论。
