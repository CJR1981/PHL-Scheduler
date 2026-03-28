"""
PHL Catering Scheduler — Authentication
Username + password (bcrypt), role-based access, token sessions via Supabase.
"""
import secrets, bcrypt
from datetime import datetime, timezone
from typing import Optional
from supabase_client import get_client

# ── Role permissions ──────────────────────────────────────────────────────
ROLE_PERMS = {
    'manager':    ['all'],
    'supervisor': ['overview','detail','schedule','unassigned','analytics',
                   'trucks','liveops','export'],
    'teamlead':   ['overview','own_team'],
    'agent':      ['overview','own_team'],
}

def can(role: str, perm: str) -> bool:
    perms = ROLE_PERMS.get(role, [])
    return 'all' in perms or perm in perms

# ── Login ─────────────────────────────────────────────────────────────────
def login(username: str, password: str) -> dict:
    """
    Verify username + password. Returns user dict + session token on success.
    Returns {'error': msg} on failure.
    """
    try:
        sb = get_client()
        r = sb.table('users').select('*') \
               .eq('username', username.strip().lower()) \
               .eq('is_active', True).limit(1).execute()

        if not r.data:
            return {'error': 'Invalid username or password'}

        user = r.data[0]
        stored_hash = user['password'].encode()

        if not bcrypt.checkpw(password.encode(), stored_hash):
            return {'error': 'Invalid username or password'}

        # Create session token
        token = secrets.token_urlsafe(32)
        sb.table('user_sessions').insert({
            'token':    token,
            'user_id':  user['id'],
            'is_active': True,
        }).execute()

        # Update last_login
        sb.table('users').update({'last_login': datetime.now(timezone.utc).isoformat()}) \
          .eq('id', user['id']).execute()

        return {
            'token':        token,
            'user_id':      user['id'],
            'username':     user['username'],
            'display_name': user['display_name'],
            'role':         user['role'],
            'team_id':      user.get('team_id'),
        }

    except Exception as e:
        return {'error': f'Login failed: {str(e)}'}

# ── Verify token ──────────────────────────────────────────────────────────
def verify_token(token: str) -> Optional[dict]:
    """
    Verify a session token. Returns user dict or None if invalid/expired.
    """
    if not token:
        return None
    try:
        sb = get_client()
        r = sb.table('user_sessions').select('*, users(*)') \
               .eq('token', token).eq('is_active', True).limit(1).execute()

        if not r.data:
            return None

        sess = r.data[0]
        # Check expiry
        expires_str = sess.get('expires_at', '')
        if expires_str:
            expires = datetime.fromisoformat(expires_str.replace('Z', '+00:00'))
            if datetime.now(timezone.utc) > expires:
                # Expired — deactivate
                sb.table('user_sessions').update({'is_active': False}) \
                  .eq('token', token).execute()
                return None

        user = sess.get('users', {})
        if not user or not user.get('is_active'):
            return None

        return {
            'user_id':      user['id'],
            'username':     user['username'],
            'display_name': user['display_name'],
            'role':         user['role'],
            'team_id':      user.get('team_id'),
        }
    except Exception as e:
        print(f"[Auth] verify_token error: {e}")
        return None

# ── Logout ────────────────────────────────────────────────────────────────
def logout(token: str) -> bool:
    try:
        sb = get_client()
        sb.table('user_sessions').update({'is_active': False}) \
          .eq('token', token).execute()
        return True
    except Exception as e:
        print(f"[Auth] logout error: {e}")
        return False

# ── Create user (manager only) ────────────────────────────────────────────
def create_user(username: str, password: str, display_name: str,
                role: str, team_id: str = None) -> dict:
    try:
        if role not in ROLE_PERMS:
            return {'error': f'Invalid role: {role}'}
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(10)).decode()
        sb = get_client()
        r = sb.table('users').insert({
            'username':     username.strip().lower(),
            'password':     hashed,
            'display_name': display_name,
            'role':         role,
            'team_id':      team_id,
            'is_active':    True,
        }).execute()
        return {'success': True, 'user': r.data[0] if r.data else {}}
    except Exception as e:
        return {'error': str(e)}

# ── List users (manager only) ─────────────────────────────────────────────
def list_users() -> list:
    try:
        sb = get_client()
        r = sb.table('users') \
               .select('id,username,display_name,role,team_id,is_active,created_at,last_login') \
               .order('role').order('username').execute()
        return r.data or []
    except Exception as e:
        print(f"[Auth] list_users error: {e}")
        return []

# ── Flask decorator helper ────────────────────────────────────────────────
def require_auth(f):
    """Flask route decorator — injects current_user or returns 401."""
    from functools import wraps
    from flask import request, jsonify
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Auth-Token') or \
                request.cookies.get('phl_auth') or \
                (request.get_json(silent=True) or {}).get('auth_token') or \
                request.args.get('auth_token')
        user = verify_token(token) if token else None
        if not user:
            return jsonify({'error': 'Not authenticated', 'code': 401}), 401
        return f(*args, user=user, **kwargs)
    return decorated

def require_role(*roles):
    """Flask route decorator — requires specific role(s)."""
    from functools import wraps
    from flask import request, jsonify
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = request.headers.get('X-Auth-Token') or \
                    request.cookies.get('phl_auth') or \
                    (request.get_json(silent=True) or {}).get('auth_token') or \
                    request.args.get('auth_token')
            user = verify_token(token) if token else None
            if not user:
                return jsonify({'error': 'Not authenticated', 'code': 401}), 401
            if user['role'] not in roles:
                return jsonify({'error': 'Insufficient permissions', 'code': 403}), 403
            return f(*args, user=user, **kwargs)
        return decorated
    return decorator
