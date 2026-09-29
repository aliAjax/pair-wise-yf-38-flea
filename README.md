# 基因组数据访问治理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8304`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8304
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `dataset`：受控数据集；`application`：访问申请；`grant`：限时数据使用凭证。

## 受控的批准—授权—生效流程

1. **批准留痕**：申请进入 `under_review` 后，`approve` 必须提交三名互不相同的委员（`reviewers: [{reviewer_id, reviewer_org}]`）；委员机构与申请人机构相同、或自行声明 `declared_conflict` 的一律拒绝。批准时冻结申请数据快照 `approval_snapshot`、用途版本 `approved_purpose_version` 和申请版本 `approved_application_version`。
2. **授权绑定版本**：`grant` 只能在申请 `approved` 后创建，且数据集必须与申请一致；签发时在授权数据中盖绑定章（申请版本、用途版本、申请人机构、批准到期日），之后不再随申请数据漂移。
3. **生效前复核**：`activate` 时再次核对来源申请仍为 `approved`、申请版本与用途版本未被改动（批准后 `amend` 用途/到期日会抬高版本并阻断生效）、`recipient_org` 与批准快照中的申请人机构一致、授权到期日不晚于批准到期日。
4. **终结即失效**：申请 `withdraw` / `reject` / `expire` 时，所有未使用（`issued`）的授权立即置为 `invalid` 并写入 `invalid_reason`（如 `application_withdrawn`）和明细；仍有 `active` 授权时返回 `GrantsInUseError`（409），提示管理员先 `revoke`，申请保持原状态不变。
5. **可追溯**：级联失效同样写入审计时间线；页面和实体视图可看清授权的来源申请、当前状态与失效原因。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

数据目录和授权凭证是治理流程演示，不包含真实数据下载、加密或机构身份联邦。
