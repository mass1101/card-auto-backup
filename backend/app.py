from flask import Flask, request, jsonify, g, send_file, send_from_directory, make_response, abort
import sqlite3
import os
import json
import base64
import io
import secrets
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


ADMIN_USER = 'admin'
ADMIN_PASS = 'xiang1101'
AUTH_TOKEN = secrets.token_hex(32)


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    if data.get('username') == ADMIN_USER and data.get('password') == ADMIN_PASS:
        resp = make_response(jsonify({'ok': True}))
        resp.set_cookie('auth_token', AUTH_TOKEN, httponly=True, samesite='Strict')
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
    return jsonify({'logged_in': token == AUTH_TOKEN})


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
        "INSERT INTO backups (chip_id, cards_json, timestamp) VALUES (?, ?, datetime('now', '+8 hours'))",
        (chip_id, json.dumps(cards_stored))
    )
    db.commit()
    backup_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    return jsonify({'backup_id': backup_id}), 201


@app.route('/api/devices', methods=['GET'])
def list_devices():
    db = get_db()
    rows = db.execute('''
        SELECT chip_id,
               MAX(timestamp) as last_backup,
               (SELECT cards_json FROM backups b2 WHERE b2.chip_id = b.chip_id ORDER BY b2.timestamp DESC LIMIT 1) as latest_cards,
               COUNT(*) as backup_count
        FROM backups b
        GROUP BY chip_id
        ORDER BY last_backup DESC
    ''').fetchall()

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
    rows = db.execute(
        'SELECT id, timestamp, cards_json FROM backups WHERE chip_id = ? ORDER BY timestamp DESC',
        (chip_id,)
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
    row = db.execute('SELECT chip_id, cards_json FROM backups WHERE id = ?', (backup_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
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
    row = db.execute('SELECT chip_id, cards_json FROM backups WHERE id = ?', (backup_id,)).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
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
    rows = db.execute('SELECT id, cards_json FROM backups WHERE chip_id = ?', (chip_id,)).fetchall()
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
    <button class="logout-btn" onclick="doLogout()">退出登录</button>
  </div>
  <div class="search-bar">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input type="text" id="searchInput" placeholder="搜索卡片名称或卡号..." oninput="filterCards()">
  </div>
  <div id="content"></div>
</div>

<div class="toast" id="toast"></div>

<script>
let _allDevices = [];

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
    await api('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p }) });
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
      if (!allCards.length) continue;
      const fid = 'f' + dev.chip_id.replace(/[^a-zA-Z0-9]/g, '_');
      html += '<div class="folder" id="' + fid + '_folder">';
      html += '<div class="folder-header">';
      html += '<div class="folder-icon" onclick="toggleFolder(\'' + fid + '\')"><svg viewBox="0 0 24 24"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg></div>';
      html += '<div class="folder-info" onclick="toggleFolder(\'' + fid + '\')"><div class="folder-name">' + dev.chip_id + '</div><div class="folder-meta">' + allCards.length + ' 张卡片 · 最后备份 ' + (dev.last_backup || '-') + '</div></div>';
      html += '<div class="folder-actions">';
      html += '<button class="btn-device-del" onclick="event.stopPropagation();deleteDevice(\'' + dev.chip_id + '\')">删除设备</button>';
      html += '<div class="folder-arrow" id="' + fid + '_arrow" onclick="toggleFolder(\'' + fid + '\')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></div>';
      html += '</div></div>';
      html += '<div class="folder-body" id="' + fid + '_body" style="display:none"><div class="card-list">';
      const cardEls = [];
      for (let i = 0; i < allCards.length; i++) {
        const card = allCards[i];
        const ci = 'c' + fid + '_' + i;
        const tag = tagLabel(card.tag || card.tag_type);
        html += '<div class="card-item" id="' + ci + '">';
        html += '<div class="card-info"><div class="name">' + (card.name || '-') + '</div><div class="meta">' + (card.uid || '-') + ' | 备份时间: ' + (card._backup_time || '-') + '</div></div>';
        html += '<div class="card-actions"><span class="tag-badge ' + tag.cls + '">' + tag.text + '</span>';
        if (card.bin_url) {
          html += '<a href="' + encodeURI(card.bin_url) + '" class="btn btn-primary" style="font-size:13px;padding:6px 14px">下载 .bin</a>';
        }
        html += '<button class="btn btn-danger" onclick="deleteCard(' + card._backup_id + ',' + card._card_index + ')">删除</button>';
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
    document.getElementById('content').innerHTML = '<div class="empty-state"><div style="font-size:16px">加载失败</div></div>';
  }
}

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

(async function init() {
  try {
    const s = await api('/api/session');
    if (s.logged_in) {
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
