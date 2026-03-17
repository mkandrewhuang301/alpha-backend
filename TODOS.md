# Alpha Backend — TODOS

## Social Layer

### [CLARITY] `list_messages` cursor WHERE clause added after LIMIT in SQLAlchemy builder
**What:** `services/groups.py` builds the cursor `WHERE created_at < cursor_dt` clause by appending `.where()` after `.limit()` in the Python builder chain (lines ~425-431).
**Why:** SQLAlchemy generates correct SQL regardless of builder method order, so there is no runtime bug. However, the ordering misleads contributors into thinking WHERE fires after LIMIT.
**How to fix:** Move the cursor condition into the initial `stmt = select(...).where(...)` block as a conditional filter alongside the existing `group_id` and `is_deleted` filters.
**Effort:** ~3 lines. **Depends on:** nothing.

---

### [ROBUSTNESS] TOCTOU residual in `/auth/register` username uniqueness
**What:** The username uniqueness `SELECT` runs before Supabase Auth signup to prevent orphaned Supabase users. However, two concurrent requests with the same username can both pass the SELECT window before either commits. The DB UNIQUE constraint blocks the second commit with a 409, but the second user's Supabase Auth account is already created and has no local profile row.
**Why:** Negligible frequency (millisecond race window, two users picking identical usernames simultaneously). The DB constraint prevents data corruption. The orphaned Supabase user is a nuisance (they can't log in to Alpha).
**Fix path:** Use `SELECT ... FOR UPDATE SKIP LOCKED` on the username check, OR delete the orphaned Supabase user on DB commit failure using the admin API (`DELETE /auth/v1/admin/users/{uid}` with `SUPABASE_SERVICE_ROLE_KEY`).
**Effort:** Medium (requires Supabase admin API call on failure path). **Depends on:** nothing.

---

### [PRODUCT DECISION] Invite code visibility: all members vs. owner/admin only
**What:** `GET /api/v1/groups` (my groups) returns `invite_code` for all groups the user is a member of, regardless of role. A user with role `follower` or `member` can see and share the invite link.
**Why:** The plan didn't specify this restriction. Current permissive behavior enables organic viral sharing (any member can invite friends). But depending on the product direction, you may want only owners/admins to control who can invite.
**Options:**
- Keep as-is: any member can see and share invite links (viral-friendly).
- Restrict `include_invite=True` to `ROLE_ORDER[membership.role] >= ROLE_ORDER["admin"]` in `_group_to_response`.
**Effort:** 2-line change if restricted. **Depends on:** product decision.
