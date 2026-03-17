# Groups Feature — Design, Architecture & Implementation

> Reference doc for agents working on the Alpha social layer. This covers the groups/squads feature end-to-end: auth, data model, API surface, domain logic, background workers, and known issues.

---

## Product Context

Groups are Alpha's social betting layer. Two flavors:

- **Private squads** — invite-only, capped at 50 members, invite code required to join
- **Public channels** — open to anyone, no member cap, discoverable via `/discover`

Users share trades, markets, articles, and profiles in group chat. The system auto-injects alerts when shared markets move significantly or resolve. **No fund pooling** — each member trades their own individual Kalshi account. Social coordination only.

---

## Database Schema (migration `0002_social_layer`)

Migration: `migrations/versions/0002_social_layer.py` — fully idempotent with guards (`_table_exists`, `_col_exists`, `_index_exists_pg`, `_constraint_exists`).

### ENUMs (created with `CREATE TYPE IF NOT EXISTS`)

| ENUM | Values |
|------|--------|
| `group_access_type` | `private`, `public` |
| `group_member_role` | `owner`, `admin`, `member`, `follower` |
| `message_type` | `text`, `image`, `trade`, `market`, `article`, `profile`, `system` |

### Tables

**`user_follows`** — follow graph (follower_id ↔ followed_id)
- Unique constraint: `uq_user_follows_pair` on `(follower_id, followed_id)`
- Indexed both directions for fast lookup

**`groups`** — group/channel entity
- `slug` — URL-safe identifier, unique across ALL rows including soft-deleted (`uq_groups_slug` is absolute)
- `access_type` — `private` or `public` (enum)
- `owner_id` — FK to `users.id`
- `invite_code` — random hex token, present for private groups, nullable for public
- `max_members` — 50 for private, NULL (unlimited) for public
- `member_count` — denormalized counter, incremented/decremented in `add_member`/`remove_member`
- `is_deleted` — soft delete flag

**`group_memberships`** — user ↔ group join table
- Unique constraint: `uq_group_memberships_pair` on `(group_id, user_id)`
- `role` — `owner`, `admin`, `member`, `follower`
- `last_read_at` — timestamp for read receipts
- `joined_at` — `server_default=func.now()`

**`group_messages`** — chat messages
- `sender_id` — nullable (NULL for system messages)
- `type` — message_type enum
- `content` — text body (required for `text` type, nullable for others)
- `msg_metadata` — JSONB blob, schema varies by message type (see Metadata Schemas below)
- `reply_to_id` — self-referencing FK for threaded replies
- `is_deleted` — soft delete
- GIN index on `msg_metadata` for JSONB queries (used by resolution alerts)
- Partial index `idx_messages_market_ref` on `(msg_metadata->>'market_id')` WHERE `type IN ('market','trade')` — used by `check_resolution_alerts`

**User columns added** (to existing `users` table):
- `supabase_uid` — Supabase Auth user ID, unique
- `username` — unique, pg_trgm GIN-indexed for fuzzy search
- `display_name`, `avatar_url`, `bio`, `is_verified`

---

## Authentication (`app/services/auth.py`)

JWT verification using Supabase's public JWKS endpoint.

**Primary path — ES256/RS256 via JWKS:**
```
JWKS URI: {SUPABASE_URL}/auth/v1/.well-known/jwks.json
Algorithms: RS256, ES256
Audience: "authenticated"
```
`PyJWKClient` caches keys automatically and re-fetches on key miss.

**Fallback path — HS256:**
Uses `SUPABASE_JWT_SECRET` env var (for older Supabase projects or local testing).

**`get_current_user` dependency:**
1. Extract bearer token from `Authorization` header
2. Decode + verify JWT (JWKS first, HS256 fallback)
3. Extract `sub` claim → query `users.supabase_uid`
4. Return `User` ORM object (or 401/404)

---

## Rate Limiting (`app/core/limiter.py`)

Uses `slowapi` with two key functions:

| Key function | How it works | Used on |
|---|---|---|
| `get_remote_address` (default) | Client IP | Auth endpoints (register, login), invite join |
| `_get_user_key` | SHA-256 hash of full bearer token (first 16 hex chars) | Messages (60/min), follows (100/min) |

**Critical design note:** The per-user key hashes the **full** JWT, not a prefix. JWT headers are identical across all users in a Supabase project (same algorithm metadata), so any prefix-based approach would create a shared bucket for all users.

---

## API Routes

### Auth & Users (`app/api/routes/v1/users.py`)

| Method | Path | Auth | Rate Limit | Description |
|--------|------|------|------------|-------------|
| POST | `/api/v1/auth/register` | No | 10/min (IP) | Create Supabase Auth user + local profile |
| POST | `/api/v1/auth/login` | No | 10/min (IP) | Supabase Auth sign-in |
| POST | `/api/v1/auth/refresh` | No | — | Refresh Supabase session |
| GET | `/api/v1/users/me` | Yes | — | Current user profile |
| PUT | `/api/v1/users/me` | Yes | — | Update profile |
| GET | `/api/v1/users/me/following` | Yes | — | Who I follow |
| GET | `/api/v1/users/me/followers` | Yes | — | Who follows me |
| GET | `/api/v1/users/me/unread` | Yes | — | Unread counts per group (from Redis) |
| POST | `/api/v1/users/{id}/follow` | Yes | 100/min (user) | Follow user |
| DELETE | `/api/v1/users/{id}/follow` | Yes | — | Unfollow user |
| GET | `/api/v1/users/search?q=` | Yes | — | Fuzzy username search (pg_trgm) |
| GET | `/api/v1/users/{id}` | Yes | — | Public profile |

### Groups (`app/api/routes/v1/groups.py`)

| Method | Path | Auth | Min Role | Description |
|--------|------|------|----------|-------------|
| POST | `/api/v1/groups` | Yes | — | Create group (caller becomes owner) |
| GET | `/api/v1/groups` | Yes | — | My groups (all groups I'm in) |
| GET | `/api/v1/groups/discover` | Yes | — | Public groups, cursor-paginated by member_count desc |
| GET | `/api/v1/groups/{id}` | Yes | — | Group details (private → 404 for non-members) |
| PUT | `/api/v1/groups/{id}` | Yes | admin | Update name/description/avatar |
| DELETE | `/api/v1/groups/{id}` | Yes | owner | Soft delete group |
| POST | `/api/v1/groups/{id}/join` | Yes | — | Join public group |
| POST | `/api/v1/groups/join/{code}` | Yes | — | Join via invite code (10/min IP limit) |
| POST | `/api/v1/groups/{id}/leave` | Yes | — | Leave group (owner cannot leave) |
| POST | `/api/v1/groups/{id}/invite` | Yes | admin | Get/generate invite link |
| GET | `/api/v1/groups/{id}/members` | Yes | — | List members with profiles |
| PUT | `/api/v1/groups/{id}/members/{uid}` | Yes | admin | Change member role |
| DELETE | `/api/v1/groups/{id}/members/{uid}` | Yes | admin | Kick member |
| PUT | `/api/v1/groups/{id}/read` | Yes | — | Mark group as read |

### Messages (`app/api/routes/v1/messages.py`)

| Method | Path | Auth | Rate Limit | Description |
|--------|------|------|------------|-------------|
| GET | `/api/v1/groups/{id}/messages` | Yes (member) | — | Paginated message history (cursor = ISO timestamp) |
| POST | `/api/v1/groups/{id}/messages` | Yes (member) | 60/min (user) | Send message |
| DELETE | `/api/v1/groups/{id}/messages/{mid}` | Yes | — | Soft delete (sender or admin+) |

---

## Domain Service (`app/services/groups.py`)

All business logic lives here. Routes are thin wrappers.

### Role Hierarchy

```python
ROLE_ORDER = {"owner": 4, "admin": 3, "member": 2, "follower": 1}
```

`assert_min_role(group_id, user_id, min_role, db, membership=)` raises HTTP 403 if the user's role ordinal is below the required minimum. Accepts an optional pre-fetched `membership` to avoid double DB queries.

### Access Control Pattern

`get_group_or_404_authorized(group_id, user_id, db)` → `tuple[Group, Optional[GroupMembership]]`

- **Private groups**: non-members get HTTP 404 (not 403) to avoid leaking group existence
- **Public groups**: non-members get `(group, None)` — they can see the group but can't read/send messages until they join
- Always fetches membership in one query so callers can pass it downstream

### Slug Normalization

All slugs go through `slugify()` from `app/workers/taxonomy.py` (lowercase, hyphens, no special chars). The DB constraint `uq_groups_slug` is absolute — applies to all rows including soft-deleted. The Python check matches this (no `is_deleted` filter).

### Message Types & Metadata Schemas

User-sendable types (enforced by `_VALID_MSG_TYPES` in messages route):
- **text** — `content` required (1–2000 chars), no metadata
- **image** — `ImageMetadata(url, width?, height?, mime_type?)`
- **trade** — `TradeMetadata(source, record_id, snapshot)` — `source` must be `"user_order"` or `"public_trade"`. If `user_order`, verifies `UserOrder.user_id == sender_id`
- **market** — `MarketMetadata(exchange, market_id, shared_yes_price, snapshot)` — verifies market exists in DB
- **article** — `ArticleMetadata(url, title, source, image_url?)`
- **profile** — `ProfileMetadata(user_id, username, display_name?, avatar_url?)`

Worker-only type (blocked at route layer):
- **system** — `SystemMetadata(event, market_id?, detail)` — events: `price_alert`, `resolution`, `member_joined`, `member_left`

All metadata is validated via Pydantic schemas in `_validate_metadata()` before storage.

### Unread Fan-out (Redis)

When a message is created:
1. Fetch member set from Redis (`group_members:{group_id}` SET) — rebuild from DB on cache miss
2. Pipeline `INCR unread:{group_id}:{user_id}` for every member except sender
3. Each unread key has 30-day TTL
4. Entire fan-out is wrapped in try/except — best-effort, never blocks message creation

Read receipt: `PUT /groups/{id}/read` → deletes `unread:{group_id}:{user_id}` in Redis + updates `last_read_at` in DB.

### Concurrency Protection

**Check-then-insert protected by DB constraints + IntegrityError catch:**

- `add_member()` — catches `IntegrityError` on `uq_group_memberships_pair` → returns 409
- `create_group()` — catches `IntegrityError` on `uq_groups_slug` → returns 409
- `member_count` uses atomic SQL `UPDATE groups SET member_count = member_count + 1` (not read-modify-write)
- `remove_member()` uses `func.greatest(member_count - 1, 0)` to prevent negative counts

---

## Background Workers (`app/workers/group_alerts.py`)

Registered in `app/core/arq_worker.py` — **production only** (not in DEV_MODE).

### `check_price_alerts` (every 5 minutes)

1. Scans `group_messages` WHERE `type='market'` AND `created_at > now() - 24h`
2. Extracts `market_id` and `shared_yes_price` from metadata
3. Fetches current price from Redis (`ticker:{exchange}:{yes_asset_id}`)
4. If price moved >= 10% from shared price → inserts system message
5. **Dedup**: Redis key `alert_dedup:{group_id}:{market_id}` with 1hr TTL

### `check_resolution_alerts` (every 2 minutes)

1. Scans `markets` WHERE `status='resolved'` AND `resolve_time > now() - 3min`
2. Finds all group messages referencing that market via JSONB index
3. Updates trade message metadata: `resolved=true`, calculates `profit_pct`
4. Inserts resolution system message per affected group
5. **Dedup**: Redis key `resolve_dedup:{group_id}:{market_id}` with 24hr TTL

Both workers: wrapped in `try/except Exception` — never crash the arq process.

---

## Redis Key Schema (Groups)

| Key pattern | Type | TTL | Purpose |
|---|---|---|---|
| `group_members:{group_id}` | SET | 24hr | Member UIDs for unread fan-out |
| `unread:{group_id}:{user_id}` | STRING (int) | 30 days | Unread message count |
| `alert_dedup:{group_id}:{market_id}` | STRING | 1hr | Price alert dedup |
| `resolve_dedup:{group_id}:{market_id}` | STRING | 24hr | Resolution alert dedup |

---

## Pagination Patterns

**Discover groups** — compound cursor: `{member_count}:{group_id}`
```sql
WHERE (member_count < :cursor_count)
   OR (member_count = :cursor_count AND id < :cursor_gid)
ORDER BY member_count DESC, id DESC
```

**Messages** — ISO timestamp cursor: `created_at`
```sql
WHERE created_at < :cursor_dt
ORDER BY created_at DESC
```

---

## Env Vars Required

```
SUPABASE_URL=https://<ref>.supabase.co          # JWKS endpoint for JWT verification
SUPABASE_JWT_SECRET=<secret>                     # HS256 fallback (optional, for local testing)
SUPABASE_JWT_PUBLIC_KEY=<jwk-json>               # ES256 public key (reference, JWKS is primary)
```

---

## Known Issues & TODOs

### [CLARITY] `list_messages` cursor WHERE clause ordering
SQLAlchemy builder appends `.where(created_at < cursor_dt)` after `.limit()`. SQL is correct but code ordering is misleading. Move cursor condition into the initial WHERE block.

### [ROBUSTNESS] TOCTOU in `/auth/register` username uniqueness
Concurrent registrations with same username: both pass SELECT, second hits DB UNIQUE constraint → 409, but orphaned Supabase Auth account remains. Fix: delete orphan via Supabase admin API on IntegrityError.

### [PRODUCT DECISION] Invite code visibility
Currently all members can see and share invite codes. May want to restrict to admin+ only. 2-line change in `_group_to_response`.

---

## File Map

```
app/
├── api/routes/v1/
│   ├── users.py          # Auth, profiles, follows, search, unread
│   ├── groups.py         # Group CRUD, membership, discovery, invite
│   └── messages.py       # Group chat: send/list/delete messages
├── services/
│   ├── auth.py           # JWT verification (JWKS ES256/RS256 + HS256)
│   └── groups.py         # Domain logic: CRUD, membership, messages, unread
├── workers/
│   └── group_alerts.py   # Price alert + resolution alert arq crons
├── core/
│   ├── limiter.py        # slowapi rate limiter (IP + user key functions)
│   └── arq_worker.py     # Worker registration (group alerts = prod only)
├── models/
│   └── db.py             # ORM: User, UserFollow, Group, GroupMembership, GroupMessage
└── migrations/versions/
    └── 0002_social_layer.py  # Idempotent migration for all social tables
```
