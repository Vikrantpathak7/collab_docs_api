from rest_framework.routers import DefaultRouter

from .views import (
    AuditLogViewSet, CommentViewSet, DocumentViewSet, TagViewSet, UserViewSet, WorkspaceViewSet,
)

router = DefaultRouter(trailing_slash=True)
router.register("users", UserViewSet, basename="user")
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("documents", DocumentViewSet, basename="document")
router.register("comments", CommentViewSet, basename="comment")
router.register("tags", TagViewSet, basename="tag")
router.register("audit-logs", AuditLogViewSet, basename="auditlog")

urlpatterns = router.urls
