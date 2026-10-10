from rest_framework.test import APITestCase

from .models import AuditLog, Document, DocumentVersion, Workspace, WorkspaceMember


class CollabDocsAPITests(APITestCase):
    def setUp(self):
        self.owner = self.post("/api/users/", {"name": "Alice", "email": "alice@example.com"}).data
        self.bob = self.post("/api/users/", {"name": "Bob", "email": "bob@example.com"}).data
        self.ws = self.post("/api/workspaces/", {"name": "Team", "owner": self.owner["id"]}).data

    def post(self, url, data, **kw):
        return self.client.post(url, data, format="json", **kw)

    def make_doc(self, title="Design notes"):
        return self.post("/api/documents/", {
            "workspace": self.ws["id"], "title": title, "content": "v1", "created_by": self.owner["id"],
        })

    # users
    def test_duplicate_email_returns_409(self):
        r = self.post("/api/users/", {"name": "Alice 2", "email": "ALICE@example.com"})
        self.assertEqual(r.status_code, 409)

    def test_get_unknown_user_404(self):
        r = self.client.get("/api/users/00000000-0000-0000-0000-000000000000/")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.data)

    # workspaces
    def test_workspace_creation_adds_admin_member_and_audit_log(self):
        member = WorkspaceMember.objects.get(workspace_id=self.ws["id"])
        self.assertEqual(str(member.user_id), self.owner["id"])
        self.assertEqual(member.role, WorkspaceMember.Role.ADMIN)
        self.assertTrue(AuditLog.objects.filter(model_name="Workspace", object_id=self.ws["id"]).exists())
        self.assertEqual(self.ws["member_count"], 1)

    def test_simulated_failure_rolls_everything_back(self):
        with self.settings(DEBUG=True):
            r = self.post("/api/workspaces/?simulate_failure=true", {"name": "Ghost", "owner": self.owner["id"]})
        self.assertEqual(r.status_code, 500)
        self.assertFalse(Workspace.objects.filter(name="Ghost").exists())
        self.assertFalse(WorkspaceMember.objects.filter(workspace__name="Ghost").exists())

    def test_duplicate_member_returns_409(self):
        url = f"/api/workspaces/{self.ws['id']}/members/"
        self.assertEqual(self.post(url, {"user_id": self.bob["id"], "role": "editor"}).status_code, 201)
        r = self.post(url, {"user_id": self.bob["id"], "role": "viewer"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(WorkspaceMember.objects.filter(workspace_id=self.ws["id"]).count(), 2)

    def test_add_member_unknown_user_404_and_bad_role_400(self):
        url = f"/api/workspaces/{self.ws['id']}/members/"
        self.assertEqual(self.post(url, {"user_id": "00000000-0000-0000-0000-000000000000"}).status_code, 404)
        self.assertEqual(self.post(url, {"user_id": self.bob["id"], "role": "boss"}).status_code, 400)

    def test_summary(self):
        self.make_doc()
        r = self.client.get(f"/api/workspaces/{self.ws['id']}/summary/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["document_count"], 1)
        self.assertEqual(r.data["member_count"], 1)

    # documents
    def test_document_create_makes_version_and_audit_log(self):
        r = self.make_doc()
        self.assertEqual(r.status_code, 201)
        doc = Document.objects.get(pk=r.data["id"])
        self.assertEqual(doc.versions.count(), 1)
        log = AuditLog.objects.get(model_name="Document", object_id=doc.pk)
        self.assertEqual(log.action, "created")

    def test_document_update_creates_new_version_and_updated_log(self):
        doc_id = self.make_doc().data["id"]
        r = self.client.put(f"/api/documents/{doc_id}/", {
            "workspace": self.ws["id"], "title": "Design notes v2", "content": "changed", "status": "published",
        }, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["version_count"], 2)
        numbers = list(DocumentVersion.objects.filter(document_id=doc_id).values_list("version_number", flat=True))
        self.assertEqual(numbers, [1, 2])
        self.assertEqual(AuditLog.objects.filter(object_id=doc_id, action="updated").count(), 1)

    def test_viewer_cannot_create_document(self):
        self.post(f"/api/workspaces/{self.ws['id']}/members/", {"user_id": self.bob["id"], "role": "viewer"})
        r = self.post("/api/documents/", {
            "workspace": self.ws["id"], "title": "Nope", "content": "", "created_by": self.bob["id"],
        })
        self.assertEqual(r.status_code, 403)

    def test_short_title_400(self):
        r = self.post("/api/documents/", {
            "workspace": self.ws["id"], "title": "ab", "created_by": self.owner["id"],
        })
        self.assertEqual(r.status_code, 400)

    def test_filters_tags_stats_versions(self):
        doc_id = self.make_doc("Python guide").data["id"]
        self.make_doc("Other page")
        tag_id = self.post("/api/tags/", {"name": "Python"}).data["id"]
        self.assertEqual(self.post("/api/tags/", {"name": "python"}).status_code, 409)
        r = self.post(f"/api/documents/{doc_id}/tags/", {"tag_ids": [tag_id]})
        self.assertEqual(r.data["tags"], ["python"])
        self.assertEqual(self.post(f"/api/documents/{doc_id}/tags/", {"tag_ids": [str(__import__('uuid').uuid4())]}).status_code, 404)

        r = self.client.get("/api/documents/", {"tag": "python"})
        self.assertEqual([d["id"] for d in r.data], [doc_id])
        r = self.client.get("/api/documents/", {"search": "OTHER"})
        self.assertEqual(len(r.data), 1)
        r = self.client.get("/api/documents/", {"status": "draft,published"})
        self.assertEqual(len(r.data), 2)
        self.assertEqual(self.client.get("/api/documents/", {"status": "bogus"}).status_code, 400)

        self.assertEqual(len(self.client.get(f"/api/documents/{doc_id}/versions/").data), 1)
        stats = self.client.get(f"/api/documents/{doc_id}/stats/").data
        self.assertEqual((stats["version_count"], stats["comment_count"], stats["contributor_count"]), (1, 0, 1))
        self.assertEqual(self.client.get("/api/documents/not-a-uuid/stats/").status_code, 404)

    # comments
    def test_threaded_comments(self):
        doc_id = self.make_doc().data["id"]
        top = self.post("/api/comments/", {"document": doc_id, "author": self.owner["id"], "body": "Top"}).data
        reply = self.post("/api/comments/", {
            "document": doc_id, "author": self.owner["id"], "body": "Reply", "parent": top["id"]})
        self.assertEqual(reply.status_code, 201)
        r = self.client.get("/api/comments/", {"document": doc_id})
        self.assertEqual(len(r.data), 1)
        self.assertEqual(r.data[0]["replies"][0]["body"], "Reply")

    def test_non_member_cannot_comment(self):
        doc_id = self.make_doc().data["id"]
        r = self.post("/api/comments/", {"document": doc_id, "author": self.bob["id"], "body": "hi"})
        self.assertEqual(r.status_code, 403)

    # audit logs
    def test_audit_log_filters(self):
        self.make_doc()
        r = self.client.get("/api/audit-logs/", {"actor": self.owner["id"], "model_name": "Document"})
        self.assertEqual(len(r.data), 1)
        self.assertEqual(self.client.get("/api/audit-logs/", {"from": "2999-01-01"}).data, [])
        self.assertEqual(self.client.get("/api/audit-logs/", {"from": "bad"}).status_code, 400)
