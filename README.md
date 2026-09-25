# 形成非遗花灯共创谱系与授权后台基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

`genealogy` 领域模块在底座之上实现“非遗花灯共创谱系与授权后台”：把传承人、学校、社区共创的同一设计登记为不可变版本谱系，沿谱系计算公开展出/商业使用所需许可，冲突时定位具体阻断来源，并支持未成年人双确认、原子授权、纠错替代与全量审计还原。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、花灯共创谱系与授权服务、HTTP 路由和离线验收；
- `tests/`：基础规则、谱系/授权规则、事务边界、接口路由和端到端验收测试。

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
```

验收命令会在临时 SQLite 数据库中登记组织、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 花灯共创谱系与授权领域

### 核心规则

- **不可变版本与谱系**：每个版本以 `summary` 内容摘要（SHA-256）登记，`family_id` 标识同一共创设计。`source_pattern`（来源纹样）为谱系根；`sketch`/`structure_plan`/`display`/`correction` 等派生版本必须引用同谱系的直接 `parent_id` 并填写 `change_note`。相同内容摘要在同一谱系内不能重复登记。
- **贡献关系**：版本登记时同时记录贡献主体与贡献角色；授权只能授予该版本的贡献者。
- **授权决定唯一**：`(版本, 贡献者, 授权范围)` 只有一份决定。重复签署内容一致时回放既有决定，内容冲突时拒绝（需走更正纠错，不能改写历史）。
- **未成年人双确认**：学生作品的公开授权必须同时提供 `guardian_confirmation`（登记的监护人）与 `institution_confirmation`（机构），两类确认与授权行在同一事务内原子写入；缺任一项整体回滚。
- **商业使用限制**：传承人可对某版本授予 `commercial` 的 `deny`，该拒绝沿谱系持续生效且不可撤销，但不追溯抹除已发生的合规公开展示。
- **展出许可计算**：提交用途（`public_display`/`commercial`）与时间范围后，沿父版本闭包逐版本逐贡献者核对许可。全部满足才可原子批准；存在冲突时返回 `422 license_conflict` 并在 `blockers` 中给出每个阻断来源（版本、贡献者、原因）。阻断原因包括 `missing_authorization`、`denied_by_rights_holder`、`authorization_revoked`、`minor_confirmation_incomplete`、`outside_validity_window`。
- **撤销只影响未来**：撤销把授权行置为 `revoked`，新的展出评估会被阻断，但已批准展出及其当时的许可快照保持有效；用同一 `request_id` 重放仍回放原批准。
- **纠错替代**：纠错通过新增 `correction` 版本并建立 `supersessions` 替代边完成，旧版本不可变、仍可审计；`audit` 接口据此判断版本是否 `is_current`。
- **审计底座**：所有写动作追加到既有哈希串联审计链，`GET /health` 与 `verify_audit` 继续校验。

### HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/subjects` | 登记传承人/学校/社区/学生/监护人主体（学生须带监护人） |
| POST | `/works` | 登记不可变版本（派生版须带 `parent_id`、`change_note`） |
| GET | `/works?family_id=...` | 列出某共创谱系全部版本 |
| POST | `/works/supersede` | 更正版替代被纠错版本 |
| POST | `/authorizations` | 登记授权决定（未成年人带双确认，可带有效时间窗） |
| POST | `/authorizations/revoke` | 撤销授权，仅影响未来使用 |
| POST | `/exhibitions/evaluate` | 只读试算许可链，返回 `allowed` 与 `blockers` |
| POST | `/exhibitions` | 许可链完整时原子批准展出，否则返回具体阻断 |
| GET | `/exhibitions?work_id=...` | 查版本的已批准展出与许可快照 |
| GET | `/works/audit/{work_id}` | 还原贡献者、许可链、替代历史与当前可用范围 |

`GET /works/audit/{work_id}` 返回版本内容与摘要、从根到该版本的 `lineage`、谱系各版本贡献者、`license_chain`（含未成年人确认）、`supersession`（替代与被替代、`is_current`）、历史 `exhibitions` 以及 `current_availability`（公开/商业两种用途当前是否可用及阻断点）。
