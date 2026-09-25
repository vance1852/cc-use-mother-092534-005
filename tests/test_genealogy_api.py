"""验证花灯谱系与授权 HTTP 路由的分派与冲突返回。"""

import unittest
from datetime import datetime, timezone

from festival_foundation.api import route
from festival_foundation.clock import FixedClock
from festival_foundation.genealogy import GenealogyService
from festival_foundation.service import DomainService
from festival_foundation.storage import Database


class GenealogyApiTest(unittest.TestCase):
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
        self.genealogy.register_subject(request_id="sub-h", actor_id="op1",
                                        subject_id="heritor1", kind="heritor", name="陈师傅")
        self.work = self.genealogy.register_work(
            request_id="work-src", actor_id="op1", family_id="fam1", kind="source_pattern",
            title="莲花纹", summary={"motif": "莲花"},
            contributors=[{"subject_id": "heritor1", "contribution_role": "传承人"}]).resource_id

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor}, self.genealogy)

    def test_register_subject_route(self):
        status, payload = self._route("POST", "/subjects", {
            "request_id": "sub-new", "subject_id": "school1", "kind": "school",
            "name": "花灯小学", "organization_id": "org1"})
        self.assertEqual(201, status)
        self.assertEqual("school1", payload["resource_id"])

    def test_register_work_route(self):
        status, payload = self._route("POST", "/works", {
            "request_id": "work-child", "family_id": "fam1", "kind": "sketch",
            "title": "草图", "summary": {"motif": "莲花鱼"},
            "contributors": [{"subject_id": "heritor1", "contribution_role": "改编"}],
            "parent_id": self.work, "change_note": "加入鱼"})
        self.assertEqual(201, status)

    def test_evaluation_reports_blockers(self):
        status, payload = self._route("POST", "/exhibitions/evaluate", {
            "work_id": self.work, "purpose": "public_display",
            "start_at": "2026-10-01T09:00:00+08:00", "end_at": "2026-10-02T09:00:00+08:00"})
        self.assertEqual(200, status)
        self.assertFalse(payload["allowed"])
        self.assertEqual("missing_authorization", payload["blockers"][0]["reason"])

    def test_approve_conflict_returns_specific_blocker(self):
        status, payload = self._route("POST", "/exhibitions", {
            "request_id": "exh1", "exhibition_id": "exh1", "work_id": self.work,
            "purpose": "public_display", "start_at": "2026-10-01T09:00:00+08:00",
            "end_at": "2026-10-02T09:00:00+08:00"})
        self.assertEqual(422, status)
        self.assertEqual("license_conflict", payload["error"])
        self.assertEqual("heritor1", payload["blockers"][0]["subject_id"])

    def test_audit_endpoint_restores_version(self):
        status, payload = self._route("GET", f"/works/audit/{self.work}")
        self.assertEqual(200, status)
        self.assertEqual(self.work, payload["work"]["work_id"])
        self.assertIn("license_chain", payload)
        self.assertIn("supersession", payload)
        self.assertIn("current_availability", payload)

    def test_family_listing(self):
        status, payload = self._route("GET", "/works?family_id=fam1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))


if __name__ == "__main__":
    unittest.main()
