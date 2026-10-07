# 协调跨站设备借调与校准基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。在此之上，`loan` 模块实现了一套**独立的跨站设备借调平台**，把唯一高精度质谱仪这类整机与可拆组件的位置、能力范围、维护状态、校准版本和运输箱状态，连接到预约时段、操作员资格与冻结科研优先级的研究承诺。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、离线验收和跨站借调平台；
- tests/：基础规则、事务边界、接口路由、借调业务规则、并发与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点、业务资料以及一条完整的质谱仪跨站借调链（登记 → 候补 → 四前置条件确认 → 包装运输 → 验收 → 实验快照），核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态、运输中的借调、等待验收的流程和审计历史继续保留。

## 跨站借调平台规则

- **资源连接**：整机（unit）与可拆组件（component）带位置、能力列表、维护状态（operational/degraded/faulty/maintenance/missing）和递增修订号；校准证书登记覆盖的能力、组件修订与有效期；运输箱按整机/组件配备并跟踪 available/packed/in_transit/inspection/damaged。
- **资格与承诺**：操作员按能力授予有时限的资格；研究承诺登记后冻结优先级，候补永远按“冻结优先级升序、入队时间、借调编号”稳定排序，不随后续操作变化。
- **分阶段确认，四前置条件同时满足才占用**：设备能力（含运输后证书是否仍覆盖当前组件修订）、借入站人员资格、包装（匹配且可用的运输箱）、运输（承运安排且发运不晚于预约开始）四项各自留痕；任一不满足即继续候补。即使低优先级申请先就绪，也不能越过仍在候补的高优先级申请。
- **唯一胜者**：相同申请（站点、承诺、资源、能力、时段、计划归还时间的摘要）在未终结前不得生成第二次借调；并发接受由 `BEGIN IMMEDIATE` 事务保证同一互斥时段只产生一个 confirmed 占用。
- **物流与验收状态机**：waitlisted → confirmed → packed → in_transit → pending_acceptance → active → return_in_transit → pending_return → completed；目的地验收时重新核对证书覆盖，运输损伤导致组件修订升级、证书不再覆盖时，该预约退回候补而不是开始实验。
- **只调整尚未完成的预约**：故障、延期归还、运输损伤、能力降级、校准撤销只把尚未开始实验（confirmed/packed/in_transit/pending_acceptance）的预约释放回候补；已开始的实验在 run 开始时固化状态快照（证书、版本、资源修订、能力），数据继续引用当时有效状态。
- **追责**：逾期归还、组件失联（归还清单中 returned=false）、运输损伤自动开立去重的 accountability 工单；`GET /loan/detect-overdue` 可按当前时间巡检。
- **可解释**：`GET /loan/explain-slot` 给出任一时段的占用者（含四前置条件证据）与每个落选候补的具体原因；`GET /loan/loans/{id}/explanation` 给出阶段检查、调整记录、交接清单、实验快照、工单与审计时间线。

### 主要接口

| 方法与路径 | 作用 |
| --- | --- |
| POST /loan/resources, /loan/calibrations, /loan/crates, /loan/qualifications, /loan/commitments | 登记资源、证书、运输箱、资格、冻结承诺（均幂等） |
| POST /loan/requests | 提交借调申请，返回 waitlisted 或直接 confirmed |
| POST /loan/phases/confirm | 分阶段确认 capability/qualification/packaging/transport |
| POST /loan/pack · /ship · /arrive · /accept-delivery | 出库、发运、到达、目的地验收 |
| POST /loan/runs/start · /loan/runs/finish | 开始/结束实验（开始时固化状态快照） |
| POST /loan/return/begin · /return/arrive · /return/accept | 归还发运、到达、所有站验收（可登记逾期/失联/损伤） |
| POST /loan/events · /loan/calibrations/revoke · /loan/detect-overdue | 故障/延期/损伤/降级上报、证书撤销、逾期巡检 |
| GET /loan/resources · /loan/waitlist · /loan/issues | 资源、候补队列、追责工单查询 |
| GET /loan/explain-slot · /loan/loans/{id}/explanation | 时段占用/落选原因与单借调全量解释 |
