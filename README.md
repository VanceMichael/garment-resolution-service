# 服装批次争议中的停供与售后责任

衣物按批次和单件追踪，投诉线索、分装及出库记录为合成。检测报告只提供人工签认事实，不构成真实产品安全结论。

本项目提供一个**可重启、可复算的 HTTP 后端**，把以下环节接起来：

1. **投诉受理**：可来自订单，也可只有吊牌、商家及购买时间；线索不足时保留待查，不猜批次。
2. **批次质量处置**：质量负责人按一份证据找到真正受影响的批次与履行，临时停供、扩大调查或确认不合格；解除只针对指定处置。
3. **仓库履行**：区分在库、已分拣未交运、在途/拦截中、退回隔离；停供阻止实物继续分配或交运，已交承运人的转拦截跟进。
4. **售后与赔付**：按件解释已完成与待办，通知、顾客确认、换货保留、退款复核；支付受理未知时保持待查，不重复支付。
5. **更正影响**：报告更正后列出应通知、应补付或转人工的事项，不自动扣回已退款。

使用 Python 3.11，仅标准库，SQLite 持久化。

## 快速开始

```sh
# 启动业务服务（首次会导入合成资料，可重复执行）
python3 -m service.server --port 8000 --state aftercare.sqlite3

# 另开终端：启动协作方回放器
python3 partner/server.py --port 8765 --state partner-state.sqlite3

# 带协作方地址启动，支付/拦截观测可自动落到业务记录
python3 -m service.server --port 8000 --state aftercare.sqlite3 --partner-url http://127.0.0.1:8765
```

```sh
# 质量按一份检测报告查影响范围
curl -s localhost:8000/reports/report-B/impact -H 'X-Actor: quality-A'

# 临时停供（只作用于报告列明的 batch-B）
curl -s localhost:8000/dispositions -H 'X-Actor: quality-A' -H 'Content-Type: application/json' \
  -d '{"id":"disp-B","type":"stop_supply","report":"report-B","reason":"合成演练"}'

# 仓库看实物在库、分拣、在途与隔离
curl -s localhost:8000/warehouse/inventory -H 'X-Actor: warehouse-A'

# 售后按一件商品解释已完成与待办
curl -s localhost:8000/complaints/complaint-A/case -H 'X-Actor: reviewer-A'
```

业务接口需要 `X-Actor`（取值见 `data/catalog.json` 的 `principals`；仅用于演练归属，不是认证凭据）。
`GET /health` 与 `GET /` 不需要身份。完整接口、角色权限与业务规则见 **[服务说明](docs/service.md)**；
原始资料字段与协作方协议见 [资料约定](docs/contracts.md)。

## 自动检查

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_materials.py`：合成资料引用完整性、协作方回放器受理与重启。
- `tests/test_service.py`：44 项端到端检查，覆盖批次隔离、范围确认、停供/解除、已分拣与在途处置、
  拦截未确认、退货不得因退款再售、顾客确认、换货重办、按件结案、支付未知与重复、更正不扣回、重启续查。

## 目录

```
service/            业务服务（HTTP、领域规则、SQLite）
  server.py         启动入口
  http_api.py       路由与视图
  materials.py      合成资料导入
  complaints.py     投诉与按件解释
  scope.py          证据 → 受影响批次/实物
  quality.py        检测报告
  dispositions.py   停供/调查/不合格/解除
  warehouse.py      出库、交运、拦截、运单查询
  returns.py        退货接收与去向
  aftercare.py      通知、选择、换货、结案、待办
  payments.py       赔付复核与支付观测
  corrections.py    更正影响评估
docs/               资料约定与服务说明
data/catalog.json   合成资料与演练身份
partner/            协作方协议回放器
tests/              自动检查
```

业务状态与协议回放状态使用不同 SQLite 文件，便于分别留存与重新演练。
