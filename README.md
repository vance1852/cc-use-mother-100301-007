# 协调跨站设备借调与校准基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。在此之上实现了一套**独立的跨站高精度设备借调平台**，把整机与可拆组件的位置、能力范围、维护状态、校准版本和运输箱状态，连接到预约时段、操作员资格与研究承诺。具体的物流、样品、能源、医疗和许可业务可在这些边界上扩展自己的状态、规则和接口。

## 借调平台的核心规则

- **设备与组件**：整机（`machine`）下挂可拆组件（`component`）；每个单元记录位置、能力集合、维护状态、运输箱状态（`sealed`/`unsealed`/`damaged`）和随配置变化的 `config_version`。
- **校准覆盖**：签发证书时冻结“整机当前全部在装组件 + 各自配置版本”的签名；运输后重新计算签名，签名不一致即判定证书不再覆盖当前组件，验收或占位都会被拒。
- **操作员资格**：按能力逐项核对时段内有效、未撤销的资格。
- **冻结科研优先级**：`priority-freezes` 固化研究承诺的全序排名，候补顺序按冻结 rank、再按提交时间稳定排序，重启后不抖动。
- **分阶段确认**：`equipment_capability`、`operator_qualification`、`packing_transport` 三阶段分别记录 `pending/passed/failed`；只有三者同时满足才原子占用时段（`slot_occupancy`），否则进入稳定候补。
- **唯一胜者**：所有写入走 `BEGIN IMMEDIATE` 短事务并叠加进程内写锁；并发接受同一时段只有一个申请占位，其余写明落选原因。
- **幂等**：相同 `request_id` 的申请不会生成第二次借调。
- **事故调整**：设备故障、运输损伤、组件失联会释放“尚未开始的已确认预约”；能力降级、校准撤销只释放重新评估后确实不再满足的预约。已在途、待验收、进行中与已完成的流程一律保留，已采集的实验数据继续引用当时有效的设备状态与校准证书快照。
- **重启保留**：业务状态全部落在 SQLite，服务重启后“运输中 / 等待验收”的流程原样恢复。
- **解释与追责**：`GET /slots` 解释任一时段的占用者与落选原因；`GET /accountability` 汇总逾期归还与组件失联，可定位到申请站、操作员和研究承诺。

## 主要接口（均为 JSON，写接口带 `X-Actor-Id`）

| 方法与路径 | 作用 |
| --- | --- |
| `POST /equipment` | 登记整机或可拆组件 |
| `POST /equipment/status`、`POST /equipment/crate` | 更新维护状态 / 运输箱状态 |
| `GET /equipment/{id}` | 查询设备能力、状态与配置版本 |
| `POST /calibrations`、`POST /calibrations/{id}/revoke` | 签发（冻结组件配置签名）/ 撤销校准 |
| `POST /qualifications`、`POST /qualifications/revoke` | 登记 / 撤销操作员资格 |
| `POST /priority-freezes` | 冻结科研优先级全序 |
| `POST /loan-requests` | 提交借调申请（立即评估设备与资格阶段） |
| `POST /loan-requests/{id}/packing` | 现场确认包装与运输前置条件 |
| `POST /loan-requests/{id}/transit`、`/accept`、`/return`、`/cancel` | 发货、到货验收、归还、取消 |
| `POST /experiment-data`、`GET /experiment-data?request_id=` | 记录 / 查询带状态快照的实验数据 |
| `POST /incidents`、`GET /incidents` | 上报 / 查询故障、降级、损伤、失联等事故 |
| `GET /slots` | 解释时段占用与候补落选原因 |
| `GET /accountability` | 逾期归还与组件失联追责汇总 |

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、跨站借调平台和离线验收；
- tests/：基础规则、事务边界、接口路由、借调业务规则和端到端验收测试。

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

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点和业务资料，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
