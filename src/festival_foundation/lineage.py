"""非遗花灯共创谱系与授权后台。

在基础层（组织/角色、幂等回执、SQLite 立即事务、哈希串联审计）之上实现：

- 作品族与不可变版本登记（草图/结构方案/公开展示版/更正版）；
- 来源纹样与贡献关系登记，派生版必须引用直接父版本并说明变化；
- 授权决定按“版本 × 贡献者”唯一，重复签署不产生第二份决定；
- 未成年人作品由监护人与机构分别确认，两份确认到齐后在单事务内原子生效；
- 展出申请沿谱系逐档核验所需许可，冲突精确到阻断来源；
- 撤销只影响未来使用窗口，已批准的合规展示快照不被抹除；
- 纠错通过新增更正版与替代关系完成，旧版保持不可变；
- 审计接口还原任一版本的贡献者、许可链、替代历史与当前可用范围。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService

VERSION_KINDS = frozenset({"sketch", "structure", "public", "correction"})
GRANT_SCOPES = frozenset({"public_display", "reuse", "derivative", "commercial"})
CONFIRMATION_KINDS = ("guardian", "institution")


def _json_load(value: str) -> Any:
    return json.loads(value)


class CoCreationService(DomainService):
    """协调共创谱系、授权决定与展出许可计算。"""

    # ---------- 通用校验辅助 ----------

    @staticmethod
    def _normalize_dt(parsed: datetime) -> str:
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _dt(self, value: str, field: str) -> str:
        value = str(value).strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是带时区的 ISO 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return self._normalize_dt(parsed)

    def _get_actor_row(self, connection, actor_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"操作者不存在: {actor_id}")
        if not row["active"]:
            raise PermissionDenied(f"操作者已停用: {actor_id}")
        return row

    def _terms(self, terms: Any) -> dict[str, Any]:
        if not isinstance(terms, dict) or not terms:
            raise ValidationError("terms 必须是非空对象")
        scopes = terms.get("scopes")
        if not isinstance(scopes, list) or not scopes or any(s not in GRANT_SCOPES for s in scopes):
            raise ValidationError(f"terms.scopes 必须是 {sorted(GRANT_SCOPES)} 的非空子集")
        normalized: dict[str, Any] = {"scopes": sorted(set(scopes))}
        note = str(terms.get("note", "")).strip()
        if len(note) > 500:
            raise ValidationError("terms.note 不能超过 500 个字符")
        if note:
            normalized["note"] = note
        end_at = terms.get("end_at")
        if end_at is not None:
            normalized["end_at"] = self._dt(end_at, "terms.end_at")
        return normalized

    # ---------- 作品族与版本 ----------

    def register_design(self, *, request_id: str, actor_id: str, design_id: str, title: str):
        payload = {"actor_id": actor_id, "design_id": design_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            design_id = self._identifier(design_id, "design_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO designs(design_id,title,created_by,created_at) VALUES(?,?,?,?)",
                        (design_id, title, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品族编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="design.registered",
                             resource_type="design", resource_id=design_id,
                             detail={"title": title}, occurred_at=self._now())
                return "design", design_id, {"design_id": design_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_design", payload=payload, create=create)

    def register_version(self, *, request_id: str, actor_id: str, version_id: str, design_id: str,
                         kind: str, content_summary: dict[str, Any], parent_version_id: str | None = None,
                         change_note: str = "", sources: list[dict[str, Any]] | None = None,
                         contributors: list[dict[str, Any]] | None = None):
        payload = {"actor_id": actor_id, "version_id": version_id, "design_id": design_id,
                   "kind": kind, "content_summary": content_summary,
                   "parent_version_id": parent_version_id, "change_note": change_note,
                   "sources": sources or [], "contributors": contributors or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version_id = self._identifier(version_id, "version_id")
            design_id = self._identifier(design_id, "design_id")
            if kind not in VERSION_KINDS:
                raise ValidationError(f"kind 必须是 {sorted(VERSION_KINDS)} 之一")
            if not isinstance(content_summary, dict) or not content_summary:
                raise ValidationError("content_summary 必须是非空对象")
            change_note = str(change_note or "").strip()
            if len(change_note) > 1000:
                raise ValidationError("change_note 不能超过 1000 个字符")
            # 来源纹样允许为空（并非每版都著录来源），但类型必须是数组。
            if sources is None:
                sources = []
            elif not isinstance(sources, list):
                raise ValidationError("sources 必须是数组")
            if not isinstance(contributors, list) or not contributors:
                raise ValidationError("contributors 至少包含一名贡献者")

            if connection.execute(
                "SELECT 1 FROM designs WHERE design_id=?", (design_id,)
            ).fetchone() is None:
                raise NotFoundError("作品族不存在")

            if parent_version_id is not None:
                parent_version_id = self._identifier(parent_version_id, "parent_version_id")
                parent_row = connection.execute(
                    "SELECT * FROM design_versions WHERE version_id=?", (parent_version_id,)
                ).fetchone()
                if parent_row is None:
                    raise NotFoundError("直接父版本不存在")
                if parent_row["design_id"] != design_id:
                    raise ValidationError("父版本必须属于同一作品族")
                if not change_note:
                    raise ValidationError("派生版本必须说明变化（change_note）")

            normalized_sources: list[dict[str, str]] = []
            seen_sources: set[str] = set()
            for item in sources:
                if not isinstance(item, dict):
                    raise ValidationError("sources 条目必须是对象")
                source_key = self._identifier(str(item.get("source_key", "")), "source_key")
                if source_key in seen_sources:
                    raise ValidationError(f"来源纹样重复: {source_key}")
                seen_sources.add(source_key)
                title = self._text(str(item.get("title", "")), "source.title")
                origin = str(item.get("origin", "") or "").strip()
                if len(origin) > 300:
                    raise ValidationError("source.origin 不能超过 300 个字符")
                normalized_sources.append({"source_key": source_key, "title": title, "origin": origin})

            normalized_contributors: list[dict[str, str]] = []
            seen_contributors: set[str] = set()
            for item in contributors:
                if not isinstance(item, dict):
                    raise ValidationError("contributors 条目必须是对象")
                contributor_actor_id = self._identifier(str(item.get("contributor_actor_id", "")),
                                                        "contributor_actor_id")
                if contributor_actor_id in seen_contributors:
                    raise ValidationError(f"贡献者重复登记: {contributor_actor_id}")
                seen_contributors.add(contributor_actor_id)
                self._get_actor_row(connection, contributor_actor_id)
                contribution_kind = self._text(str(item.get("contribution_kind", "")),
                                               "contribution_kind", 80)
                note = str(item.get("note", "") or "").strip()
                if len(note) > 300:
                    raise ValidationError("contributor.note 不能超过 300 个字符")
                normalized_contributors.append({"contributor_actor_id": contributor_actor_id,
                                                "contribution_kind": contribution_kind, "note": note})

            content_digest = digest(content_summary)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                # 依赖当前状态的规则放在幂等回执判定之后：原始请求重放时先命中回执，
                # 不会因状态已推进而被误判；新请求才执行以下检查。
                if parent_version_id is None:
                    if connection.execute(
                        "SELECT 1 FROM design_versions WHERE design_id=?", (design_id,)
                    ).fetchone():
                        raise ValidationError("作品族已有版本，再次派生必须引用直接父版本")
                elif connection.execute(
                    "SELECT 1 FROM version_supersessions WHERE old_version_id=?",
                    (parent_version_id,),
                ).fetchone():
                    raise ConflictError("父版本已被替代，请基于其替代版本派生")
                try:
                    connection.execute(
                        "INSERT INTO design_versions(version_id,design_id,kind,content_summary_json,"
                        "content_digest,parent_version_id,change_note,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (version_id, design_id, kind, canonical_json(content_summary), content_digest,
                         parent_version_id, change_note, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("版本编号已经存在") from exc
                for source in normalized_sources:
                    connection.execute(
                        "INSERT INTO version_sources(version_id,source_key,title,origin) VALUES(?,?,?,?)",
                        (version_id, source["source_key"], source["title"], source["origin"]),
                    )
                for contributor in normalized_contributors:
                    connection.execute(
                        "INSERT INTO version_contributors(version_id,contributor_actor_id,"
                        "contribution_kind,note) VALUES(?,?,?,?)",
                        (version_id, contributor["contributor_actor_id"],
                         contributor["contribution_kind"], contributor["note"]),
                    )
                append_event(connection, actor_id=actor_id, action="version.registered",
                             resource_type="design_version", resource_id=version_id,
                             detail={"design_id": design_id, "kind": kind,
                                     "parent_version_id": parent_version_id,
                                     "change_note": change_note, "content_digest": content_digest,
                                     "sources": normalized_sources,
                                     "contributors": normalized_contributors},
                             occurred_at=now)
                return "design_version", version_id, {"version_id": version_id,
                                                       "content_digest": content_digest}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_version", payload=payload, create=create)

    # ---------- 授权决定 ----------

    def submit_license(self, *, request_id: str, actor_id: str, version_id: str,
                       contributor_actor_id: str, terms: dict[str, Any], is_minor: bool = False,
                       guardian_actor_id: str | None = None,
                       institution_org_id: str | None = None):
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "contributor_actor_id": contributor_actor_id, "terms": terms,
                   "is_minor": bool(is_minor), "guardian_actor_id": guardian_actor_id,
                   "institution_org_id": institution_org_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator") and actor.actor_id != contributor_actor_id:
                raise PermissionDenied("只能由贡献者本人或管理员/操作员登记授权")
            version_id = self._identifier(version_id, "version_id")
            contributor_actor_id = self._identifier(contributor_actor_id, "contributor_actor_id")
            if connection.execute(
                "SELECT * FROM design_versions WHERE version_id=?", (version_id,)
            ).fetchone() is None:
                raise NotFoundError("版本不存在")
            if connection.execute(
                "SELECT 1 FROM version_contributors WHERE version_id=? AND contributor_actor_id=?",
                (version_id, contributor_actor_id),
            ).fetchone() is None:
                raise ValidationError("该操作者不是此版本的登记贡献者")
            normalized_terms = self._terms(terms)
            terms_hash = digest(normalized_terms)
            is_minor = bool(is_minor)
            guardian_actor_id = guardian_actor_id.strip() if guardian_actor_id else None
            institution_org_id = institution_org_id.strip() if institution_org_id else None
            if is_minor:
                if not guardian_actor_id or not institution_org_id:
                    raise ValidationError("未成年人授权必须同时指定监护人与所属机构")
                guardian = self._get_actor_row(connection, guardian_actor_id)
                if guardian["role"] != "guardian":
                    raise ValidationError("监护人确认人必须具有 guardian 角色")
                if connection.execute(
                    "SELECT 1 FROM organizations WHERE organization_id=?", (institution_org_id,)
                ).fetchone() is None:
                    raise NotFoundError("机构组织不存在")
            elif guardian_actor_id or institution_org_id:
                raise ValidationError("非未成年人授权不需要监护人/机构确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM license_grants WHERE version_id=? AND contributor_actor_id=?",
                    (version_id, contributor_actor_id),
                ).fetchone()
                if existing is not None:
                    # 重复签署不能制造两份决定：同内容视为同一决定，不同内容拒绝改写。
                    if (_json_load(existing["terms_json"]) == normalized_terms
                            and bool(existing["is_minor"]) == is_minor
                            and existing["guardian_actor_id"] == guardian_actor_id
                            and existing["institution_org_id"] == institution_org_id):
                        return ("license_grant", existing["grant_id"],
                                {"grant_id": existing["grant_id"], "status": existing["status"],
                                 "deduplicated": True})
                    raise ConflictError("该贡献者对此版本的授权决定已存在，内容不同；变更请通过更正版处理")
                grant_id = uuid.uuid4().hex
                now = self._now()
                status = "pending" if is_minor else "effective"
                effective_at = None if is_minor else now
                connection.execute(
                    "INSERT INTO license_grants(grant_id,version_id,contributor_actor_id,terms_json,"
                    "is_minor,guardian_actor_id,institution_org_id,status,effective_at,revoked_at,"
                    "revoke_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (grant_id, version_id, contributor_actor_id, canonical_json(normalized_terms),
                     1 if is_minor else 0, guardian_actor_id, institution_org_id, status,
                     effective_at, None, "", actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="license.submitted",
                             resource_type="license_grant", resource_id=grant_id,
                             detail={"version_id": version_id,
                                     "contributor_actor_id": contributor_actor_id,
                                     "terms_hash": terms_hash, "is_minor": is_minor,
                                     "guardian_actor_id": guardian_actor_id,
                                     "institution_org_id": institution_org_id, "status": status},
                             occurred_at=now)
                if not is_minor:
                    append_event(connection, actor_id=actor_id, action="license.effective",
                                 resource_type="license_grant", resource_id=grant_id,
                                 detail={"version_id": version_id,
                                         "contributor_actor_id": contributor_actor_id},
                                 occurred_at=now)
                return "license_grant", grant_id, {"grant_id": grant_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_license", payload=payload, create=create)

    def confirm_license(self, *, request_id: str, actor_id: str, grant_id: str, kind: str):
        payload = {"actor_id": actor_id, "grant_id": grant_id, "kind": kind}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            grant_id = self._identifier(grant_id, "grant_id")
            if kind not in CONFIRMATION_KINDS:
                raise ValidationError("kind 必须是 guardian 或 institution")

            grant = connection.execute(
                "SELECT * FROM license_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if grant is None:
                raise NotFoundError("授权决定不存在")
            if not grant["is_minor"]:
                raise ValidationError("仅未成年人授权需要双重确认")
            if kind == "guardian" and actor.actor_id != grant["guardian_actor_id"]:
                raise PermissionDenied("监护人确认必须由登记的监护人本人作出")
            if kind == "institution" and (
                actor.organization_id != grant["institution_org_id"]
                or actor.role not in ("admin", "operator")
            ):
                raise PermissionDenied("机构确认必须由所属机构的管理员/操作员作出")
            if grant["status"] == "revoked":
                raise ConflictError("授权已被撤销，不能再确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                became_effective = False
                # 同一确认人重复确认不制造第二份决定：仅在首次确认时写入与留痕。
                if connection.execute(
                    "SELECT 1 FROM grant_confirmations WHERE grant_id=? AND confirmation_kind=?",
                    (grant_id, kind),
                ).fetchone() is None:
                    connection.execute(
                        "INSERT INTO grant_confirmations(grant_id,confirmation_kind,"
                        "confirmer_actor_id,confirmed_at) VALUES(?,?,?,?)",
                        (grant_id, kind, actor_id, now),
                    )
                    append_event(connection, actor_id=actor_id, action="license.confirmed",
                                 resource_type="license_grant", resource_id=grant_id,
                                 detail={"version_id": grant["version_id"],
                                         "contributor_actor_id": grant["contributor_actor_id"],
                                         "confirmation_kind": kind},
                                 occurred_at=now)
                    count = connection.execute(
                        "SELECT COUNT(*) AS count FROM grant_confirmations WHERE grant_id=?",
                        (grant_id,),
                    ).fetchone()["count"]
                    # 两份确认到齐：状态翻转与全部确认写入在同一事务内原子生效。
                    if count == len(CONFIRMATION_KINDS) and grant["status"] == "pending":
                        updated = connection.execute(
                            "UPDATE license_grants SET status='effective', effective_at=? "
                            "WHERE grant_id=? AND status='pending'",
                            (now, grant_id),
                        )
                        if updated.rowcount == 1:
                            append_event(connection, actor_id=actor_id, action="license.effective",
                                         resource_type="license_grant", resource_id=grant_id,
                                         detail={"version_id": grant["version_id"],
                                                 "contributor_actor_id": grant["contributor_actor_id"],
                                                 "atomic": True},
                                         occurred_at=now)
                            became_effective = True
                status = connection.execute(
                    "SELECT status FROM license_grants WHERE grant_id=?", (grant_id,)
                ).fetchone()["status"]
                return "license_confirmation", grant_id, {"grant_id": grant_id, "status": status,
                                                          "became_effective": became_effective}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_license", payload=payload, create=create)

    def revoke_license(self, *, request_id: str, actor_id: str, grant_id: str, reason: str = ""):
        payload = {"actor_id": actor_id, "grant_id": grant_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            grant_id = self._identifier(grant_id, "grant_id")
            reason = str(reason or "").strip()
            if len(reason) > 500:
                raise ValidationError("reason 不能超过 500 个字符")

            grant = connection.execute(
                "SELECT * FROM license_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if grant is None:
                raise NotFoundError("授权决定不存在")
            if (actor.actor_id != grant["contributor_actor_id"]
                    and actor.actor_id != grant["guardian_actor_id"] and actor.role != "admin"):
                raise PermissionDenied("只能由贡献者本人、其监护人或管理员撤销")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                if grant["status"] == "revoked":
                    # 重复撤销是同一决定状态，不重复留痕、不产生第二个决定。
                    return "license_grant", grant_id, {"grant_id": grant_id, "status": "revoked",
                                                       "deduplicated": True}
                connection.execute(
                    "UPDATE license_grants SET status='revoked', revoked_at=?, revoke_reason=? "
                    "WHERE grant_id=?",
                    (now, reason, grant_id),
                )
                append_event(connection, actor_id=actor_id, action="license.revoked",
                             resource_type="license_grant", resource_id=grant_id,
                             detail={"version_id": grant["version_id"],
                                     "contributor_actor_id": grant["contributor_actor_id"],
                                     "reason": reason},
                             occurred_at=now)
                return "license_grant", grant_id, {"grant_id": grant_id, "status": "revoked"}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_license", payload=payload, create=create)

    # ---------- 纠错与替代 ----------

    def register_supersession(self, *, request_id: str, actor_id: str,
                              old_version_id: str, new_version_id: str, reason: str):
        payload = {"actor_id": actor_id, "old_version_id": old_version_id,
                   "new_version_id": new_version_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            old_version_id = self._identifier(old_version_id, "old_version_id")
            new_version_id = self._identifier(new_version_id, "new_version_id")
            reason = self._text(reason, "reason", 500)
            if old_version_id == new_version_id:
                raise ValidationError("被替代版本与更正版本不能相同")
            old_row = connection.execute(
                "SELECT * FROM design_versions WHERE version_id=?", (old_version_id,)
            ).fetchone()
            new_row = connection.execute(
                "SELECT * FROM design_versions WHERE version_id=?", (new_version_id,)
            ).fetchone()
            if old_row is None or new_row is None:
                raise NotFoundError("版本不存在")
            if new_row["kind"] != "correction":
                raise ValidationError("替代版本必须是更正版（kind=correction）")
            if new_row["parent_version_id"] != old_version_id:
                raise ValidationError("更正版必须直接派生自被替代版本")
            if connection.execute(
                "SELECT 1 FROM version_supersessions WHERE old_version_id=?", (old_version_id,)
            ).fetchone():
                raise ConflictError("旧版本已有替代关系，纠错链不能分叉")

            def create() -> tuple[str, str, dict[str, Any]]:
                supersession_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO version_supersessions(supersession_id,old_version_id,new_version_id,"
                    "reason,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (supersession_id, old_version_id, new_version_id, reason, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="version.superseded",
                             resource_type="design_version", resource_id=new_version_id,
                             detail={"old_version_id": old_version_id,
                                     "new_version_id": new_version_id, "reason": reason},
                             occurred_at=now)
                return ("version_supersession", supersession_id,
                        {"supersession_id": supersession_id, "old_version_id": old_version_id,
                         "new_version_id": new_version_id})

            return self._idempotent(connection, request_id=request_id,
                                    action="register_supersession", payload=payload, create=create)

    # ---------- 展出许可计算 ----------

    def _lineage(self, connection, version_id: str) -> list[sqlite3.Row]:
        chain: list[sqlite3.Row] = []
        current = version_id
        seen: set[str] = set()
        while current:
            if current in seen:
                raise ConflictError("谱系存在环")
            seen.add(current)
            row = connection.execute(
                "SELECT * FROM design_versions WHERE version_id=?", (current,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"谱系中的版本不存在: {current}")
            chain.append(row)
            current = row["parent_version_id"]
        return list(reversed(chain))  # 根版本在前，展出目标版本在最后

    def _evaluate_window(self, connection, chain, commercial: bool, window_end: str):
        blockers: list[dict[str, Any]] = []
        permits: list[dict[str, Any]] = []
        for version in chain:
            contributor_rows = connection.execute(
                "SELECT * FROM version_contributors WHERE version_id=? ORDER BY contributor_actor_id",
                (version["version_id"],),
            ).fetchall()
            for contributor in contributor_rows:
                contributor_actor_id = contributor["contributor_actor_id"]
                grant = connection.execute(
                    "SELECT * FROM license_grants WHERE version_id=? AND contributor_actor_id=?",
                    (version["version_id"], contributor_actor_id),
                ).fetchone()
                base = {"version_id": version["version_id"],
                        "contributor_actor_id": contributor_actor_id}
                if grant is None:
                    blockers.append({**base, "type": "license_missing",
                                     "reason": "该贡献者尚未对此版本签署授权"})
                    continue
                terms = _json_load(grant["terms_json"])
                if grant["status"] == "pending":
                    missing = [kind for kind in CONFIRMATION_KINDS if connection.execute(
                        "SELECT 1 FROM grant_confirmations WHERE grant_id=? AND confirmation_kind=?",
                        (grant["grant_id"], kind),
                    ).fetchone() is None]
                    blockers.append({**base, "type": "license_pending", "grant_id": grant["grant_id"],
                                     "missing_confirmations": missing,
                                     "reason": "未成年人授权尚未完成监护人/机构双重确认"})
                    continue
                # 撤销只影响未来：撤销时点不晚于展出窗口结束时阻断本次申请；
                # 若撤销发生在窗口完整结束之后，授权在窗口内持续有效，计入许可，
                # 从而不追溯抹除已经合规发生的展示。
                if grant["status"] == "revoked":
                    if grant["revoked_at"] <= window_end:
                        blockers.append({**base, "type": "license_revoked",
                                         "grant_id": grant["grant_id"],
                                         "revoked_at": grant["revoked_at"],
                                         "reason": "授权已撤销，撤销影响撤销时点之后的使用"})
                        continue
                    permits.append({**base, "grant_id": grant["grant_id"], "terms": terms,
                                    "is_minor": bool(grant["is_minor"]),
                                    "revoked_at": grant["revoked_at"],
                                    "revoked_after_window": True})
                    continue
                terms_end = terms.get("end_at")
                if terms_end is not None and terms_end < window_end:
                    blockers.append({**base, "type": "terms_expired", "grant_id": grant["grant_id"],
                                     "terms_end_at": terms_end,
                                     "reason": "授权期限不能覆盖展出结束时间"})
                    continue
                if "public_display" not in terms["scopes"]:
                    blockers.append({**base, "type": "scope_denied", "grant_id": grant["grant_id"],
                                     "required_scope": "public_display",
                                     "reason": "授权范围不包含公开展示"})
                    continue
                if commercial and "commercial" not in terms["scopes"]:
                    blockers.append({**base, "type": "commercial_restricted",
                                     "grant_id": grant["grant_id"],
                                     "reason": "传承人限制商业使用"})
                    continue
                permits.append({**base, "grant_id": grant["grant_id"], "terms": terms,
                                "is_minor": bool(grant["is_minor"])})
        return blockers, permits

    def submit_exhibition(self, *, request_id: str, actor_id: str, exhibition_id: str,
                          version_id: str, purpose: str, commercial: bool,
                          start_at: str, end_at: str):
        payload = {"actor_id": actor_id, "exhibition_id": exhibition_id, "version_id": version_id,
                   "purpose": purpose, "commercial": bool(commercial),
                   "start_at": start_at, "end_at": end_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            exhibition_id = self._identifier(exhibition_id, "exhibition_id")
            version_id = self._identifier(version_id, "version_id")
            purpose = self._text(purpose, "purpose", 300)
            commercial = bool(commercial)
            start = self._dt(start_at, "start_at")
            end = self._dt(end_at, "end_at")
            if start > end:
                raise ValidationError("start_at 不能晚于 end_at")
            if connection.execute(
                "SELECT * FROM design_versions WHERE version_id=?", (version_id,)
            ).fetchone() is None:
                raise NotFoundError("版本不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM exhibitions WHERE exhibition_id=?", (exhibition_id,)
                ).fetchone()
                if existing is not None:
                    same = (existing["version_id"] == version_id and existing["purpose"] == purpose
                            and bool(existing["commercial"]) == commercial
                            and existing["start_at"] == start and existing["end_at"] == end)
                    if not same:
                        raise ConflictError("展出申请编号已被不同内容使用")
                    return "exhibition", exhibition_id, {
                        "exhibition_id": exhibition_id, "status": existing["status"],
                        "blockers": _json_load(existing["blockers_json"]), "deduplicated": True}
                now = self._now()
                chain = self._lineage(connection, version_id)
                blockers: list[dict[str, Any]] = []
                supersession = connection.execute(
                    "SELECT * FROM version_supersessions WHERE old_version_id=?", (version_id,)
                ).fetchone()
                if supersession is not None:
                    blockers.append({"version_id": version_id, "type": "version_superseded",
                                     "current_version_id": supersession["new_version_id"],
                                     "reason": "该版本已被更正版替代，新展出应使用当前版本"})
                license_blockers, permits = self._evaluate_window(connection, chain, commercial, end)
                blockers.extend(license_blockers)
                approved = not blockers
                snapshot = {"evaluated_at": now, "window": {"start_at": start, "end_at": end},
                            "commercial": commercial, "permits": permits}
                connection.execute(
                    "INSERT INTO exhibitions(exhibition_id,version_id,purpose,commercial,start_at,"
                    "end_at,exhibitor_actor_id,status,blockers_json,permit_snapshot_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (exhibition_id, version_id, purpose, 1 if commercial else 0, start, end,
                     actor_id, "approved" if approved else "blocked",
                     canonical_json(blockers), canonical_json(snapshot), now),
                )
                append_event(connection, actor_id=actor_id,
                             action="exhibition.approved" if approved else "exhibition.blocked",
                             resource_type="exhibition", resource_id=exhibition_id,
                             detail={"version_id": version_id, "commercial": commercial,
                                     "start_at": start, "end_at": end,
                                     "blocker_count": len(blockers), "blockers": blockers},
                             occurred_at=now)
                return ("exhibition", exhibition_id,
                        {"exhibition_id": exhibition_id,
                         "status": "approved" if approved else "blocked", "blockers": blockers})

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_exhibition", payload=payload, create=create)

    # ---------- 查询与审计还原 ----------

    def get_exhibition(self, exhibition_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM exhibitions WHERE exhibition_id=?", (exhibition_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("展出申请不存在")
        return {"exhibition_id": row["exhibition_id"], "version_id": row["version_id"],
                "purpose": row["purpose"], "commercial": bool(row["commercial"]),
                "start_at": row["start_at"], "end_at": row["end_at"],
                "exhibitor_actor_id": row["exhibitor_actor_id"], "status": row["status"],
                "blockers": _json_load(row["blockers_json"]),
                "permit_snapshot": _json_load(row["permit_snapshot_json"]),
                "created_at": row["created_at"]}

    def _version_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        version_id = row["version_id"]
        connection = self.database.connection
        sources = [dict(source_key=r["source_key"], title=r["title"], origin=r["origin"])
                   for r in connection.execute(
                       "SELECT * FROM version_sources WHERE version_id=? ORDER BY source_key",
                       (version_id,))]
        contributors = [dict(contributor_actor_id=r["contributor_actor_id"],
                             contribution_kind=r["contribution_kind"], note=r["note"])
                        for r in connection.execute(
                            "SELECT * FROM version_contributors WHERE version_id=? "
                            "ORDER BY contributor_actor_id", (version_id,))]
        return {"version_id": version_id, "design_id": row["design_id"], "kind": row["kind"],
                "content_summary": _json_load(row["content_summary_json"]),
                "content_digest": row["content_digest"],
                "parent_version_id": row["parent_version_id"], "change_note": row["change_note"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "sources": sources, "contributors": contributors}

    def _grants_for(self, version_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        grants = []
        for row in connection.execute(
            "SELECT * FROM license_grants WHERE version_id=? ORDER BY contributor_actor_id",
            (version_id,),
        ):
            confirmations = [
                {"kind": r["confirmation_kind"], "confirmer_actor_id": r["confirmer_actor_id"],
                 "confirmed_at": r["confirmed_at"]}
                for r in connection.execute(
                    "SELECT * FROM grant_confirmations WHERE grant_id=? ORDER BY confirmation_kind",
                    (row["grant_id"],))]
            grants.append({"grant_id": row["grant_id"], "version_id": row["version_id"],
                           "contributor_actor_id": row["contributor_actor_id"],
                           "terms": _json_load(row["terms_json"]),
                           "is_minor": bool(row["is_minor"]),
                           "guardian_actor_id": row["guardian_actor_id"],
                           "institution_org_id": row["institution_org_id"],
                           "status": row["status"], "effective_at": row["effective_at"],
                           "revoked_at": row["revoked_at"], "revoke_reason": row["revoke_reason"],
                           "confirmations": confirmations})
        return grants

    def _supersession_history(self, version_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        edges = {row["old_version_id"]: row for row in connection.execute(
            "SELECT * FROM version_supersessions")}
        # 先回退到该替代链上最早的被替代版本，再沿替代关系向前推进。
        earliest = version_id
        while True:
            previous = next((old for old, edge in edges.items()
                             if edge["new_version_id"] == earliest), None)
            if previous is None:
                break
            earliest = previous
        history = []
        current = earliest
        while current in edges:
            edge = edges[current]
            history.append({"old_version_id": edge["old_version_id"],
                            "new_version_id": edge["new_version_id"],
                            "reason": edge["reason"], "created_by": edge["created_by"],
                            "created_at": edge["created_at"]})
            current = edge["new_version_id"]
        return history

    def _current_availability(self, chain: list[sqlite3.Row]) -> dict[str, Any]:
        connection = self.database.connection
        now = self._now()
        scopes: set[str] | None = None
        gaps: list[dict[str, Any]] = []
        for version in chain:
            for contributor in connection.execute(
                "SELECT * FROM version_contributors WHERE version_id=? ORDER BY contributor_actor_id",
                (version["version_id"],),
            ):
                grant = connection.execute(
                    "SELECT * FROM license_grants WHERE version_id=? AND contributor_actor_id=?",
                    (version["version_id"], contributor["contributor_actor_id"]),
                ).fetchone()
                base = {"version_id": version["version_id"],
                        "contributor_actor_id": contributor["contributor_actor_id"]}
                if grant is None:
                    gaps.append({**base, "type": "license_missing"})
                    scopes = set()
                    continue
                if grant["status"] != "effective":
                    gaps.append({**base, "type": f"license_{grant['status']}",
                                 "grant_id": grant["grant_id"]})
                    scopes = set()
                    continue
                terms = _json_load(grant["terms_json"])
                if terms.get("end_at") is not None and terms["end_at"] < now:
                    gaps.append({**base, "type": "terms_expired", "grant_id": grant["grant_id"]})
                    scopes = set()
                    continue
                granted = set(terms["scopes"])
                scopes = granted if scopes is None else scopes & granted
        target = chain[-1]
        supersession = connection.execute(
            "SELECT new_version_id FROM version_supersessions WHERE old_version_id=?",
            (target["version_id"],),
        ).fetchone()
        effective_scopes = scopes or set()
        return {"evaluated_at": now, "scopes": sorted(effective_scopes),
                "commercial_allowed": "commercial" in effective_scopes,
                "gaps": gaps,
                "superseded": supersession is not None,
                "recommended_version_id": supersession["new_version_id"] if supersession else None}

    def get_version_audit(self, version_id: str) -> dict[str, Any]:
        """还原任一展示版本的贡献者、许可链、替代历史和当前可用范围。"""

        connection = self.database.connection
        row = connection.execute(
            "SELECT * FROM design_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("版本不存在")
        chain = self._lineage(connection, version_id)
        lineage = []
        for ancestor in chain:
            payload = self._version_payload(ancestor)
            payload["grants"] = self._grants_for(ancestor["version_id"])
            lineage.append(payload)
        return {"version": self._version_payload(row),
                "lineage_order": [item["version_id"] for item in lineage],
                "lineage": lineage,
                "supersession_history": self._supersession_history(version_id),
                "current_availability": self._current_availability(chain)}
