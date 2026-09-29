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

## 受控流程：批准 → 授权 → 生效

- **批准**：`approve` 必须提供三名互不相同且无利益关联的委员（`reviewers: [{committee_id, conflict_of_interest:false}]`，兼容旧字段 `approvals`）；利益关联委员或申请人本人不得参与。批准时在 `data.approved` 冻结申请快照（申请人、机构、数据集、用途 `purpose`、用途版本 `purpose_version`、条款、有效期、批准人与时间），快照版本等于申请实体版本。
- **用途/有效期变更**：申请已批准后用 `amend` 修订（用途变更会令 `purpose_version` 递增并写入 `amendments`），冻结快照保持不变；需要让变更重新生效时用 `reconsider` 回到 `under_review`，重新 `approve` 后生成新的冻结快照版本（旧授权仍绑定旧版本，按新版本重新签发的授权才能生效）；`expire` 将申请登记为过期。
- **授权签发**：`grant` 创建时只能绑定状态为 `approved` 的申请，数据集与申请人机构必须一致；可显式传 `application_version`（必须等于当前已批准版本，否则冲突拒绝）。授权数据中的 `bound_approval` 保存绑定快照，此后不再随申请当前数据漂移。`recipient_institution` 为必填。
- **生效前复核**：`activate` 会重新核对来源申请仍为已批准、申请人机构/用途/用途版本/有效期与绑定快照一致、授权有效期不超出批准窗口且未过期。任一项漂移即拒绝生效，并提示按当前批准版本重新申请授权。
- **申请失效联动**：申请 `withdraw`/`reject`/`expire` 时，若存在 `active` 授权则整体拒绝并提示管理员先撤销（申请状态不变）；其余未生效（`issued`）授权由系统自动置为 `voided` 并写入 `invalid_reason` 和级联审计。

## 演示页面

打开根路径可见治理看板：授权卡片展示来源申请、绑定批准版本、状态徽标与失效原因（失效时红色提示），申请卡片展示批准快照、三名委员与修订记录，并提供操作面板和系统级联审计时间线。

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
