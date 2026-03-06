"""
SimpleInventory — lightweight inventory management
https://github.com/yourusername/simpleinventory

A minimal Flask + SQLite web app to track items across
hierarchical locations (Group → Position).

No external database required. Everything lives in a single .db file.
"""

from flask import Flask, render_template, request, jsonify, send_file
import sqlite3, csv, io, datetime, os, json
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER

app = Flask(__name__)
DB         = os.path.join(os.path.dirname(__file__), 'inventory.db')
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')

# ── CONFIG ─────────────────────────────────────────────────────────────────────

_CONFIG_DEFAULTS = {
    "app_name":  "SimpleInventory",
    "id1_label": "Identifier 1",
    "id2_label": "Identifier 2",
}

def load_config():
    """Load config.json, falling back to defaults for any missing key."""
    cfg = dict(_CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[SimpleInventory] Warning: could not read config.json: {e}")
    return cfg

# Config is loaded once at startup. Restart the app after editing config.json.
CFG       = load_config()
APP_NAME  = CFG['app_name']
ID1_LABEL = CFG['id1_label']
ID2_LABEL = CFG['id2_label']

# ── DATABASE ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    INTEGER NOT NULL REFERENCES groups(id),
            name        TEXT NOT NULL,
            description TEXT,
            UNIQUE(group_id, name)
        );

        CREATE TABLE IF NOT EXISTS items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            identifier1 TEXT,
            identifier2 TEXT,
            position_id INTEGER REFERENCES positions(id),
            status      TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available','away','disposed')),
            notes       TEXT,
            added_on    TEXT DEFAULT (date('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS movements (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id             INTEGER NOT NULL REFERENCES items(id),
            from_position_id    INTEGER REFERENCES positions(id),
            to_position_id      INTEGER REFERENCES positions(id),
            action              TEXT NOT NULL
                                CHECK(action IN ('add','move','checkout','checkin','dispose')),
            notes               TEXT,
            operator            TEXT,
            date                TEXT DEFAULT (date('now','localtime'))
        );
        """)

init_db()

# ── HELPERS ────────────────────────────────────────────────────────────────────

def _status_label(s):
    return {'available': 'Available', 'away': 'Away', 'disposed': 'Disposed'}.get(s, s)

def _action_label(a):
    return {'add': 'Added', 'move': 'Moved', 'checkout': 'Checked out',
            'checkin': 'Checked in', 'dispose': 'Disposed'}.get(a, a)

# ── PAGE ───────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', app_name=APP_NAME,
                           id1_label=ID1_LABEL, id2_label=ID2_LABEL)

@app.route('/api/config')
def config():
    return jsonify({'app_name': APP_NAME, 'id1_label': ID1_LABEL, 'id2_label': ID2_LABEL})

# ── GROUPS ─────────────────────────────────────────────────────────────────────

@app.route('/api/groups', methods=['GET'])
def get_groups():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT g.*,
                   COUNT(DISTINCT p.id) as position_count,
                   COUNT(DISTINCT CASE WHEN i.status != 'disposed' THEN i.id END) as item_count
            FROM groups g
            LEFT JOIN positions p ON p.group_id = g.id
            LEFT JOIN items i ON i.position_id = p.id
            GROUP BY g.id ORDER BY g.name
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/groups', methods=['POST'])
def add_group():
    d = request.json
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO groups (name, description) VALUES (?,?)",
                         (name, d.get('description', '')))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True})

@app.route('/api/groups/<int:gid>', methods=['DELETE'])
def del_group(gid):
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE group_id=?", (gid,)
        ).fetchone()[0]
        if count > 0:
            return jsonify({'error': f'Group still has {count} positions'}), 400
        conn.execute("DELETE FROM groups WHERE id=?", (gid,))
    return jsonify({'ok': True})

# ── POSITIONS ──────────────────────────────────────────────────────────────────

@app.route('/api/positions', methods=['GET'])
def get_positions():
    group_id = request.args.get('group_id', '')
    sql = """
        SELECT p.*, g.name as group_name,
               COUNT(CASE WHEN i.status != 'disposed' THEN i.id END) as item_count
        FROM positions p
        JOIN groups g ON p.group_id = g.id
        LEFT JOIN items i ON i.position_id = p.id
        WHERE 1=1
    """
    params = []
    if group_id:
        sql += " AND p.group_id = ?"
        params.append(group_id)
    sql += " GROUP BY p.id ORDER BY g.name, p.name"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/positions', methods=['POST'])
def add_position():
    d = request.json
    name = (d.get('name') or '').strip()
    group_id = d.get('group_id')
    if not name or not group_id:
        return jsonify({'error': 'Name and group are required'}), 400
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO positions (group_id, name, description) VALUES (?,?,?)",
                (group_id, name, d.get('description', '')))
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True})

@app.route('/api/positions/<int:pid>', methods=['DELETE'])
def del_position(pid):
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE position_id=? AND status!='disposed'", (pid,)
        ).fetchone()[0]
        if count > 0:
            return jsonify({'error': f'Position still has {count} active items'}), 400
        conn.execute("DELETE FROM positions WHERE id=?", (pid,))
    return jsonify({'ok': True})

# ── ITEMS ──────────────────────────────────────────────────────────────────────

@app.route('/api/items', methods=['GET'])
def get_items():
    q        = request.args.get('q', '').strip()
    pos_id   = request.args.get('position_id', '')
    group_id = request.args.get('group_id', '')
    status   = request.args.get('status', '')
    sql = """
        SELECT i.*, p.name as position_name, g.name as group_name, g.id as group_id
        FROM items i
        LEFT JOIN positions p ON i.position_id = p.id
        LEFT JOIN groups g ON p.group_id = g.id
        WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (i.name LIKE ? OR i.identifier1 LIKE ? OR i.identifier2 LIKE ? OR i.notes LIKE ?)"
        params += [f'%{q}%'] * 4
    if pos_id:
        sql += " AND i.position_id = ?"; params.append(pos_id)
    if group_id:
        sql += " AND g.id = ?"; params.append(group_id)
    if status:
        sql += " AND i.status = ?"; params.append(status)
    sql += " ORDER BY i.added_on DESC, i.name"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/items', methods=['POST'])
def add_item():
    d    = request.json
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    date = d.get('date') or datetime.date.today().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO items (name, identifier1, identifier2, position_id, notes, added_on)
               VALUES (?,?,?,?,?,?)""",
            (name, d.get('identifier1') or None, d.get('identifier2') or None,
             d.get('position_id') or None, d.get('notes') or None, date))
        item_id = cur.lastrowid
        conn.execute(
            "INSERT INTO movements (item_id, to_position_id, action, notes, operator, date) VALUES (?,?,?,?,?,?)",
            (item_id, d.get('position_id') or None, 'add', 'Added to inventory',
             d.get('operator', ''), date))
    return jsonify({'ok': True, 'id': item_id})

@app.route('/api/items/<int:iid>', methods=['GET'])
def get_item(iid):
    with get_db() as conn:
        item = conn.execute("""
            SELECT i.*, p.name as position_name, g.name as group_name
            FROM items i
            LEFT JOIN positions p ON i.position_id = p.id
            LEFT JOIN groups g ON p.group_id = g.id
            WHERE i.id = ?
        """, (iid,)).fetchone()
        history = conn.execute("""
            SELECT m.*, fp.name as from_pos, tp.name as to_pos,
                   fg.name as from_group, tg.name as to_group
            FROM movements m
            LEFT JOIN positions fp ON m.from_position_id = fp.id
            LEFT JOIN positions tp ON m.to_position_id   = tp.id
            LEFT JOIN groups fg ON fp.group_id = fg.id
            LEFT JOIN groups tg ON tp.group_id = tg.id
            WHERE m.item_id = ? ORDER BY m.date DESC, m.id DESC
        """, (iid,)).fetchall()
    if not item:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'item': dict(item), 'history': [dict(h) for h in history]})

@app.route('/api/items/<int:iid>', methods=['PUT'])
def update_item(iid):
    d = request.json
    with get_db() as conn:
        conn.execute(
            "UPDATE items SET name=?, identifier1=?, identifier2=?, notes=? WHERE id=?",
            (d['name'], d.get('identifier1') or None,
             d.get('identifier2') or None, d.get('notes') or None, iid))
    return jsonify({'ok': True})

# ── MOVEMENTS ──────────────────────────────────────────────────────────────────

@app.route('/api/move', methods=['POST'])
def move_item():
    d       = request.json
    item_id = d['item_id']
    to_pos  = d.get('to_position_id') or None
    action  = d.get('action', 'move')
    date    = d.get('date') or datetime.date.today().isoformat()
    with get_db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            return jsonify({'error': 'Item not found'}), 404
        from_pos   = item['position_id']
        new_status = item['status']
        if action == 'checkout': new_status = 'away'
        elif action == 'checkin': new_status = 'available'
        elif action == 'dispose': new_status = 'disposed'
        conn.execute("UPDATE items SET position_id=?, status=? WHERE id=?",
                     (to_pos, new_status, item_id))
        conn.execute("""INSERT INTO movements
                        (item_id, from_position_id, to_position_id, action, notes, operator, date)
                        VALUES (?,?,?,?,?,?,?)""",
                     (item_id, from_pos, to_pos, action,
                      d.get('notes', ''), d.get('operator', ''), date))
    return jsonify({'ok': True})

@app.route('/api/movements', methods=['GET'])
def get_movements():
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    sql = """
        SELECT m.*, i.name as item_name, i.identifier1, i.identifier2,
               fp.name as from_pos, tp.name as to_pos,
               fg.name as from_group, tg.name as to_group
        FROM movements m
        JOIN items i ON m.item_id = i.id
        LEFT JOIN positions fp ON m.from_position_id = fp.id
        LEFT JOIN positions tp ON m.to_position_id   = tp.id
        LEFT JOIN groups fg ON fp.group_id = fg.id
        LEFT JOIN groups tg ON tp.group_id = tg.id
        WHERE 1=1
    """
    params = []
    if date_from:
        sql += " AND m.date >= ?"; params.append(date_from)
    if date_to:
        sql += " AND m.date <= ?"; params.append(date_to)
    sql += " ORDER BY m.date DESC, m.id DESC LIMIT 500"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])

# ── STATS ──────────────────────────────────────────────────────────────────────

@app.route('/api/stats')
def stats():
    with get_db() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM items WHERE status!='disposed'").fetchone()[0]
        available = conn.execute("SELECT COUNT(*) FROM items WHERE status='available'").fetchone()[0]
        away      = conn.execute("SELECT COUNT(*) FROM items WHERE status='away'").fetchone()[0]
        disposed  = conn.execute("SELECT COUNT(*) FROM items WHERE status='disposed'").fetchone()[0]
        groups    = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        recent    = conn.execute(
            "SELECT COUNT(*) FROM movements WHERE date >= date('now','-7 days','localtime')"
        ).fetchone()[0]
    return jsonify({'total': total, 'available': available, 'away': away,
                    'disposed': disposed, 'groups': groups, 'positions': positions,
                    'recent_moves': recent})

# ── EXPORT CSV ─────────────────────────────────────────────────────────────────

@app.route('/api/export/items')
def export_items_csv():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT i.id, i.name, i.identifier1, i.identifier2,
                   g.name as grp, p.name as pos, i.status, i.notes, i.added_on
            FROM items i
            LEFT JOIN positions p ON i.position_id = p.id
            LEFT JOIN groups g ON p.group_id = g.id
            ORDER BY g.name, p.name, i.name
        """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['ID', 'Name', ID1_LABEL, ID2_LABEL, 'Group', 'Position', 'Status', 'Notes', 'Added on'])
    for r in rows:
        w.writerow(list(r))
    buf.seek(0)
    return send_file(io.BytesIO(buf.getvalue().encode('utf-8-sig')),
                     mimetype='text/csv', as_attachment=True,
                     download_name=f'inventory_{datetime.date.today()}.csv')

@app.route('/api/export/movements')
def export_movements_csv():
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    sql = """
        SELECT m.date, i.name, i.identifier1, i.identifier2,
               fg.name as from_group, fp.name as from_pos,
               tg.name as to_group,   tp.name as to_pos,
               m.action, m.operator, m.notes
        FROM movements m JOIN items i ON m.item_id = i.id
        LEFT JOIN positions fp ON m.from_position_id = fp.id
        LEFT JOIN positions tp ON m.to_position_id   = tp.id
        LEFT JOIN groups fg ON fp.group_id = fg.id
        LEFT JOIN groups tg ON tp.group_id = tg.id
        WHERE 1=1
    """
    params = []
    if date_from: sql += " AND m.date >= ?"; params.append(date_from)
    if date_to:   sql += " AND m.date <= ?"; params.append(date_to)
    sql += " ORDER BY m.date DESC, m.id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Date', 'Item', ID1_LABEL, ID2_LABEL,
                'From Group', 'From Position', 'To Group', 'To Position',
                'Action', 'Operator', 'Notes'])
    for r in rows:
        w.writerow(list(r))
    buf.seek(0)
    return send_file(io.BytesIO(buf.getvalue().encode('utf-8-sig')),
                     mimetype='text/csv', as_attachment=True,
                     download_name=f'movements_{datetime.date.today()}.csv')

# ── PDF HELPERS ────────────────────────────────────────────────────────────────

def _pdf_styles():
    dark   = colors.HexColor('#1a1a2e')
    accent = colors.HexColor('#3a86ff')
    gray   = colors.HexColor('#555555')
    return {
        'title':  ParagraphStyle('SI_title',  fontName='Helvetica-Bold', fontSize=20, textColor=dark, spaceAfter=2),
        'sub':    ParagraphStyle('SI_sub',    fontName='Helvetica',      fontSize=9,  textColor=gray, spaceAfter=10),
        'footer': ParagraphStyle('SI_footer', fontName='Helvetica',      fontSize=8,  textColor=gray, alignment=TA_CENTER),
        'dark': dark, 'accent': accent, 'gray': gray,
    }

def _pdf_header(story, title, subtitle, s):
    story.append(Paragraph(APP_NAME, s['title']))
    story.append(Paragraph(
        f'{title} &nbsp;·&nbsp; generated on {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}', s['sub']))
    if subtitle:
        story.append(Paragraph(subtitle, s['sub']))
    story.append(HRFlowable(width='100%', thickness=1.5, color=s['accent'], spaceAfter=14))

def _base_table_style(s):
    return [
        ('BACKGROUND',    (0, 0), (-1, 0), s['dark']),
        ('TEXTCOLOR',     (0, 0), (-1, 0), colors.white),
        ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, 0), 8),
        ('FONTNAME',      (0, 1), (-1,-1), 'Helvetica'),
        ('FONTSIZE',      (0, 1), (-1,-1), 7.5),
        ('ROWBACKGROUNDS',(0, 1), (-1,-1), [colors.white, colors.HexColor('#f4f7ff')]),
        ('VALIGN',        (0, 0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1,-1), 4),
        ('BOTTOMPADDING', (0, 0), (-1,-1), 4),
        ('LEFTPADDING',   (0, 0), (-1,-1), 5),
        ('BOX',           (0, 0), (-1,-1), 0.5, s['gray']),
        ('LINEBELOW',     (0, 0), (-1, 0), 1,   s['accent']),
        ('INNERGRID',     (0, 1), (-1,-1), 0.2, colors.HexColor('#dddddd')),
    ]

# ── EXPORT PDF ─────────────────────────────────────────────────────────────────

@app.route('/api/export/items/pdf')
def export_items_pdf():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT i.id, i.name, i.identifier1, i.identifier2,
                   g.name as grp, p.name as pos, i.status, i.notes
            FROM items i
            LEFT JOIN positions p ON i.position_id = p.id
            LEFT JOIN groups g ON p.group_id = g.id
            ORDER BY g.name, p.name, i.name
        """).fetchall()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=18*mm, rightMargin=18*mm,
                            topMargin=18*mm, bottomMargin=18*mm)
    s = _pdf_styles()
    story = []

    available = sum(1 for r in rows if r['status'] == 'available')
    away      = sum(1 for r in rows if r['status'] == 'away')
    disposed  = sum(1 for r in rows if r['status'] == 'disposed')
    _pdf_header(story, 'Inventory', f'Total active items: {available + away}', s)

    st = Table([['Available', 'Away', 'Disposed'], [str(available), str(away), str(disposed)]],
               colWidths=[55*mm]*3)
    st.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,0), colors.HexColor('#f0f4ff')),
        ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',      (0,0), (-1,0), 8),
        ('FONTNAME',      (0,1), (-1,1), 'Helvetica-Bold'),
        ('FONTSIZE',      (0,1), (-1,1), 14),
        ('TEXTCOLOR',     (0,1), (-1,1), s['accent']),
        ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('BOX',           (0,0), (-1,-1), 0.5, s['gray']),
        ('INNERGRID',     (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
    ]))
    story.append(st)
    story.append(Spacer(1, 8*mm))

    headers = ['#', 'Name', ID1_LABEL, ID2_LABEL, 'Group', 'Position', 'Status']
    col_w   = [10*mm, 60*mm, 28*mm, 28*mm, 28*mm, 24*mm, 22*mm]
    data    = [headers] + [
        [str(r['id']), r['name'] or '', r['identifier1'] or '—', r['identifier2'] or '—',
         r['grp'] or '—', r['pos'] or '—', _status_label(r['status'])]
        for r in rows
    ]
    tbl = Table(data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle(_base_table_style(s) + [
        ('ALIGN', (0,0), (0,-1), 'CENTER'),
        ('ALIGN', (5,0), (6,-1), 'CENTER'),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8*mm))
    story.append(Paragraph(f'{APP_NAME} · Inventory Report · {datetime.date.today()}', s['footer']))
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name=f'inventory_{datetime.date.today()}.pdf')


@app.route('/api/export/movements/pdf')
def export_movements_pdf():
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    sql = """
        SELECT m.date, i.name, i.identifier1, i.identifier2,
               fg.name as from_group, fp.name as from_pos,
               tg.name as to_group,   tp.name as to_pos,
               m.action, m.operator, m.notes
        FROM movements m JOIN items i ON m.item_id = i.id
        LEFT JOIN positions fp ON m.from_position_id = fp.id
        LEFT JOIN positions tp ON m.to_position_id   = tp.id
        LEFT JOIN groups fg ON fp.group_id = fg.id
        LEFT JOIN groups tg ON tp.group_id = tg.id
        WHERE 1=1
    """
    params = []
    if date_from: sql += " AND m.date >= ?"; params.append(date_from)
    if date_to:   sql += " AND m.date <= ?"; params.append(date_to)
    sql += " ORDER BY m.date DESC, m.id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=14*mm, rightMargin=14*mm,
                            topMargin=18*mm, bottomMargin=18*mm)
    s = _pdf_styles()
    story = []
    range_label = ''
    if date_from or date_to:
        range_label = f'Period: {date_from or "start"} → {date_to or "today"}  ·  '
    _pdf_header(story, 'Movement Log', f'{range_label}{len(rows)} entries', s)

    headers = ['Date', 'Item', ID1_LABEL, 'Action', 'From', 'To', 'Operator', 'Notes']
    col_w   = [22*mm, 42*mm, 25*mm, 20*mm, 28*mm, 28*mm, 22*mm, 35*mm]
    data    = [headers] + [
        [r['date'] or '',
         r['name'] or '',
         r['identifier1'] or '—',
         _action_label(r['action']),
         f"{r['from_group'] or ''}  {r['from_pos'] or ''}".strip() or '—',
         f"{r['to_group'] or ''}  {r['to_pos'] or ''}".strip() or '—',
         r['operator'] or '',
         r['notes'] or '']
        for r in rows
    ]
    tbl = Table(data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle(_base_table_style(s)))
    story.append(tbl)
    story.append(Spacer(1, 8*mm))
    story.append(Paragraph(f'{APP_NAME} · Movement Log · {datetime.date.today()}', s['footer']))
    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=True,
                     download_name=f'movements_{datetime.date.today()}.pdf')

# ── RUN ────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
