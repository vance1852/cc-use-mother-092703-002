# 产学研联合项目管理平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/joint_project/`：产学研联合研发项目的版本化协议、里程碑验收、争议冻结与资金复核；
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
PYTHONPATH=src python3 -m joint_project.acceptance
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和联合研发协议流程，不访问外部网络。

## 联合研发项目管理（joint_project）

面向高校、企业与产业园联合投入的研发项目，解决"财务按旧版计划拨款、合作方仍在争论阶段成果"这类协作脱节：

- **协议版本化**：参与方承诺、可交付成果、验收规则与资金来源全部绑定签署时的协议版本（SHA-256 摘要），历史版本只追加、不可改写；
- **里程碑状态机**：支持完全接受、部分接受（按达成比例折算拨付）、退回补证、暂停与恢复；修订后未开始的节点可重新定义，已评审或已拨付的节点必须原样延续；
- **争议局部冻结**：争议只冻结相关里程碑的未结拨付，其他节点照常推进；
- **权责分离**：技术验收与财务复核必须由不同授权人完成，评审人不得复核自己提交的证据，资金决定只追加、不可翻案；
- **可重启追溯**：每次验收冗余保存当时的验收规则全文快照，资金决定记录所依据的协议版本；进程重启后仍可查询。

角色：`secretary`（项目秘书）、`technical_reviewer`（技术验收）、`finance_officer`（财务复核）、`party_representative`（合作方）、`auditor`（审计）。接口通过 `X-Actor-Id` 头标识操作者；首个管理员可在用户表为空时免头引导创建。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m joint_project.api --database joint.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON（joint_project 另提供版本查询、里程碑证据/评审/争议/拨付与 `/projects/{id}/report` 完整报告）。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
