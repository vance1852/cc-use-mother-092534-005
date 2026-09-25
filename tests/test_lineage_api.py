import unittest

from festival_foundation.api import route
from festival_foundation.lineage import CoCreationService
from festival_foundation.storage import Database


class LineageApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CoCreationService(self.database)
        r = route

        def call(method, path, body=None, actor=""):
            return r(self.service, method, path, body, {"X-Actor-Id": actor})

        self.call = call
        call("POST", "/organizations",
             {"request_id": "org01", "organization_id": "o1", "name": "花灯中心"}, "bootstrap")
        call("POST", "/actors",
             {"request_id": "admin01", "new_actor_id": "a1", "display_name": "管理员",
              "role": "admin", "organization_id": "o1"}, "bootstrap")
        call("POST", "/actors",
             {"request_id": "op01", "new_actor_id": "op1", "display_name": "运营",
              "role": "operator", "organization_id": "o1"}, "a1")
        call("POST", "/actors",
             {"request_id": "g01", "new_actor_id": "g1", "display_name": "监护人",
              "role": "guardian", "organization_id": "o1"}, "a1")
        call("POST", "/actors",
             {"request_id": "i01", "new_actor_id": "i1", "display_name": "传承人",
              "role": "reviewer", "organization_id": "o1"}, "a1")
        call("POST", "/actors",
             {"request_id": "m01", "new_actor_id": "m1", "display_name": "未成年学员",
              "role": "reviewer", "organization_id": "o1"}, "a1")

    def tearDown(self):
        self.database.close()

    def test_lineage_license_and_audit_endpoints(self):
        status, _ = self.call("POST", "/designs",
                              {"request_id": "d01", "design_id": "d1", "title": "莲花灯"}, "op1")
        self.assertEqual(201, status)
        status, body = self.call("POST", "/design-versions", {
            "request_id": "rv1", "version_id": "v1", "design_id": "d1", "kind": "sketch",
            "content_summary": {"motif": "莲花"},
            "contributors": [
                {"contributor_actor_id": "i1", "contribution_kind": "纹样"},
                {"contributor_actor_id": "m1", "contribution_kind": "配色"}],
        }, "op1")
        self.assertEqual(201, status)

        status, body = self.call("POST", "/licenses", {
            "request_id": "lic-i1", "version_id": "v1", "contributor_actor_id": "i1",
            "terms": {"scopes": ["public_display", "commercial"]}}, "op1")
        self.assertEqual(201, status)

        status, body = self.call("POST", "/licenses", {
            "request_id": "lic-m1", "version_id": "v1", "contributor_actor_id": "m1",
            "terms": {"scopes": ["public_display"]}, "is_minor": True,
            "guardian_actor_id": "g1", "institution_org_id": "o1"}, "op1")
        self.assertEqual(201, status)
        grant_id = body["resource_id"]

        # 仅监护人确认后展出仍阻断
        self.call("POST", "/license-confirmations",
                  {"request_id": "cfm-g1", "grant_id": grant_id, "kind": "guardian"}, "g1")
        self.call("POST", "/exhibitions", {
            "request_id": "rex1", "exhibition_id": "ex1", "version_id": "v1",
            "purpose": "灯会", "commercial": False,
            "start_at": "2026-10-01T09:00:00+08:00", "end_at": "2026-10-05T18:00:00+08:00"}, "op1")
        _, body = self.call("GET", "/exhibitions?exhibition_id=ex1")
        self.assertEqual("blocked", body["status"])
        self.assertEqual("license_pending", body["blockers"][0]["type"])

        # 机构确认后原子生效，重提展出通过
        self.call("POST", "/license-confirmations", {
            "request_id": "cfm-s1", "grant_id": grant_id, "kind": "institution"}, "op1")
        self.call("POST", "/exhibitions", {
            "request_id": "rex2", "exhibition_id": "ex2", "version_id": "v1",
            "purpose": "灯会", "commercial": False,
            "start_at": "2026-10-01T09:00:00+08:00", "end_at": "2026-10-05T18:00:00+08:00"}, "op1")
        _, body = self.call("GET", "/exhibitions?exhibition_id=ex2")
        self.assertEqual("approved", body["status"])

        status, body = self.call("GET", "/version-audit?version_id=v1")
        self.assertEqual(200, status)
        self.assertEqual(["v1"], body["lineage_order"])
        self.assertIn("public_display", body["current_availability"]["scopes"])

        status, body = self.call("GET", "/exhibitions?exhibition_id=ex2")
        self.assertEqual(200, status)
        self.assertEqual("approved", body["status"])

    def test_missing_query_params_are_400(self):
        status, body = self.call("GET", "/version-audit")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"])
        status, body = self.call("GET", "/exhibitions")
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
