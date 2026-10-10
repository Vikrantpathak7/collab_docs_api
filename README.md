# CollabDocs API

Backend API for CollabDocs, a simplified Notion / Google Docs: workspaces, role-based members,
versioned documents, threaded comments, tags and an audit log.
Built with Django, Django REST Framework and PostgreSQL. Postman is the client.

**Demo video:** https://drive.google.com/file/d/1imPq8sZAm64CZ19vmEhCc0JtGmcXl0Ac/view?usp=sharing

## Setup

1. **Environment file**
   ```
   cp .env.example .env        # Windows: copy .env.example .env
   ```
   Edit `.env` and set `SECRET_KEY` and the `POSTGRES_*` values. Nothing is hardcoded in `settings.py`.

2. **Database** (Postgres in Docker, port 5433)
   ```
   docker compose up -d
   ```
   Already have Postgres? Skip this and point the `POSTGRES_*` values in `.env` at it.

3. **Python environment**
   ```
   python -m venv venv
   venv\Scripts\activate         # Linux/Mac: source venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Apply migrations**
   ```
   python manage.py migrate
   ```

5. **Run the server**
   ```
   python manage.py runserver
   ```
   Runs at http://127.0.0.1:8000/. Every request is printed in the terminal as
   `[REQUEST] METHOD /path -> status (time ms)`.

6. **Run the tests** (optional)
   ```
   python manage.py test
   ```

## Endpoints (17 required)

| # | Method | URL | Description |
|---|--------|-----|-------------|
| 1 | POST | `/api/users/` | Create a user. Duplicate email -> 409 |
| 2 | GET | `/api/users/{id}/` | Get a user |
| 3 | POST | `/api/workspaces/` | Create workspace; owner is added as `admin` member in one transaction |
| 4 | GET | `/api/workspaces/{id}/` | Workspace with member count |
| 5 | POST | `/api/workspaces/{id}/members/` | Add member `{"user_id": "...", "role": "editor"}`. Duplicate -> 409 |
| 6 | GET | `/api/workspaces/{id}/members/` | List members and roles |
| 7 | GET | `/api/workspaces/{id}/summary/` | Document / member / comment counts (aggregation) |
| 8 | POST | `/api/documents/` | Create document + version 1 in one transaction |
| 9 | PUT | `/api/documents/{id}/` | Update; every save creates a new version. Optional `saved_by` |
| 10 | GET | `/api/documents/` | Filters: `workspace`, `status` (comma separated), `tag`, `search` (title OR content), `created_after`, `created_before` |
| 11 | GET | `/api/documents/{id}/versions/` | All versions in order |
| 12 | GET | `/api/documents/{id}/stats/` | Version, comment and contributor counts (aggregation) |
| 13 | POST | `/api/documents/{id}/tags/` | Add tags `{"tag_ids": ["..."]}` |
| 14 | POST | `/api/comments/` | Add a comment, or a reply by passing `parent` |
| 15 | GET | `/api/comments/?document={id}` | Threaded comments (replies nested under parents) |
| 16 | POST | `/api/tags/` | Create a tag. Duplicate name -> 409 |
| 17 | GET | `/api/audit-logs/` | Newest first. Filters: `actor`, `action`, `model_name`, `from`, `to` (YYYY-MM-DD) |

Errors look like `{"error": "..."}` (or DRF field errors) with 400, 403, 404 or 409.

## How the requirements are met

- **Models:** User, Workspace, WorkspaceMember, Document, DocumentVersion, Comment, Tag, AuditLog. UUID primary keys, `TextChoices` for roles, document status and audit actions, `UniqueConstraint` on `(workspace, user)`, self-referencing `Comment.parent`, `Document.tags` many-to-many.
- **Transactions:** workspace + admin member + audit log, and document save + version, each run inside one `transaction.atomic()`. `IntegrityError` on a duplicate member returns 409.
- **Signal:** `api/signals.py` writes an `AuditLog` on every Document create/update (connected in `ApiConfig.ready()`). `instance._state.adding` is already `False` inside `post_save`, so a `pre_save` receiver records it and `post_save` reads it back. Because it runs during `save()`, the audit row is in the same atomic block as the save.
- **Middleware:** `api/middleware.py`, registered in `settings.py`.
- **Query optimisation:** `select_related` / `prefetch_related` on list endpoints, `Q` for OR search, `Count` aggregation/annotation in workspace detail, workspace summary and document stats, `values_list` where only ids are needed, and the comment tree is built from a single query.
- **Roles:** only `admin` / `editor` members can create or edit documents (403 otherwise); any member can comment.
- **No login:** the API has no authentication, so ids such as `owner`, `created_by` and `author` are sent in the request body.

## Demonstrating the rollback

With `DEBUG=True`, creating a workspace with `?simulate_failure=true` raises an error after the workspace,
member and audit log were written, inside the transaction. The response is 500 and **nothing** is saved:

```
POST /api/workspaces/?simulate_failure=true      -> 500
GET  /api/workspaces/                            -> the workspace is not in the list
```

## Postman

`CollabDocs.postman_collection.json` (repo root) covers all 17 endpoints plus the error cases, grouped into
Users, Workspaces, Documents, Comments, Tags and Audit Logs, with sample bodies for every POST and PUT.
Import it and run it top to bottom; each request saves the ids the next ones need. Requests named
`DEMO:` are for the video.
