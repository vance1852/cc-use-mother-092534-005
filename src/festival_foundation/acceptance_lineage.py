"""运行共创谱系与授权后台的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .lineage import CoCreationService
from .storage import Database

FULL = ["public_display", "reuse", "derivative", "commercial"]
NON_COMMERCIAL = ["public_display", "reuse", "derivative"]
WIN_START = "2026-10-01T09:00:00+08:00"
WIN_END = "2026-10-05T18:00:00+08:00"
LATER_START = "2026-11-01T09:00:00+08:00"
LATER_END = "2026-11-05T18:00:00+08:00"


def run() -> dict[str, object]:
    """执行完整的谱系登记、双重确认、许可计算与纠错流程。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "lineage_acceptance.sqlite3")
        service = CoCreationService(
            database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        )

        # 组织与角色（复用基础层底座；新增 guardian 监护人角色）
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-lantern", name="花灯保护中心")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                               display_name="管理员", role="admin", organization_id="org-lantern")
        service.register_actor(request_id="operator", actor_id="admin", new_actor_id="operator",
                               display_name="运营员", role="operator", organization_id="org-lantern")
        service.register_actor(request_id="guardian", actor_id="admin", new_actor_id="guardian",
                               display_name="学员监护人", role="guardian",
                               organization_id="org-lantern")
        service.register_actor(request_id="inheritor", actor_id="admin", new_actor_id="inheritor",
                               display_name="花灯传承人", role="reviewer",
                               organization_id="org-lantern")
        service.register_actor(request_id="minor", actor_id="admin", new_actor_id="minor",
                               display_name="未成年学员", role="reviewer",
                               organization_id="org-lantern")

        # 作品族：草图 → 结构方案 → 公开展示版
        service.register_design(request_id="design", actor_id="operator",
                                design_id="lotus-001", title="莲花灯共创")
        service.register_version(
            request_id="v1", actor_id="operator", version_id="v1", design_id="lotus-001",
            kind="sketch", content_summary={"motif": "莲花", "palette": ["朱红", "明黄"]},
            sources=[{"source_key": "qing-lotus", "title": "清代莲花纹样", "origin": "馆藏灯谱"}],
            contributors=[{"contributor_actor_id": "inheritor", "contribution_kind": "传统纹样"},
                          {"contributor_actor_id": "minor", "contribution_kind": "配色草图"}])
        service.register_version(
            request_id="v2", actor_id="operator", version_id="v2", design_id="lotus-001",
            kind="structure", parent_version_id="v1", change_note="细化竹骨架与光源结构",
            content_summary={"frame": "竹篾六角", "light": "暖白 LED"},
            sources=[{"source_key": "qing-lotus", "title": "清代莲花纹样", "origin": "馆藏灯谱"}],
            contributors=[{"contributor_actor_id": "inheritor", "contribution_kind": "结构定稿"}])
        service.register_version(
            request_id="v3", actor_id="operator", version_id="v3", design_id="lotus-001",
            kind="public", parent_version_id="v2", change_note="形成中秋灯会公开展示版",
            content_summary={"motif": "莲花", "scale": "1.2m"},
            sources=[{"source_key": "qing-lotus", "title": "清代莲花纹样", "origin": "馆藏灯谱"}],
            contributors=[{"contributor_actor_id": "inheritor", "contribution_kind": "纹样定稿"},
                          {"contributor_actor_id": "minor", "contribution_kind": "公开展示配色"}])

        # 授权：传承人全范围（含商业）；未成年学员非商业且需双重确认
        service.submit_license(request_id="lic-i-v1", actor_id="inheritor", version_id="v1",
                               contributor_actor_id="inheritor", terms={"scopes": FULL})
        service.submit_license(request_id="lic-i-v2", actor_id="inheritor", version_id="v2",
                               contributor_actor_id="inheritor", terms={"scopes": FULL})
        service.submit_license(request_id="lic-i-v3", actor_id="inheritor", version_id="v3",
                               contributor_actor_id="inheritor", terms={"scopes": FULL})
        for req, version in [("lic-m-v1", "v1"), ("lic-m-v3", "v3")]:
            pending = service.submit_license(
                request_id=req, actor_id="operator", version_id=version,
                contributor_actor_id="minor", terms={"scopes": NON_COMMERCIAL}, is_minor=True,
                guardian_actor_id="guardian", institution_org_id="org-lantern")
            service.confirm_license(request_id=f"{req}-g", actor_id="guardian",
                                    grant_id=pending.resource_id, kind="guardian")
            service.confirm_license(request_id=f"{req}-s", actor_id="operator",
                                    grant_id=pending.resource_id, kind="institution")

        # 商业展出被未成年人非商业条款阻断；非商业展出通过
        service.submit_exhibition(
            request_id="ex-commercial", actor_id="operator", exhibition_id="ex-commercial",
            version_id="v3", purpose="商业巡展", commercial=True,
            start_at=WIN_START, end_at=WIN_END)
        commercial = service.get_exhibition("ex-commercial")
        service.submit_exhibition(
            request_id="ex-public", actor_id="operator", exhibition_id="ex-public",
            version_id="v3", purpose="中秋灯会公益展示", commercial=False,
            start_at=WIN_START, end_at=WIN_END)
        public = service.get_exhibition("ex-public")

        # 纠错：新增更正版并建立替代关系
        service.register_version(
            request_id="v4", actor_id="operator", version_id="v4", design_id="lotus-001",
            kind="correction", parent_version_id="v3", change_note="更正学员配色贡献的署名比例",
            content_summary={"motif": "莲花", "scale": "1.2m", "credit": "corrected"},
            sources=[{"source_key": "qing-lotus", "title": "清代莲花纹样", "origin": "馆藏灯谱"}],
            contributors=[{"contributor_actor_id": "inheritor", "contribution_kind": "纹样定稿"},
                          {"contributor_actor_id": "minor", "contribution_kind": "公开展展示配色"}])
        service.submit_license(request_id="lic-i-v4", actor_id="inheritor", version_id="v4",
                               contributor_actor_id="inheritor", terms={"scopes": FULL})
        pending_v4 = service.submit_license(
            request_id="lic-m-v4", actor_id="operator", version_id="v4",
            contributor_actor_id="minor", terms={"scopes": NON_COMMERCIAL}, is_minor=True,
            guardian_actor_id="guardian", institution_org_id="org-lantern")
        service.confirm_license(request_id="lic-m-v4-g", actor_id="guardian",
                                grant_id=pending_v4.resource_id, kind="guardian")
        service.confirm_license(request_id="lic-m-v4-s", actor_id="operator",
                                grant_id=pending_v4.resource_id, kind="institution")
        service.register_supersession(request_id="ss-v3-v4", actor_id="operator",
                                      old_version_id="v3", new_version_id="v4",
                                      reason="更正署名比例")

        # 对旧版的新展出应阻断并指向更正版；撤销只影响未来
        service.submit_exhibition(
            request_id="ex-old", actor_id="operator", exhibition_id="ex-old",
            version_id="v3", purpose="旧版展示", commercial=False,
            start_at=LATER_START, end_at=LATER_END)
        old = service.get_exhibition("ex-old")

        audit_v4 = service.get_version_audit("v4")
        audit_v3 = service.get_version_audit("v3")
        valid, event_count = service.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "commercial_exhibition": commercial["status"],
            "commercial_blocker": next(
                (b["type"] for b in commercial["blockers"]
                 if b["type"] == "commercial_restricted"), None),
            "public_exhibition": public["status"],
            "old_version_blocker": next(
                (b["type"] for b in old["blockers"] if b["type"] == "version_superseded"), None),
            "recommended_version": audit_v3["current_availability"]["recommended_version_id"],
            "lineage_order": audit_v4["lineage_order"],
            "supersessions": [(h["old_version_id"], h["new_version_id"])
                              for h in audit_v4["supersession_history"]],
            "current_scopes": audit_v4["current_availability"]["scopes"],
            "commercial_allowed": audit_v4["current_availability"]["commercial_allowed"],
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = (result["status"] == "ok" and result["audit_valid"]
                and result["commercial_exhibition"] == "blocked"
                and result["commercial_blocker"] == "commercial_restricted"
                and result["public_exhibition"] == "approved"
                and result["old_version_blocker"] == "version_superseded"
                and result["recommended_version"] == "v4"
                and result["lineage_order"] == ["v1", "v2", "v3", "v4"]
                and result["supersessions"] == [("v3", "v4")]
                and result["commercial_allowed"] is False)
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
