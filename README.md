# 形成非遗花灯共创谱系与授权后台基础服务

本项目在节日公共服务通用后台基础层（组织、服务站点、操作者与结构化参考资料登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计）之上，构建**非遗花灯共创谱系与授权后台**：

- **不可变版本谱系**：作品族下的草图、结构方案、公开展示版与更正版均为不可变版本，服务端对内容摘要计算 SHA-256 哈希；派生版本必须引用**直接父版本**并填写变化说明，同时登记来源纹样与贡献关系。
- **授权决定去重**：授权按“版本 × 贡献者”唯一。重复签署同一内容不产生第二份决定；不同内容不能改写既有决定，变更只能走更正版。
- **未成年人双重确认**：未成年人作品的公开授权先处于 `pending`，监护人与所属机构**分别确认**，两份确认到齐后在同一事务内原子生效。
- **谱系许可计算**：展出方提交用途、是否商业与时间窗口后，系统沿父版本链逐档聚合全部贡献者核验；冲突时返回精确到“版本 + 贡献者 + 原因”的阻断来源（缺授权、待确认、已撤销、超期、超范围、商业受限、版本已被替代）。
- **撤销只影响未来**：撤销后的新窗口被阻断；撤销时点之后才结束的窗口才受影响，已批准展出的许可快照不被追溯抹除。
- **纠错不抹历史**：纠错通过新增 `correction` 版本并建立替代关系完成，旧版保持不可变但会指向当前推荐版本；替代链不分叉。
- **审计还原**：`GET /version-audit` 一次还原任一展示版本的贡献者、完整许可链（含确认记录与状态）、替代历史与当前可用范围；所有写动作进入既有哈希串联审计链。

## 目录

- `src/festival_foundation/`：
  - `service.py` / `storage.py` / `audit.py`：组织角色、SQLite 事务、幂等回执与哈希审计底座（新增 `guardian` 监护人角色）；
  - `lineage.py`：共创谱系、授权决定、双重确认、展出许可计算与审计还原；
  - `api.py`：HTTP/JSON 路由；
  - `acceptance.py` / `acceptance_lineage.py`：基础层与共创授权的离线验收。
- `tests/`：基础规则、谱系授权规则、事务边界、接口路由和端到端验收测试。

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
PYTHONPATH=src python3 -m festival_foundation.acceptance
PYTHONPATH=src python3 -m festival_foundation.acceptance_lineage
```

第一条命令核对基础层的组织、操作者、站点、参考资料、幂等回执与审计链；第二条在临时库中完整走查：传统草图 → 结构方案 → 公开展示版 → 更正版的谱系、未成年人监护人与机构双重确认、商业/非商业展出的沿链许可计算、撤销只影响未来以及替代关系。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写入都需要 `request_id` 以获得幂等回执，服务重启后 SQLite 中的业务状态和审计链继续保留。

### 共创授权接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/designs` | 登记作品族 |
| POST | `/design-versions` | 登记不可变版本（草图/结构/公开展示/更正；派生须 `parent_version_id` + `change_note`，附 `sources` 与 `contributors`） |
| POST | `/licenses` | 提交授权决定（同一版本+贡献者唯一；未成年人须 `is_minor` + 监护人与机构信息） |
| POST | `/license-confirmations` | 作出监护人/机构确认（`kind=guardian|institution`），两份到齐原子生效 |
| POST | `/license-revocations` | 撤销授权（只影响未来窗口） |
| POST | `/supersessions` | 建立更正版对旧版的替代关系 |
| POST | `/exhibitions` | 提交展出申请（用途、`commercial`、`start_at`/`end_at`），沿谱系计算并保存许可快照 |
| GET | `/exhibitions?exhibition_id=` | 查看审批结果与具体阻断来源 |
| GET | `/version-audit?version_id=` | 还原贡献者、许可链、替代历史与当前可用范围 |
