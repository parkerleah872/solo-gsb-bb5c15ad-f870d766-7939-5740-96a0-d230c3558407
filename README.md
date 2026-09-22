# Release Wave Planning API（发布波次规划服务）

基于 **Python 3.11 + FastAPI + OR-Tools CP-SAT** 的发布波次规划服务。客户端提交
服务依赖图、每波并发上限以及不能同波发布的故障域，服务给出**顺序确定、波次最少**
的发布方案；依赖成环或在给定发布窗口内无法排入时，返回**可定位到具体服务与约束**
的结构化原因。方案持久化到 SQLite，支持查询与删除；`POST /plans/{id}/simulate`
可在不落库的前提下模拟约束变更对方案的影响；`POST /plans/{id}/gate` 基于已存储的
（不可变）波次方案做发布执行门禁，上报已完成与执行失败的服务后，逐波给出可执行
服务、阻塞服务及可定位原因；`POST /plans/{id}/rollback-preview` 针对当前已上线
（仍在运行）的服务给出**波次最少、顺序确定**的回滚波次预览（只读，不改方案）。
容器启动时自动安装依赖并建表。

## 一条命令启动

```bash
docker compose up --build -d
```

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | API 根地址 |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger UI（可直接发请求） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |

停止并保留数据：`docker compose down`；连同数据卷一起清除：`docker compose down -v`。

> 端口可通过环境变量调整：`PLANNER_PORT=9000 docker compose up --build -d`。

容器启动后门禁接口即随服务一起可用（无需额外组件或迁移，数据落在 `planner-data`
命名卷）。快速体验：先 `POST /plans` 建一个方案记下返回的 `id`，再把当前发布
进度发给门禁：

```bash
curl -X POST http://localhost:8000/plans/<id>/gate \
  -H 'Content-Type: application/json' \
  -d '{"completed_services": ["svc-a"], "failed_services": []}'
```

## 请求示例

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "payment-platform",
    "services": ["web", "api", "auth", "db", "cache"],
    "dependencies": [
      {"service": "web", "depends_on": "api"},
      {"service": "api",  "depends_on": "auth"},
      {"service": "api",  "depends_on": "db"},
      {"service": "auth", "depends_on": "db"}
    ],
    "max_concurrency": 2,
    "fault_domains": [
      {"domain": "az-a", "services": ["db", "cache"]}
    ],
    "max_waves": null
  }'
```

响应：

```json
{
  "id": 1,
  "name": "payment-platform",
  "waves": [
    {"index": 1, "services": ["db"]},
    {"index": 2, "services": ["auth"]},
    {"index": 3, "services": ["api"]},
    {"index": 4, "services": ["cache", "web"]}
  ],
  "wave_count": 4,
  "max_concurrency": 2,
  "max_waves": null,
  "optimal": true,
  "created_at": "2026-09-21T12:00:00+00:00"
}
```

## 约束语义

- **依赖（dependencies）**：`service -> depends_on`，被依赖服务必须出现在更早的波次；
  依赖图必须是 DAG。
- **每波并发上限（max_concurrency）**：每波服务数不超过该值。
- **故障域（fault_domains）**：同一 `domain` 内的任意两个服务不能出现在同一波
  （反亲和）。典型用法：一个物理故障域（机房/可用区/机架）对应一条记录，列出其中的
  服务；若两个服务跨多个域同时冲突，只需让它们在任一域内同组即可。
- **发布窗口（max_waves，可选）**：传入后若最优方案需要更多波，返回 422 及原因；
  不传则以「波次最少」为目标（DAG 下始终可排）。

**确定性**：CP-SAT 固定随机种子、单搜索线程，并在「最少波次」基础上做第二阶段
优化，使服务尽量靠前且波内按 id 排序。相同输入永远得到相同输出。
`optimal=true` 表示求解器证明了波次最少；`false` 仅在触发求解时间上限时出现
（此时方案仍可行但不保证最优）。

## 约束变更模拟（POST /plans/{id}/simulate）

在已存储方案的基础上评估「如果约束这样改，方案会变成什么样」。**只读不落库**，
不影响已有记录。请求体所有字段均可选，未提供的约束保持原方案的值：

```bash
curl -X POST http://localhost:8000/plans/1/simulate \
  -H 'Content-Type: application/json' \
  -d '{
    "max_concurrency": 3,
    "add_dependencies": [{"service": "web", "depends_on": "cache"}],
    "remove_dependencies": [{"service": "api", "depends_on": "db"}],
    "add_fault_domains": [{"domain": "az-b", "services": ["api", "auth"]}],
    "remove_fault_domains": ["az-a"]
  }'
```

变更语义：

- **删除**必须命中已存储的约束，否则 422（`dependency_not_found` /
  `fault_domain_not_found`），便于发现客户端与服务端的状态漂移。
- **修改故障域** = 同一请求内先 `remove_fault_domains` 再同名 `add_fault_domains`；
  未删除就重复添加同名域返回 422 `duplicate_fault_domain`。
- 添加已存在的依赖边视为无操作（与创建时重复边静默合并一致）。
- 原方案的 `max_waves` 发布窗口**不参与**模拟——模拟直接报告波次增减。

求解目标按字典序：**先保证新方案波次最少，再在其中最小化相对原方案的换波
服务数**，最后做与创建相同的规范化，保证确定性（`optimal=true` 表示波次数与
换波数都被证明最小；触发求解时间上限时为 `false`，结果仍可行但不保证最优）。

响应：

```json
{
  "plan_id": 1,
  "name": "payment-platform",
  "waves": [
    {"index": 1, "services": ["db"]},
    {"index": 2, "services": ["auth", "cache"]}
  ],
  "wave_count": 5,
  "original_wave_count": 4,
  "wave_delta": 1,
  "max_concurrency": 3,
  "optimal": true,
  "assignments": [
    {"service": "api", "from_wave": 3, "to_wave": 4}
  ],
  "moved_services": ["api"],
  "affected_services": ["api", "auth", "cache", "web"]
}
```

| 字段 | 含义 |
| --- | --- |
| `waves` / `wave_count` | 模拟得到的新波次（结构同创建响应） |
| `original_wave_count` / `wave_delta` | 原波次数与增减量（新 − 原，正为增波） |
| `assignments` | 每个服务的原/新波次（1 起始，按服务 id 排序） |
| `moved_services` | 换了波次的服务 |
| `affected_services` | 被**新增**约束直接引用的服务（新增依赖的两端、新增故障域的成员） |

模拟不可行时返回与创建相同的结构化 422：新增依赖成环（`dependency_cycle`）、
引用未知服务（`unknown_dependency_*` / `unknown_fault_domain_member`）、
删除不存在的约束等，均可定位到具体服务与约束。

## 执行门禁（POST /plans/{id}/gate）

发布过程中持续上报本方案内**已完成**与**执行失败**的服务，门禁严格按照**已存储的
波次方案与依赖图**（不会被门禁修改、不会重排）逐波判定剩余服务：

- 所有直接依赖均已完成 → **可执行**；
- 否则**阻塞**，并为每个有问题的依赖给出一条可定位原因；
- 失败沿「依赖于」边**向下游递归传播**：直接依赖失败为 `dependency_failed`，
  依赖被更上游的失败阻塞为 `upstream_failure`（context 中给出失败根因），
  依赖既未失败也未完成为 `dependency_incomplete`。

```bash
curl -X POST http://localhost:8000/plans/1/gate \
  -H 'Content-Type: application/json' \
  -d '{
    "completed_services": ["db"],
    "failed_services": ["cache"]
  }'
```

（承接上面 `payment-platform` 示例：存储波次为 `[db] → [auth] → [api] → [cache, web]`，
门禁不会改变它。）

响应：

```json
{
  "plan_id": 1,
  "name": "payment-platform",
  "wave_count": 4,
  "current_wave": 2,
  "executable_services": ["auth"],
  "blocked_services": [
    {
      "service": "api",
      "wave": 3,
      "reasons": [
        {
          "code": "dependency_incomplete",
          "message": "Service 'api' is blocked because its dependency 'auth' is not completed yet.",
          "constraint": "dependencies",
          "service": "api",
          "related_services": ["auth"],
          "context": {"dependency": "auth", "dependency_status": "pending", "wave": 3}
        }
      ]
    },
    {
      "service": "web",
      "wave": 4,
      "reasons": [
        {
          "code": "dependency_incomplete",
          "message": "Service 'web' is blocked because its dependency 'api' is not completed yet.",
          "constraint": "dependencies",
          "service": "web",
          "related_services": ["api"],
          "context": {"dependency": "api", "dependency_status": "pending", "wave": 4}
        }
      ]
    }
  ],
  "completed_services": ["db"],
  "failed_services": ["cache"],
  "waves": [
    {"index": 1, "executable": [], "blocked": [], "completed": ["db"], "failed": []},
    {"index": 2, "executable": ["auth"], "blocked": [], "completed": [], "failed": []},
    {"index": 3, "executable": [], "blocked": ["api"], "completed": [], "failed": []},
    {"index": 4, "executable": [], "blocked": ["web"], "completed": [], "failed": ["cache"]}
  ]
}
```

失败递归传播的例子：`db` 失败后，直接依赖它的 `auth` 以 `dependency_failed`
阻塞，而 `api`、`web` 即使只间接依赖 `db`，也会以 `upstream_failure` 阻塞，
其 `context.failed_roots` 给出失败根因 `["db"]`，方便沿链定位。

| 字段 | 含义 |
| --- | --- |
| `current_wave` | 仍存在可执行服务的**最早波次**（1 起始）；全部完成或全部阻塞时为 `null` |
| `executable_services` | 当前可执行的剩余服务：先按存储波次归组、波内按 id 排序（同波服务互不影响） |
| `blocked_services[].reasons` | 每个阻塞服务对应一条或多条原因，均带 `service` / `related_services` / `context`，可直接定位到出问题的依赖 |
| `waves` | 存储方案每个波次的可执行/阻塞/已完成/已失败拆分（波次方案本身不变） |
| `completed_services` / `failed_services` | 回显已排序、去重后的上报状态 |

进度上报本身做一致性校验，冲突时统一返回 **422**（信封同下）：

| issue code | 触发条件 |
| --- | --- |
| `unknown_service` | 上报了不属于本方案的服务（区分 `completed_services` / `failed_services`） |
| `duplicate_service_status` | 同一列表中服务重复出现 |
| `service_status_conflict` | 同一服务同时上报为完成和失败 |
| `completed_with_failed_dependency` | 上报完成，但其依赖链上（含跨未上报服务的传递路径）存在失败服务 |
| `completed_before_dependencies` | 上报完成，但其直接依赖尚未全部完成（与存储的发布顺序矛盾） |

门禁为只读操作，不写库：重复请求得到相同结果（确定性），方案不存在时返回 404。

## 回滚波次预览（POST /plans/{id}/rollback-preview）

当部分（或全部）服务已经上线、需要规划回滚时，把**当前已上线（仍在运行）的服务**
发给本接口。服务只为这些上线服务生成回滚波次：

- **回滚顺序**：某服务必须**晚于所有已上线且依赖它的下游服务**回滚，即回滚顺序
  是发布顺序的逆序（在「上线服务」诱导子图上把依赖边反向后按同一波次模型求解）；
  尚未上线的服务不参与，也不会阻塞其上游回滚。
- **约束继承**：继续使用原方案的**每波并发上限**与**故障域反亲和**
  （故障域只保留其中已上线的成员）。
- **目标**：波次最少；同输入顺序稳定——与创建相同的 CP-SAT 固定种子、单线程与
  字典序规范化保证结果确定，且**不依赖请求列表的排列顺序**。
- **只读**：不改动、不删除已存储方案，也不落库任何结果。

```bash
curl -X POST http://localhost:8000/plans/1/rollback-preview \
  -H 'Content-Type: application/json' \
  -d '{"deployed_services": ["web", "api", "auth", "db", "cache"]}'
```

承接上面 `payment-platform` 示例（存储发布波次为
`[db] → [auth] → [api] → [cache, web]`），全部上线时回滚波次恰好是发布的逆序：

```json
{
  "plan_id": 1,
  "name": "payment-platform",
  "deployed_services": ["api", "auth", "cache", "db", "web"],
  "waves": [
    {"index": 1, "services": ["web"]},
    {"index": 2, "services": ["api"]},
    {"index": 3, "services": ["auth", "cache"]},
    {"index": 4, "services": ["db"]}
  ],
  "wave_count": 4,
  "max_concurrency": 2,
  "optimal": true
}
```

如果当时只上线到 `db`、`auth`、`api`（`cache`、`web` 未上线），只需
`{"deployed_services": ["db", "auth", "api"]}`，得到
`[api] → [auth] → [db]` 三个回滚波，未上线的服务不会出现。`deployed_services`
传空数组是合法请求，返回 0 个波次。

**依赖闭包校验**：上线集合必须满足依赖闭包——一个已上线服务的所有前置（直接
依赖）也必须已上线；否则返回 **422**，并为每个违反闭包的服务给出一条 issue，
精确定位到缺失的前置服务。例如只上报 `api` 已上线，但其前置 `auth`、`db` 缺失：

```json
{
  "error": "planning_failed",
  "message": "The deployed services violate dependency closure: ...",
  "issues": [
    {
      "code": "rollback_prerequisite_not_deployed",
      "message": "Deployed service 'api' requires prerequisite service(s) 'auth' and 'db' to also be deployed, but they are not reported as live; the deployed set is not dependency-closed.",
      "constraint": "deployed_services",
      "service": "api",
      "related_services": ["auth", "db"],
      "context": {"missing_prerequisites": ["auth", "db"]}
    }
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| `deployed_services` | 回显已排序、去重后的上线服务（回滚只覆盖这些服务） |
| `waves` / `wave_count` | 各回滚波（1 起始，波内服务按 id 排序）及总波数 |
| `max_concurrency` | 继承自原方案的每波并发上限 |
| `optimal` | `true` 表示求解器证明了波次最少 |

此外，引用不属于本方案的服务返回 422 `unknown_service`，同一服务在请求中重复
出现返回 422 `duplicate_service`，方案不存在返回 404。

## 错误结构（422）

所有失败使用统一信封 `error + message + issues[]`，每个 issue 可定位：

```json
{
  "error": "planning_failed",
  "message": "The dependency graph contains a cycle.",
  "issues": [
    {
      "code": "dependency_cycle",
      "message": "... First cycle: x -> y -> x",
      "constraint": "dependencies",
      "service": "x",
      "related_services": ["x", "y"],
      "context": {"cycles": [["x", "y"]], "cycle_count": 1}
    }
  ]
}
```

| issue code | 定位的问题 |
| --- | --- |
| `dependency_cycle` | 依赖成环（含自依赖），给出具体环路径 |
| `unknown_dependency_service` / `unknown_dependency_target` | 依赖引用了不存在的服务 |
| `unknown_fault_domain_member` / `duplicate_*` | 故障域引用未知服务或重复声明 |
| `dependency_chain_too_long` | 最长依赖链所需波次超过 `max_waves`（给出关键路径） |
| `concurrency_cap_too_low` | `ceil(服务数 / 并发上限)` 超过 `max_waves` |
| `fault_domain_anti_affinity` | 故障域反亲和所需的最少波次超过 `max_waves` |
| `release_window_infeasible` | 组合约束下窗口不可行，附无窗口时所需的确切波次 |
| `dependency_not_found` / `fault_domain_not_found` | 模拟请求要求删除不存在的依赖/故障域 |
| `dependency_failed` / `upstream_failure` / `dependency_incomplete` | 门禁阻塞原因：直接依赖失败 / 依赖被上游失败递归阻塞（附失败根因）/ 依赖尚未完成 |
| `unknown_service` / `duplicate_service_status` / `service_status_conflict` | 门禁或回滚预览上报了未知服务；门禁中同一状态重复上报或完成/失败状态冲突 |
| `completed_with_failed_dependency` / `completed_before_dependencies` | 门禁上报的「已完成」与其失败上游或未完成依赖矛盾 |
| `rollback_prerequisite_not_deployed` | 回滚预览的上线集合违反依赖闭包：已上线服务的某个前置未上线（附缺失前置） |
| `duplicate_service` | 回滚预览请求中同一服务重复出现 |
| `invalid_request_field` | JSON/Schema 校验失败，附带字段位置 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plans` | 提交并生成方案，成功 201，失败 422（结构化原因） |
| POST | `/plans/{id}/simulate` | 约束变更影响模拟（不落库）：新波次、波次增减、逐服务新旧波次、受影响服务 |
| POST | `/plans/{id}/gate` | 执行门禁（只读）：上报已完成/失败服务，返回最早可执行波次、可执行与阻塞服务及每项可定位原因；输入冲突统一 422 |
| POST | `/plans/{id}/rollback-preview` | 回滚波次预览（只读）：上报当前已上线服务，按逆序约束、原并发上限与故障域反亲和生成波次最少且顺序确定的回滚波；上线集合违反依赖闭包时 422 并定位缺失前置 |
| GET | `/plans?limit=50&offset=0` | 方案列表（按 id 倒序分页） |
| GET | `/plans/{id}` | 查询单个方案（含完整波次与原始约束） |
| DELETE | `/plans/{id}` | 删除方案，成功 200，不存在 404 |
| GET | `/health` | 健康检查 |

## 必要配置（环境变量，均有默认值）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PLANNER_DB_PATH` | `/data/plans.db` | SQLite 文件路径，目录自动创建 |
| `PLANNER_SOLVER_TIME_LIMIT` | `10` | 每个求解阶段的时间上限（秒） |
| `PLANNER_SOLVER_WORKERS` | `1` | CP-SAT 搜索线程数（1 保证确定性） |
| `PLANNER_PORT`（仅 compose） | `8000` | 宿主机映射端口 |

数据存储在 compose 命名卷 `planner-data`（容器内 `/data`），重启不丢记录。
数据库表在**每次启动时**通过 `CREATE TABLE IF NOT EXISTS` 自动创建，无需手工迁移。

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PLANNER_DB_PATH=./data/plans.db uvicorn app.main:app --reload --port 8000
```

运行测试（额外需要 pytest、httpx）：

```bash
pip install -r requirements.txt pytest httpx
python3 -m pytest -q
```

## 项目结构

```
app/
  config.py     # 环境变量配置
  schemas.py    # 请求/响应模型与错误结构
  errors.py     # 结构化规划异常
  graph.py      # 依赖图：环检测（Tarjan SCC）、关键路径
  planner.py    # CP-SAT 建模、两阶段求解、变更模拟、不可行诊断
  gate.py       # 执行门禁：逐波可执行判定、失败递归传播、进度一致性校验
  rollback.py   # 回滚预览：上线集合依赖闭包校验、反向边回滚波次生成
  db.py         # SQLite 初始化与 CRUD
  main.py       # FastAPI 路由、启动建表、异常处理
tests/          # 端到端测试（规划/错误定位/CRUD/模拟/门禁/回滚预览/确定性）
Dockerfile
docker-compose.yml
requirements.txt
```
