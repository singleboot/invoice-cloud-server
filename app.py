import os
import sys
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import jwt
import functools

_basedir = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, _basedir)

from models import db, CloudUser, Client, Invoice, InvoiceItem, Payment, Expense

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'sqlite:///' + os.path.join(_basedir, 'cloud.db'))
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
CORS(app)


def init_db():
    with app.app_context():
        db.create_all()


# ─── JWT helpers ────────────────────────────────────────

def make_token(user_id):
    return jwt.encode({'uid': user_id, 'iat': datetime.utcnow()},
                      app.config['SECRET_KEY'], algorithm='HS256')


def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({'error': 'Missing token'}), 401
        try:
            data = jwt.decode(auth[7:], app.config['SECRET_KEY'], algorithms=['HS256'])
            kwargs['cloud_user'] = CloudUser.query.get(data['uid'])
            if not kwargs['cloud_user']:
                return jsonify({'error': 'User not found'}), 401
        except Exception:
            return jsonify({'error': 'Invalid token'}), 401
        return f(*args, **kwargs)
    return wrapper


def parse_iso(dt_str):
    return datetime.fromisoformat(dt_str) if dt_str else datetime.min


def iso(dt):
    return dt.isoformat() if dt else None


# ─── Auth endpoints ─────────────────────────────────────

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    email = data.get('email', '').strip()
    password = data.get('password', '')
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    if CloudUser.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 409
    user = CloudUser(email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({'token': make_token(user.id), 'email': user.email})


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    user = CloudUser.query.filter_by(email=data.get('email', '')).first()
    if not user or not user.check_password(data.get('password', '')):
        return jsonify({'error': 'Invalid credentials'}), 401
    return jsonify({'token': make_token(user.id), 'email': user.email})


# ─── Sync push ──────────────────────────────────────────

MODEL_MAP = {
    'user': (None, None),  # handled separately
    'clients': (Client, 'client_cloud_id'),
    'invoices': (Invoice, 'client_cloud_id'),
    'invoice_items': (InvoiceItem, 'invoice_cloud_id'),
    'payments': (Payment, 'invoice_cloud_id'),
    'expenses': (Expense, None),
}

TABLE_FK_MAP = {
    'clients': None,
    'invoices': 'client_cloud_id',
    'invoice_items': 'invoice_cloud_id',
    'payments': 'invoice_cloud_id',
    'expenses': None,
}

SERIALIZABLE_FIELDS = {
    'clients': ['cloud_id', 'name', 'email', 'phone', 'address', 'gst_number',
                'created_at', 'updated_at'],
    'invoices': ['cloud_id', 'client_cloud_id', 'invoice_number', 'issue_date',
                 'due_date', 'project_name', 'currency', 'status', 'subtotal',
                 'tax_rate', 'tax_amount', 'discount', 'total', 'notes', 'terms',
                 'created_at', 'updated_at'],
    'invoice_items': ['cloud_id', 'invoice_cloud_id', 'description', 'quantity',
                      'unit_price', 'amount', 'updated_at'],
    'payments': ['cloud_id', 'invoice_cloud_id', 'amount', 'payment_date',
                 'method', 'reference', 'notes', 'created_at', 'updated_at'],
    'expenses': ['cloud_id', 'category', 'amount', 'expense_date', 'description',
                 'vendor', 'payment_method', 'created_at', 'updated_at'],
}


DATE_COLS = {'issue_date', 'due_date', 'payment_date', 'expense_date'}
DATETIME_COLS = {'created_at', 'updated_at'}


def _parse_val(val, col):
    if val is None:
        return None
    if col in DATE_COLS and isinstance(val, str):
        try:
            return datetime.strptime(val, '%Y-%m-%d').date()
        except Exception:
            return None
    if col in DATETIME_COLS and isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except Exception:
            return None
    return val


def upsert_records(model_cls, records, cloud_user_id):
    results = []
    for rec in records:
        cid = rec.get('cloud_id')
        if not cid:
            results.append((None, False))
            continue
        existing = model_cls.query.filter_by(cloud_id=cid, user_id=cloud_user_id).first()
        remote_updated = _parse_val(rec.get('updated_at'), 'updated_at')
        if existing:
            if existing.updated_at and remote_updated and remote_updated <= existing.updated_at:
                results.append((cid, False))
                continue
            for col in model_cls.__table__.columns.keys():
                if col in ('id', 'user_id', 'cloud_id'):
                    continue
                if col in rec:
                    val = _parse_val(rec[col], col)
                    if val is not None:
                        setattr(existing, col, val)
            if remote_updated:
                existing.updated_at = remote_updated
            results.append((cid, True))
        else:
            kwargs = {'user_id': cloud_user_id, 'cloud_id': cid}
            for col in model_cls.__table__.columns.keys():
                if col in ('id', 'user_id', 'cloud_id'):
                    continue
                if col in rec:
                    val = _parse_val(rec[col], col)
                    if val is not None:
                        kwargs[col] = val
            if remote_updated:
                kwargs['updated_at'] = remote_updated
            db.session.add(model_cls(**kwargs))
            results.append((cid, True))
    return results


def serialize_records(model_cls, cloud_user_id, since, fields):
    q = model_cls.query.filter(
        model_cls.user_id == cloud_user_id,
        model_cls.updated_at > since
    )
    out = []
    for r in q.all():
        d = {'cloud_id': r.cloud_id}
        for f in fields:
            val = getattr(r, f, None)
            if isinstance(val, datetime):
                val = iso(val)
            elif hasattr(val, 'isoformat'):
                val = val.isoformat()
            d[f] = val
        out.append(d)
    return out


@app.route('/api/sync/push', methods=['POST'])
@require_auth
def sync_push(cloud_user):
    data = request.get_json() or {}
    results = {}

    for table_name in ('clients', 'invoices', 'invoice_items', 'payments', 'expenses'):
        records = data.get(table_name, [])
        model_cls = MODEL_MAP[table_name][0]
        if model_cls and records:
            r = upsert_records(model_cls, records, cloud_user.id)
            results[table_name] = [{'cloud_id': c, 'synced': s} for c, s in r]

    db.session.commit()
    return jsonify({'status': 'ok', 'results': results})


@app.route('/api/sync/pull', methods=['GET'])
@require_auth
def sync_pull(cloud_user):
    since_str = request.args.get('since', '')
    since = parse_iso(since_str)

    out = {}
    for table_name in ('clients', 'invoices', 'invoice_items', 'payments', 'expenses'):
        model_cls = MODEL_MAP[table_name][0]
        fields = SERIALIZABLE_FIELDS.get(table_name, [])
        if model_cls:
            out[table_name] = serialize_records(model_cls, cloud_user.id, since, fields)

    return jsonify(out)


@app.route('/api/sync/status', methods=['GET'])
@require_auth
def sync_status(cloud_user):
    counts = {}
    for name, (cls, _) in MODEL_MAP.items():
        if cls:
            counts[name] = cls.query.filter_by(user_id=cloud_user.id).count()
    return jsonify({'email': cloud_user.email, 'counts': counts})


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=True)
