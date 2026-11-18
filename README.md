# 合作媒体供稿授权入仓

这是用于本地数据接收、事实核对和列式交换的仓库服务。数据使用 DuckDB 持久化，导出为 Parquet；不需要远程数据库或运行期网络。示例数据仅为本地合成的业务记录。

## 现有业务

供稿包先原样登记，再按**逐行判定**接收：单条记录字段不合格只隔离该记录（`pending_rows.state='invalid'`），同包中与它无关的合法节目、版本、授权仍进入业务表并可发布；字段合格但引用的父记录尚不可关联的记录进入等待（`pending_rows.state='waiting'`），其授权不会进入可分发集市。节目、语种版本与授权按供稿方隔离，分发查询只返回当前可关联且未被撤销链路阻断的有效节目，授权结束日期不包含当日。

合作方按原记录标识发来的修正放在信封的 `corrections` 段：补正成功后该记录进入集市，并自动释放原来因它而等待的下游版本/授权，无需重新报送整包。正式撤销放在 `revocations` 段：被撤销标识及其下游依赖停止可用，但业务行、原始投递与每次判定依据（`row_events`，只追加）都保留为历史事实，不被覆盖。重发同一包、重复送达同一修正、同一标识在不同媒体出现，均按业务键（含供稿方）幂等处理，不产生重复或串改。

`distribution.publish` 冻结某天可分发的节目授权，`distribution.read_release` 读取已发布内容；版本保留供追溯，当前在线可用查询仍以现有事实关联为准。运营可通过 `status`、`unavailable`、`report` 查看一项内容为何不可用、关联等待与自身错误分别是什么，并核对部分成功后原包的记录总数。最终可用结果与把合法修正一次性正确导入等价，校验不会被绕过。

## 使用

运行时支持 Python 3.11.x，独立容器使用摘要固定的 Python 3.11 镜像。`requirements.txt` 精确锁定依赖版本，`requirements.lock` 保存相同版本的 Linux CPython 3.11 wheel 哈希，容器构建时校验哈希。推荐使用下列容器方式；镜像构建时准备依赖，之后运行不联网。镜像只包含本仓库应用及依赖，不启动额外服务。

```bash
docker build -t quarantine-warehouse .
docker run --rm --network none quarantine-warehouse
```

已经准备依赖的环境中也可以直接执行：

```bash
python -m unittest discover -s tests -v
python -m compileall -q warehouse tests
python -m warehouse.cli --database /tmp/quarantine.duckdb load samples/delivery.json
python -m warehouse.cli --database /tmp/quarantine.duckdb overview
python -m warehouse.cli --database /tmp/quarantine.duckdb report sample-delivery
python -m warehouse.cli --database /tmp/quarantine.duckdb status grants media-a g1
python -m warehouse.cli --database /tmp/quarantine.duckdb unavailable
python -m warehouse.cli --database /tmp/quarantine.duckdb load samples/correction.json
python -m warehouse.cli --database /tmp/quarantine.duckdb export /tmp/quarantine-exports release1
```

上面的导出版本不可覆盖；再次核对应使用新的数据库路径和新的导出目录，或直接运行使用临时目录的测试。测试同时验证接收事务、业务主键冲突、批号重送、持久化及列式文件回读。完整测试和样本运行通常数秒结束，没有等待外部服务的步骤。

## 数据与接口

构建所需工具和传递依赖同样精确锁定，并包含在容器依赖中。执行 `python -m build --wheel --no-isolation` 可在不下载额外构建依赖的情况下生成 wheel；SQL 表定义随包分发。

接收包包含 `batch_id` 和 `tables`，并可选携带 `corrections`（按原记录标识补正）与 `revocations`（正式撤销标记，含 `partner`、对应记录标识与可选 `reason`）。合法字段由各表契约与领域校验共同约束；同批号同内容重送不重复写入，同业务键异内容拒绝该记录并隔离，不影响同包其他记录。已有原始行和接收回执保留，业务值更正须采用 `corrections` 独立记录，不能覆写原行；逐行判定写入只追加的 `row_events`，批次逐项计数写入 `batch_reports`。

`warehouse.storage.open_store` 打开仓库，`warehouse.ingest.ingest` 接收业务包并返回逐行判定 `report`，`warehouse.decisions` 承载逐行隔离、依赖等待、补正释放与撤销生命周期，`warehouse.status` 与 `warehouse.queries` 提供可用性原因与批次核对。`warehouse.export.export_tables` 固定一次读取视图并生成版本目录，`read_export` 校验成员文件摘要后读取。`samples/delivery.json` 展示投递字段，`samples/correction.json` 展示修正与撤销信封。

领域校验、事实计算与查询分别存在于同一应用的不同职责中。SQL 定义保存当前存储契约；批号回执、逐行事件与原始行登记属于接收事务，不是业务汇总来源。文件导出只交换现有业务表，接收回执与判定账本不会充当事实行。
