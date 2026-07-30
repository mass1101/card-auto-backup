from flask import Flask, request, jsonify, g, send_file, send_from_directory, make_response, abort
import sqlite3
import os
import json
import base64
import io
import secrets
import hashlib
from functools import wraps

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
DB_PATH = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(__file__)), 'backup.db')
BINS_DIR = os.path.join(os.environ.get('DATA_DIR', os.path.dirname(__file__)), 'bins')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(BINS_DIR, exist_ok=True)


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chip_id TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                cards_json TEXT NOT NULL
            )
        ''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_backups_chip_id ON backups(chip_id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_backups_timestamp ON backups(timestamp)')
        try:
            db.execute('ALTER TABLE backups ADD COLUMN created_by TEXT DEFAULT ''admin''')
        except:
            pass
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        existing = db.execute('SELECT COUNT(*) FROM users').fetchone()
        if existing[0] == 0:
            db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                       ('admin', hashlib.sha256('xiang1101'.encode()).hexdigest()))
        db.commit()


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('auth_token')
        if token != AUTH_TOKEN:
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


AUTH_TOKEN = secrets.token_hex(32)


def get_current_user():
    return request.cookies.get('username', 'admin')


def is_admin():
    return get_current_user() == 'admin'


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '')
    password = data.get('password', '')
    db = get_db()
    row = db.execute('SELECT password_hash FROM users WHERE username = ?', (username,)).fetchone()
    if row and row['password_hash'] == hashlib.sha256(password.encode()).hexdigest():
        resp = make_response(jsonify({'ok': True, 'username': username}))
        resp.set_cookie('auth_token', AUTH_TOKEN, httponly=True, samesite='Strict')
        resp.set_cookie('username', username, samesite='Strict')
        return resp
    return jsonify({'error': '用户名或密码错误'}), 401


@app.route('/api/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({'ok': True}))
    resp.delete_cookie('auth_token')
    return resp


@app.route('/api/session', methods=['GET'])
def check_session():
    token = request.cookies.get('auth_token')
    username = get_current_user()
    return jsonify({'logged_in': token == AUTH_TOKEN, 'username': username, 'is_admin': username == 'admin'})


@app.route('/api/users', methods=['GET'])
@login_required
def list_users():
    if not is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = get_db()
    rows = db.execute('SELECT id, username, created_at FROM users ORDER BY id').fetchall()
    users = [{'id': r['id'], 'username': r['username'], 'created_at': r['created_at']} for r in rows]
    return jsonify(users)


@app.route('/api/users', methods=['POST'])
@login_required
def add_user():
    if not is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    if len(password) < 4:
        return jsonify({'error': '密码至少4位'}), 400
    db = get_db()
    existing = db.execute('SELECT COUNT(*) FROM users WHERE username = ?', (username,)).fetchone()
    if existing[0] > 0:
        return jsonify({'error': '用户名已存在'}), 409
    db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
               (username, hashlib.sha256(password.encode()).hexdigest()))
    db.commit()
    return jsonify({'ok': True}), 201


@app.route('/api/users/<username>', methods=['DELETE'])
@login_required
def delete_user(username):
    if not is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = get_db()
    row = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if not row:
        return jsonify({'error': '用户不存在'}), 404
    count = db.execute('SELECT COUNT(*) FROM users').fetchone()
    if count[0] <= 1:
        return jsonify({'error': '不能删除最后一个用户'}), 400
    db.execute('DELETE FROM users WHERE id = ?', (row['id'],))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/backup', methods=['POST'])
def receive_backup():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    chip_id = data.get('chip_id')
    cards = data.get('cards')

    if not chip_id:
        return jsonify({'error': 'chip_id is required'}), 400
    if not isinstance(cards, list):
        return jsonify({'error': 'cards must be a list'}), 400

    chip_bin_dir = os.path.join(BINS_DIR, chip_id)
    os.makedirs(chip_bin_dir, exist_ok=True)

    cards_stored = []
    for card in cards:
        stored = {k: v for k, v in card.items() if k != 'bin_data'}
        bin_data_b64 = card.get('bin_data')
        if bin_data_b64:
            try:
                bin_bytes = base64.b64decode(bin_data_b64)
                name = card.get('name', 'unknown')
                uid = card.get('uid', 'unknown')
                safe_name = "".join(c for c in name if c.isalnum() or c in '._-')
                safe_uid = "".join(c for c in uid if c.isalnum() or c in '._-')
                filename = f'{safe_uid}_{safe_name}.bin'
                filepath = os.path.join(chip_bin_dir, filename)
                with open(filepath, 'wb') as f:
                    f.write(bin_bytes)
                stored['bin_url'] = f'/api/bins/{chip_id}/{filename}'
            except Exception as e:
                stored['bin_error'] = str(e)
        cards_stored.append(stored)

    db = get_db()
    db.execute(
        "INSERT INTO backups (chip_id, cards_json, timestamp, created_by) VALUES (?, ?, datetime('now', '+8 hours'), ?)",
        (chip_id, json.dumps(cards_stored), get_current_user())
    )
    db.commit()
    backup_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    return jsonify({'backup_id': backup_id}), 201


@app.route('/api/devices', methods=['GET'])
def list_devices():
    db = get_db()
    if is_admin():
        rows = db.execute('''
            SELECT chip_id,
                   MAX(timestamp) as last_backup,
                   (SELECT cards_json FROM backups b2 WHERE b2.chip_id = b.chip_id ORDER BY b2.timestamp DESC LIMIT 1) as latest_cards,
                   COUNT(*) as backup_count
            FROM backups b
            GROUP BY chip_id
            ORDER BY last_backup DESC
        ''').fetchall()
    else:
        rows = db.execute('''
            SELECT chip_id,
                   MAX(timestamp) as last_backup,
                   (SELECT cards_json FROM backups b2 WHERE b2.chip_id = b.chip_id ORDER BY b2.timestamp DESC LIMIT 1) as latest_cards,
                   COUNT(*) as backup_count
            FROM backups b
            WHERE created_by = ?
            GROUP BY chip_id
            ORDER BY last_backup DESC
        ''', (get_current_user(),)).fetchall()

    devices = []
    for row in rows:
        cards = json.loads(row['latest_cards']) if row['latest_cards'] else []
        devices.append({
            'chip_id': row['chip_id'],
            'last_backup': row['last_backup'],
            'card_count': len(cards),
            'backup_count': row['backup_count']
        })
    return jsonify(devices)


@app.route('/api/backups/<chip_id>', methods=['GET'])
def device_backups(chip_id):
    db = get_db()
    if is_admin():
        rows = db.execute(
            'SELECT id, timestamp, cards_json FROM backups WHERE chip_id = ? ORDER BY timestamp DESC',
            (chip_id,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT id, timestamp, cards_json FROM backups WHERE chip_id = ? AND created_by = ? ORDER BY timestamp DESC',
            (chip_id, get_current_user())
        ).fetchall()

    backups = []
    for row in rows:
        cards = json.loads(row['cards_json'])
        backups.append({
            'id': row['id'],
            'timestamp': row['timestamp'],
            'card_count': len(cards),
            'cards': cards
        })
    return jsonify(backups)


@app.route('/')
def admin():
    return ADMIN_HTML


@app.route('/api/backups/<int:backup_id>', methods=['DELETE'])
@login_required
def delete_backup(backup_id):
    db = get_db()
    row = db.execute('SELECT chip_id, cards_json, created_by FROM backups WHERE id = ?', (backup_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if not is_admin() and row['created_by'] != get_current_user():
        return jsonify({'error': 'unauthorized'}), 403
    cards = json.loads(row['cards_json'])
    for card in cards:
        bin_url = card.get('bin_url', '')
        if bin_url:
            filename = bin_url.rsplit('/', 1)[-1]
            filepath = os.path.join(BINS_DIR, row['chip_id'], filename)
            if os.path.isfile(filepath):
                os.remove(filepath)
    db.execute('DELETE FROM backups WHERE id = ?', (backup_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/backups/<int:backup_id>/cards/<int:card_index>', methods=['DELETE'])
@login_required
def delete_card(backup_id, card_index):
    db = get_db()
    row = db.execute('SELECT chip_id, cards_json, created_by FROM backups WHERE id = ?', (backup_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    if not is_admin() and row['created_by'] != get_current_user():
        return jsonify({'error': 'unauthorized'}), 403
    cards = json.loads(row['cards_json'])
    if card_index < 0 or card_index >= len(cards):
        return jsonify({'error': 'invalid card index'}), 400
    removed = cards.pop(card_index)
    bin_url = removed.get('bin_url', '')
    if bin_url:
        filename = bin_url.rsplit('/', 1)[-1]
        filepath = os.path.join(BINS_DIR, row['chip_id'], filename)
        if os.path.isfile(filepath):
            os.remove(filepath)
    db.execute('UPDATE backups SET cards_json = ? WHERE id = ?', (json.dumps(cards), backup_id))
    db.commit()
    return jsonify({'ok': True, 'remaining': len(cards)})


@app.route('/api/devices/<chip_id>', methods=['DELETE'])
@login_required
def delete_device(chip_id):
    db = get_db()
    rows = db.execute('SELECT id, cards_json, created_by FROM backups WHERE chip_id = ?', (chip_id,)).fetchall()
    if rows and not is_admin():
        for row in rows:
            if row['created_by'] != get_current_user():
                return jsonify({'error': 'unauthorized'}), 403
    for row in rows:
        cards = json.loads(row['cards_json'])
        for card in cards:
            bin_url = card.get('bin_url', '')
            if bin_url:
                filename = bin_url.rsplit('/', 1)[-1]
                filepath = os.path.join(BINS_DIR, chip_id, filename)
                if os.path.isfile(filepath):
                    os.remove(filepath)
    db.execute('DELETE FROM backups WHERE chip_id = ?', (chip_id,))
    db.commit()
    chip_bin_dir = os.path.join(BINS_DIR, chip_id)
    if os.path.isdir(chip_bin_dir):
        try:
            os.rmdir(chip_bin_dir)
        except OSError:
            pass
    return jsonify({'ok': True})


@app.route('/api/devices', methods=['POST'])
def create_device():
    data = request.get_json(silent=True)
    if not data or not data.get('chip_id'):
        return jsonify({'error': 'chip_id is required'}), 400
    chip_id = data['chip_id'].strip()
    if not chip_id:
        return jsonify({'error': 'chip_id cannot be empty'}), 400

    db = get_db()
    existing = db.execute('SELECT COUNT(*) FROM backups WHERE chip_id = ?', (chip_id,)).fetchone()
    if existing[0] > 0:
        return jsonify({'error': '设备名已存在'}), 409

    db.execute(
        "INSERT INTO backups (chip_id, cards_json, timestamp, created_by) VALUES (?, ?, datetime('now', '+8 hours'), ?)",
        (chip_id, json.dumps([]), get_current_user())
    )
    db.commit()
    return jsonify({'chip_id': chip_id}), 201


@app.route('/api/devices/<chip_id>/cards', methods=['POST'])
def upload_cards(chip_id):
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'no files selected'}), 400

    import hashlib
    chip_bin_dir = os.path.join(BINS_DIR, chip_id)
    os.makedirs(chip_bin_dir, exist_ok=True)

    cards = []
    for f in files:
        if f.filename == '':
            continue
        content = f.read()
        card_hash = hashlib.md5(content).hexdigest()[:8]
        name = os.path.splitext(f.filename)[0]
        safe_name = ''.join(c for c in f.filename if c.isalnum() or c in '._-')
        f.seek(0)
        filepath = os.path.join(chip_bin_dir, safe_name)
        f.save(filepath)
        size = len(content)
        if size == 5:
            tag = '100'
        elif size in (256, 512, 1024, 2048, 4096):
            tag = '1001'
        else:
            tag = '手动上传'
        cards.append({
            'uid': card_hash,
            'name': name,
            'tag': tag,
            'bin_url': f'/api/bins/{chip_id}/{safe_name}'
        })

    db = get_db()
    db.execute(
        "INSERT INTO backups (chip_id, cards_json, timestamp, created_by) VALUES (?, ?, datetime('now', '+8 hours'), ?)",
        (chip_id, json.dumps(cards), get_current_user())
    )
    db.commit()
    backup_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    return jsonify({'backup_id': backup_id, 'card_count': len(cards)}), 201


@app.route('/api/bins/<chip_id>/<path:filename>')
def download_bin(chip_id, filename):
    bin_path = os.path.join(BINS_DIR, chip_id, filename)
    if not os.path.isfile(bin_path):
        abort(404)
    return send_file(bin_path, as_attachment=True, download_name=filename)


# ---------- Web Admin ----------
ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>卡片备份管理</title>
<style>
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #f5f5f7; --surface: #ffffff; --text: #1d1d1f; --text-secondary: #86868b;
  --accent: #007aff; --accent-hover: #0071e3; --danger: #ff3b30;
  --border: #e5e5ea; --shadow: 0 2px 12px rgba(0,0,0,.08);
  --radius: 16px; --radius-sm: 10px;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}
.login-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.4);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.login-card {
  background: var(--surface); border-radius: var(--radius); padding: 40px 32px;
  width: 360px; max-width: 90vw; box-shadow: 0 20px 60px rgba(0,0,0,.15); text-align: center;
}
.login-card h2 { font-size: 24px; font-weight: 600; margin-bottom: 4px; }
.login-card .sub { color: var(--text-secondary); font-size: 14px; margin-bottom: 28px; }
.login-card input {
  width: 100%; padding: 14px 16px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 16px; outline: none; margin-bottom: 12px;
}
.login-card input:focus { border-color: var(--accent); }
.login-card .login-btn {
  width: 100%; padding: 14px; background: var(--accent); color: #fff;
  border: none; border-radius: var(--radius-sm); font-size: 17px;
  font-weight: 500; cursor: pointer; margin-top: 8px;
}
.login-card .login-btn:hover { background: var(--accent-hover); }
.login-err { color: var(--danger); font-size: 14px; margin-top: 10px; display: none; }
.app { display: none; max-width: 800px; margin: 0 auto; padding: 32px 20px 60px; }
.app.visible { display: block; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.header h1 { font-size: 28px; font-weight: 700; }
.header .logout-btn { font-size: 15px; color: var(--accent); background: none; border: none; cursor: pointer; font-weight: 500; }
.search-bar {
  margin-bottom: 20px; position: relative;
}
.search-bar input {
  width: 100%; padding: 12px 16px 12px 40px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); font-size: 15px; outline: none; background: var(--surface);
}
.search-bar input:focus { border-color: var(--accent); }
.search-bar svg {
  position: absolute; left: 13px; top: 50%; transform: translateY(-50%);
  width: 16px; height: 16px; color: var(--text-secondary);
}
.folder {
  background: var(--surface); border-radius: var(--radius);
  box-shadow: var(--shadow); margin-bottom: 16px; overflow: hidden;
}
.folder-header {
  display: flex; align-items: center; padding: 16px 20px; cursor: pointer;
  user-select: none; gap: 12px; transition: background .15s;
}
.folder-header:hover { background: rgba(0,0,0,.02); }
.folder-icon {
  width: 36px; height: 36px; border-radius: 9px;
  background: linear-gradient(135deg, var(--accent) 0%, #5856d6 100%);
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.folder-icon svg { width: 18px; height: 18px; fill: #fff; }
.folder-info { flex: 1; min-width: 0; }
.folder-name { font-size: 16px; font-weight: 600; }
.folder-meta { font-size: 13px; color: var(--text-secondary); margin-top: 1px; }
.folder-actions { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
.folder-arrow {
  transition: transform .25s; flex-shrink: 0;
  color: var(--text-secondary);
}
.folder-arrow.open { transform: rotate(90deg); }
.folder-body { border-top: 1px solid var(--border); }
.card-list { display: flex; flex-direction: column; }
.card-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 20px; transition: background .1s;
}
.card-item:hover { background: rgba(0,0,0,.015); }
.card-item + .card-item { border-top: 1px solid var(--border); }
.card-info .name { font-size: 15px; font-weight: 500; }
.card-info .meta { font-size: 13px; color: var(--text-secondary); margin-top: 2px; }
.card-actions { display: flex; gap: 10px; align-items: center; flex-shrink: 0; }
.tag-badge {
  display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px;
  font-weight: 600; letter-spacing: .5px;
}
.tag-ic { background: #e8f5e9; color: #2e7d32; }
.tag-id { background: #e3f2fd; color: #1565c0; }
.tag-other { background: #f3e5f5; color: #7b1fa2; }
.btn {
  padding: 8px 16px; border-radius: 20px; font-size: 14px; font-weight: 500;
  border: none; cursor: pointer; text-decoration: none;
  display: inline-flex; align-items: center; gap: 4px;
}
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-ghost { background: #f2f2f7; color: var(--text); }
.btn-ghost:hover { background: #e5e5ea; }
.btn-danger { background: none; color: var(--danger); padding: 8px 10px; font-size: 13px; }
.btn-danger:hover { background: rgba(255,59,48,.08); }
.btn-device-del { background: none; color: var(--danger); font-size: 12px; padding: 4px 10px; border: 1px solid rgba(255,59,48,.3); border-radius: 12px; cursor: pointer; }
.btn-device-del:hover { background: rgba(255,59,48,.08); }
.empty-state { text-align: center; color: var(--text-secondary); padding: 60px 20px; }
.toast {
  position: fixed; top: 24px; left: 50%; transform: translateX(-50%);
  background: #1d1d1f; color: #fff; padding: 12px 24px; border-radius: 20px;
  font-size: 14px; z-index: 999; opacity: 0; transition: opacity .3s; pointer-events: none;
}
.toast.show { opacity: 1; }
@keyframes spin { to { transform: rotate(360deg); } }
.spinner { width: 20px; height: 20px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; margin: 40px auto; }
.hidden { display: none !important; }
.modal { position: fixed; inset: 0; z-index: 900; display: flex; align-items: center; justify-content: center; }
.modal-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.4); backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); }
.modal-card { position: relative; background: var(--surface); border-radius: var(--radius); padding: 28px 24px; width: 360px; max-width: 90vw; box-shadow: 0 20px 60px rgba(0,0,0,.2); z-index: 1; }
.modal-card h3 { font-size: 18px; font-weight: 600; margin-bottom: 20px; }
.modal-card input { width: 100%; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 15px; outline: none; margin-bottom: 16px; }
.modal-card input:focus { border-color: var(--accent); }
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; }
.modal-err { color: var(--danger); font-size: 13px; margin-bottom: 12px; display: none; }
.btn-sm { padding: 5px 12px; border-radius: 14px; font-size: 12px; font-weight: 500; border: none; cursor: pointer; }
.btn-sm-primary { background: var(--accent); color: #fff; }
.btn-sm-primary:hover { background: var(--accent-hover); }
.btn-sm-ghost { background: #f2f2f7; color: var(--text); }
.btn-sm-ghost:hover { background: #e5e5ea; }
</style>
</head>
<body>

<div class="login-overlay" id="loginOverlay">
  <div class="login-card">
    <h2>卡片备份管理</h2>
    <div class="sub">请输入管理员账号登录</div>
    <input type="text" id="loginUser" placeholder="用户名" autocomplete="username">
    <input type="password" id="loginPass" placeholder="密码" autocomplete="current-password">
    <button class="login-btn" onclick="doLogin()">登 录</button>
    <div class="login-err" id="loginErr">用户名或密码错误</div>
  </div>
</div>

<div class="app" id="app">
  <div class="header">
    <h1>卡片备份管理</h1>
    <div style="display:flex;gap:10px;align-items:center">
      <span id="headerUser" style="font-size:13px;color:var(--text-secondary)"></span>
      <button class="btn-sm btn-sm-ghost" id="btnUserMgmt" onclick="showUserModal()" style="display:none">用户管理</button>
      <button class="btn-sm btn-sm-primary" onclick="showNewDeviceModal()">新建设备</button>
      <button class="logout-btn" onclick="doLogout()">退出登录</button>
    </div>
  </div>
  <div class="search-bar">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input type="text" id="searchInput" placeholder="搜索卡片名称或卡号..." oninput="filterCards()">
  </div>
  <div id="content"></div>

<div class="modal hidden" id="newDeviceModal">
  <div class="modal-backdrop" onclick="hideNewDeviceModal()"></div>
  <div class="modal-card">
    <h3>新建设备</h3>
    <input type="text" id="newDeviceName" placeholder="输入设备名称（自定义命名）" autocomplete="off">
    <div class="modal-err" id="newDeviceErr"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="hideNewDeviceModal()">取消</button>
      <button class="btn btn-primary" onclick="createDevice()">创建</button>
    </div>
  </div>
</div>

<div class="modal hidden" id="cardPreviewModal">
  <div class="modal-backdrop" onclick="hideCardPreview()"></div>
  <div class="modal-card" style="width:750px;max-width:95vw;max-height:80vh;overflow-y:auto">
    <h3 id="cardPreviewTitle">卡片数据</h3>
    <div id="cardPreviewHex" style="font-family:monospace;font-size:12px;line-height:1.7;background:#1e293b;color:#e2e8f0;padding:16px;border-radius:10px;overflow-x:auto;min-height:60px"></div>
    <div class="modal-actions" style="margin-top:16px">
      <button class="btn btn-ghost" onclick="hideCardPreview()">关闭</button>
    </div>
  </div>
</div>

<div class="modal hidden" id="userModal">
  <div class="modal-backdrop" onclick="hideUserModal()"></div>
  <div class="modal-card" style="width:400px;max-width:90vw">
    <h3>用户管理</h3>
    <div id="userList" style="margin-bottom:16px"></div>
    <div style="display:flex;gap:8px">
      <input type="text" id="newUsername" placeholder="新用户名" autocomplete="off" style="flex:1;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;outline:none">
      <input type="password" id="newPassword" placeholder="密码" autocomplete="off" style="flex:1;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;outline:none">
      <button class="btn btn-primary" onclick="addUser()" style="flex-shrink:0">添加</button>
    </div>
    <div class="modal-err" id="userErr"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="hideUserModal()">关闭</button>
    </div>
  </div>
</div>

<input type="file" id="fileUploadInput" multiple style="display:none" onchange="handleFileUpload(this)">
</div>

<div class="toast" id="toast"></div>

<script>
let _allDevices = [];

function escapeHtml(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 401) { showLogin(); throw new Error('unauthorized'); }
  if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || res.statusText); }
  return res.json();
}

async function doLogin() {
  const u = document.getElementById('loginUser').value;
  const p = document.getElementById('loginPass').value;
  try {
    const r = await api('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p }) });
    document.getElementById('headerUser').textContent = r.username;
    if (r.username === 'admin') {
      document.getElementById('btnUserMgmt').style.display = '';
    } else {
      document.getElementById('btnUserMgmt').style.display = 'none';
    }
    document.getElementById('loginOverlay').style.display = 'none';
    document.getElementById('app').classList.add('visible');
    loadCards();
  } catch(e) { document.getElementById('loginErr').style.display = 'block'; }
}
document.getElementById('loginUser').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('loginPass').focus(); });
document.getElementById('loginPass').addEventListener('keydown', e => { if (e.key === 'Enter') { document.getElementById('loginErr').style.display = 'none'; doLogin(); } });

async function doLogout() {
  await fetch('/api/logout', { method: 'POST' });
  showLogin();
}

function showLogin() {
  document.getElementById('loginOverlay').style.display = 'flex';
  document.getElementById('app').classList.remove('visible');
  document.getElementById('loginErr').style.display = 'none';
  document.getElementById('loginUser').value = '';
  document.getElementById('loginPass').value = '';
}

function tagLabel(tag) {
  const t = (tag || '').toString().toLowerCase();
  if (t === '1001' || t.includes('mifare') || t.includes('classic') || t.includes('ic')) return { text: 'IC', cls: 'tag-ic' };
  if (t === '100' || t.includes('id') || t.includes('em')) return { text: 'ID', cls: 'tag-id' };
  const n = parseInt(t);
  if (n === 1001) return { text: 'IC', cls: 'tag-ic' };
  if (n === 100) return { text: 'ID', cls: 'tag-id' };
  if (t.includes('手动') || t.includes('upload')) return { text: '上传', cls: 'tag-other' };
  if (n >= 0 && n <= 3) return { text: 'IC', cls: 'tag-ic' };
  return { text: tag || '-', cls: 'tag-other' };
}

function filterCards() {
  const q = document.getElementById('searchInput').value.toLowerCase().trim();
  for (const dev of _allDevices) {
    let matchCount = 0;
    const cards = dev._cards;
    for (let i = 0; i < cards.length; i++) {
      const c = cards[i];
      const nm = (c.name || '').toLowerCase();
      const uid = (c.uid || '').toLowerCase();
      const el = c._el;
      if (!q || nm.includes(q) || uid.includes(q)) {
        el.classList.remove('hidden');
        matchCount++;
      } else {
        el.classList.add('hidden');
      }
    }
    if (matchCount > 0 || !q) {
      dev._folderEl.classList.remove('hidden');
    } else {
      dev._folderEl.classList.add('hidden');
    }
  }
}

async function loadCards() {
  document.getElementById('content').innerHTML = '<div class="spinner"></div>';
  _allDevices = [];
  try {
    const devices = await api('/api/devices');
    if (!devices.length) {
      document.getElementById('content').innerHTML = '<div class="empty-state"><div style="font-size:16px">暂无备份数据</div></div>';
      return;
    }
    let html = '';
    for (const dev of devices) {
      const backups = await api('/api/backups/' + dev.chip_id);
      const allCards = [];
      const seen = new Set();
      for (const b of backups) {
        if (!b.cards) continue;
        for (let ci = 0; ci < b.cards.length; ci++) {
          const c = b.cards[ci];
          const key = (c.uid || '') + '_' + (c.name || '');
          if (seen.has(key)) continue;
          seen.add(key);
          allCards.push({ ...c, _backup_time: b.timestamp, _backup_id: b.id, _card_index: ci });
        }
      }
      const fid = 'f' + dev.chip_id.replace(/[^a-zA-Z0-9]/g, '_');
      html += '<div class="folder" id="' + fid + '_folder">';
      html += '<div class="folder-header">';
      html += '<div class="folder-icon" onclick="toggleFolder(\'' + fid + '\')"><svg viewBox="0 0 24 24"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg></div>';
      html += '<div class="folder-info" onclick="toggleFolder(\'' + fid + '\')"><div class="folder-name">' + escapeHtml(dev.chip_id) + '</div><div class="folder-meta">' + allCards.length + ' 张卡片 · 最后备份 ' + (dev.last_backup || '-') + '</div></div>';
      html += '<div class="folder-actions">';
      const escChipId = dev.chip_id.replace(/'/g, "\\'");
      html += '<button class="btn-sm btn-sm-ghost" onclick="event.stopPropagation();triggerUpload(\'' + escChipId + '\')">上传卡片</button>';
      html += '<button class="btn-device-del" onclick="event.stopPropagation();deleteDevice(\'' + escChipId + '\')">删除设备</button>';
      html += '<div class="folder-arrow" id="' + fid + '_arrow" onclick="toggleFolder(\'' + fid + '\')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></div>';
      html += '</div></div>';
      html += '<div class="folder-body" id="' + fid + '_body" style="display:none"><div class="card-list">';
      const cardEls = [];
      if (allCards.length === 0) {
        html += '<div style="padding:20px;color:var(--text-secondary);font-size:14px;text-align:center">暂无卡片，点击上方"上传卡片"添加</div>';
      }
      for (let i = 0; i < allCards.length; i++) {
        const card = allCards[i];
        const ci = 'c' + fid + '_' + i;
        const tag = tagLabel(card.tag || card.tag_type);
        const binUrl = (card.bin_url || '').replace(/"/g, '&quot;');
        const escName = (card.name || '').replace(/"/g, '&quot;');
        const escUid = (card.uid || '').replace(/"/g, '&quot;');
        html += '<div class="card-item" id="' + ci + '" data-bin-url="' + binUrl + '" data-name="' + escName + '" data-uid="' + escUid + '" style="cursor:pointer">';
        html += '<div class="card-info"><div class="name">' + (card.name || '-') + '</div><div class="meta">' + (card.uid || '-') + ' | 备份时间: ' + (card._backup_time || '-') + '</div></div>';
        html += '<div class="card-actions"><span class="tag-badge ' + tag.cls + '">' + tag.text + '</span>';
        if (card.bin_url) {
          html += '<a href="' + encodeURI(card.bin_url) + '" class="btn btn-primary" style="font-size:13px;padding:6px 14px" onclick="event.stopPropagation()">下载 .bin</a>';
        }
        html += '<button class="btn btn-danger" onclick="event.stopPropagation();deleteCard(' + card._backup_id + ',' + card._card_index + ')">删除</button>';
        html += '</div></div>';
        cardEls.push(ci);
      }
      html += '</div></div></div>';
      _allDevices.push({ chip_id: dev.chip_id, _cards: allCards, _folderEl: null, _cardEls: cardEls });
    }
    document.getElementById('content').innerHTML = html || '<div class="empty-state"><div style="font-size:16px">暂无卡片数据</div></div>';
    // Link DOM elements
    for (const dev of _allDevices) {
      dev._folderEl = document.getElementById('f' + dev.chip_id.replace(/[^a-zA-Z0-9]/g, '_') + '_folder');
      for (let i = 0; i < dev._cards.length; i++) {
        dev._cards[i]._el = document.getElementById(dev._cardEls[i]);
      }
    }
  } catch(e) {
    document.getElementById('content').innerHTML = '<div class="empty-state"><div style="font-size:16px">加载失败</div><div style="font-size:13px;color:var(--text-secondary);margin-top:8px">' + escapeHtml(e.message) + '</div></div>';
  }
}

document.getElementById('content').addEventListener('click', function(e) {
  const cardItem = e.target.closest('.card-item');
  if (!cardItem) return;
  previewCard(
    cardItem.dataset.binUrl,
    cardItem.dataset.name,
    cardItem.dataset.uid
  );
});

function toggleFolder(fid) {
  const body = document.getElementById(fid + '_body');
  const arrow = document.getElementById(fid + '_arrow');
  if (body.style.display === 'none') {
    body.style.display = 'block';
    arrow.classList.add('open');
  } else {
    body.style.display = 'none';
    arrow.classList.remove('open');
  }
}

async function deleteDevice(chipId) {
  if (!confirm('确定要删除设备 ' + chipId + ' 的所有备份吗？此操作不可撤销。')) return;
  try {
    await api('/api/devices/' + encodeURIComponent(chipId), { method: 'DELETE' });
    toast('设备已删除');
    loadCards();
  } catch(e) { toast('删除失败: ' + e.message); }
}

async function deleteCard(backupId, idx) {
  if (!confirm('确定要删除这张卡片吗？')) return;
  await api('/api/backups/' + backupId + '/cards/' + idx, { method: 'DELETE' });
  toast('卡片已删除');
  loadCards();
}

let _pendingChipId = null;

function showNewDeviceModal() {
  document.getElementById('newDeviceModal').classList.remove('hidden');
  document.getElementById('newDeviceName').value = '';
  document.getElementById('newDeviceErr').style.display = 'none';
  document.getElementById('newDeviceName').focus();
}

function hideNewDeviceModal() {
  document.getElementById('newDeviceModal').classList.add('hidden');
}

async function createDevice() {
  const name = document.getElementById('newDeviceName').value.trim();
  if (!name) {
    document.getElementById('newDeviceErr').textContent = '请输入设备名称';
    document.getElementById('newDeviceErr').style.display = 'block';
    return;
  }
  try {
    await api('/api/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chip_id: name })
    });
    hideNewDeviceModal();
    toast('设备已创建');
    loadCards();
  } catch (e) {
    const msg = e.message;
    if (msg === 'unauthorized') {
      hideNewDeviceModal();
      return;
    }
    document.getElementById('newDeviceErr').textContent = msg;
    document.getElementById('newDeviceErr').style.display = 'block';
  }
}

function triggerUpload(chipId) {
  _pendingChipId = chipId;
  document.getElementById('fileUploadInput').click();
}

async function handleFileUpload(input) {
  const files = input.files;
  if (!files.length || !_pendingChipId) {
    input.value = '';
    return;
  }
  const chipId = _pendingChipId;
  _pendingChipId = null;
  const formData = new FormData();
  for (const f of files) {
    formData.append('files', f);
  }
  try {
    const res = await fetch('/api/devices/' + encodeURIComponent(chipId) + '/cards', {
      method: 'POST',
      body: formData
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.error || res.statusText);
    }
    const data = await res.json();
    toast('已上传 ' + data.card_count + ' 张卡片');
    loadCards();
  } catch (e) {
    toast('上传失败: ' + e.message);
  }
  input.value = '';
}

document.getElementById('newDeviceName').addEventListener('keydown', e => {
  if (e.key === 'Enter') { document.getElementById('newDeviceErr').style.display = 'none'; createDevice(); }
});

function hexByte(b) { return b.toString(16).padStart(2, '0'); }

function colorByte(b, color) {
  return '<span style="color:' + color + '">' + hexByte(b) + '</span>';
}

function formatBlock(block, blockIdx, sectorIdx, totalSectors) {
  const isTrailer = (blockIdx === 3) || (sectorIdx >= 32 && blockIdx === 15);
  var html = '<div style="display:flex;gap:8px"><span style="color:#64748b;min-width:60px;text-align:right;flex-shrink:0">' + blockIdx + '区块:</span><span>';
  for (var j = 0; j < block.length; j++) {
    var b = block[j];
    var color = null;
    if (sectorIdx === 0 && blockIdx === 0) {
      if (j <= 3) color = '#60a5fa';
    }
    if (isTrailer) {
      if (j <= 5) color = '#f87171';
      else if (j <= 8) color = '#fbbf24';
      else if (j >= 10) color = '#22d3ee';
    }
    if (color) {
      html += colorByte(b, color);
    } else {
      html += hexByte(b);
    }
    if (j < block.length - 1) html += ' ';
  }
  html += '</span></div>';
  return html;
}

function isAllFF(block) {
  for (var i = 0; i < block.length; i++) {
    if (block[i] !== 0xFF) return false;
  }
  return true;
}

function isAll00(block) {
  for (var i = 0; i < block.length; i++) {
    if (block[i] !== 0x00) return false;
  }
  return true;
}

function parseChameleonDump(text) {
  var lines = text.split('\n');
  var allBytes = [];
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (line.length === 0) continue;
    if (line.startsWith('+Sector:')) continue;
    if (/^[0-9A-Fa-f]+$/.test(line) && line.length >= 32) {
      for (var j = 0; j < line.length; j += 2) {
        allBytes.push(parseInt(line.substring(j, j + 2), 16));
      }
    }
  }
  return new Uint8Array(allBytes);
}

function isTextDump(bytes) {
  if (bytes.length < 8) return false;
  for (var i = 0; i < Math.min(bytes.length, 64); i++) {
    if (bytes[i] === 0) return false;
  }
  var head = String.fromCharCode.apply(null, Array.from(bytes.slice(0, 200)));
  return head.indexOf('+Sector:') !== -1;
}

function formatMifareHex(bytes) {
  var size = bytes.length;
  var sectors, blocksPerSector;
  if (size === 1024) { sectors = 16; blocksPerSector = 4; }
  else if (size === 4096) { sectors = 40; blocksPerSector = function(s) { return s < 32 ? 4 : 16; }; }
  else return formatHex(bytes);

  var html = '<div style="color:#64748b;margin-bottom:12px">Mifare Classic ' + (size === 1024 ? '1K' : '4K') + ' | ' + sectors + '扇区 | ' + size + '字节</div>';
  var offset = 0;
  for (var s = 0; s < sectors; s++) {
    var bps = typeof blocksPerSector === 'function' ? blocksPerSector(s) : blocksPerSector;
    html += '<div style="margin-bottom:12px">';
    html += '<div style="color:#38bdf8;font-size:13px;font-weight:600;margin-bottom:6px">' + s + '扇区</div>';
    for (var b = 0; b < bps; b++) {
      var block = bytes.slice(offset, offset + 16);
      offset += 16;
      var dim = '';
      if (isAll00(block)) dim = ';opacity:0.4';
      else if (isAllFF(block)) dim = ';opacity:0.5';
      html += '<div style="padding:2px 0' + dim + '">' + formatBlock(block, b, s, sectors) + '</div>';
    }
    html += '</div>';
  }
  return html;
}

function formatHex(bytes) {
  var lines = [];
  for (var i = 0; i < bytes.length; i += 16) {
    var offset = i.toString(16).padStart(6, '0');
    var chunk = bytes.slice(i, i + 16);
    var hex = Array.from(chunk, function(b) { return b.toString(16).padStart(2, '0'); }).join(' ');
    var ascii = Array.from(chunk, function(b) { return (b >= 32 && b <= 126) ? String.fromCharCode(b) : '.'; }).join('');
    lines.push(offset + '  ' + hex.padEnd(48) + '  ' + ascii);
  }
  return lines.join('\n');
}

async function previewCard(binUrl, name, uid) {
  document.getElementById('cardPreviewTitle').textContent = (name || uid || '卡片') + ' 数据';
  var div = document.getElementById('cardPreviewHex');
  if (!binUrl) {
    div.innerHTML = '<div style="color:#64748b;padding:20px;text-align:center">无二进制数据</div>';
  } else {
    div.innerHTML = '<div style="color:#64748b;padding:20px;text-align:center">加载中...</div>';
    try {
      var res = await fetch(binUrl);
      if (!res.ok) throw new Error('load failed');
      var buf = await res.arrayBuffer();
      var bytes = new Uint8Array(buf);
      if (isTextDump(bytes)) {
        bytes = parseChameleonDump(new TextDecoder().decode(bytes));
      }
      div.innerHTML = formatMifareHex(bytes);
    } catch(e) {
      div.innerHTML = '<div style="color:#f87171;padding:20px;text-align:center">加载失败: ' + e.message + '</div>';
    }
  }
  document.getElementById('cardPreviewModal').classList.remove('hidden');
}

function hideCardPreview() {
  document.getElementById('cardPreviewModal').classList.add('hidden');
}

async function showUserModal() {
  document.getElementById('userModal').classList.remove('hidden');
  document.getElementById('userErr').style.display = 'none';
  await refreshUserList();
}

function hideUserModal() {
  document.getElementById('userModal').classList.add('hidden');
}

async function refreshUserList() {
  try {
    const users = await api('/api/users');
    var html = '';
    for (var i = 0; i < users.length; i++) {
      html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)">';
      html += '<div><div style="font-size:15px;font-weight:500">' + users[i].username + '</div><div style="font-size:12px;color:var(--text-secondary)">' + (users[i].created_at || '-') + '</div></div>';
      if (users.length > 1) {
        html += '<button class="btn btn-danger" onclick="deleteUser(\'' + users[i].username + '\')">删除</button>';
      }
      html += '</div>';
    }
    document.getElementById('userList').innerHTML = html;
  } catch(e) {
    document.getElementById('userList').innerHTML = '<div style="color:var(--danger)">加载失败</div>';
  }
}

async function addUser() {
  const username = document.getElementById('newUsername').value.trim();
  const password = document.getElementById('newPassword').value.trim();
  if (!username || !password) {
    document.getElementById('userErr').textContent = '用户名和密码不能为空';
    document.getElementById('userErr').style.display = 'block';
    return;
  }
  try {
    await api('/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username, password: password })
    });
    document.getElementById('newUsername').value = '';
    document.getElementById('newPassword').value = '';
    document.getElementById('userErr').style.display = 'none';
    toast('用户已添加');
    await refreshUserList();
  } catch(e) {
    document.getElementById('userErr').textContent = e.message;
    document.getElementById('userErr').style.display = 'block';
  }
}

async function deleteUser(username) {
  if (!confirm('确定要删除用户 ' + username + ' 吗？')) return;
  try {
    await api('/api/users/' + encodeURIComponent(username), { method: 'DELETE' });
    toast('用户已删除');
    await refreshUserList();
  } catch(e) {
    toast('删除失败: ' + e.message);
  }
}

(async function init() {
  try {
    const s = await api('/api/session');
    if (s.logged_in) {
      document.getElementById('headerUser').textContent = s.username;
      if (s.is_admin) {
        document.getElementById('btnUserMgmt').style.display = '';
      }
      document.getElementById('loginOverlay').style.display = 'none';
      document.getElementById('app').classList.add('visible');
      loadCards();
    }
  } catch(e) {}
  document.getElementById('loginUser').focus();
})();
</script>
</body>
</html>'''

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)
