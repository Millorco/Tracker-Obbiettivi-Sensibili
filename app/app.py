from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
import os, json, csv, io, math, smtplib, glob, re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import xml.etree.ElementTree as ET
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'gps-tracker-secret-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////app/db/gps_tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

APP_PASSWORD   = os.environ.get('APP_PASSWORD', 'admin123')
FTP_UPLOAD_DIR = os.environ.get('FTP_UPLOAD_DIR', '/ftp-data/uploads')

SHIFTS = [
    ('Notte',      '00:00', '07:00'),
    ('Mattina',    '07:00', '13:00'),
    ('Pomeriggio', '13:00', '19:00'),
    ('Sera',       '19:00', '00:00'),
]

# Shift time ranges for PDF title  e.g. "13-19"
SHIFT_RANGES = {
    'Notte':      '00-07',
    'Mattina':    '07-13',
    'Pomeriggio': '13-19',
    'Sera':       '19-00',
}

# --- Models ---

class Location(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200), nullable=False)
    lat        = db.Column(db.Float, nullable=False)
    lon        = db.Column(db.Float, nullable=False)
    notes      = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Setting(db.Model):
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(500))

with app.app_context():
    db.create_all()
    defaults = [
        ('proximity_meters', '50'),
        ('cooldown_minutes', '30'),
        ('smtp_host', 'smtp.gmail.com'),
        ('smtp_port', '587'),
        ('smtp_user', ''),
        ('smtp_pass', ''),
        ('report_email', ''),
    ]
    for k, v in defaults:
        if not Setting.query.get(k):
            db.session.add(Setting(key=k, value=v))
    db.session.commit()

def get_setting(key, default=''):
    s = Setting.query.get(key)
    return s.value if s else default

def set_setting(key, value):
    s = Setting.query.get(key)
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()

def get_smtp_config():
    # DB settings override env vars
    return {
        'host':  get_setting('smtp_host') or os.environ.get('SMTP_HOST', 'smtp.gmail.com'),
        'port':  int(get_setting('smtp_port') or os.environ.get('SMTP_PORT', '587')),
        'user':  get_setting('smtp_user') or os.environ.get('SMTP_USER', ''),
        'pass_': get_setting('smtp_pass') or os.environ.get('SMTP_PASS', ''),
        'to':    get_setting('report_email') or os.environ.get('REPORT_EMAIL', ''),
    }

# --- Auth ---

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# --- GPS helpers ---

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def parse_coords(raw):
    raw = raw.strip()
    parts = [p.strip() for p in raw.split(',')]
    if len(parts) == 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
    raise ValueError(f"Formato coordinate non valido: '{raw}'. Usa: 46.123456, 12.123456")

def latest_gpx_file():
    if not os.path.exists(FTP_UPLOAD_DIR):
        return None
    files = [f for f in os.listdir(FTP_UPLOAD_DIR) if f.lower().endswith('.gpx')]
    if not files:
        return None
    files.sort(key=lambda f: os.path.getmtime(os.path.join(FTP_UPLOAD_DIR, f)), reverse=True)
    return files[0]

def list_track_files():
    if not os.path.exists(FTP_UPLOAD_DIR):
        return []
    files = []
    for f in os.listdir(FTP_UPLOAD_DIR):
        if f.lower().endswith(('.gpx', '.csv', '.json')):
            fp = os.path.join(FTP_UPLOAD_DIR, f)
            files.append({'name': f, 'size': os.path.getsize(fp),
                          'mtime': datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%d/%m/%Y %H:%M'),
                          'ts': os.path.getmtime(fp)})
    files.sort(key=lambda x: x['ts'], reverse=True)
    return files

# --- Parsers ---

def parse_gpx(content):
    points = []
    try:
        root = ET.fromstring(content)
        namespaces_to_try = [
            {'gpx': 'http://www.topografix.com/GPX/1/1'},
            {'gpx': 'http://www.topografix.com/GPX/1/0'},
        ]
        trkpts = []
        for ns in namespaces_to_try:
            trkpts = root.findall('.//gpx:trkpt', ns)
            if trkpts:
                break
        if not trkpts:
            trkpts = root.findall('.//{*}trkpt')
        if not trkpts:
            trkpts = root.findall('.//trkpt')
        for trkpt in trkpts:
            lat = float(trkpt.get('lat'))
            lon = float(trkpt.get('lon'))
            time_el = trkpt.find('{http://www.topografix.com/GPX/1/1}time')
            if time_el is None:
                time_el = trkpt.find('{http://www.topografix.com/GPX/1/0}time')
            if time_el is None:
                time_el = trkpt.find('{*}time')
            if time_el is None:
                time_el = trkpt.find('time')
            time_str = time_el.text if time_el is not None else None
            ts = None
            if time_str:
                for fmt in ['%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S']:
                    try:
                        ts = datetime.strptime(time_str, fmt); break
                    except: pass
            points.append({'lat': lat, 'lon': lon, 'time': ts})
    except Exception as e:
        app.logger.error(f"GPX parse error: {e}")
    return points

def parse_csv(content):
    points = []
    try:
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            lat_key  = next((k for k in row if 'lat' in k.lower()), None)
            lon_key  = next((k for k in row if 'lon' in k.lower() or 'lng' in k.lower()), None)
            time_key = next((k for k in row if 'time' in k.lower() or 'date' in k.lower()), None)
            if lat_key and lon_key:
                ts = None
                if time_key and row[time_key]:
                    for fmt in ['%Y-%m-%dT%H:%M:%SZ','%Y-%m-%dT%H:%M:%S','%Y-%m-%d %H:%M:%S','%d/%m/%Y %H:%M:%S']:
                        try:
                            ts = datetime.strptime(row[time_key].strip(), fmt); break
                        except: pass
                points.append({'lat': float(row[lat_key]), 'lon': float(row[lon_key]), 'time': ts})
    except Exception as e:
        app.logger.error(f"CSV parse error: {e}")
    return points

def parse_json(content):
    points = []
    try:
        data = json.loads(content)
        items = data if isinstance(data, list) else data.get('points', data.get('track', data.get('features', [data])))
        for item in items:
            if not isinstance(item, dict): continue
            lat = item.get('lat', item.get('latitude'))
            lon = item.get('lon', item.get('lng', item.get('longitude')))
            if lat is None and isinstance(item.get('geometry'), dict):
                c = item['geometry'].get('coordinates', [None, None])
                lat, lon = c[1], c[0]
            time_val = item.get('time', item.get('timestamp', item.get('datetime')))
            if time_val is None and isinstance(item.get('properties'), dict):
                time_val = item['properties'].get('time')
            if lat is not None and lon is not None:
                ts = None
                if time_val:
                    for fmt in ['%Y-%m-%dT%H:%M:%SZ','%Y-%m-%dT%H:%M:%S','%Y-%m-%d %H:%M:%S']:
                        try:
                            ts = datetime.strptime(str(time_val)[:19], fmt); break
                        except: pass
                points.append({'lat': float(lat), 'lon': float(lon), 'time': ts})
    except Exception as e:
        app.logger.error(f"JSON parse error: {e}")
    return points

# --- Analysis ---

def analyze_track(points, locations, proximity_m, cooldown_min):
    visits = []
    cooldown = {}
    for pt in points:
        if not pt['time']: continue
        for loc in locations:
            dist = haversine(pt['lat'], pt['lon'], loc.lat, loc.lon)
            if dist <= proximity_m:
                last_visit = cooldown.get(loc.id)
                if last_visit is None or (pt['time'] - last_visit).total_seconds() > cooldown_min * 60:
                    visits.append({'time': pt['time'], 'location': loc, 'distance': dist})
                    cooldown[loc.id] = pt['time']
    visits.sort(key=lambda x: x['time'])
    return visits

def detect_shift(fallback_dt=None):
    ref = fallback_dt or datetime.now()
    hour = ref.hour
    for name, start, end in SHIFTS:
        sh = int(start.split(':')[0])
        eh = int(end.split(':')[0])
        if start == '19:00' and end == '00:00':
            if hour >= 19: return name
        elif sh <= hour < (eh if eh > 0 else 24):
            return name
    return 'Notte'

def build_report_text(visits, shift_name, report_date):
    shift_range = SHIFT_RANGES.get(shift_name, shift_name)
    lines = [
        f"Scheda Obbiettivi Sensibili turno {shift_range} del {report_date.strftime('%d.%m.%Y')}",
        "=" * 50, ""
    ]
    if visits:
        for v in visits:
            lines.append(f"{v['time'].strftime('%H:%M')}\t{v['location'].name}")
    else:
        lines.append("Nessun luogo speciale visitato durante questo turno.")
    return "\n".join(lines)

def pdf_filename(shift_name, report_date):
    shift_range = SHIFT_RANGES.get(shift_name, shift_name)
    return f"{report_date.strftime('%d.%m.%Y')} turno {shift_range} - Scheda Obbiettivi Sensibili.pdf"

def pdf_title(shift_name, report_date):
    shift_range = SHIFT_RANGES.get(shift_name, shift_name)
    return f"Scheda Obbiettivi Sensibili turno {shift_range} del {report_date.strftime('%d.%m.%Y')}"

# --- PDF generation ---

def build_report_pdf(visits, shift_name, report_date):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.5*cm, rightMargin=2.5*cm,
        topMargin=2.5*cm, bottomMargin=2.5*cm
    )

    PRIMARY = colors.HexColor('#1a56db')
    LIGHT   = colors.HexColor('#f4f6f9')
    BORDER  = colors.HexColor('#e2e8f0')
    TEXT    = colors.HexColor('#0f172a')
    MUTED   = colors.HexColor('#64748b')

    story = []
    story.append(Paragraph(pdf_title(shift_name, report_date),
        ParagraphStyle('title', fontName='Helvetica-Bold', fontSize=16,
                       textColor=PRIMARY, spaceAfter=4, leading=20)))

    story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY, spaceAfter=18))

    if visits:
        table_data = [
            [Paragraph('<b>Orario</b>', ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=10, textColor=MUTED)),
             Paragraph('<b>Luogo</b>',  ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=10, textColor=MUTED))]
        ]
        for v in visits:
            table_data.append([
                Paragraph(f"<b>{v['time'].strftime('%H:%M')}</b>",
                          ParagraphStyle('time', fontName='Helvetica-Bold', fontSize=14, textColor=PRIMARY)),
                Paragraph(v['location'].name,
                          ParagraphStyle('place', fontName='Helvetica', fontSize=13, textColor=TEXT))
            ])
        t = Table(table_data, colWidths=[3.5*cm, None])
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0), LIGHT),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, LIGHT]),
            ('LINEBELOW',     (0,0), (-1,-1), 0.5, BORDER),
            ('TOPPADDING',    (0,0), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,-1), 10),
            ('LEFTPADDING',   (0,0), (-1,-1), 12),
            ('RIGHTPADDING',  (0,0), (-1,-1), 12),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("Nessun luogo speciale visitato durante questo turno.",
            ParagraphStyle('normal', fontName='Helvetica', fontSize=11, textColor=TEXT)))

    doc.build(story)
    buf.seek(0)
    return buf

# --- Email ---

def send_email_report(report_text, shift_name, report_date, visits):
    smtp = get_smtp_config()
    if not smtp['user'] or not smtp['to']:
        return False, "Configurazione email mancante (utente SMTP / email destinatario)"
    try:
        msg = MIMEMultipart('mixed')
        msg['Subject'] = pdf_title(shift_name, report_date)
        msg['From']    = smtp['user']
        msg['To']      = smtp['to']

        html_rows = ""
        for line in report_text.split('\n'):
            if '\t' in line:
                t, p = line.split('\t', 1)
                html_rows += f"<tr><td class='time'>{t}</td><td class='place'>{p}</td></tr>"
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{font-family:Arial,sans-serif;background:#f4f6f9;padding:20px;color:#0f172a}}
.wrap{{max-width:560px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}
.head{{background:#1a56db;color:#fff;padding:24px 28px}}
.head h2{{font-size:15px;font-weight:700;margin:0 0 4px;line-height:1.4}}
.head p{{font-size:12px;opacity:.8;margin:0}}
.body{{padding:24px 28px}}
table{{width:100%;border-collapse:collapse}}
.time{{color:#1a56db;padding:12px 16px 12px 0;font-size:18px;font-weight:700;white-space:nowrap;border-bottom:1px solid #e2e8f0;font-family:monospace}}
.place{{padding:12px 0;font-size:15px;border-bottom:1px solid #e2e8f0;color:#0f172a}}
.footer{{padding:16px 28px;background:#f4f6f9;font-size:11px;color:#94a3b8;border-top:1px solid #e2e8f0}}
.empty{{color:#94a3b8;padding:20px 0;font-size:14px}}
</style></head><body>
<div class='wrap'>
<div class='head'><h2>{pdf_title(shift_name, report_date)}</h2></div>
<div class='body'><table>{html_rows if html_rows else "<tr><td colspan='2' class='empty'>Nessun luogo visitato</td></tr>"}</table></div>
<div class='footer'>Report automatico — GPS Tracker</div>
</div></body></html>"""

        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(report_text, 'plain'))
        alt.attach(MIMEText(html, 'html'))
        msg.attach(alt)

        pdf_buf  = build_report_pdf(visits, shift_name, report_date)
        fname    = pdf_filename(shift_name, report_date)
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(pdf_buf.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{fname}"')
        msg.attach(part)

        with smtplib.SMTP(smtp['host'], smtp['port']) as server:
            server.starttls()
            server.login(smtp['user'], smtp['pass_'])
            server.sendmail(smtp['user'], smtp['to'], msg.as_string())
        return True, "Email inviata con successo"
    except Exception as e:
        return False, str(e)

# --- Routes ---

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        flash('Password errata')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    files = list_track_files()
    auto  = latest_gpx_file()
    locations = Location.query.order_by(Location.name).all()
    return render_template('index.html', ftp_files=files, auto_file=auto, locations=locations)

@app.route('/analyze', methods=['POST'])
@login_required
def analyze():
    filename = request.form.get('filename') or latest_gpx_file()
    if not filename:
        flash('Nessun file disponibile')
        return redirect(url_for('index'))
    filepath = os.path.join(FTP_UPLOAD_DIR, os.path.basename(filename))
    if not os.path.exists(filepath):
        flash('File non trovato')
        return redirect(url_for('index'))
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    ext = filename.rsplit('.', 1)[-1].lower()
    parsers = {'gpx': parse_gpx, 'csv': parse_csv, 'json': parse_json}
    if ext not in parsers:
        flash('Formato file non supportato')
        return redirect(url_for('index'))
    points = parsers[ext](content)
    if not points:
        flash('Nessun punto GPS trovato nel file')
        return redirect(url_for('index'))

    proximity_m  = float(get_setting('proximity_meters', '50'))
    cooldown_min = float(get_setting('cooldown_minutes', '30'))

    locs   = Location.query.all()
    visits = analyze_track(points, locs, proximity_m, cooldown_min)
    timed  = [p for p in points if p['time']]
    ref_dt = timed[0]['time'] if timed else datetime.now()
    shift_name = detect_shift(ref_dt)

    report_text = build_report_text(visits, shift_name, ref_dt)
    session['last_report'] = report_text
    session['last_shift']  = shift_name
    session['last_date']   = ref_dt.strftime('%d/%m/%Y')
    session['last_file']   = filename
    session['last_visits'] = [
        {'time': v['time'].strftime('%H:%M'), 'name': v['location'].name}
        for v in visits
    ]

    return render_template('report.html',
        visits=visits, shift_name=shift_name, report_date=ref_dt,
        report_text=report_text, filename=filename,
        total_points=len(points),
        pdf_title=pdf_title(shift_name, ref_dt))

@app.route('/send_report', methods=['POST'])
@login_required
def send_report():
    report_text = session.get('last_report', '')
    shift_name  = session.get('last_shift', '')
    date_str    = session.get('last_date', datetime.now().strftime('%d/%m/%Y'))
    report_date = datetime.strptime(date_str, '%d/%m/%Y')
    raw_visits  = session.get('last_visits', [])
    visits = [{'time': datetime.strptime(v['time'], '%H:%M').replace(
                   year=report_date.year, month=report_date.month, day=report_date.day),
               'location': type('L', (), {'name': v['name']})()}
              for v in raw_visits]
    ok, msg = send_email_report(report_text, shift_name, report_date, visits)
    return jsonify({'success': ok, 'message': msg})

@app.route('/download_report')
@login_required
def download_report():
    shift_name  = session.get('last_shift', 'Report')
    date_str    = session.get('last_date', datetime.now().strftime('%d/%m/%Y'))
    report_date = datetime.strptime(date_str, '%d/%m/%Y')
    raw_visits  = session.get('last_visits', [])
    visits = [{'time': datetime.strptime(v['time'], '%H:%M').replace(
                   year=report_date.year, month=report_date.month, day=report_date.day),
               'location': type('L', (), {'name': v['name']})()}
              for v in raw_visits]
    buf   = build_report_pdf(visits, shift_name, report_date)
    fname = pdf_filename(shift_name, report_date)
    return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=fname)

@app.route('/delete_file', methods=['POST'])
@login_required
def delete_file():
    filename = request.json.get('filename')
    if not filename:
        return jsonify({'success': False, 'message': 'Nome file mancante'})
    filepath = os.path.join(FTP_UPLOAD_DIR, os.path.basename(filename))
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({'success': True, 'message': f'File {filename} eliminato'})
    return jsonify({'success': False, 'message': 'File non trovato'})

# --- Locations CRUD ---

@app.route('/locations')
@login_required
def locations():
    locs = Location.query.order_by(Location.name).all()
    return render_template('locations.html', locations=locs)

@app.route('/locations/add', methods=['POST'])
@login_required
def add_location():
    try:
        lat, lon = parse_coords(request.form['coords'])
        loc = Location(name=request.form['name'].strip(), lat=lat, lon=lon,
                       notes=request.form.get('notes','').strip())
        db.session.add(loc)
        db.session.commit()
        flash(f'Luogo "{loc.name}" aggiunto con successo')
    except Exception as e:
        flash(f'Errore: {e}')
    return redirect(url_for('locations'))

@app.route('/locations/edit/<int:id>', methods=['POST'])
@login_required
def edit_location(id):
    loc = Location.query.get_or_404(id)
    try:
        lat, lon = parse_coords(request.json['coords'])
        loc.name  = request.json['name'].strip()
        loc.lat, loc.lon = lat, lon
        loc.notes = request.json.get('notes','').strip()
        db.session.commit()
        return jsonify({'success': True, 'lat': loc.lat, 'lon': loc.lon})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/locations/delete/<int:id>', methods=['POST'])
@login_required
def delete_location(id):
    loc = Location.query.get_or_404(id)
    db.session.delete(loc)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/locations/export')
@login_required
def export_locations():
    locs = Location.query.all()
    return jsonify([{'id': l.id, 'name': l.name, 'lat': l.lat, 'lon': l.lon, 'notes': l.notes} for l in locs])

@app.route('/locations/import', methods=['POST'])
@login_required
def import_locations():
    try:
        data = request.json
        count = 0
        for item in data:
            if not Location.query.filter_by(name=item['name']).first():
                db.session.add(Location(name=item['name'], lat=item['lat'], lon=item['lon'], notes=item.get('notes','')))
                count += 1
        db.session.commit()
        return jsonify({'success': True, 'message': f'{count} luoghi importati'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# --- Settings ---

@app.route('/settings', methods=['GET','POST'])
@login_required
def settings():
    if request.method == 'POST':
        data = request.json
        for key in ('proximity_meters', 'cooldown_minutes', 'smtp_host', 'smtp_port',
                    'smtp_user', 'smtp_pass', 'report_email'):
            if key in data:
                set_setting(key, data[key])
        return jsonify({'success': True, 'message': 'Impostazioni salvate'})

    smtp = get_smtp_config()
    return render_template('settings.html',
        smtp_host=get_setting('smtp_host') or smtp['host'],
        smtp_port=get_setting('smtp_port') or str(smtp['port']),
        smtp_user=get_setting('smtp_user') or smtp['user'],
        smtp_pass=get_setting('smtp_pass') or smtp['pass_'],
        report_email=get_setting('report_email') or smtp['to'],
        proximity=get_setting('proximity_meters', '50'),
        cooldown=get_setting('cooldown_minutes', '30'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
