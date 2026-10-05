# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/offline-records`：回传机房无信号期间暂存在本机的检验和困人报警（`{"records":[...]}`）。
- `POST /api/offline-records/retry`：重连后重试收件箱里`pending`/`failed`的记录，可带`{"record_ids":[...]}`只重试指定编号。
- `GET /api/offline-records`：查看离线收件箱，可用`?status=`过滤（`pending`/`failed`/`merged`/`conflict`/`resolved`）。
- `POST /api/entities/<冲突id>/actions`：管理员对`sync_conflict`提交`{"action":"resolve","data":{"choice":"field"|"server"}}`裁决。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制。

## 离线回传与合并

机房无信号时，巡检员先在本机记录检验结论和困人报警，回网后通过`POST /api/offline-records`一起回传。每条现场记录是一个信封：

```json
{
  "record_id": "现场记录编号（主键，同一编号只入库一次）",
  "entity_id": "目标检验/报警编号，缺省取record_id",
  "kind": "inspection 或 alarm",
  "status": "现场最终状态，如 passed / failed / false_alarm",
  "data": {"equipment_id": "...", "findings": "..."},
  "base": {"version": 1, "status": "scheduled", "data": {}},
  "source_id": "可选，本机设备标识"
}
```

合并规则：

- **按编号去重**：每条`record_id`只入库一次，重复回传（包括同一批次重复和并发回传）直接回放**首次合并结果**，不会多记一份。
- **仅现场改过**（服务端自`base`起未变）：把检验结论或报警快进合并到服务端，并走原状态机校验（如`passed`必须带`findings`）。
- **两边都改过且不一致**：原检验/报警保持不动，另建一个`sync_conflict`实体同时保存服务端和现场两份副本及共同基线；管理员裁决前设备状态不变。
- **管理员裁决**：只有`admin`可对冲突执行`resolve`，选`field`则落地现场版，选`server`则保留服务端版；裁决后冲突变为`resolved`，收件箱记录结果。
- **合并失败留住现场记录**：如引用的设备尚不存在、现场状态一步不可达，记录保留为`failed`并带错误原因和尝试次数，不写实体；重连后调用重试接口即可继续，成功后转为`merged`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
