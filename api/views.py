import datetime
from collections import defaultdict

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from .models import AuditLog, Comment, Document, DocumentVersion, Tag, User, Workspace, WorkspaceMember
from .serializers import (
    AuditLogSerializer, CommentSerializer, DocumentSerializer, DocumentVersionSerializer,
    TagSerializer, UserSerializer, WorkspaceMemberSerializer, WorkspaceSerializer, parse_uuid,
)


class SimulatedFailure(Exception):
    """Raised on purpose (DEBUG only) to demonstrate that a transaction rolls back."""


def error(message, code):
    return Response({"error": message}, status=code)


def parse_date(value, field_name):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise serializers.ValidationError({field_name: "Use the format YYYY-MM-DD."})


# --------------------------------------------------------------------------- users
class UserViewSet(ModelViewSet):
    queryset = User.objects.all().order_by("created_at")
    serializer_class = UserSerializer
    http_method_names = ["get", "post", "head", "options"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return error("A user with this email already exists.", status.HTTP_409_CONFLICT)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------- workspaces
class WorkspaceViewSet(ModelViewSet):
    serializer_class = WorkspaceSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return (
            Workspace.objects.select_related("owner")
            .annotate(annotated_member_count=Count("members"))
            .order_by("-created_at")
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        simulate = settings.DEBUG and request.query_params.get("simulate_failure") == "true"
        try:
            # Workspace + admin membership + audit log succeed together or not at all
            with transaction.atomic():
                workspace = serializer.save()
                WorkspaceMember.objects.create(
                    workspace=workspace, user=workspace.owner, role=WorkspaceMember.Role.ADMIN
                )
                AuditLog.objects.create(
                    actor=workspace.owner, action=AuditLog.Action.CREATED,
                    model_name="Workspace", object_id=workspace.pk,
                )
                if simulate:
                    raise SimulatedFailure()
        except SimulatedFailure:
            return error("Simulated failure: the whole transaction was rolled back, nothing was saved.",
                         status.HTTP_500_INTERNAL_SERVER_ERROR)
        except IntegrityError:
            return error("Could not create the workspace because of a data conflict.", status.HTTP_409_CONFLICT)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"], url_path="members")
    def members(self, request, pk=None):
        workspace = self.get_object()

        if request.method == "GET":
            members = workspace.members.select_related("user").order_by("joined_at")
            return Response(WorkspaceMemberSerializer(members, many=True).data)

        user_id = parse_uuid(request.data.get("user_id"), "user_id")
        role = request.data.get("role", WorkspaceMember.Role.VIEWER)
        if role not in WorkspaceMember.Role.values:
            return error(f"Invalid role. Choose one of: {', '.join(WorkspaceMember.Role.values)}.",
                         status.HTTP_400_BAD_REQUEST)
        if not workspace.is_active:
            return error("This workspace is inactive.", status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return error("User not found.", status.HTTP_404_NOT_FOUND)

        try:
            with transaction.atomic():
                member = WorkspaceMember.objects.create(workspace=workspace, user=user, role=role)
                AuditLog.objects.create(
                    actor=None, action=AuditLog.Action.MEMBER_ADDED,
                    model_name="WorkspaceMember", object_id=member.pk,
                )
        except IntegrityError:
            return error("This user is already a member of the workspace.", status.HTTP_409_CONFLICT)
        member = WorkspaceMember.objects.select_related("user").get(pk=member.pk)
        return Response(WorkspaceMemberSerializer(member).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="summary")
    def summary(self, request, pk=None):
        workspace = self.get_object()
        documents = Document.objects.filter(workspace=workspace).aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(status=Document.Status.DRAFT)),
            published=Count("id", filter=Q(status=Document.Status.PUBLISHED)),
            archived=Count("id", filter=Q(status=Document.Status.ARCHIVED)),
        )
        members = workspace.members.aggregate(
            total=Count("id"),
            admins=Count("id", filter=Q(role=WorkspaceMember.Role.ADMIN)),
            editors=Count("id", filter=Q(role=WorkspaceMember.Role.EDITOR)),
            viewers=Count("id", filter=Q(role=WorkspaceMember.Role.VIEWER)),
        )
        document_ids = Document.objects.filter(workspace=workspace).values_list("id", flat=True)
        total_comments = Comment.objects.filter(document_id__in=document_ids).count()
        return Response({
            "workspace": workspace.name,
            "document_count": documents["total"],
            "documents_by_status": {k: documents[k] for k in ("draft", "published", "archived")},
            "member_count": members["total"],
            "members_by_role": {k: members[k] for k in ("admins", "editors", "viewers")},
            "total_comments": total_comments,
        })


# ---------------------------------------------------------------------- documents
class DocumentViewSet(ModelViewSet):
    serializer_class = DocumentSerializer
    http_method_names = ["get", "post", "put", "head", "options"]

    def get_queryset(self):
        qs = (
            Document.objects.select_related("workspace", "created_by")
            .prefetch_related("tags")
            .annotate(annotated_version_count=Count("versions", distinct=True))
        )
        if self.action != "list":
            return qs

        params = self.request.query_params
        if params.get("workspace"):
            qs = qs.filter(workspace_id=parse_uuid(params["workspace"], "workspace"))
        if params.get("status"):
            statuses = [s.strip() for s in params["status"].split(",") if s.strip()]
            invalid = [s for s in statuses if s not in Document.Status.values]
            if invalid:
                raise serializers.ValidationError({"status": f"Invalid status: {', '.join(invalid)}."})
            qs = qs.filter(status__in=statuses)
        if params.get("tag"):
            qs = qs.filter(tags__name=params["tag"].strip().lower())
        if params.get("search"):
            term = params["search"].strip()
            qs = qs.filter(Q(title__icontains=term) | Q(content__icontains=term))
        if params.get("created_after"):
            qs = qs.filter(created_at__date__gte=parse_date(params["created_after"], "created_after"))
        if params.get("created_before"):
            qs = qs.filter(created_at__date__lte=parse_date(params["created_before"], "created_before"))
        return qs.distinct().order_by("-created_at")

    def _respond(self, document_id, code=status.HTTP_200_OK):
        document = self.get_queryset().get(pk=document_id)
        return Response(DocumentSerializer(document).data, status=code)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        author = serializer.validated_data["created_by"]
        # Document + first version (+ the audit log written by the signal) are one unit
        with transaction.atomic():
            document = serializer.save()
            DocumentVersion.objects.create(
                document=document, version_number=document.versions.count() + 1,
                title=document.title, content=document.content, saved_by=author,
            )
        return self._respond(document.pk, status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        actor = data.get("saved_by") or data.get("created_by") or instance.created_by
        with transaction.atomic():
            # Lock the row so two simultaneous saves can't get the same version number
            locked = Document.objects.select_for_update().get(pk=instance.pk)
            instance._actor = actor  # read by the post_save signal for the audit log
            document = serializer.save()
            DocumentVersion.objects.create(
                document=document, version_number=locked.versions.count() + 1,
                title=document.title, content=document.content, saved_by=actor,
            )
        return self._respond(document.pk)

    @action(detail=True, methods=["get"], url_path="versions")
    def versions(self, request, pk=None):
        document = self.get_object()
        versions = document.versions.select_related("saved_by")
        return Response(DocumentVersionSerializer(versions, many=True).data)

    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, pk=None):
        try:
            document = Document.objects.annotate(
                version_count=Count("versions", distinct=True),
                comment_count=Count("comments", distinct=True),
                contributor_count=Count("versions__saved_by", distinct=True),
            ).get(pk=pk)
        except (Document.DoesNotExist, DjangoValidationError):
            return error("Document not found.", status.HTTP_404_NOT_FOUND)
        return Response({
            "document": document.title,
            "version_count": document.version_count,
            "comment_count": document.comment_count,
            "contributor_count": document.contributor_count,
        })

    @action(detail=True, methods=["post"], url_path="tags")
    def tags(self, request, pk=None):
        document = self.get_object()
        tag_ids = request.data.get("tag_ids")
        if not isinstance(tag_ids, list) or not tag_ids:
            return error("tag_ids must be a non-empty list of tag ids.", status.HTTP_400_BAD_REQUEST)
        wanted = {parse_uuid(t, "tag_ids") for t in tag_ids}
        found = set(Tag.objects.filter(id__in=wanted).values_list("id", flat=True))
        if wanted - found:
            missing = ", ".join(str(t) for t in wanted - found)
            return error(f"Tag(s) not found: {missing}", status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            document.tags.add(*found)
            AuditLog.objects.create(
                actor=None, action=AuditLog.Action.TAGGED, model_name="Document", object_id=document.pk,
            )
        return self._respond(document.pk)


# ----------------------------------------------------------------------- comments
class CommentViewSet(ModelViewSet):
    serializer_class = CommentSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return Comment.objects.select_related("author", "document__workspace")

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        document_id = request.query_params.get("document")
        if document_id:
            document_id = parse_uuid(document_id, "document")
            if not Document.objects.filter(pk=document_id).exists():
                return error("Document not found.", status.HTTP_404_NOT_FOUND)
            queryset = queryset.filter(document_id=document_id)

        # One query for every comment, then nest replies under their parent in Python
        comments = list(queryset)
        children = defaultdict(list)
        for comment in comments:
            if comment.parent_id:
                children[comment.parent_id].append(comment)
        top_level = [c for c in comments if c.parent_id is None]
        context = {**self.get_serializer_context(), "children": children}
        return Response(CommentSerializer(top_level, many=True, context=context).data)


# --------------------------------------------------------------------------- tags
class TagViewSet(ModelViewSet):
    queryset = Tag.objects.all().order_by("name")
    serializer_class = TagSerializer
    http_method_names = ["get", "post", "head", "options"]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return error(f"Tag '{serializer.validated_data['name']}' already exists.", status.HTTP_409_CONFLICT)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


# ------------------------------------------------------------------------ audit logs
class AuditLogViewSet(ModelViewSet):
    serializer_class = AuditLogSerializer
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor")
        params = self.request.query_params
        if params.get("actor"):
            qs = qs.filter(actor_id=parse_uuid(params["actor"], "actor"))
        if params.get("action"):
            actions = [a.strip() for a in params["action"].split(",") if a.strip()]
            qs = qs.filter(action__in=actions)
        if params.get("model_name"):
            qs = qs.filter(model_name__iexact=params["model_name"])
        if params.get("from"):
            qs = qs.filter(created_at__date__gte=parse_date(params["from"], "from"))
        if params.get("to"):
            qs = qs.filter(created_at__date__lte=parse_date(params["to"], "to"))
        return qs.order_by("-created_at")
