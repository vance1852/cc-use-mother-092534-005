import unittest
from datetime import datetime, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from festival_foundation.lineage import CoCreationService
from festival_foundation.storage import Database

WIN_START = "2026-10-01T09:00:00+08:00"
WIN_END = "2026-10-05T18:00:00+08:00"
PAST_START = "2026-08-01T09:00:00+08:00"
PAST_END = "2026-08-10T18:00:00+08:00"
NOV_START = "2026-11-01T09:00:00+08:00"
NOV_END = "2026-11-05T18:00:00+08:00"

FULL = ["public_display", "reuse", "derivative", "commercial"]
NON_COMMERCIAL = ["public_display", "reuse", "derivative"]


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CoCreationService(
            self.database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        )
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="花灯保护中心")
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="req-op", actor_id="a1", new_actor_id="op1",
                         display_name="运营员", role="operator", organization_id="o1")
        s.register_actor(request_id="req-guardian", actor_id="a1", new_actor_id="g1",
                         display_name="监护人", role="guardian", organization_id="o1")
        s.register_actor(request_id="req-au", actor_id="a1", new_actor_id="au1",
                         display_name="审计员", role="auditor", organization_id="o1")
        for req, actor_id, name in [
            ("req-i", "i1", "传承人"),
            ("req-m", "m1", "未成年学员"),
            ("req-t", "t1", "学校教师"),
        ]:
            s.register_actor(request_id=req, actor_id="a1", new_actor_id=actor_id,
                             display_name=name, role="reviewer", organization_id="o1")
        self.counter = 0

    def tearDown(self):
        self.database.close()

    def req(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def register_design(self, design_id="d1", title="莲花灯共创"):
        return self.service.register_design(request_id=self.req("design"), actor_id="op1",
                                            design_id=design_id, title=title)

    def add_version(self, version_id, kind, parent=None, note="", contributors=None,
                    sources=None, design_id="d1", summary=None, actor_id="op1"):
        return self.service.register_version(
            request_id=self.req("ver"), actor_id=actor_id, version_id=version_id,
            design_id=design_id, kind=kind,
            content_summary=summary or {"title": version_id, "detail": "..."},
            parent_version_id=parent, change_note=note,
            sources=sources if sources is not None else [
                {"source_key": "lotus-traditional", "title": "传统莲花纹样", "origin": "清代灯谱"}],
            contributors=contributors if contributors is not None else [
                {"contributor_actor_id": "i1", "contribution_kind": "纹样改编"}],
        )

    def grant(self, version_id, contributor, scopes, *, minor=False, guardian=None,
              institution=None, actor_id="op1", end_at=None):
        terms = {"scopes": scopes}
        if end_at:
            terms["end_at"] = end_at
        return self.service.submit_license(
            request_id=self.req("lic"), actor_id=actor_id, version_id=version_id,
            contributor_actor_id=contributor, terms=terms, is_minor=minor,
            guardian_actor_id=guardian, institution_org_id=institution,
        )

    def confirm(self, grant_id, kind, actor_id):
        return self.service.confirm_license(request_id=self.req("cfm"), actor_id=actor_id,
                                            grant_id=grant_id, kind=kind)

    def build_licensed_chain(self):
        """v1 草图（传承人+未成年学员）→ v2 结构（传承人+教师）→ v3 公开展示版。"""
        self.register_design()
        self.add_version("v1", "sketch",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"},
                                       {"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        self.add_version("v2", "structure", parent="v1", note="细化骨架与光源结构",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"},
                                       {"contributor_actor_id": "t1", "contribution_kind": "结构"}])
        self.add_version("v3", "public", parent="v2", note="形成公开展示版",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"},
                                       {"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        self.grant("v1", "i1", FULL)
        minor_v1 = self.grant("v1", "m1", NON_COMMERCIAL, minor=True,
                              guardian="g1", institution="o1")
        self.confirm(minor_v1.resource_id, "guardian", "g1")
        self.confirm(minor_v1.resource_id, "institution", "op1")
        self.grant("v2", "i1", FULL)
        self.grant("v2", "t1", NON_COMMERCIAL)
        self.grant("v3", "i1", FULL)
        minor_v3 = self.grant("v3", "m1", NON_COMMERCIAL, minor=True,
                              guardian="g1", institution="o1")
        self.confirm(minor_v3.resource_id, "guardian", "g1")
        self.confirm(minor_v3.resource_id, "institution", "op1")
        return "v3"

    def exhibit(self, version_id, exhibition_id, commercial=False,
                start=WIN_START, end=WIN_END, actor_id="op1", purpose="中秋灯会"):
        return self.service.submit_exhibition(
            request_id=self.req("exh"), actor_id=actor_id, exhibition_id=exhibition_id,
            version_id=version_id, purpose=purpose, commercial=commercial,
            start_at=start, end_at=end,
        )

    def exhibit_state(self, exhibition_id):
        return self.service.get_exhibition(exhibition_id)

    # ---------- 谱系规则 ----------

    def test_root_version_then_derivation_rules(self):
        self.register_design()
        self.add_version("v1", "sketch")
        with self.assertRaises(ValidationError):
            self.add_version("v2", "structure")  # 已存在版本，必须引用直接父版本
        with self.assertRaises(ValidationError):
            self.add_version("v2", "structure", parent="v1")  # 派生必须说明变化
        self.add_version("v2", "structure", parent="v1", note="调整骨架")
        audit = self.service.get_version_audit("v2")
        self.assertEqual(64, len(audit["version"]["content_digest"]))  # 内容摘要服务端哈希
        self.assertEqual(["v1", "v2"], audit["lineage_order"])
        self.assertEqual("lotus-traditional", audit["version"]["sources"][0]["source_key"])

    def test_parent_must_belong_to_same_design(self):
        self.register_design("d1")
        self.register_design("d2", title="另一件作品")
        self.add_version("v1", "sketch", design_id="d1")
        with self.assertRaises(ValidationError):
            self.add_version("w1", "sketch", design_id="d2", parent="v1", note="跨族引用")

    def test_unknown_parent_and_non_contributor_rejected(self):
        self.register_design()
        with self.assertRaises(NotFoundError):
            self.add_version("v1", "sketch", parent="nope", note="x")
        with self.assertRaises(NotFoundError):
            self.add_version("v1", "sketch",
                             contributors=[{"contributor_actor_id": "ghost",
                                            "contribution_kind": "x"}])

    def test_auditor_cannot_register_versions(self):
        self.register_design()
        with self.assertRaises(PermissionDenied):
            self.add_version("v1", "sketch", actor_id="au1")

    # ---------- 授权决定：去重与未成年人双确认 ----------

    def test_duplicate_signing_does_not_create_second_decision(self):
        self.register_design()
        self.add_version("v1", "sketch")
        first = self.grant("v1", "i1", FULL)
        again = self.grant("v1", "i1", FULL)
        self.assertEqual(first.resource_id, again.resource_id)
        grants = self.service.get_version_audit("v1")["lineage"][0]["grants"]
        self.assertEqual(1, len(grants))  # 重复签署没有制造第二份决定
        with self.assertRaises(ConflictError):
            self.grant("v1", "i1", NON_COMMERCIAL)  # 不同条款不能改写既有决定

    def test_minor_license_requires_both_confirmations_atomically(self):
        self.register_design()
        self.add_version("v1", "sketch",
                         contributors=[{"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        receipt = self.grant("v1", "m1", NON_COMMERCIAL, minor=True,
                             guardian="g1", institution="o1")
        grant_id = receipt.resource_id
        self.assertEqual("pending", self.grant_status("v1", "m1"))
        self.confirm(grant_id, "guardian", "g1")
        self.assertEqual("pending", self.grant_status("v1", "m1"))  # 单方确认不生效
        self.assertEqual(1, len(self.grants("v1", "m1")[0]["confirmations"]))
        self.confirm(grant_id, "institution", "op1")
        self.assertEqual("effective", self.grant_status("v1", "m1"))  # 双方到齐原子生效
        self.confirm(grant_id, "guardian", "g1")  # 重复确认
        self.assertEqual(2, len(self.grants("v1", "m1")[0]["confirmations"]))

    def test_wrong_guardian_and_other_institution_are_blocked(self):
        self.register_design()
        self.add_version("v1", "sketch",
                         contributors=[{"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        with self.assertRaises(ValidationError):
            self.grant("v1", "m1", NON_COMMERCIAL, minor=True, institution="o1")  # 缺监护人
        receipt = self.grant("v1", "m1", NON_COMMERCIAL, minor=True,
                             guardian="g1", institution="o1")
        with self.assertRaises(PermissionDenied):
            self.confirm(receipt.resource_id, "guardian", "op1")
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="其他机构")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="外机构运营", role="operator", organization_id="o2")
        with self.assertRaises(PermissionDenied):
            self.confirm(receipt.resource_id, "institution", "op2")
        self.assertEqual("pending", self.grant_status("v1", "m1"))

    def test_non_contributor_cannot_have_grant(self):
        self.register_design()
        self.add_version("v1", "sketch")  # 仅 i1 为贡献者
        with self.assertRaises(ValidationError):
            self.grant("v1", "t1", FULL)

    # ---------- 沿谱系计算展出许可 ----------

    def test_exhibition_blocked_until_lineage_licensed(self):
        self.register_design()
        self.add_version("v1", "sketch",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"}])
        self.add_version("v2", "public", parent="v1", note="展示版",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"}])
        self.exhibit("v2", "ex-missing")
        blockers = self.exhibit_state("ex-missing")["blockers"]
        self.assertTrue(any(b["type"] == "license_missing" and b["version_id"] == "v1"
                            and b["contributor_actor_id"] == "i1" for b in blockers))
        self.grant("v1", "i1", FULL)
        self.exhibit("v2", "ex-missing2")
        blockers2 = self.exhibit_state("ex-missing2")["blockers"]
        self.assertTrue(any(b["version_id"] == "v2" and b["type"] == "license_missing"
                            for b in blockers2))
        self.grant("v2", "i1", FULL)
        self.exhibit("v2", "ex-ok")
        self.assertEqual("approved", self.exhibit_state("ex-ok")["status"])

    def test_pending_minor_license_blocks_with_missing_confirmations(self):
        self.register_design()
        self.add_version("v1", "public",
                         contributors=[{"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        receipt = self.grant("v1", "m1", NON_COMMERCIAL, minor=True,
                             guardian="g1", institution="o1")
        self.exhibit("v1", "ex-pending")
        blocker = next(b for b in self.exhibit_state("ex-pending")["blockers"]
                       if b["type"] == "license_pending")
        self.assertEqual("m1", blocker["contributor_actor_id"])
        self.assertEqual({"guardian", "institution"}, set(blocker["missing_confirmations"]))
        self.confirm(receipt.resource_id, "guardian", "g1")
        self.confirm(receipt.resource_id, "institution", "op1")
        self.exhibit("v1", "ex-ok")
        self.assertEqual("approved", self.exhibit_state("ex-ok")["status"])

    def test_commercial_use_restricted_with_specific_blocker(self):
        target = self.build_licensed_chain()
        self.exhibit(target, "ex-nc", commercial=False)
        self.assertEqual("approved", self.exhibit_state("ex-nc")["status"])
        self.exhibit(target, "ex-c", commercial=True)
        blockers = self.exhibit_state("ex-c")["blockers"]
        pinpoint = {(b["version_id"], b["contributor_actor_id"])
                    for b in blockers if b["type"] == "commercial_restricted"}
        self.assertIn(("v1", "m1"), pinpoint)
        self.assertIn(("v2", "t1"), pinpoint)
        self.assertIn(("v3", "m1"), pinpoint)
        self.assertNotIn(("v1", "i1"), pinpoint)

    def test_approved_snapshot_survives_revocation_which_only_affects_future(self):
        target = self.build_licensed_chain()
        self.exhibit(target, "ex-future", commercial=False)
        before = self.exhibit_state("ex-future")
        self.assertEqual("approved", before["status"])
        self.assertEqual(6, len(before["permit_snapshot"]["permits"]))

        self.service.revoke_license(
            request_id=self.req("rev"), actor_id="i1",
            grant_id=self.grants("v3", "i1")[0]["grant_id"],
            reason="传承人撤回对后续使用的授权")
        # 已批准的合规展示不被追溯抹除：决定快照原样保留
        after = self.exhibit_state("ex-future")
        self.assertEqual("approved", after["status"])
        self.assertEqual(6, len(after["permit_snapshot"]["permits"]))
        # 撤销之后的新展出窗口被阻断，阻断来源指明撤销的版本与贡献者
        self.exhibit(target, "ex-after", commercial=False, start=NOV_START, end=NOV_END)
        blockers = self.exhibit_state("ex-after")["blockers"]
        self.assertTrue(any(b["type"] == "license_revoked" and b["version_id"] == "v3"
                            and b["contributor_actor_id"] == "i1" for b in blockers))
        # 撤销时点之前已完整结束的历史窗口不受撤销影响
        self.service.revoke_license(
            request_id=self.req("rev2"), actor_id="i1",
            grant_id=self.grants("v2", "i1")[0]["grant_id"], reason="仅影响未来")
        self.exhibit(target, "ex-past", commercial=False, start=PAST_START, end=PAST_END)
        past_blockers = self.exhibit_state("ex-past")["blockers"]
        self.assertFalse(any(b["type"] == "license_revoked" for b in past_blockers))

    def test_duplicate_revocation_is_one_decision(self):
        target = self.build_licensed_chain()
        grant_id = self.grants("v3", "i1")[0]["grant_id"]
        self.service.revoke_license(request_id="rev-one", actor_id="i1",
                                    grant_id=grant_id, reason="第一次撤销")
        self.service.revoke_license(request_id="rev-two", actor_id="i1",
                                    grant_id=grant_id, reason="再次撤销")
        events = [e for e in self.service.audit_events() if e["action"] == "license.revoked"
                  and e["resource_id"] == grant_id]
        self.assertEqual(1, len(events))

    def test_terms_expiry_blocks_window_beyond_end(self):
        self.register_design("d2", title="短期授权作品")
        self.add_version("x1", "sketch", design_id="d2",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"}])
        self.grant("x1", "i1", ["public_display"], end_at="2026-09-20T00:00:00Z")
        self.exhibit("x1", "ex-expired", start=WIN_START, end=WIN_END)
        expired = next(b for b in self.exhibit_state("ex-expired")["blockers"]
                       if b["type"] == "terms_expired")
        self.assertEqual("x1", expired["version_id"])
        self.assertEqual("i1", expired["contributor_actor_id"])
        # 窗口完全落在授权期限内则通过
        self.register_design("d3", title="长期授权作品")
        self.add_version("y1", "sketch", design_id="d3",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"}])
        self.grant("y1", "i1", ["public_display"], end_at="2026-12-31T00:00:00Z")
        self.exhibit("y1", "ex-covered", start=WIN_START, end=WIN_END)
        self.assertEqual("approved", self.exhibit_state("ex-covered")["status"])

    # ---------- 纠错：更正版与替代关系 ----------

    def test_correction_uses_new_version_and_supersession(self):
        target = self.build_licensed_chain()
        # 非更正版不能建立替代关系
        self.add_version("badver", "structure", parent=target, note="非更正版")
        with self.assertRaises(ValidationError):
            self.service.register_supersession(request_id=self.req("ss"), actor_id="op1",
                                               old_version_id=target, new_version_id="badver",
                                               reason="结构版不能替代")
        self.add_version("v4", "correction", parent=target, note="更正纹样署名比例",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"},
                                       {"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        with self.assertRaises(ValidationError):
            # 更正版必须直接派生自被替代版本
            self.service.register_supersession(request_id=self.req("ss"), actor_id="op1",
                                               old_version_id="v1", new_version_id="v4",
                                               reason="父子关系不匹配")
        self.service.register_supersession(request_id=self.req("ss"), actor_id="op1",
                                           old_version_id=target, new_version_id="v4",
                                           reason="署名比例更正")
        # 旧版本仍可读、内容不可变；审计提示已被替代并指向当前版本
        old_audit = self.service.get_version_audit(target)
        self.assertEqual(target, old_audit["version"]["version_id"])
        self.assertTrue(old_audit["current_availability"]["superseded"])
        self.assertEqual("v4", old_audit["current_availability"]["recommended_version_id"])
        # 针对旧版的新展出被阻断并指向当前版本
        self.exhibit(target, "ex-old")
        pointer = next(b for b in self.exhibit_state("ex-old")["blockers"]
                       if b["type"] == "version_superseded")
        self.assertEqual("v4", pointer["current_version_id"])
        # 不能继续从已被替代的版本派生
        with self.assertRaises(ConflictError):
            self.add_version("v5", "structure", parent=target, note="试图从旧版派生")
        # 纠错链不能分叉：同一旧版本不能重复建立替代关系
        with self.assertRaises(ConflictError):
            self.service.register_supersession(request_id=self.req("ss2"), actor_id="op1",
                                               old_version_id=target, new_version_id="v4",
                                               reason="重复替代")

    def test_supersession_history_and_availability_in_audit(self):
        target = self.build_licensed_chain()
        self.add_version("v4", "correction", parent=target, note="第一次更正",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"},
                                       {"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        self.grant("v4", "i1", FULL)
        m4 = self.grant("v4", "m1", NON_COMMERCIAL, minor=True, guardian="g1", institution="o1")
        self.confirm(m4.resource_id, "guardian", "g1")
        self.confirm(m4.resource_id, "institution", "op1")
        self.service.register_supersession(request_id=self.req("ss"), actor_id="op1",
                                           old_version_id=target, new_version_id="v4",
                                           reason="第一次更正")
        self.add_version("v5", "correction", parent="v4", note="第二次更正",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"},
                                       {"contributor_actor_id": "m1", "contribution_kind": "配色"}])
        self.grant("v5", "i1", FULL)
        m5 = self.grant("v5", "m1", NON_COMMERCIAL, minor=True, guardian="g1", institution="o1")
        self.confirm(m5.resource_id, "guardian", "g1")
        self.confirm(m5.resource_id, "institution", "op1")
        self.service.register_supersession(request_id=self.req("ss"), actor_id="op1",
                                           old_version_id="v4", new_version_id="v5",
                                           reason="第二次更正")
        audit = self.service.get_version_audit("v5")
        self.assertEqual([("v3", "v4"), ("v4", "v5")],
                         [(h["old_version_id"], h["new_version_id"])
                          for h in audit["supersession_history"]])
        self.assertEqual(["v1", "v2", "v3", "v4", "v5"], audit["lineage_order"])
        avail = audit["current_availability"]
        self.assertFalse(avail["commercial_allowed"])  # 未成年学员/教师未授 commercial
        self.assertNotIn("commercial", avail["scopes"])
        self.assertIn("public_display", avail["scopes"])
        self.assertEqual([], avail["gaps"])

    # ---------- 幂等与审计链 ----------

    def test_request_replays_are_idempotent(self):
        self.register_design()
        kwargs = dict(actor_id="op1", version_id="v1", design_id="d1", kind="sketch",
                      content_summary={"x": 1}, parent_version_id=None, change_note="",
                      sources=[], contributors=[{"contributor_actor_id": "i1",
                                                 "contribution_kind": "纹样"}])
        first = self.service.register_version(request_id="same-req", **kwargs)
        second = self.service.register_version(request_id="same-req", **kwargs)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    def test_exhibition_same_id_different_content_conflicts(self):
        self.register_design()
        self.add_version("v1", "public",
                         contributors=[{"contributor_actor_id": "i1", "contribution_kind": "纹样"}])
        self.grant("v1", "i1", FULL)
        self.exhibit("v1", "ex-same")
        with self.assertRaises(ConflictError):
            self.exhibit("v1", "ex-same", purpose="不同用途")

    def test_audit_chain_covers_new_actions_and_stays_valid(self):
        target = self.build_licensed_chain()
        self.exhibit(target, "ex1", commercial=False)
        actions = {event["action"] for event in self.service.audit_events()}
        for expected in ("design.registered", "version.registered", "license.submitted",
                         "license.confirmed", "license.effective", "exhibition.approved"):
            self.assertIn(expected, actions)
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)

    # ---------- 辅助 ----------

    def grants(self, version_id, contributor):
        audit = self.service.get_version_audit(version_id)
        entry = next(v for v in audit["lineage"] if v["version_id"] == version_id)
        return [g for g in entry["grants"] if g["contributor_actor_id"] == contributor]

    def grant_status(self, version_id, contributor):
        return self.grants(version_id, contributor)[0]["status"]


if __name__ == "__main__":
    unittest.main()
