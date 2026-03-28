#!/usr/bin/env python3
"""
PHL Catering Scheduler — Admin Bootstrap
Run once to create the admin (manager) account.

Usage:
    python3 create_admin.py

This is safe to re-run — it will tell you if the user already exists.
"""
import sys, os

# Make sure we can import from the app directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    print("\n PHL Catering Scheduler — Admin Setup\n" + "─"*40)

    # ── Hardcoded admin credentials ───────────────────────────────────────
    # Change these before running if you want different credentials.
    USERNAME     = "crologas"          # your login username
    PASSWORD     = "PHL_Admin_2026!"   # change this to something personal
    DISPLAY_NAME = "ChrisRologas"    # shown in the UI
    ROLE         = "manager"
    # ─────────────────────────────────────────────────────────────────────

    try:
        from supabase_client import get_client
        sb = get_client()

        # Check if user already exists
        existing = sb.table('users').select('id,username,role') \
                     .eq('username', USERNAME.lower()).execute()
        if existing.data:
            u = existing.data[0]
            print(f"  ✓ User '{u['username']}' already exists (role={u['role']}, id={u['id']})")
            print("  Nothing changed. To reset the password, run:")
            print(f"    python3 reset_password.py {USERNAME} <new_password>")
            return

        # Create user
        import bcrypt
        hashed = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(10)).decode()
        r = sb.table('users').insert({
            'username':     USERNAME.lower(),
            'password':     hashed,
            'display_name': DISPLAY_NAME,
            'role':         ROLE,
            'is_active':    True,
        }).execute()

        if r.data:
            u = r.data[0]
            print(f"  ✓ Admin user created!")
            print(f"    Username:  {u['username']}")
            print(f"    Name:      {DISPLAY_NAME}")
            print(f"    Role:      {ROLE}")
            print(f"    ID:        {u['id']}")
            print(f"\n  Login at:  http://localhost:5001")
            print(f"  Password:  {PASSWORD}")
            print(f"\n  ⚠  Change your password after first login via the Admin panel.")
        else:
            print("  ✗ Insert returned no data — check Supabase table permissions.")

    except Exception as e:
        print(f"\n  ✗ Error: {e}")
        print("\n  Make sure:")
        print("    1. SUPABASE_URL and SUPABASE_KEY env vars are set (or hardcoded in supabase_client.py)")
        print("    2. The 'users' table exists in Supabase")
        print("    3. bcrypt is installed:  pip install bcrypt --break-system-packages")
        sys.exit(1)

if __name__ == '__main__':
    main()
