import uuid

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from .models import AuditLog, Comment, Document, DocumentVersion, Tag, User, Workspace, WorkspaceMember

EDIT_ROLES = (WorkspaceMember.Role.ADMIN, WorkspaceMember.Role.EDITOR)


def get_membership(workspace, user):
    return WorkspaceMember.objects.filter(workspace=workspace, user=user).first()


class UserSerializer(serializers.ModelSerializer):
    # Declared by hand so a duplicate email can return 409 (handled in the view)
    email = serializers.EmailField()

    class Meta:
        model = User
        fields = ["id", "name", "email", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Name must be at least 2 characters long.")
        return value

    def validate_email(self, value):
        return value.strip().lower()


class WorkspaceSerializer(serializers.ModelSerializer):
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = ["id", "name", "description", "owner", "is_active", "member_count", "created_at"]
        read_only_fields = ["id", "created_at", "is_active"]

    def get_member_count(self, obj):
        # The detail view annotates member_count; fall back to a query otherwise
        annotated = getattr(obj, "annotated_member_count", None)
        return annotated if annotated is not None else obj.members.count()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Workspace name cannot be blank.")
        return value


class WorkspaceMemberSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = WorkspaceMember
        fields = ["id", "workspace", "user", "user_name", "user_email", "role", "joined_at"]


class TagSerializer(serializers.ModelSerializer):
    # Declared by hand so a duplicate name returns 409 instead of a 400
    name = serializers.CharField(max_length=50)

    class Meta:
        model = Tag
        fields = ["id", "name"]
        read_only_fields = ["id"]

    def validate_name(self, value):
        value = value.strip().lower()
        if not value:
            raise serializers.ValidationError("Tag name cannot be blank.")
        return value


class DocumentSerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False)
    saved_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False, write_only=True)
    tags = serializers.SerializerMethodField()
    version_count = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id", "workspace", "title", "content", "status", "created_by", "saved_by",
            "tags", "version_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_tags(self, obj):
        return [tag.name for tag in obj.tags.all()]

    def get_version_count(self, obj):
        annotated = getattr(obj, "annotated_version_count", None)
        return annotated if annotated is not None else obj.versions.count()

    def create(self, validated_data):
        validated_data.pop("saved_by", None)  # not a model field
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("saved_by", None)
        validated_data.pop("created_by", None)  # the original author never changes
        return super().update(instance, validated_data)

    def validate_title(self, value):
        value = value.strip()
        if len(value) < 3:
            raise serializers.ValidationError("Title must be at least 3 characters long.")
        return value

    def validate(self, attrs):
        creating = self.instance is None
        workspace = attrs.get("workspace") or (self.instance.workspace if self.instance else None)
        if not creating and "workspace" in attrs and attrs["workspace"] != self.instance.workspace:
            raise serializers.ValidationError({"workspace": "A document cannot be moved to another workspace."})

        if creating:
            actor = attrs.get("created_by")
            if actor is None:
                raise serializers.ValidationError({"created_by": "This field is required."})
        else:
            actor = attrs.get("saved_by") or attrs.get("created_by") or self.instance.created_by

        if not workspace.is_active:
            raise serializers.ValidationError({"workspace": "This workspace is inactive."})

        membership = get_membership(workspace, actor)
        if membership is None:
            raise PermissionDenied("The user is not a member of this workspace.")
        if membership.role not in EDIT_ROLES:
            raise PermissionDenied("Viewers cannot create or edit documents.")
        return attrs


class DocumentVersionSerializer(serializers.ModelSerializer):
    saved_by_name = serializers.CharField(source="saved_by.name", read_only=True)

    class Meta:
        model = DocumentVersion
        fields = ["id", "document", "version_number", "title", "content", "saved_by", "saved_by_name", "created_at"]


class CommentSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.name", read_only=True)
    replies = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ["id", "document", "author", "author_name", "body", "parent", "replies", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_replies(self, obj):
        # The list view passes in a pre-built tree so replies cost no extra queries
        children = self.context.get("children", {})
        return CommentSerializer(children.get(obj.id, []), many=True, context=self.context).data

    def validate_body(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Comment body cannot be blank.")
        return value

    def validate(self, attrs):
        document, author, parent = attrs["document"], attrs["author"], attrs.get("parent")
        if parent is not None and parent.document_id != document.id:
            raise serializers.ValidationError({"parent": "The parent comment belongs to a different document."})
        if get_membership(document.workspace, author) is None:
            raise PermissionDenied("Only workspace members can comment.")
        return attrs


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "actor", "actor_name", "action", "model_name", "object_id", "created_at"]

    def get_actor_name(self, obj):
        return obj.actor.name if obj.actor else None


def parse_uuid(value, field_name):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise serializers.ValidationError({field_name: f"'{value}' is not a valid UUID."})
