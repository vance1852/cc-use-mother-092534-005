"""验证共创谱系、授权决定、展出许可计算、纠错替代与审计还原。"""

import unittest
from datetime import datetime, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.errors import (
    ConflictError,
    LicenseConflict,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from festival_foundation.genealogy import GenealogyService
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


class GenealogyTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 8, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.genealogy = GenealogyService(self.database, clock)
        self.service.register_organization(request_id="org-req", actor_id="bootstrap",
                                           organization_id="org1", name="非遗保护中心")
        self.service.register_actor(request_id="admin-req", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="系统管理员", role="admin", organization_id="org1")
        self.service.register_actor(request_id="op-req", actor_id="admin1", new_actor_id="op1",
                                    display_name="工坊负责人", role="operator", organization_id="org1")
        self.service.register_actor(request_id="au-req", actor_id="admin1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="org1")
        self.genealogy.register_subject(request_id="sub-heritor", actor_id="op1",
                                        subject_id="heritor1", kind="heritor", name="陈师傅")
        self.genealogy.register_subject(request_id="sub-guardian", actor_id="op1",
                                        subject_id="guard1", kind="guardian", name="张家长")
        self.genealogy.register_subject(request_id="sub-student", actor_id="op1", subject_id="stu1",
                                        kind="student", name="张同学", guardian_id="guard1")
        self.genealogy.register_subject(request_id="sub-school", actor_id="op1", subject_id="school1",
                                        kind="school", name="花灯小学", organization_id="org1")
        self.window = ("2026-10-01T09:00:00+08:00", "2026-10-03T17:00:00+08:00")

    def tearDown(self):
        self.database.close()

    def _source(self):
        return self.genealogy.register_work(
            request_id="work-src", actor_id="op1", family_id="fam1", kind="source_pattern",
            title="传统莲花纹", summary={"motif": "莲花", "colors": ["红", "金"]},
            contributors=[{"subject_id": "heritor1", "contribution_role": "原始纹样传承人"}]).resource_id

    def _sketch(self, parent):
        return self.genealogy.register_work(
            request_id="work-sketch", actor_id="op1", family_id="fam1", kind="sketch",
            title="共创草图", summary={"motif": "莲花鱼", "colors": ["红", "金", "蓝"]},
            contributors=[{"subject_id": "heritor1", "contribution_role": "纹样指导"},
                          {"subject_id": "stu1", "contribution_role": "改编绘制"}],
            parent_id=parent, change_note="加入游鱼").resource_id

    def _display(self, parent):
        return self.genealogy.register_work(
            request_id="work-display", actor_id="op1", family_id="fam1", kind="display",
            title="公开展示版", summary={"motif": "莲花鱼灯", "format": "灯面喷绘"},
            contributors=[{"subject_id": "school1", "contribution_role": "展出制作"}],
            parent_id=parent, change_note="转为灯面稿").resource_id

    def _authorize_chain(self, source, sketch, display):
        self.genealogy.grant_authorization(
            request_id="auth-src-pub", actor_id="op1", work_id=source, subject_id="heritor1",
            scope="public_display", decision="allow")
        self.genealogy.grant_authorization(
            request_id="auth-src-com", actor_id="op1", work_id=source, subject_id="heritor1",
            scope="commercial", decision="deny")
        self.genealogy.grant_authorization(
            request_id="auth-sketch-heritor", actor_id="op1", work_id=sketch, subject_id="heritor1",
            scope="public_display", decision="allow")
        self.genealogy.grant_authorization(
            request_id="auth-sketch-student", actor_id="op1", work_id=sketch, subject_id="stu1",
            scope="public_display", decision="allow",
            guardian_confirmation={"guardian_subject_id": "guard1"},
            institution_confirmation={"organization_id": "org1"})
        self.genealogy.grant_authorization(
            request_id="auth-display-school", actor_id="op1", work_id=display, subject_id="school1",
            scope="public_display", decision="allow")

    # ------------------------------------------------------------- 谱系登记

    def test_derived_version_must_cite_direct_parent(self):
        with self.assertRaises(ValidationError):
            self.genealogy.register_work(
                request_id="bad-derived", actor_id="op1", family_id="fam1", kind="sketch",
                title="无父草图", summary={"x": 1},
                contributors=[{"subject_id": "heritor1", "contribution_role": "绘制"}])

    def test_source_pattern_cannot_have_parent(self):
        source = self._source()
        with self.assertRaises(ValidationError):
            self.genealogy.register_work(
                request_id="bad-root", actor_id="op1", family_id="fam1", kind="source_pattern",
                title="第二条根", summary={"x": 1},
                contributors=[{"subject_id": "heritor1", "contribution_role": "绘制"}],
                parent_id=source)

    def test_parent_must_belong_to_same_family(self):
        source = self._source()
        with self.assertRaises(ValidationError):
            self.genealogy.register_work(
                request_id="cross-family", actor_id="op1", family_id="fam-other", kind="sketch",
                title="跨族派生", summary={"x": 1},
                contributors=[{"subject_id": "heritor1", "contribution_role": "绘制"}],
                parent_id=source, change_note="试图跨族")

    def test_identical_summary_is_rejected_as_duplicate_version(self):
        self._source()
        with self.assertRaises(ConflictError):
            self.genealogy.register_work(
                request_id="work-dup", actor_id="op1", family_id="fam1", kind="source_pattern",
                title="同样内容", summary={"motif": "莲花", "colors": ["红", "金"]},
                contributors=[{"subject_id": "heritor1", "contribution_role": "原始纹样传承人"}])

    def test_contributor_must_exist_and_not_repeat(self):
        with self.assertRaises(NotFoundError):
            self.genealogy.register_work(
                request_id="ghost", actor_id="op1", family_id="fam1", kind="source_pattern",
                title="幽灵贡献者", summary={"x": 1},
                contributors=[{"subject_id": "nobody", "contribution_role": "绘制"}])
        with self.assertRaises(ValidationError):
            self.genealogy.register_work(
                request_id="repeat-contrib", actor_id="op1", family_id="fam1", kind="source_pattern",
                title="重复贡献者", summary={"x": 1},
                contributors=[{"subject_id": "heritor1", "contribution_role": "绘制"},
                              {"subject_id": "heritor1", "contribution_role": "审核"}])

    def test_auditor_cannot_register_works(self):
        with self.assertRaises(PermissionDenied):
            self.genealogy.register_work(
                request_id="auditor-work", actor_id="au1", family_id="fam1", kind="source_pattern",
                title="审计员越权", summary={"x": 1},
                contributors=[{"subject_id": "heritor1", "contribution_role": "绘制"}])

    def test_student_subject_requires_guardian(self):
        with self.assertRaises(ValidationError):
            self.genealogy.register_subject(
                request_id="orphan-student", actor_id="op1", subject_id="stu2",
                kind="student", name="无监护学生")

    # ------------------------------------------------------------- 授权决定

    def test_minor_public_authorization_requires_both_confirmations(self):
        source = self._source()
        sketch = self._sketch(source)
        with self.assertRaises(ValidationError):
            self.genealogy.grant_authorization(
                request_id="only-guardian", actor_id="op1", work_id=sketch, subject_id="stu1",
                scope="public_display", decision="allow",
                guardian_confirmation={"guardian_subject_id": "guard1"})
        # 失败时授权与确认都不得残留，保证原子性。
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) FROM work_authorizations").fetchone()[0])
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) FROM authorization_confirmations").fetchone()[0])

    def test_guardian_confirmation_must_match_registered_guardian(self):
        self.genealogy.register_subject(request_id="other-guardian", actor_id="op1",
                                       subject_id="guard2", kind="guardian", name="李家长")
        source = self._source()
        sketch = self._sketch(source)
        with self.assertRaises(ValidationError):
            self.genealogy.grant_authorization(
                request_id="wrong-guardian", actor_id="op1", work_id=sketch, subject_id="stu1",
                scope="public_display", decision="allow",
                guardian_confirmation={"guardian_subject_id": "guard2"},
                institution_confirmation={"organization_id": "org1"})

    def test_minor_dual_confirmation_is_written_atomically(self):
        source = self._source()
        sketch = self._sketch(source)
        self.genealogy.grant_authorization(
            request_id="dual-ok", actor_id="op1", work_id=sketch, subject_id="stu1",
            scope="public_display", decision="allow",
            guardian_confirmation={"guardian_subject_id": "guard1"},
            institution_confirmation={"organization_id": "org1"})
        confirmations = self.database.connection.execute(
            "SELECT confirmer_type FROM authorization_confirmations ORDER BY confirmer_type"
        ).fetchall()
        self.assertEqual(["guardian", "institution"], [row[0] for row in confirmations])

    def test_repeated_signing_does_not_create_second_decision(self):
        source = self._source()
        first = self.genealogy.grant_authorization(
            request_id="sign-1", actor_id="op1", work_id=source, subject_id="heritor1",
            scope="public_display", decision="allow")
        second = self.genealogy.grant_authorization(
            request_id="sign-2", actor_id="op1", work_id=source, subject_id="heritor1",
            scope="public_display", decision="allow")
        self.assertEqual(first.resource_id, second.resource_id)
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM work_authorizations").fetchone()[0])
        with self.assertRaises(ConflictError):
            self.genealogy.grant_authorization(
                request_id="sign-3", actor_id="op1", work_id=source, subject_id="heritor1",
                scope="public_display", decision="deny")

    def test_authorization_only_for_contributors(self):
        source = self._source()
        with self.assertRaises(ValidationError):
            self.genealogy.grant_authorization(
                request_id="not-contrib", actor_id="op1", work_id=source, subject_id="school1",
                scope="public_display", decision="allow")

    # ------------------------------------------------------------- 许可计算

    def test_commercial_use_blocked_by_heritor_with_specific_source(self):
        source, sketch, display = self._source(), None, None
        sketch = self._sketch(source)
        display = self._display(sketch)
        self._authorize_chain(source, sketch, display)
        public = self.genealogy.evaluate_exhibition(
            actor_id="op1", work_id=display, purpose="public_display",
            start_at=self.window[0], end_at=self.window[1])
        self.assertTrue(public["allowed"])
        commercial = self.genealogy.evaluate_exhibition(
            actor_id="op1", work_id=display, purpose="commercial",
            start_at=self.window[0], end_at=self.window[1])
        self.assertFalse(commercial["allowed"])
        root_block = next(b for b in commercial["blockers"] if b["work_id"] == source)
        self.assertEqual("heritor1", root_block["subject_id"])
        self.assertEqual("denied_by_rights_holder", root_block["reason"])

    def test_approve_is_atomic_and_surfaces_blocker(self):
        source, sketch = self._source(), None
        sketch = self._sketch(source)
        display = self._display(sketch)
        # 缺少所有授权，批准必须失败且不写入展出记录。
        with self.assertRaises(LicenseConflict) as captured:
            self.genealogy.approve_exhibition(
                request_id="exh-fail", actor_id="op1", exhibition_id="exh-fail-id",
                work_id=display, purpose="public_display",
                start_at=self.window[0], end_at=self.window[1])
        self.assertTrue(captured.exception.blockers)
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) FROM exhibitions").fetchone()[0])
        self._authorize_chain(source, sketch, display)
        receipt = self.genealogy.approve_exhibition(
            request_id="exh-ok", actor_id="op1", exhibition_id="exh-ok-id", work_id=display,
            purpose="public_display", start_at=self.window[0], end_at=self.window[1])
        self.assertEqual("exh-ok-id", receipt.resource_id)

    def test_validity_window_blocks_use_outside_granted_range(self):
        source = self._source()
        self.genealogy.grant_authorization(
            request_id="windowed", actor_id="op1", work_id=source, subject_id="heritor1",
            scope="public_display", decision="allow",
            valid_from="2026-10-01T00:00:00+08:00", valid_until="2026-10-07T00:00:00+08:00")
        inside = self.genealogy.evaluate_exhibition(
            actor_id="op1", work_id=source, purpose="public_display",
            start_at="2026-10-02T09:00:00+08:00", end_at="2026-10-02T17:00:00+08:00")
        self.assertTrue(inside["allowed"])
        outside = self.genealogy.evaluate_exhibition(
            actor_id="op1", work_id=source, purpose="public_display",
            start_at="2026-10-08T09:00:00+08:00", end_at="2026-10-08T17:00:00+08:00")
        self.assertFalse(outside["allowed"])
        self.assertEqual("outside_validity_window", outside["blockers"][0]["reason"])

    def test_revocation_only_affects_future_use_but_keeps_compliant_history(self):
        source = self._source()
        auth = self.genealogy.grant_authorization(
            request_id="revokable", actor_id="op1", work_id=source, subject_id="heritor1",
            scope="public_display", decision="allow")
        self.genealogy.approve_exhibition(
            request_id="past-exh", actor_id="op1", exhibition_id="past-exh-id", work_id=source,
            purpose="public_display", start_at=self.window[0], end_at=self.window[1])
        self.genealogy.revoke_authorization(
            request_id="do-revoke", actor_id="op1", auth_id=auth.resource_id)
        # 已发生的合规展示仍然保留、可查，不被追溯抹除。
        self.assertEqual("approved", self.genealogy.list_exhibitions(source)[0]["status"])
        future = self.genealogy.evaluate_exhibition(
            actor_id="op1", work_id=source, purpose="public_display",
            start_at="2026-12-01T09:00:00+08:00", end_at="2026-12-02T17:00:00+08:00")
        self.assertFalse(future["allowed"])
        self.assertEqual("authorization_revoked", future["blockers"][0]["reason"])
        with self.assertRaises(LicenseConflict):
            self.genealogy.approve_exhibition(
                request_id="future-exh", actor_id="op1", exhibition_id="future-exh-id",
                work_id=source, purpose="public_display",
                start_at="2026-12-01T09:00:00+08:00", end_at="2026-12-02T17:00:00+08:00")
        # 撤销后用同一 request_id 重复提交，必须回放当时已原子生效的批准，
        # 而不是按当前（已撤销）状态重新评估。
        replay = self.genealogy.approve_exhibition(
            request_id="past-exh", actor_id="op1", exhibition_id="past-exh-id", work_id=source,
            purpose="public_display", start_at=self.window[0], end_at=self.window[1])
        self.assertTrue(replay.replayed)
        self.assertEqual("past-exh-id", replay.resource_id)

    def test_commercial_deny_cannot_be_revoked(self):
        source = self._source()
        deny = self.genealogy.grant_authorization(
            request_id="deny-com", actor_id="op1", work_id=source, subject_id="heritor1",
            scope="commercial", decision="deny")
        with self.assertRaises(ConflictError):
            self.genealogy.revoke_authorization(
                request_id="revoke-deny", actor_id="op1", auth_id=deny.resource_id)

    # ------------------------------------------------------------- 纠错与审计

    def test_correction_supersession_and_audit_restoration(self):
        source = self._source()
        sketch = self._sketch(source)
        display = self._display(sketch)
        self._authorize_chain(source, sketch, display)
        correction = self.genealogy.register_work(
            request_id="work-correction", actor_id="op1", family_id="fam1", kind="correction",
            title="色彩更正版", summary={"motif": "莲花鱼灯", "format": "灯面喷绘", "fix": "鱼色改青"},
            contributors=[{"subject_id": "school1", "contribution_role": "勘误制作"}],
            parent_id=display, change_note="纠正游鱼颜色")
        correction_id = correction.resource_id
        with self.assertRaises(ValidationError):
            # 非 correction 类型不能建立替代关系。
            self.genealogy.supersede(
                request_id="bad-supersede", actor_id="op1", replacement_work_id=sketch,
                superseded_work_id=display, reason="草图不能替代")
        self.genealogy.supersede(
            request_id="supersede-ok", actor_id="op1", replacement_work_id=correction_id,
            superseded_work_id=display, reason="游鱼颜色与传统不符")
        # 重复建立同一边是幂等回放，不产生第二条关系。
        replay = self.genealogy.supersede(
            request_id="supersede-ok", actor_id="op1", replacement_work_id=correction_id,
            superseded_work_id=display, reason="游鱼颜色与传统不符")
        self.assertTrue(replay.replayed)

        record = self.genealogy.audit_version(actor_id="au1", work_id=display)
        self.assertEqual([source, sketch, display], [item["work_id"] for item in record["lineage"]])
        contributor_ids = {c["subject_id"] for c in record["contributors"]}
        self.assertEqual({"school1"}, contributor_ids)
        all_lineage_contributors = {
            c["subject_id"] for contributors in record["lineage_contributors"].values()
            for c in contributors}
        self.assertEqual({"heritor1", "stu1", "school1"}, all_lineage_contributors)
        self.assertEqual(5, len(record["license_chain"]))
        self.assertFalse(record["supersession"]["is_current"])
        self.assertEqual(correction_id, record["supersession"]["replaced_by"][0]["replacement_work_id"])
        self.assertTrue(record["current_availability"]["public_display"]["usable"])
        self.assertFalse(record["current_availability"]["commercial"]["usable"])

        corrected = self.genealogy.audit_version(actor_id="au1", work_id=correction_id)
        self.assertTrue(corrected["supersession"]["is_current"])
        # 旧版仍然完整可查、内容哈希未被改动。
        self.assertEqual(display, record["work"]["work_id"])
        self.assertTrue(self.service.verify_audit()[0])

    def test_request_replay_returns_original_receipt(self):
        first = self._source()
        replay = self.genealogy.register_work(
            request_id="work-src", actor_id="op1", family_id="fam1", kind="source_pattern",
            title="传统莲花纹", summary={"motif": "莲花", "colors": ["红", "金"]},
            contributors=[{"subject_id": "heritor1", "contribution_role": "原始纹样传承人"}])
        self.assertTrue(replay.replayed)
        self.assertEqual(first, replay.resource_id)


if __name__ == "__main__":
    unittest.main()
