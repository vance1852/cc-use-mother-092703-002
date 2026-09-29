# 产学研联合项目管理平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入，以及产学研联合研发项目的协议版本、多方承诺、里程碑验收、争议拨付冻结与财务复核。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险、财务、项目秘书和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/joint_project/`：产学研联合研发项目的协议版本、承诺、里程碑验收、争议冻结与拨付复核；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m joint_project.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入，以及联合项目的签署、部分验收、争议冻结、协议修订与重启追溯流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m joint_project.api --database joint.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 联合研发项目管理规则

`joint_project` 服务把高校、企业、产业园的协作固化为可追溯规则：

- **版本锚定**：承诺、可交付成果、验收规则、资金来源都登记在某个协议版本下；协议修订只追加新版本（正文以 SHA-256 固化），旧版本及其里程碑决定原样保留，里程碑永远指向签署时采用的版本。
- **里程碑验收**：成果验收人按锚定版本的全部规则逐条给出 `accept` / `partial` / `return` / `pause`，汇总为接受、部分接受、退回补证或暂停；部分接受必须填写已接受范围，补证后可重新提交。
- **争议隔离**：里程碑争议或暂停只把该里程碑（项目暂停时为全部）未支付的拨付置为 `frozen`，其他里程碑的拨付与复核不受影响；争议解决后自动解冻，需重新财务复核。
- **职责分离**：成果验收（`acceptor`）与拨付复核（`finance`）是不同角色，且拨付复核人不得是该笔拨付的发起人；未接受或部分接受以外的里程碑不得批准拨付，并校验资金来源承诺总额。
- **重启可追溯**：项目暂停/重启记入生命周期；重启后秘书仍可通过里程碑历史查询当时的验收规则、证据、决定，以及未结资金决定（拟拨付、冻结中、解冻待复核、已批准未支付）。
- 所有写操作进入 SHA-256 哈希链审计，`GET /audit/chain` 可校验历史是否被篡改。

