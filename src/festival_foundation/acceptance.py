"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .genealogy import GenealogyService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        genealogy = GenealogyService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范服务机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="服务负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号服务站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="organization_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="organization_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 花灯共创谱系：来源纹样 -> 草图 -> 公开展示版 -> 色彩更正版。
        genealogy.register_subject(request_id="req-heritor", actor_id="operator-001",
                                   subject_id="heritor-001", kind="heritor", name="陈师傅")
        genealogy.register_subject(request_id="req-guardian", actor_id="operator-001",
                                   subject_id="guardian-001", kind="guardian", name="张家长")
        genealogy.register_subject(request_id="req-student", actor_id="operator-001",
                                   subject_id="student-001", kind="student",
                                   name="张同学", guardian_id="guardian-001")
        genealogy.register_subject(request_id="req-school", actor_id="operator-001",
                                   subject_id="school-001", kind="school",
                                   name="花灯小学", organization_id="org-001")
        source = genealogy.register_work(
            request_id="req-work-source", actor_id="operator-001", family_id="lantern-001",
            kind="source_pattern", title="传统莲花纹",
            summary={"motif": "莲花", "colors": ["红", "金"]},
            contributors=[{"subject_id": "heritor-001", "contribution_role": "原始纹样传承人"}]).resource_id
        sketch = genealogy.register_work(
            request_id="req-work-sketch", actor_id="operator-001", family_id="lantern-001",
            kind="sketch", title="共创草图",
            summary={"motif": "莲花鱼", "colors": ["红", "金", "蓝"]},
            contributors=[{"subject_id": "heritor-001", "contribution_role": "纹样指导"},
                          {"subject_id": "student-001", "contribution_role": "改编绘制"}],
            parent_id=source, change_note="加入游鱼").resource_id
        display = genealogy.register_work(
            request_id="req-work-display", actor_id="operator-001", family_id="lantern-001",
            kind="display", title="公开展示版",
            summary={"motif": "莲花鱼灯", "format": "灯面喷绘"},
            contributors=[{"subject_id": "school-001", "contribution_role": "展出制作"}],
            parent_id=sketch, change_note="转为灯面稿").resource_id
        genealogy.grant_authorization(request_id="req-auth-source-pub", actor_id="operator-001",
                                      work_id=source, subject_id="heritor-001",
                                      scope="public_display", decision="allow")
        # 传承人限制商业使用，该拒绝沿谱系持续生效且不可撤销。
        genealogy.grant_authorization(request_id="req-auth-source-com", actor_id="operator-001",
                                      work_id=source, subject_id="heritor-001",
                                      scope="commercial", decision="deny")
        genealogy.grant_authorization(request_id="req-auth-sketch-heritor", actor_id="operator-001",
                                      work_id=sketch, subject_id="heritor-001",
                                      scope="public_display", decision="allow")
        # 未成年人公开授权由监护人与机构分别确认，同事务原子生效。
        genealogy.grant_authorization(request_id="req-auth-sketch-student", actor_id="operator-001",
                                      work_id=sketch, subject_id="student-001",
                                      scope="public_display", decision="allow",
                                      guardian_confirmation={"guardian_subject_id": "guardian-001"},
                                      institution_confirmation={"organization_id": "org-001"})
        genealogy.grant_authorization(request_id="req-auth-display", actor_id="operator-001",
                                      work_id=display, subject_id="school-001",
                                      scope="public_display", decision="allow")
        window = {"start_at": "2026-10-01T09:00:00+08:00", "end_at": "2026-10-03T17:00:00+08:00"}
        public = genealogy.evaluate_exhibition(actor_id="operator-001", work_id=display,
                                               purpose="public_display", **window)
        commercial = genealogy.evaluate_exhibition(actor_id="operator-001", work_id=display,
                                                   purpose="commercial", **window)
        genealogy.approve_exhibition(request_id="req-exhibition", actor_id="operator-001",
                                     exhibition_id="exhibition-001", work_id=display,
                                     purpose="public_display", **window)
        # 纠错通过新增更正版与替代关系完成，不修改旧版。
        correction = genealogy.register_work(
            request_id="req-work-correction", actor_id="operator-001", family_id="lantern-001",
            kind="correction", title="色彩更正版",
            summary={"motif": "莲花鱼灯", "format": "灯面喷绘", "fix": "鱼色改青"},
            contributors=[{"subject_id": "school-001", "contribution_role": "勘误制作"}],
            parent_id=display, change_note="纠正游鱼颜色").resource_id
        genealogy.supersede(request_id="req-supersede", actor_id="operator-001",
                            replacement_work_id=correction, superseded_work_id=display,
                            reason="游鱼颜色与传统不符")
        audited = genealogy.audit_version(actor_id="operator-001", work_id=display)

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "public_allowed": public["allowed"],
                  "commercial_blocked": (not commercial["allowed"]
                                         and commercial["blockers"][0]["reason"] == "denied_by_rights_holder"),
                  "display_superseded": not audited["supersession"]["is_current"],
                  "correction_current": genealogy.audit_version(
                      actor_id="operator-001", work_id=correction)["supersession"]["is_current"],
                  "license_chain_length": len(audited["license_chain"])}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
