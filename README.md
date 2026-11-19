# 理赔明细分批提取服务（FastAPI + PostgreSQL）

保险精算离线测算场景：按地区开启**快照会话**，按理赔 id 升序分页提取明细。
会话期间保单/理赔两表固定为开启时刻的同一快照，源端的增改删照常提交、
互不影响；会话有数量上限与 TTL，可主动关闭，关闭/到期后未开始响应的取页
一律失败且不返回明细。

在此基础上提供**再保报送清单**：从指定地区快照会话读取指定保单的全部理赔
（至多 100 笔），把每笔金额按 2~5 家分保方的约定比例（整数基点）取到整分，
使每笔各方合计等于理赔金额、各方跨笔合计等于已核定总额，并可原样保留
已入账明细。

## 一、快速启动（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>
- Swagger 交互文档：<http://localhost:8000/docs>
- OpenAPI 原文：<http://localhost:8000/openapi.json>
- PostgreSQL：localhost:55432（库 `claims`，用户/密码 `actuary/actuary`）

首次启动时 `db/init/` 下脚本按文件名顺序自动执行：

| 文件 | 作用 |
| --- | --- |
| `01_schema.sql` | 建表：`policies(id, region)`、`claims(id, policy_id, amount)`，金额为非负整数（单位：分） |
| `02_seed.sql` | 演示数据：3 个地区、260 条理赔（华北 120 / 华东 100 / 华南 40） |
| `03_sql_examples.sql` | SQL 补录示例：新增保单/理赔、改金额、删除、按地区汇总 |

## 二、核心语义与实现方式

| 需求 | 实现 |
| --- | --- |
| 两表同一快照、后页/重复页一致 | 每会话独占一条连接，开启 `START TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY`，连接保持 idle-in-transaction，快照事务全程不结束 |
| 源端增改删照常提交 | 写操作走连接池内其它自动提交连接，MVCC 下与快照只读事务互不阻塞 |
| 不预存结果全集 | 每次取页现场执行带 `LIMIT/OFFSET` 的 SQL，服务端不缓存明细 |
| claim_id 升序、每页至多 100 行 | `ORDER BY c.id ASC LIMIT n+1 OFFSET ...`，多取 1 行判断 `has_more`；`page_size` 服务端强制截断为 100 |
| 最多 2 个会话，满额拒绝 | 内存注册表 + 名额计数，超额返回 `429 capacity_reached`（可用 `MAX_SESSIONS` 调整） |
| 开启 60 秒到期 | 后台 sweeper 线程定时清理（`SESSION_TTL_SECONDS` 可调），到期取页返回 410/409 且无明细 |
| 主动关闭及时受理 | 关闭请求先把状态翻为 `closing`（新取页立即失败），等待在途取页结束后回滚事务、归还连接、释放名额，再回报 `closed` |
| 不影响另一会话/后续新会话 | 名额只统计 open/closing 会话；终态不占名额，连接归还池后立即可复用 |
| 连接丢失/服务重启后旧会话失效 | 会话状态只存进程内存；连接断裂的会话作废返回 503 且绝不换快照续取；重启后旧 id 查询 404 |

### 会话状态机

```
open ──主动关闭──▶ closing ──回滚事务/还连接/释放名额──▶ closed
  │                                                  ▲
  └──TTL 到期（sweeper）─────────────────────────── expired
```

终态记录保留 10 分钟供状态查询，之后从注册表回收；终态不占用会话名额。

## 三、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 按地区开启快照会话，body：`{"region": "华北"}` |
| GET | `/sessions` | 列出会话 |
| GET | `/sessions/{id}` | 查询会话状态 |
| POST | `/sessions/{id}/close` | 主动关闭 |
| GET | `/sessions/{id}/claims?page=1&page_size=100` | 快照内分页取理赔明细 |
| POST | `/sessions/{id}/ceding-report` | 再保报送清单：快照内取指定保单全部理赔并按约定比例取整整分 |
| POST | `/admin/policies` | 源端补录保单 |
| POST | `/admin/claims` | 源端补录理赔 |
| PATCH | `/admin/claims/{id}?amount=999` | 源端改金额 |
| DELETE | `/admin/claims/{id}` | 源端删除理赔 |
| GET | `/health`、`/meta` | 健康检查、服务参数 |

取页失败响应只含 `detail`/`code`，不含任何明细行：

```json
HTTP 409
{"detail": "会话 ... 关闭已受理或会话已结束，取页失败且不返回明细",
 "code": "session_not_open"}
```

## 四、再保报送清单（份额取整到整分）

`POST /sessions/{session_id}/ceding-report` 在**会话快照内**读取指定保单的
全部理赔（claim_id 升序，至多 100 笔），按分保方约定比例把每笔份额取到整分，
对齐已核定的分保方总额。该接口与取页共用同一套开放/到期/关闭闸门：
会话关闭已受理、到期或失效时一律失败且不返回清单。

### 请求体

```json
{
  "policy_id": 5,
  "parties": [
    {"party": "P0", "ratio_bps": 5000, "approved_total": 953000},
    {"party": "P1", "ratio_bps": 3000, "approved_total": 571800},
    {"party": "P2", "ratio_bps": 2000, "approved_total": 381200}
  ],
  "booked": [
    {"claim_id": 221, "party": "P1", "amount": 840}
  ]
}
```

- `parties`：2~5 家分保方；`ratio_bps` 为整数基点（1bp=0.01%），合计必须
  恰为 `10000`；`approved_total` 为该分保方已核定的跨笔合计总额（分）。
- `booked`：可为空的已入账明细（理赔 ID、分保方、整分金额）。入账金额必须
  本身就是该笔按约定比例的 floor/ceil 取值，服务端**原样保留**且标记
  `"booked": true`；其余格点在剩余可行空间内取整。

### 取整与约束语义

| 规则 | 实现 |
| --- | --- |
| 每格只能向上或向下取到整分 | 全程整数运算，格点取值域为 `[floor(amount·ratio/10000), ceil(...)]`，整除时二者相等（标记 `exact`） |
| 每笔各方合计 = 理赔金额 | 归约为每行固定向上取总格点数 `r_i = amount - Σfloor` 的 0/1 矩阵 |
| 各方跨笔合计 = 核定总额 | 归约为每列固定向上取总格点数 `c_j = approved_j - Σfloor`（含入账预占） |
| 已入账原样保留 | 入账 ceil/floor 直接锁定对应 0/1，不参与自由分配；非法入账值拒绝 |
| 无法同时满足 | 返回 422 并说明原因，**不返回部分清单** |
| 同快照同输入重取一致 | 最小费用最大流 + 全局唯一费用序（余数降序，平局按理赔 id、分保方次序），规范解唯一确定 |

算法本质是二部图 b-匹配（理赔→分保方），用最小费用最大流求可行流，费用序
对应"最大余数法"：余数 `amount·ratio - floor·10000` 越大的格点越优先向上
取整。纯函数实现位于 `app/apportionment.py`，不依赖数据库，可独立单测。

### 响应（节选）

```json
{
  "session_id": "...", "region": "华南", "policy_id": 5,
  "claim_count": 40, "snapshot_time": "2026-...",
  "parties": [
    {"party": "P0", "ratio_bps": 5000, "approved_total": 953000, "allocated_total": 953000}
  ],
  "claims": [
    {"claim_id": 221, "amount": 2800, "shares": [
      {"party": "P0", "ratio_bps": 5000, "amount": 1400, "rounded": "exact", "booked": false},
      {"party": "P1", "ratio_bps": 3000, "amount": 840,  "rounded": "exact", "booked": true}
    ]}
  ]
}
```

### 失败码（响应体只含 `detail`/`code`，无 `claims` 字段）

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 404 | `session_not_found` / `policy_not_found` | 会话不存在/失效，或保单不在会话地区快照中 |
| 409 | `session_not_open` | 关闭已受理/会话不可用 |
| 410 | `session_expired` | 会话已到期 |
| 422 | `invalid_ratios` / `invalid_parties` / `invalid_approved_total` | 分保方数量、基点合计、重名、负核定总额等输入不合法 |
| 422 | `totals_mismatch` | 各方核定总额之和 ≠ 保单理赔总额，行和与列和不可能同时成立 |
| 422 | `booked_not_roundable` / `booked_claim_unknown` / `booked_party_unknown` / `duplicate_booked` | 已入账明细不合法 |
| 422 | `allocation_infeasible` | 输入合法但不存在满足全部约束的取整方案（含入账锁定导致无解） |
| 422 | `too_many_claims` | 保单在该快照下理赔超过 100 笔 |
| 503 | `session_broken` | 快照连接失效，会话作废 |

## 五、慢取页演示

镜像默认 `ENABLE_DEBUG_HOOKS=true`，带 `X-Debug-Sleep-Seconds` 请求头
可让本次取页在执行 SQL 前先睡眠，用于演示"关闭及时受理"：

```bash
python3 demo/demo.py                 # 完整端到端演示（标准库即可运行）
BASE_URL=http://localhost:8000 python3 demo/demo.py
DEMO_TTL_WAIT=1 python3 demo/demo.py # 追加等待到期场景（约多花 1 分钟）
```

演示覆盖：分页与重复取页一致性、源端写入的快照隔离、名额满 429、
慢取页期间关闭（在途请求结束后失败、无明细）、到期失效，以及再保报送清单
（取整整分、行和/列和精确对齐、入账原样保留、无解 422 不返回部分清单）。

## 六、本地自测（无 Docker 时）

仓库测试以真实 uvicorn + PostgreSQL 运行：

```bash
pip install -r requirements.txt
# 准备一个 PostgreSQL 15，并执行 db/init/01_schema.sql、02_seed.sql
DATABASE_URL=postgresql://actuary:actuary@127.0.0.1:5432/claims \
SESSION_TTL_SECONDS=2 ENABLE_DEBUG_HOOKS=true \
python -m unittest tests.test_apportionment tests.test_api -v
```

- `tests.test_apportionment`（28 个，纯算法、无需数据库）：floor/ceil、
  行和/列和、入账锁定与各类非法输入、确定性重取，并对小实例**穷举全部可行
  0/1 矩阵**验证全局成本最优；对 100 笔 × 5 家做压力与不变量核对。
- `tests.test_api`（28 个端到端）：分页/上限/参数校验、重复取页同快照、
  两表 JOIN 快照、源端增改删不可见、名额与释放、关闭幂等、慢取页+关闭竞争、
  到期清理、连接丢失作废不续取、服务重启旧会话 404；以及报送清单的
  happy path、确定性、入账保留、无解 422、保单跨地区 404、关闭/到期拒绝、
  快照隔离、空保单与 100 笔上限。

## 七、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://actuary:actuary@db:5432/claims` | 数据库连接串 |
| `MAX_SESSIONS` | `2` | 并发会话上限 |
| `SESSION_TTL_SECONDS` | `60` | 会话存活秒数 |
| `PAGE_SIZE_CAP` | `100` | 单页行数硬上限 |
| `ENABLE_DEBUG_HOOKS` | `false`（compose 中为 true） | 是否允许慢取页调试头 |
| `SWEEPER_INTERVAL_SECONDS` | `0.25` | 到期检查间隔 |

> 说明：服务固定单 uvicorn worker——会话注册表与名额是进程内状态，
> 多 worker 会各自持有独立状态，破坏会话语义。
