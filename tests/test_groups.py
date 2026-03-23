"""
End-to-end test for groups feature.
Seeds test users, generates HS256 JWTs (dev mode), and exercises every group route.

Usage: python3 test_groups.py
Requires: server running on localhost:8000 with DEV_MODE=true
"""

import asyncio
import time
import uuid
import httpx
import jwt

BASE = "http://localhost:8000"
JWT_SECRET = "test-jwt-secret-for-local-dev-only"

# Two test users — privy_did acts as the JWT sub claim
USER_A_PRIVY_DID = f"did:privy:{uuid.uuid4()}"
USER_B_PRIVY_DID = f"did:privy:{uuid.uuid4()}"
USER_A_EOA = f"0x{'a1' * 20}"
USER_B_EOA = f"0x{'b2' * 20}"
USER_A_USERNAME = f"testuser_a_{int(time.time())}"
USER_B_USERNAME = f"testuser_b_{int(time.time())}"


def make_token(sub: str) -> str:
    payload = {
        "sub": sub,
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


TOKEN_A = make_token(USER_A_PRIVY_DID)
TOKEN_B = make_token(USER_B_PRIVY_DID)
HEADERS_A = {"Authorization": f"Bearer {TOKEN_A}"}
HEADERS_B = {"Authorization": f"Bearer {TOKEN_B}"}


async def seed_users(client: httpx.AsyncClient):
    """Insert test users directly via DB."""
    import sys
    sys.path.insert(0, ".")
    from app.core.database import async_session_factory
    from app.models.db import User

    async with async_session_factory() as db:
        for privy_did, eoa, uname in [
            (USER_A_PRIVY_DID, USER_A_EOA, USER_A_USERNAME),
            (USER_B_PRIVY_DID, USER_B_EOA, USER_B_USERNAME),
        ]:
            user = User(
                privy_did=privy_did,
                eoa_address=eoa,
                username=uname,
                display_name=uname.replace("_", " ").title(),
                email=f"{uname}@test.local",
            )
            db.add(user)
        await db.commit()
        print(f"  Seeded users: {USER_A_USERNAME}, {USER_B_USERNAME}")


async def cleanup_users():
    """Remove test users."""
    import sys
    sys.path.insert(0, ".")
    from sqlalchemy import delete, select
    from app.core.database import async_session_factory
    from app.models.db import User, UserFollow, GroupMembership, GroupMessage, Group

    async with async_session_factory() as db:
        # Get user IDs
        stmt = select(User).where(User.privy_did.in_([USER_A_PRIVY_DID, USER_B_PRIVY_DID]))
        result = await db.execute(stmt)
        users = result.scalars().all()
        user_ids = [u.id for u in users]

        if user_ids:
            # Delete group messages by sender
            await db.execute(delete(GroupMessage).where(GroupMessage.sender_id.in_(user_ids)))
            # Delete system messages in groups owned by test users
            groups_stmt = select(Group.id).where(Group.owner_id.in_(user_ids))
            groups_result = await db.execute(groups_stmt)
            group_ids = [g[0] for g in groups_result.fetchall()]
            if group_ids:
                await db.execute(delete(GroupMessage).where(GroupMessage.group_id.in_(group_ids)))
                await db.execute(delete(GroupMembership).where(GroupMembership.group_id.in_(group_ids)))
                await db.execute(delete(Group).where(Group.id.in_(group_ids)))
            # Delete memberships and follows
            await db.execute(delete(GroupMembership).where(GroupMembership.user_id.in_(user_ids)))
            await db.execute(delete(UserFollow).where(UserFollow.follower_id.in_(user_ids)))
            await db.execute(delete(UserFollow).where(UserFollow.followed_id.in_(user_ids)))
            # Delete users
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()
            print(f"  Cleaned up {len(users)} test users and related data")


def check(resp: httpx.Response, expected: int, label: str):
    if resp.status_code != expected:
        print(f"  FAIL {label}: expected {expected}, got {resp.status_code}")
        print(f"       Body: {resp.text[:300]}")
        return False
    print(f"  PASS {label} ({resp.status_code})")
    return True


async def run_tests():
    passed = 0
    failed = 0
    total = 0

    def track(ok: bool):
        nonlocal passed, failed, total
        total += 1
        if ok:
            passed += 1
        else:
            failed += 1

    async with httpx.AsyncClient(base_url=BASE, timeout=120) as c:
        # Track IDs across sections
        msg_id = None

        # ── 0. Seed users ──
        print("\n=== Seeding test users ===")
        await seed_users(c)

        # ── 1. Auth: verify tokens work ──
        print("\n=== 1. Auth / User Profile ===")
        r = await c.get("/api/v1/users/me", headers=HEADERS_A)
        track(check(r, 200, "GET /users/me (user A)"))
        user_a_id = r.json().get("id") if r.status_code == 200 else None

        r = await c.get("/api/v1/users/me", headers=HEADERS_B)
        track(check(r, 200, "GET /users/me (user B)"))
        user_b_id = r.json().get("id") if r.status_code == 200 else None

        # ── 2. Follow / Unfollow ──
        print("\n=== 2. Follow / Unfollow ===")
        if user_b_id:
            r = await c.post(f"/api/v1/users/{user_b_id}/follow", headers=HEADERS_A)
            track(check(r, 201, "POST follow user B"))

            r = await c.get("/api/v1/users/me/following", headers=HEADERS_A)
            track(check(r, 200, "GET /users/me/following"))
            if r.status_code == 200:
                following = r.json()
                track(check_val := any(f.get("id") == user_b_id for f in following))
                if not check_val:
                    print(f"  FAIL: user B not in following list")

            r = await c.get("/api/v1/users/me/followers", headers=HEADERS_B)
            track(check(r, 200, "GET /users/me/followers"))

            r = await c.delete(f"/api/v1/users/{user_b_id}/follow", headers=HEADERS_A)
            track(check(r, 200, "DELETE unfollow user B"))

            # Double unfollow → 404
            r = await c.delete(f"/api/v1/users/{user_b_id}/follow", headers=HEADERS_A)
            track(check(r, 404, "DELETE unfollow again → 404"))

        # ── 3. Create Private Group ──
        print("\n=== 3. Create Private Group ===")
        slug_ts = int(time.time())
        r = await c.post("/api/v1/groups", headers=HEADERS_A, json={
            "name": "Test Squad",
            "slug": f"test-squad-{slug_ts}",
            "access_type": "private",
            "description": "A test private group",
        })
        track(check(r, 201, "POST create private group"))
        private_group = r.json() if r.status_code == 201 else {}
        private_group_id = private_group.get("id")
        invite_code = private_group.get("invite_code")
        print(f"       Group ID: {private_group_id}")
        print(f"       Invite code: {invite_code}")

        # Verify invite_code is present for private group
        if private_group_id:
            track(ok := invite_code is not None)
            if not ok:
                print("  FAIL: private group missing invite_code")
            else:
                print("  PASS: invite_code present")

        # ── 4. Create Public Group ──
        print("\n=== 4. Create Public Group ===")
        r = await c.post("/api/v1/groups", headers=HEADERS_A, json={
            "name": "Public Channel",
            "slug": f"public-channel-{slug_ts}",
            "access_type": "public",
            "description": "A test public group",
        })
        track(check(r, 201, "POST create public group"))
        public_group = r.json() if r.status_code == 201 else {}
        public_group_id = public_group.get("id")

        # Verify no invite_code for public group
        if public_group_id:
            track(ok := public_group.get("invite_code") is None)
            if not ok:
                print("  FAIL: public group should NOT have invite_code")
            else:
                print("  PASS: no invite_code for public group")

        # ── 5. Duplicate slug → 409 ──
        print("\n=== 5. Duplicate Slug ===")
        r = await c.post("/api/v1/groups", headers=HEADERS_A, json={
            "name": "Dupe",
            "slug": f"test-squad-{slug_ts}",
            "access_type": "private",
        })
        track(check(r, 409, "POST duplicate slug → 409"))

        # ── 6. Get Group Details ──
        print("\n=== 6. Get Group Details ===")
        if private_group_id:
            r = await c.get(f"/api/v1/groups/{private_group_id}", headers=HEADERS_A)
            track(check(r, 200, "GET private group (owner)"))

            # User B can't see private group (not a member → 404)
            r = await c.get(f"/api/v1/groups/{private_group_id}", headers=HEADERS_B)
            track(check(r, 404, "GET private group (non-member) → 404"))

        # ── 7. Join via Invite Code ──
        print("\n=== 7. Join via Invite Code ===")
        if invite_code:
            r = await c.post(f"/api/v1/groups/join/{invite_code}", headers=HEADERS_B)
            track(check(r, 200, "POST join via invite code"))

            # Now user B can see the private group
            r = await c.get(f"/api/v1/groups/{private_group_id}", headers=HEADERS_B)
            track(check(r, 200, "GET private group (now member)"))

            # Double join → idempotent 200 (returns existing group)
            r = await c.post(f"/api/v1/groups/join/{invite_code}", headers=HEADERS_B)
            track(check(r, 200, "POST join again → 200 (idempotent)"))

        # ── 8. Join Public Group ──
        print("\n=== 8. Join Public Group ===")
        if public_group_id:
            r = await c.post(f"/api/v1/groups/{public_group_id}/join", headers=HEADERS_B)
            track(check(r, 200, "POST join public group"))

        # ── 9. List My Groups ──
        print("\n=== 9. List My Groups ===")
        r = await c.get("/api/v1/groups", headers=HEADERS_A)
        track(check(r, 200, "GET /groups (my groups)"))
        if r.status_code == 200:
            my_groups = r.json()
            print(f"       User A has {len(my_groups)} groups")

        r = await c.get("/api/v1/groups", headers=HEADERS_B)
        track(check(r, 200, "GET /groups (user B groups)"))
        if r.status_code == 200:
            print(f"       User B has {len(r.json())} groups")

        # ── 10. Discover Public Groups ──
        print("\n=== 10. Discover Public Groups ===")
        r = await c.get("/api/v1/groups/discover", headers=HEADERS_A)
        track(check(r, 200, "GET /groups/discover"))
        if r.status_code == 200:
            data = r.json()
            print(f"       Discovered {len(data)} public groups")

        # ── 11. List Members ──
        print("\n=== 11. List Members ===")
        if private_group_id:
            r = await c.get(f"/api/v1/groups/{private_group_id}/members", headers=HEADERS_A)
            track(check(r, 200, "GET /groups/{id}/members"))
            if r.status_code == 200:
                print(f"       {len(r.json())} members in private group")

        # ── 12. Send Messages ──
        print("\n=== 12. Send Messages ===")
        if private_group_id:
            # Text message
            r = await c.post(f"/api/v1/groups/{private_group_id}/messages", headers=HEADERS_A, json={
                "type": "text",
                "content": "Hello group!",
            })
            track(check(r, 201, "POST text message"))
            msg_id = r.json().get("id") if r.status_code == 201 else None

            # Text from user B
            r = await c.post(f"/api/v1/groups/{private_group_id}/messages", headers=HEADERS_B, json={
                "type": "text",
                "content": "Hey everyone!",
            })
            track(check(r, 201, "POST text message (user B)"))

            # Empty text → 422
            r = await c.post(f"/api/v1/groups/{private_group_id}/messages", headers=HEADERS_A, json={
                "type": "text",
                "content": "",
            })
            track(check(r, 422, "POST empty text → 422"))

            # System type blocked at route layer
            r = await c.post(f"/api/v1/groups/{private_group_id}/messages", headers=HEADERS_A, json={
                "type": "system",
                "content": "hacked",
                "metadata": {"event": "test", "detail": "test"},
            })
            track(check(r, 422, "POST system type → 422 (blocked)"))

            # Reply to message
            if msg_id:
                r = await c.post(f"/api/v1/groups/{private_group_id}/messages", headers=HEADERS_B, json={
                    "type": "text",
                    "content": "Replying to you!",
                    "reply_to_id": msg_id,
                })
                track(check(r, 201, "POST reply message"))

        # ── 13. List Messages ──
        print("\n=== 13. List Messages ===")
        if private_group_id:
            r = await c.get(f"/api/v1/groups/{private_group_id}/messages", headers=HEADERS_A)
            track(check(r, 200, "GET messages"))
            if r.status_code == 200:
                data = r.json()
                msgs = data.get("messages", data) if isinstance(data, dict) else data
                print(f"       {len(msgs)} messages in group")
                # Check sender profile is joined
                for m in msgs:
                    sender = m.get("sender") if isinstance(m, dict) else None
                    if sender and isinstance(sender, dict) and "username" not in sender:
                        print(f"  FAIL: message missing sender.username")
                        failed += 1
                        total += 1

            # Non-member can't read messages
            r = await c.get(f"/api/v1/groups/{private_group_id}/messages", headers={
                "Authorization": f"Bearer {make_token(f'did:privy:{uuid.uuid4()}')}"
            })
            # This should fail with 404 (user not found in DB)
            track(check(r, 404, "GET messages (unregistered user) → 404"))

        # ── 14. Delete Message ──
        print("\n=== 14. Delete Message ===")
        if private_group_id and msg_id:
            # User B can't delete user A's message
            r = await c.delete(f"/api/v1/groups/{private_group_id}/messages/{msg_id}", headers=HEADERS_B)
            track(check(r, 403, "DELETE other user's message → 403"))

            # User A (owner) can delete own message
            r = await c.delete(f"/api/v1/groups/{private_group_id}/messages/{msg_id}", headers=HEADERS_A)
            track(check(r, 200, "DELETE own message"))

        # ── 15. Unread Counts ──
        print("\n=== 15. Unread Counts ===")
        r = await c.get("/api/v1/users/me/unread", headers=HEADERS_A)
        track(check(r, 200, "GET /users/me/unread"))
        if r.status_code == 200:
            print(f"       Unread: {r.json()}")

        # Reset unread
        if private_group_id:
            r = await c.put(f"/api/v1/groups/{private_group_id}/read", headers=HEADERS_A)
            track(check(r, 200, "PUT /groups/{id}/read"))

        # ── 16. Update Group ──
        print("\n=== 16. Update Group ===")
        if private_group_id:
            r = await c.put(f"/api/v1/groups/{private_group_id}", headers=HEADERS_A, json={
                "name": "Updated Squad Name",
                "description": "Updated description",
            })
            track(check(r, 200, "PUT update group (owner)"))

            # User B (member) can't update
            r = await c.put(f"/api/v1/groups/{private_group_id}", headers=HEADERS_B, json={
                "name": "Hacked Name",
            })
            track(check(r, 403, "PUT update group (member) → 403"))

        # ── 17. Member Role Management ──
        print("\n=== 17. Member Role Management ===")
        if private_group_id and user_b_id:
            # Promote user B to admin
            r = await c.put(
                f"/api/v1/groups/{private_group_id}/members/{user_b_id}",
                headers=HEADERS_A,
                json={"role": "admin"},
            )
            track(check(r, 200, "PUT promote to admin"))

            # Demote back to member
            r = await c.put(
                f"/api/v1/groups/{private_group_id}/members/{user_b_id}",
                headers=HEADERS_A,
                json={"role": "member"},
            )
            track(check(r, 200, "PUT demote to member"))

        # ── 18. Leave Group ──
        print("\n=== 18. Leave Group ===")
        if private_group_id:
            r = await c.post(f"/api/v1/groups/{private_group_id}/leave", headers=HEADERS_B)
            track(check(r, 200, "POST leave group (user B)"))

            # Owner can't leave
            r = await c.post(f"/api/v1/groups/{private_group_id}/leave", headers=HEADERS_A)
            track(check(r, 400, "POST leave group (owner) → 400"))

        # ── 19. Soft Delete Group ──
        print("\n=== 19. Soft Delete Group ===")
        if public_group_id:
            # Non-owner can't delete
            r = await c.delete(f"/api/v1/groups/{public_group_id}", headers=HEADERS_B)
            track(check(r, 403, "DELETE group (non-owner) → 403"))

            # Owner can delete
            r = await c.delete(f"/api/v1/groups/{public_group_id}", headers=HEADERS_A)
            track(check(r, 200, "DELETE group (owner)"))

            # Deleted group → 404
            r = await c.get(f"/api/v1/groups/{public_group_id}", headers=HEADERS_A)
            track(check(r, 404, "GET deleted group → 404"))

        # ── 20. User Search ──
        print("\n=== 20. User Search ===")
        r = await c.get(f"/api/v1/users/search?q={USER_A_USERNAME[:8]}", headers=HEADERS_A)
        track(check(r, 200, "GET /users/search"))
        if r.status_code == 200:
            results = r.json()
            print(f"       Found {len(results)} users matching '{USER_A_USERNAME[:8]}'")

        # ── Summary ──
        print(f"\n{'='*50}")
        print(f"RESULTS: {passed}/{total} passed, {failed} failed")
        print(f"{'='*50}")

        # Cleanup
        print("\n=== Cleaning up ===")
        await cleanup_users()

        return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    exit(0 if success else 1)
