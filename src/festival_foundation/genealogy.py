"""实现非遗花灯共创谱系、授权条款与展出许可计算。

本模块建立在基础层的组织/操作者/角色、幂等回执、SQLite 事务与哈希审计
能力之上，新增四类不可变业务事实：

- 作品版本（works）：以内容摘要登记，派生版本必须引用直接父版本并说明变化；
- 授权决定（work_authorizations）：同一版本/贡献者/授权范围只有一份决定，
  未成年人公开授权由监护人与机构在同一事务内分别确认；
- 展出审批（exhibitions）：沿父版本谱系逐贡献者计算所需许可，全部满足才
  原子生效，冲突时定位到具体阻断来源；
- 替代关系（supersessions）：纠错通过新增更正版与替代边完成，旧版不可变。

撤销只关闭授权行的未来效力，已批准的展出保留当时固化的许可快照。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, LicenseConflict, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService
from .storage import Database


WORK_KINDS = frozenset({"source_pattern", "sketch", "structure_plan", "display", "correction"})
DERIVED_KINDS = frozenset({"sketch", "structure_plan", "display", "correction"})
SUBJECT_KINDS = frozenset({"heritor", "school", "community", "student", "guardian"})
SCOPES = frozenset({"public_display", "commercial"})
WRITE_ROLES = ("admin", "operator")
CONTENT_ROLES = ("admin", "operator", "reviewer")


class GenealogyService:
    """协调共创谱系登记、授权决定、展出许可计算与审计还原。"""

    def __init__(self, database: Database, clock=None) -> None:
        self.database = database
        self.base = DomainService(database, clock)
        self.clock = self.base.clock

    # ------------------------------------------------------------------ 通用辅助

    def _now(self) -> str:
        return self.base._now()

    def _dt(self, value: str, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc)

    def _subject(self, connection, subject_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"主体不存在: {subject_id}")
        return dict(row)

    def _work(self, connection, work_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"作品版本不存在: {work_id}")
        return dict(row)

    def _lineage(self, connection, work_id: str) -> list[dict[str, Any]]:
        """按从根到目标的顺序返回直接父版本链。"""

        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current = work_id
        while current:
            if current in seen:
                raise ConflictError("谱系中出现了版本环")
            seen.add(current)
            work = self._work(connection, current)
            chain.append(work)
            current = work["parent_id"]
        chain.reverse()
        return chain

    def _contributors(self, connection, work_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT wc.work_id, wc.subject_id, wc.contribution_role, wc.note, s.kind AS subject_kind, s.name "
            "FROM work_contributors wc JOIN subjects s ON s.subject_id=wc.subject_id "
            "WHERE wc.work_id=? ORDER BY wc.subject_id",
            (work_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _authorizations(self, connection, work_ids: list[str]) -> list[dict[str, Any]]:
        if not work_ids:
            return []
        marks = ",".join("?" for _ in work_ids)
        rows = connection.execute(
            f"SELECT * FROM work_authorizations WHERE work_id IN ({marks}) ORDER BY work_id, subject_id, scope",
            work_ids,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["is_minor"] = bool(item["is_minor"])
            item["confirmations"] = [
                dict(conf) for conf in connection.execute(
                    "SELECT confirmer_type, confirmer_id, actor_id, confirmed_at "
                    "FROM authorization_confirmations WHERE auth_id=? ORDER BY confirmer_type",
                    (item["auth_id"],),
                ).fetchall()
            ]
            result.append(item)
        return result

    # ------------------------------------------------------------------ 主体登记

    def register_subject(self, *, request_id: str, actor_id: str, subject_id: str,
                         kind: str, name: str, organization_id: str | None = None,
                         guardian_id: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "subject_id": subject_id, "kind": kind, "name": name,
                   "organization_id": organization_id, "guardian_id": guardian_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.base._actor(connection, actor_id)
            self.base._require(actor, *WRITE_ROLES)
            subject_id = self.base._identifier(subject_id, "subject_id")
            name = self.base._text(name, "name")
            if kind not in SUBJECT_KINDS:
                raise ValidationError("subject kind 不在允许范围内")
            if organization_id is not None:
                organization_id = self.base._identifier(organization_id, "organization_id")
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (organization_id,)).fetchone() is None:
                    raise NotFoundError("组织不存在")
            if kind == "student":
                if not guardian_id:
                    raise ValidationError("未成年学生必须登记监护人")
                guardian_id = self.base._identifier(guardian_id, "guardian_id")
                guardian = self._subject(connection, guardian_id)
                if guardian["kind"] != "guardian":
                    raise ValidationError("guardian_id 必须指向监护人主体")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO subjects(subject_id,kind,name,organization_id,guardian_id,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (subject_id, kind, name, organization_id, guardian_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("主体编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="subject.registered",
                             resource_type="subject", resource_id=subject_id,
                             detail={"kind": kind, "name": name, "organization_id": organization_id,
                                     "guardian_id": guardian_id}, occurred_at=self._now())
                return "subject", subject_id, {"subject_id": subject_id}

            return self.base._idempotent(connection, request_id=request_id, action="register_subject",
                                         payload=payload, create=create)

    # ------------------------------------------------------------------ 版本登记

    def register_work(self, *, request_id: str, actor_id: str, family_id: str, kind: str,
                      title: str, summary: dict[str, Any], contributors: list[dict[str, str]],
                      parent_id: str | None = None, change_note: str | None = None) -> Any:
        if not isinstance(summary, dict) or not summary:
            raise ValidationError("summary 必须是非空对象，用于登记内容摘要")
        if not isinstance(contributors, list) or not contributors:
            raise ValidationError("contributors 必须是非空数组")
        payload = {"actor_id": actor_id, "family_id": family_id, "kind": kind, "title": title,
                   "summary": summary, "contributors": contributors, "parent_id": parent_id,
                   "change_note": change_note}
        with self.database.transaction(immediate=True) as connection:
            actor = self.base._actor(connection, actor_id)
            self.base._require(actor, *CONTENT_ROLES)
            family_id = self.base._identifier(family_id, "family_id")
            title = self.base._text(title, "title")
            if kind not in WORK_KINDS:
                raise ValidationError("work kind 不在允许范围内")
            summary_hash = digest(summary)

            parent: dict[str, Any] | None = None
            if kind == "source_pattern":
                if parent_id is not None:
                    raise ValidationError("来源纹样是谱系根版本，不能声明父版本")
            else:
                if kind not in DERIVED_KINDS:
                    raise ValidationError("派生版本类型无效")
                if not parent_id:
                    raise ValidationError("派生版本必须引用直接父版本")
                parent_id = self.base._identifier(parent_id, "parent_id")
                parent = self._work(connection, parent_id)
                if parent["family_id"] != family_id:
                    raise ValidationError("父版本不属于同一共创谱系")
                change_note = self.base._text(change_note, "change_note", 1000)

            normalized: list[tuple[str, str, str]] = []
            for entry in contributors:
                if not isinstance(entry, dict) or "subject_id" not in entry:
                    raise ValidationError("贡献关系必须包含 subject_id")
                contributor_id = self.base._identifier(entry["subject_id"], "contributors.subject_id")
                self._subject(connection, contributor_id)
                role = self.base._text(entry.get("contribution_role", ""), "contributors.contribution_role", 80)
                note = str(entry.get("note", "") or "")[:500]
                normalized.append((contributor_id, role, note))
            if len({item[0] for item in normalized}) != len(normalized):
                raise ValidationError("同一版本中贡献者不能重复")

            def create() -> tuple[str, str, dict[str, Any]]:
                duplicate = connection.execute(
                    "SELECT work_id FROM works WHERE family_id=? AND summary_hash=?",
                    (family_id, summary_hash),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError(f"相同内容摘要已登记为不可变版本 {duplicate['work_id']}")
                work_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO works(work_id,family_id,kind,title,summary_json,summary_hash,parent_id,"
                    "change_note,registered_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (work_id, family_id, kind, title, canonical_json(summary), summary_hash, parent_id,
                     change_note, actor_id, self._now()),
                )
                connection.executemany(
                    "INSERT INTO work_contributors(work_id,subject_id,contribution_role,note) VALUES(?,?,?,?)",
                    [(work_id, subject_id, role, note) for subject_id, role, note in normalized],
                )
                append_event(connection, actor_id=actor_id, action="work.registered",
                             resource_type="work", resource_id=work_id,
                             detail={"family_id": family_id, "kind": kind, "title": title,
                                     "summary_hash": summary_hash, "parent_id": parent_id,
                                     "change_note": change_note,
                                     "contributors": [item[0] for item in normalized]},
                             occurred_at=self._now())
                return "work", work_id, {"work_id": work_id, "family_id": family_id,
                                         "summary_hash": summary_hash}

            return self.base._idempotent(connection, request_id=request_id, action="register_work",
                                         payload=payload, create=create)

    # ------------------------------------------------------------------ 授权决定

    def grant_authorization(self, *, request_id: str, actor_id: str, work_id: str, subject_id: str,
                            scope: str, decision: str, valid_from: str | None = None,
                            valid_until: str | None = None,
                            guardian_confirmation: dict[str, str] | None = None,
                            institution_confirmation: dict[str, str] | None = None) -> Any:
        payload = {"actor_id": actor_id, "work_id": work_id, "subject_id": subject_id, "scope": scope,
                   "decision": decision, "valid_from": valid_from, "valid_until": valid_until,
                   "guardian_confirmation": guardian_confirmation,
                   "institution_confirmation": institution_confirmation}
        with self.database.transaction(immediate=True) as connection:
            actor = self.base._actor(connection, actor_id)
            self.base._require(actor, *WRITE_ROLES)
            work_id = self.base._identifier(work_id, "work_id")
            subject_id = self.base._identifier(subject_id, "subject_id")
            work = self._work(connection, work_id)
            subject = self._subject(connection, subject_id)
            if scope not in SCOPES:
                raise ValidationError("scope 必须是 public_display 或 commercial")
            if decision not in ("allow", "deny"):
                raise ValidationError("decision 必须是 allow 或 deny")
            if connection.execute(
                "SELECT 1 FROM work_contributors WHERE work_id=? AND subject_id=?",
                (work_id, subject_id),
            ).fetchone() is None:
                raise ValidationError("只能为该版本的贡献者登记授权")
            window_from = window_until = None
            if valid_from is not None:
                window_from = self._dt(valid_from, "valid_from")
            if valid_until is not None:
                window_until = self._dt(valid_until, "valid_until")
            if window_from and window_until and window_until <= window_from:
                raise ValidationError("valid_until 必须晚于 valid_from")

            is_minor = subject["kind"] == "student"
            guardian_ref = institution_ref = None
            if is_minor:
                if scope != "public_display":
                    raise ValidationError("未成年人作品仅登记公开授权，商业授权不在本服务范围")
                if not guardian_confirmation or not institution_confirmation:
                    raise ValidationError("未成年人公开授权必须同时包含监护人与机构确认")
                guardian_ref = self.base._identifier(guardian_confirmation.get("guardian_subject_id", ""),
                                                     "guardian_confirmation.guardian_subject_id")
                if guardian_ref != subject["guardian_id"]:
                    raise ValidationError("监护人确认必须由登记的监护人作出")
                self._subject(connection, guardian_ref)
                institution_ref = self.base._identifier(institution_confirmation.get("organization_id", ""),
                                                        "institution_confirmation.organization_id")
                institution = connection.execute(
                    "SELECT organization_id FROM organizations WHERE organization_id=?", (institution_ref,)
                ).fetchone()
                if institution is None:
                    raise NotFoundError("确认机构不存在")
                if actor.role != "admin" and actor.organization_id != institution_ref:
                    raise PermissionDenied("机构确认必须由该组织的操作者记录")
            elif guardian_confirmation or institution_confirmation:
                raise ValidationError("只有未成年人授权需要监护人与机构确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM work_authorizations WHERE work_id=? AND subject_id=? AND scope=?",
                    (work_id, subject_id, scope),
                ).fetchone()
                if existing is not None:
                    # 重复签署不能制造两份决定：内容一致则返回既有决定，冲突则拒绝。
                    if existing["decision"] != decision:
                        raise ConflictError("该授权范围已有不可更改的决定，请通过更正版纠错流程处理")
                    return ("authorization", existing["auth_id"],
                            {"auth_id": existing["auth_id"], "deduplicated": True})
                auth_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO work_authorizations(auth_id,work_id,subject_id,scope,decision,is_minor,"
                    "status,valid_from,valid_until,granted_by,granted_at) VALUES(?,?,?,?,?,?,'active',?,?,?,?)",
                    (auth_id, work_id, subject_id, scope, decision, 1 if is_minor else 0,
                     valid_from, valid_until, actor_id, self._now()),
                )
                # 两类确认与授权行在同一事务内写入，原子生效。
                if is_minor:
                    connection.execute(
                        "INSERT INTO authorization_confirmations(auth_id,confirmer_type,confirmer_id,"
                        "actor_id,confirmed_at) VALUES(?,?,?,?,?)",
                        (auth_id, "guardian", guardian_ref, actor_id, self._now()),
                    )
                    connection.execute(
                        "INSERT INTO authorization_confirmations(auth_id,confirmer_type,confirmer_id,"
                        "actor_id,confirmed_at) VALUES(?,?,?,?,?)",
                        (auth_id, "institution", institution_ref, actor_id, self._now()),
                    )
                append_event(connection, actor_id=actor_id, action="authorization.granted",
                             resource_type="authorization", resource_id=auth_id,
                             detail={"work_id": work_id, "subject_id": subject_id, "scope": scope,
                                     "decision": decision, "is_minor": is_minor,
                                     "valid_from": valid_from, "valid_until": valid_until,
                                     "family_id": work["family_id"]},
                             occurred_at=self._now())
                return "authorization", auth_id, {"auth_id": auth_id, "deduplicated": False}

            return self.base._idempotent(connection, request_id=request_id, action="grant_authorization",
                                         payload=payload, create=create)

    def revoke_authorization(self, *, request_id: str, actor_id: str, auth_id: str) -> Any:
        payload = {"actor_id": actor_id, "auth_id": auth_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self.base._actor(connection, actor_id)
            self.base._require(actor, *WRITE_ROLES)
            auth_id = self.base._identifier(auth_id, "auth_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM work_authorizations WHERE auth_id=?", (auth_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("授权不存在")
                # 撤销只影响未来使用：拒绝决定本身就是持续限制，不存在“撤销授权”。
                if row["decision"] != "allow":
                    raise ConflictError("拒绝决定不能撤销，商业使用限制持续有效")
                if row["status"] == "revoked":
                    return "authorization", auth_id, {"auth_id": auth_id, "deduplicated": True}
                connection.execute(
                    "UPDATE work_authorizations SET status='revoked', revoked_by=?, revoked_at=? "
                    "WHERE auth_id=?",
                    (actor_id, self._now(), auth_id),
                )
                append_event(connection, actor_id=actor_id, action="authorization.revoked",
                             resource_type="authorization", resource_id=auth_id,
                             detail={"work_id": row["work_id"], "subject_id": row["subject_id"],
                                     "scope": row["scope"]}, occurred_at=self._now())
                return "authorization", auth_id, {"auth_id": auth_id, "deduplicated": False}

            return self.base._idempotent(connection, request_id=request_id, action="revoke_authorization",
                                         payload=payload, create=create)

    # ------------------------------------------------------------------ 许可计算

    def _check_requirement(self, connection, *, work: dict[str, Any], contributor: dict[str, Any],
                           scope: str, window_start: datetime | None,
                           window_end: datetime | None) -> dict[str, Any]:
        subject_id = contributor["subject_id"]
        check = {"work_id": work["work_id"], "work_kind": work["kind"], "subject_id": subject_id,
                 "subject_kind": contributor["subject_kind"], "scope": scope, "satisfied": False,
                 "reason": "missing_authorization", "auth_id": None}
        row = connection.execute(
            "SELECT * FROM work_authorizations WHERE work_id=? AND subject_id=? AND scope=?",
            (work["work_id"], subject_id, scope),
        ).fetchone()
        if row is None:
            return check
        check["auth_id"] = row["auth_id"]
        if row["decision"] == "deny":
            check["reason"] = "denied_by_rights_holder"
            return check
        if row["status"] == "revoked":
            check["reason"] = "authorization_revoked"
            return check
        if row["is_minor"]:
            confirmations = connection.execute(
                "SELECT COUNT(*) AS count FROM authorization_confirmations WHERE auth_id=?",
                (row["auth_id"],),
            ).fetchone()["count"]
            if confirmations < 2:
                check["reason"] = "minor_confirmation_incomplete"
                return check
        if window_start is not None:
            if row["valid_from"]:
                valid_from = self._dt(row["valid_from"], "valid_from")
                if valid_from > window_start:
                    check["reason"] = "outside_validity_window"
                    return check
            if row["valid_until"]:
                valid_until = self._dt(row["valid_until"], "valid_until")
                if valid_until < window_end:
                    check["reason"] = "outside_validity_window"
                    return check
        check["satisfied"] = True
        check["reason"] = "ok"
        return check

    def evaluate_exhibition(self, *, actor_id: str, work_id: str, purpose: str,
                            start_at: str, end_at: str) -> dict[str, Any]:
        """只读地沿谱系计算展出所需许可，并列出每个阻断来源。"""

        with self.database.transaction() as connection:
            self.base._actor(connection, actor_id)
            work_id = self.base._identifier(work_id, "work_id")
            target = self._work(connection, work_id)
            if purpose not in SCOPES:
                raise ValidationError("purpose 必须是 public_display 或 commercial")
            window_start = self._dt(start_at, "start_at")
            window_end = self._dt(end_at, "end_at")
            if window_end <= window_start:
                raise ValidationError("end_at 必须晚于 start_at")
            lineage = self._lineage(connection, work_id)
            checks: list[dict[str, Any]] = []
            for ancestor in lineage:
                for contributor in self._contributors(connection, ancestor["work_id"]):
                    checks.append(self._check_requirement(
                        connection, work=ancestor, contributor=contributor, scope=purpose,
                        window_start=window_start, window_end=window_end))
            blockers = [check for check in checks if not check["satisfied"]]
            return {"work_id": work_id, "family_id": target["family_id"], "purpose": purpose,
                    "start_at": start_at, "end_at": end_at,
                    "lineage": [{"work_id": item["work_id"], "kind": item["kind"],
                                 "parent_id": item["parent_id"]} for item in lineage],
                    "allowed": not blockers, "checks": checks, "blockers": blockers}

    def approve_exhibition(self, *, request_id: str, actor_id: str, exhibition_id: str,
                           work_id: str, purpose: str, start_at: str, end_at: str) -> Any:
        """许可链完整时原子登记展出审批，否则带着具体阻断来源拒绝。"""

        payload = {"actor_id": actor_id, "exhibition_id": exhibition_id, "work_id": work_id,
                   "purpose": purpose, "start_at": start_at, "end_at": end_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self.base._actor(connection, actor_id)
            self.base._require(actor, *WRITE_ROLES)
            exhibition_id = self.base._identifier(exhibition_id, "exhibition_id")
            work_id = self.base._identifier(work_id, "work_id")
            # 重复提交必须回放既有决定：即使授权此后被撤销，也不能把已生效的
            # 原子批准重新评估成阻断。
            replayed = self.base._find_receipt(connection, request_id=request_id,
                                               action="approve_exhibition", payload=payload)
            if replayed is not None:
                return replayed
            if purpose not in SCOPES:
                raise ValidationError("purpose 必须是 public_display 或 commercial")
            window_start = self._dt(start_at, "start_at")
            window_end = self._dt(end_at, "end_at")
            if window_end <= window_start:
                raise ValidationError("end_at 必须晚于 start_at")
            target = self._work(connection, work_id)
            lineage = self._lineage(connection, work_id)
            checks = []
            for ancestor in lineage:
                for contributor in self._contributors(connection, ancestor["work_id"]):
                    checks.append(self._check_requirement(
                        connection, work=ancestor, contributor=contributor, scope=purpose,
                        window_start=window_start, window_end=window_end))
            blockers = [check for check in checks if not check["satisfied"]]
            if blockers:
                raise LicenseConflict("展出许可链不完整", blockers)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO exhibitions(exhibition_id,work_id,purpose,start_at,end_at,status,"
                        "decided_by,result_json,created_at) VALUES(?,?,?,?,?, 'approved',?,?,?)",
                        (exhibition_id, work_id, purpose, start_at, end_at, actor_id,
                         canonical_json({"lineage": [item["work_id"] for item in lineage],
                                         "checks": checks, "decided_at": self._now()}),
                         self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("展出编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="exhibition.approved",
                             resource_type="exhibition", resource_id=exhibition_id,
                             detail={"work_id": work_id, "family_id": target["family_id"],
                                     "purpose": purpose, "start_at": start_at, "end_at": end_at,
                                     "lineage": [item["work_id"] for item in lineage]},
                             occurred_at=self._now())
                return "exhibition", exhibition_id, {"exhibition_id": exhibition_id, "status": "approved"}

            return self.base._idempotent(connection, request_id=request_id, action="approve_exhibition",
                                         payload=payload, create=create)

    # ------------------------------------------------------------------ 纠错替代

    def supersede(self, *, request_id: str, actor_id: str, replacement_work_id: str,
                  superseded_work_id: str, reason: str) -> Any:
        payload = {"actor_id": actor_id, "replacement_work_id": replacement_work_id,
                   "superseded_work_id": superseded_work_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self.base._actor(connection, actor_id)
            self.base._require(actor, *CONTENT_ROLES)
            replacement_work_id = self.base._identifier(replacement_work_id, "replacement_work_id")
            superseded_work_id = self.base._identifier(superseded_work_id, "superseded_work_id")
            reason = self.base._text(reason, "reason", 1000)
            if replacement_work_id == superseded_work_id:
                raise ValidationError("更正版不能替代自身")
            replacement = self._work(connection, replacement_work_id)
            superseded = self._work(connection, superseded_work_id)
            if replacement["kind"] != "correction":
                raise ValidationError("只有更正版可以建立替代关系")
            if replacement["family_id"] != superseded["family_id"]:
                raise ValidationError("只能替代同一共创谱系内的版本")
            ancestor_ids = {item["work_id"] for item in self._lineage(connection, replacement_work_id)}
            if superseded_work_id not in ancestor_ids:
                raise ValidationError("更正版必须派生自被替代版本所在的谱系")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO supersessions(replacement_work_id,superseded_work_id,reason,"
                        "created_by,created_at) VALUES(?,?,?,?,?)",
                        (replacement_work_id, superseded_work_id, reason, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该替代关系已经存在") from exc
                append_event(connection, actor_id=actor_id, action="work.superseded",
                             resource_type="work", resource_id=superseded_work_id,
                             detail={"replacement_work_id": replacement_work_id, "reason": reason},
                             occurred_at=self._now())
                return ("supersession", f"{replacement_work_id}:{superseded_work_id}",
                        {"replacement_work_id": replacement_work_id,
                         "superseded_work_id": superseded_work_id})

            return self.base._idempotent(connection, request_id=request_id, action="supersede",
                                         payload=payload, create=create)

    # ------------------------------------------------------------------ 查询与审计

    def get_work(self, work_id: str) -> dict[str, Any]:
        row = self.database.connection.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"作品版本不存在: {work_id}")
        work = dict(row)
        work["summary"] = json.loads(work.pop("summary_json"))
        return work

    def list_family(self, family_id: str) -> list[dict[str, Any]]:
        family_id = self.base._identifier(family_id, "family_id")
        rows = self.database.connection.execute(
            "SELECT work_id,family_id,kind,title,summary_hash,parent_id,change_note,registered_by,created_at "
            "FROM works WHERE family_id=? ORDER BY created_at, work_id", (family_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_exhibitions(self, work_id: str) -> list[dict[str, Any]]:
        work_id = self.base._identifier(work_id, "work_id")
        rows = self.database.connection.execute(
            "SELECT exhibition_id,work_id,purpose,start_at,end_at,status,decided_by,result_json,created_at "
            "FROM exhibitions WHERE work_id=? ORDER BY created_at", (work_id,)).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["result"] = json.loads(item.pop("result_json"))
        return items

    def _current_availability(self, connection, lineage: list[dict[str, Any]]) -> dict[str, Any]:
        now = self.clock.now().astimezone(timezone.utc)
        availability: dict[str, Any] = {}
        for purpose in sorted(SCOPES):
            checks = []
            for work in lineage:
                for contributor in self._contributors(connection, work["work_id"]):
                    checks.append(self._check_requirement(
                        connection, work=work, contributor=contributor, scope=purpose,
                        window_start=now, window_end=now))
            blockers = [check for check in checks if not check["satisfied"]]
            availability[purpose] = {"usable": not blockers, "blockers": blockers}
        return availability

    def audit_version(self, *, actor_id: str, work_id: str) -> dict[str, Any]:
        """还原任一展示版本的贡献者、许可链、替代历史与当前可用范围。"""

        with self.database.transaction() as connection:
            self.base._actor(connection, actor_id)
            work_id = self.base._identifier(work_id, "work_id")
            work = self._work(connection, work_id)
            lineage = self._lineage(connection, work_id)
            lineage_ids = [item["work_id"] for item in lineage]
            supersession_rows = connection.execute(
                "SELECT ss.replacement_work_id, ss.superseded_work_id, ss.reason, ss.created_by, ss.created_at, "
                "w1.kind AS replacement_kind, w2.kind AS superseded_kind "
                "FROM supersessions ss "
                "JOIN works w1 ON w1.work_id=ss.replacement_work_id "
                "JOIN works w2 ON w2.work_id=ss.superseded_work_id "
                "WHERE w1.family_id=? ORDER BY ss.created_at", (work["family_id"],),
            ).fetchall()
            family_edge_set = [dict(row) for row in supersession_rows]
            lineage_set = set(lineage_ids)
            result = {
                "work": {
                    "work_id": work["work_id"], "family_id": work["family_id"], "kind": work["kind"],
                    "title": work["title"], "summary": json.loads(work["summary_json"]),
                    "summary_hash": work["summary_hash"], "parent_id": work["parent_id"],
                    "change_note": work["change_note"], "registered_by": work["registered_by"],
                    "created_at": work["created_at"],
                },
                "lineage": [
                    {"work_id": item["work_id"], "kind": item["kind"], "title": item["title"],
                     "parent_id": item["parent_id"], "summary_hash": item["summary_hash"]}
                    for item in lineage
                ],
                "contributors": self._contributors(connection, work_id),
                "lineage_contributors": {
                    item["work_id"]: self._contributors(connection, item["work_id"]) for item in lineage
                },
                "license_chain": self._authorizations(connection, lineage_ids),
                "supersession": {
                    "replaces": [edge for edge in family_edge_set
                                 if edge["replacement_work_id"] in lineage_set],
                    "replaced_by": [edge for edge in family_edge_set
                                    if edge["superseded_work_id"] in lineage_set],
                    "family_edges": family_edge_set,
                    "is_current": not any(edge["superseded_work_id"] == work_id
                                          for edge in family_edge_set),
                },
                "exhibitions": [
                    {"exhibition_id": row["exhibition_id"], "purpose": row["purpose"],
                     "start_at": row["start_at"], "end_at": row["end_at"], "status": row["status"],
                     "decided_by": row["decided_by"], "created_at": row["created_at"]}
                    for row in connection.execute(
                        "SELECT * FROM exhibitions WHERE work_id=? ORDER BY created_at", (work_id,)).fetchall()
                ],
            }
            result["current_availability"] = self._current_availability(connection, lineage)
            return result
