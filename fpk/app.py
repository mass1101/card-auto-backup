from flask import Flask, request, jsonify, g
import sqlite3
import os

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'backup.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


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

    import json
    db = get_db()
    db.execute(
        'INSERT INTO backups (chip_id, cards_json) VALUES (?, ?)',
        (chip_id, json.dumps(cards))
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

    import json
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

    import json
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


ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Card Backup Admin</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 24px; }
h1 { font-size: 22px; margin-bottom: 20px; color: #38bdf8; }
.tabs { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
.tab { padding: 8px 18px; border: 1px solid #334155; border-radius: 6px; cursor: pointer; background: #1e293b; color: #94a3b8; font-size: 14px; }
.tab.active { background: #38bdf8; color: #0f172a; border-color: #38bdf8; }
.card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
.row { display: flex; justify-content: space-between; align-items: center; }
.chip-id { font-family: monospace; color: #38bdf8; font-size: 15px; }
.meta { color: #64748b; font-size: 13px; margin-top: 4px; }
.btn { padding: 6px 14px; border: 1px solid #334155; border-radius: 5px; cursor: pointer; background: #334155; color: #e2e8f0; font-size: 13px; }
.btn:hover { background: #475569; }
.back-btn { margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #334155; font-size: 14px; }
th { color: #64748b; font-weight: 500; }
.empty { text-align: center; color: #64748b; padding: 40px 0; }
</style>
</head>
<body>
<h1>Card Backup Admin</h1>
<div class="tabs">
  <div class="tab active" onclick="showDevices()">Devices</div>
</div>
<div id="content"></div>

<script>
let state = { chipId: null };

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

async function showDevices() {
  setActiveTab(0);
  state.chipId = null;
  const tabs = document.querySelector('.tabs');
  tabs.innerHTML = '<div class="tab active" onclick="showDevices()">Devices</div>';
  try {
    const devices = await api('/api/devices');
    if (!devices.length) {
      document.getElementById('content').innerHTML = '<div class="empty">No devices yet</div>';
      return;
    }
    document.getElementById('content').innerHTML = devices.map(d => `
      <div class="card">
        <div class="row">
          <div>
            <div class="chip-id">${d.chip_id}</div>
            <div class="meta">Last backup: ${d.last_backup} | Cards: ${d.card_count} | Backups: ${d.backup_count}</div>
          </div>
          <button class="btn" onclick="showBackups('${d.chip_id}')">View</button>
        </div>
      </div>
    `).join('');
  } catch(e) {
    document.getElementById('content').innerHTML = '<div class="empty">Error loading devices</div>';
  }
}

async function showBackups(chipId) {
  state.chipId = chipId;
  const tabs = document.querySelector('.tabs');
  tabs.innerHTML = '<div class="tab" onclick="showDevices()">Devices</div><div class="tab active">' + chipId + '</div>';
  try {
    const backups = await api('/api/backups/' + chipId);
    document.getElementById('content').innerHTML = `
      <button class="btn back-btn" onclick="showDevices()">Back to devices</button>
      ${backups.map(b => `
        <div class="card">
          <div class="row">
            <div>
              <div>Backup #${b.id}</div>
              <div class="meta">${b.timestamp} | ${b.card_count} cards</div>
            </div>
            <button class="btn" onclick="showDetail(${b.id}, ${JSON.stringify(b.cards).replace(/"/g, '&quot;')})">Detail</button>
          </div>
        </div>
      `).join('')}
    `;
  } catch(e) {
    document.getElementById('content').innerHTML = '<div class="empty">Error loading backups</div>';
  }
}

function showDetail(backupId, cards) {
  const tabs = document.querySelector('.tabs');
  tabs.innerHTML = '<div class="tab" onclick="showDevices()">Devices</div><div class="tab" onclick="showBackups(\'' + state.chipId + '\')">' + state.chipId + '</div><div class="tab active">Backup #' + backupId + '</div>';
  document.getElementById('content').innerHTML = `
    <button class="btn back-btn" onclick="showBackups('${state.chipId}')">Back to backups</button>
    <table>
      <tr><th>Name</th><th>UID</th><th>Type</th></tr>
      ${cards.map(c => `<tr><td>${c.name || '-'}</td><td style="font-family:monospace">${c.uid || '-'}</td><td>${c.tag || '-'}</td></tr>`).join('')}
    </table>
  `;
}

function setActiveTab(idx) {
  document.querySelectorAll('.tab').forEach((t, i) => t.classList.toggle('active', i === idx));
}

showDevices();
</script>
</body>
</html>'''


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
