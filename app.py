from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort, Response
from supabase import create_client, Client
import qrcode
import io
import os
import uuid
import base64
import mimetypes
import threading
import time
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename
from supabase import create_client, Client
from dotenv import load_dotenv

# Set to "1" automatically by Vercel at runtime. Used below to adjust
# behaviour that only makes sense on a normal always-on server (background
# threads) vs. a serverless deployment (Vercel).
ON_VERCEL = bool(os.environ.get("VERCEL"))

# Load .env file FIRST
load_dotenv()

# Then read variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

print("URL:", SUPABASE_URL)
print("KEY:", SUPABASE_SERVICE_KEY[:20] + "...")  # Don't print the full key

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

app = Flask(__name__, template_folder='template')
# Reads FLASK_SECRET_KEY if set (recommended in production / Vercel env vars),
# otherwise falls back to the original hardcoded value so nothing breaks.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "navkar_stationary_secret")
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024  # 15 MB max upload size

# ---------------------------------------------------------------------------
# Cache-busting for /static/*.
# vercel.json sets "Cache-Control: public, max-age=31536000, immutable" on
# everything under /static/ for performance — but that means once a browser
# has fetched e.g. theme.css, it will NOT re-fetch it for a year, even after
# we deploy changes (not even on a hard refresh, in some browsers). To make
# updates actually show up, every static asset URL gets a ?v=<version> query
# string appended, computed from that file's last-modified time. Changing the
# file changes the URL, so it's always a fresh cache miss — the 1-year cache
# is preserved for assets that haven't changed, and busted for ones that have.
# ---------------------------------------------------------------------------
_STATIC_DIR = os.path.join(app.root_path, 'static')

def _static_version(filename):
    try:
        return str(int(os.path.getmtime(os.path.join(_STATIC_DIR, filename))))
    except OSError:
        return "1"

@app.context_processor
def inject_asset_version():
    def versioned_static(filename):
        return url_for('static', filename=filename) + '?v=' + _static_version(filename)
    return dict(versioned_static=versioned_static)


# Compress every HTML/CSS/JS/JSON response with gzip (falls back automatically
# if the browser doesn't support it). This cuts payload size dramatically for
# almost no CPU cost and helps a lot under concurrent load.
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

# Tell browsers to cache static files (images, css) for a year. Because the
# upload routes already give files random uuid-based filenames, a changed
# file gets a new URL automatically, so long caching here is safe and means
# repeat visitors barely re-download anything.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 60 * 60 * 24 * 365

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
XEROX_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'xerox')
PRODUCT_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'products')
if not ON_VERCEL:
    # Vercel's filesystem is read-only at runtime (only /tmp is writable,
    # and it doesn't persist between requests), so these local folders are
    # only created/used when running on a normal server (e.g. locally).
    os.makedirs(XEROX_UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(PRODUCT_UPLOAD_FOLDER, exist_ok=True)

XEROX_ALLOWED_EXT = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png'}
IMAGE_ALLOWED_EXT = {'jpg', 'jpeg', 'png', 'webp'}

# ---------------------------------------------------------------------------
# File storage (Supabase Storage)
# ---------------------------------------------------------------------------
# Vercel Functions run on a read-only filesystem, so uploaded files (product
# photos, xerox documents) can't be saved to disk like the original local
# version did. They're stored in a Supabase Storage bucket instead - this
# works identically whether the app runs locally or on Vercel, and uploaded
# files now persist properly instead of disappearing when the serverless
# function restarts.
STORAGE_BUCKET = "uploads"
STORAGE_PRODUCTS_PREFIX = "products"
STORAGE_XEROX_PREFIX = "xerox"


def _ensure_storage_bucket():
    """Create the storage bucket on first run. Safe to call every startup -
    if it already exists, Supabase returns an error which is ignored."""
    try:
        supabase.storage.create_bucket(STORAGE_BUCKET, options={"public": True})
    except Exception:
        pass


_ensure_storage_bucket()

# How many days an uploaded xerox/print document is kept before it is
# automatically wiped (both the database row and the file on disk).
XEROX_RETENTION_DAYS = 7

# ---------------------------------------------------------------------------
# Supabase Connection
# ---------------------------------------------------------------------------
# Set these two as environment variables (or in a local .env file):
#   SUPABASE_URL          -> Project Settings > API > Project URL
#   SUPABASE_SERVICE_KEY  -> Project Settings > API > service_role secret key
# The service_role key is used because this is a trusted server-side backend;
# never expose it in frontend/browser code.

TABLE_QUERIES = "queries"
TABLE_XEROX = "xerox_requests"
TABLE_PRODUCTS = "products"
TABLE_ORDERS = "orders"
TABLE_TRASH = "trash"
TABLE_ANNOUNCEMENTS = "announcements"
TABLE_REVIEWS = "reviews"

# WhatsApp number for the shop (used for the "Chat on WhatsApp" button)
SHOP_WHATSAPP_NUMBER = "919825089454"


def allowed_file(filename, allowed_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_set


def _resize_image_for_upload(file_storage, ext, max_width=900, quality=80):
    """Shrink & compress a product photo before it's stored, so a phone
    camera photo (often 3-5 MB) never ends up served to every shopper at
    full size. Falls back to the original bytes if Pillow can't process it
    (e.g. an unusual format)."""
    try:
        from PIL import Image
        raw = file_storage.read()
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB") if ext in ("jpg", "jpeg") else img.convert("RGBA")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        out = io.BytesIO()
        if ext in ("jpg", "jpeg"):
            img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
            content_type = "image/jpeg"
        elif ext == "png":
            img.save(out, "PNG", optimize=True)
            content_type = "image/png"
        else:  # webp or anything else Pillow can still write
            img.save(out, "WEBP", quality=quality)
            content_type = "image/webp"
        return out.getvalue(), content_type
    except Exception:
        file_storage.seek(0)
        return file_storage.read(), (file_storage.mimetype or "application/octet-stream")


# ---------------------------------------------------------------------------
# Tiny TTL cache for hot, read-heavy pages (homepage announcements, shop
# products). These are read on nearly every visitor request but only ever
# change when the admin edits them, so caching for a few seconds massively
# cuts the number of Supabase calls when many people browse at once, without
# ever showing data more than a few seconds stale.
# ---------------------------------------------------------------------------
_cache_store = {}
_cache_lock = threading.Lock()


def cached(key, ttl_seconds, fetch_fn):
    now = time.time()
    with _cache_lock:
        entry = _cache_store.get(key)
        if entry and entry[0] > now:
            return entry[1]
    value = fetch_fn()
    with _cache_lock:
        _cache_store[key] = (now + ttl_seconds, value)
    return value


def invalidate_cache(key):
    with _cache_lock:
        _cache_store.pop(key, None)


def _with_alias(rows):
    """Supabase rows use 'id' (bigint). Existing templates were written against
    MongoDB's '_id' field, so we mirror it as a string here to keep every
    template working unchanged."""
    for r in rows or []:
        if r and 'id' in r:
            r['_id'] = str(r['id'])
    return rows or []


def _one_or_none(rows):
    return rows[0] if rows else None


def _strip_row_id(row):
    """Remove identity/meta columns before re-inserting a row into another table."""
    if not row:
        return row
    row = dict(row)
    row.pop('id', None)
    row.pop('_id', None)
    row.pop('created_at', None)
    return row


def count_rows(table, **filters):
    q = supabase.table(table).select("id", count="exact")
    for key, value in filters.items():
        q = q.eq(key, value)
    return q.execute().count or 0


def get_admin_counts():
    # These 5 counts are independent, so run them concurrently on a small
    # thread pool instead of one-after-another - cuts this from ~5 sequential
    # network round-trips to ~1, which matters a lot when several admins/
    # pages are loading at once.
    from concurrent.futures import ThreadPoolExecutor

    jobs = {
        "xerox_pending": lambda: count_rows(TABLE_XEROX, status="Pending"),
        "orders_pending": lambda: count_rows(TABLE_ORDERS, status="Pending"),
        "trash": lambda: count_rows(TABLE_TRASH),
        "products": lambda: count_rows(TABLE_PRODUCTS),
        "announcements": lambda: count_rows(TABLE_ANNOUNCEMENTS),
        "reviews_pending": lambda: count_rows(TABLE_REVIEWS, status="Pending"),
    }
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {key: pool.submit(fn) for key, fn in jobs.items()}
        return {key: f.result() for key, f in futures.items()}


def login_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return view_func(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# HOME PAGE + QUERY SYSTEM
# ---------------------------------------------------------------------------
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        name = request.form.get('name')
        whatsapp_number = request.form.get('whatsapp_number')
        department = request.form.get('department')
        semester = request.form.get('semester')
        subject = request.form.get('subject')

        clean_number = ''.join(filter(str.isdigit, whatsapp_number))

        new_query = {
            "name": name,
            "whatsapp_number": clean_number,
            "department": department,
            "semester": semester,
            "subject": subject,
            "status": "Pending",
            "admin_message": "",
            "date": datetime.now().strftime("%d %b %Y, %I:%M %p")
        }
        supabase.table(TABLE_QUERIES).insert(new_query).execute()
        flash("Query submitted successfully! 🚀", "success")
        return redirect(url_for('index'))

    def _fetch():
        resp = supabase.table(TABLE_ANNOUNCEMENTS).select("*").order("id", desc=True).limit(12).execute()
        return _with_alias(resp.data)

    announcements = cached("home_announcements", 30, _fetch)

    def _fetch_reviews():
        resp = supabase.table(TABLE_REVIEWS).select("*").eq("status", "Approved").order("id", desc=True).limit(12).execute()
        return _with_alias(resp.data)

    happy_customer_reviews = cached("home_reviews", 30, _fetch_reviews)

    return render_template('index.html', whatsapp_number=SHOP_WHATSAPP_NUMBER, announcements=announcements,
                            reviews=happy_customer_reviews)


# ---------------------------------------------------------------------------
# LOGIN SYSTEM (admin only)
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == 'admin' and password == 'admin123':
            session['logged_in'] = True
            return redirect(url_for('admin'))
        else:
            flash("Invalid Credentials. Please try again.", "danger")

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# ADMIN - QUERY DASHBOARD
# ---------------------------------------------------------------------------
@app.route('/admin')
@login_required
def admin():
    search_q = request.args.get('q', '').strip()

    q = supabase.table(TABLE_QUERIES).select("*").order("id", desc=True)
    if search_q:
        q = q.ilike("name", f"%{search_q}%")
    all_queries = _with_alias(q.execute().data)

    stats = {
        "total": count_rows(TABLE_QUERIES),
        "pending": count_rows(TABLE_QUERIES, status="Pending"),
        "available": count_rows(TABLE_QUERIES, status="Available"),
        "non_available": count_rows(TABLE_QUERIES, status="Non-Available"),
    }

    return render_template('admin.html', queries=all_queries, stats=stats, counts=get_admin_counts(), search_q=search_q)


@app.route('/update_status/<query_id>', methods=['POST'])
@login_required
def update_status(query_id):
    new_status = request.form.get('status')
    admin_message = request.form.get('admin_message')

    supabase.table(TABLE_QUERIES).update({
        "status": new_status,
        "admin_message": admin_message
    }).eq("id", int(query_id)).execute()

    flash("Query updated successfully! ✅", "success")
    return redirect(url_for('admin'))

@app.route('/delete_query/<int:query_id>', methods=['POST'])
@login_required
def delete_query(query_id):
    try:
        supabase.table(TABLE_QUERIES).delete().eq("id", query_id).execute()
        flash("Query deleted successfully. 🗑️", "success")
    except Exception as e:
        flash(f"Error deleting query: {e}", "danger")

    return redirect(url_for('admin'))

# ---------------------------------------------------------------------------
# XEROX / PRINTOUT SYSTEM
# ---------------------------------------------------------------------------
def _delete_xerox_file(stored_filename):
    """Remove the uploaded document from Supabase Storage, if it exists."""
    if not stored_filename:
        return
    try:
        supabase.storage.from_(STORAGE_BUCKET).remove([f"{STORAGE_XEROX_PREFIX}/{stored_filename}"])
    except Exception as e:
        print("Could not delete xerox file:", stored_filename, e)


def _delete_xerox_row(row):
    """Delete a single xerox request: its uploaded file AND its database row."""
    if not row:
        return
    _delete_xerox_file(row.get('stored_filename'))
    supabase.table(TABLE_XEROX).delete().eq("id", row['id']).execute()


def _xerox_created_at(row):
    """Parse the Supabase 'created_at' timestamp into a timezone-aware datetime."""
    raw = row.get("created_at")

    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))

        # If timezone missing, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except Exception:
        return None

def purge_expired_xerox():
    """
    Delete Xerox requests older than XEROX_RETENTION_DAYS.
    Removes both the uploaded file and the database record.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(days=XEROX_RETENTION_DAYS)

    resp = supabase.table(TABLE_XEROX).select("*").execute()

    removed = 0

    for row in resp.data or []:
        created = _xerox_created_at(row)

        if created and created < cutoff:
            _delete_xerox_row(row)
            removed += 1

    return removed

def _xerox_auto_cleanup_loop():
    """Background loop: sweep old xerox documents automatically every hour,
    so files are deleted from Supabase + disk even if no admin ever opens
    the Xerox admin page."""
    while True:
        try:
            n = purge_expired_xerox()
            if n:
                print(f"[xerox auto-cleanup] Deleted {n} expired document(s).")
        except Exception as e:
            print("[xerox auto-cleanup] error:", e)
        time.sleep(60 * 60)  # run once every hour


def _start_xerox_auto_cleanup():
    t = threading.Thread(target=_xerox_auto_cleanup_loop, daemon=True)
    t.start()


# Start the background auto-delete thread exactly once. With Flask's debug
# reloader there are two processes; only the real running one sets
# WERKZEUG_RUN_MAIN, so we guard against starting it twice.
# This background thread only makes sense on a normal always-on server -
# Vercel Functions are short-lived, so instead a Vercel Cron Job calls
# /api/cron/cleanup-xerox once a day (see vercel.json).
if not ON_VERCEL and (not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true'):
    _start_xerox_auto_cleanup()


@app.route('/api/cron/cleanup-xerox')
def cron_cleanup_xerox():
    """Triggered by Vercel Cron (see vercel.json) once a day to sweep
    expired xerox documents, replacing the background thread used when
    running on a normal server."""
    expected_secret = os.environ.get('CRON_SECRET')
    auth_header = request.headers.get('Authorization', '')
    if expected_secret and auth_header != f"Bearer {expected_secret}":
        abort(401)
    removed = purge_expired_xerox()
    return {"deleted": removed}


@app.route('/xerox', methods=['GET', 'POST'])
def xerox():
    if request.method == 'POST':
        name = request.form.get('name')
        whatsapp_number = request.form.get('whatsapp_number')
        department = request.form.get('department')
        semester = request.form.get('semester')
        copies = request.form.get('copies') or "1"
        instructions = request.form.get('instructions')
        file = request.files.get('document')

        if not file or file.filename == '':
            flash("Please choose a file to upload.", "danger")
            return redirect(url_for('xerox'))

        if not allowed_file(file.filename, XEROX_ALLOWED_EXT):
            flash("Only Word (.doc/.docx), PDF (.pdf) or Image (.jpg/.jpeg/.png) files are allowed.", "danger")
            return redirect(url_for('xerox'))

        ext = file.filename.rsplit('.', 1)[1].lower()
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        file_bytes = file.read()
        content_type = file.mimetype or "application/octet-stream"
        supabase.storage.from_(STORAGE_BUCKET).upload(
            f"{STORAGE_XEROX_PREFIX}/{stored_name}",
            file_bytes,
            {"content-type": content_type}
        )

        clean_number = ''.join(filter(str.isdigit, whatsapp_number))

        supabase.table(TABLE_XEROX).insert({
            "name": name,
            "whatsapp_number": clean_number,
            "department": department,
            "semester": semester,
            "original_filename": secure_filename(file.filename),
            "stored_filename": stored_name,
            "copies": copies,
            "instructions": instructions,
            "status": "Pending",
            "date": datetime.now().strftime("%d %b %Y, %I:%M %p")
        }).execute()

        flash("Document uploaded! We will review it and confirm on WhatsApp. 📄", "success")
        return redirect(url_for('xerox'))

    return render_template('xerox.html', whatsapp_number=SHOP_WHATSAPP_NUMBER)


@app.route('/admin/xerox')
@login_required
def admin_xerox():
    # Sweep any documents that have already passed the retention window
    # before rendering the page, so the list always reflects reality.
    purge_expired_xerox()

    search_q = request.args.get('q', '').strip()
    sort_order = request.args.get('sort', 'newest')

    q = supabase.table(TABLE_XEROX).select("*")
    if search_q:
        q = q.ilike("name", f"%{search_q}%")
    q = q.order("id", desc=(sort_order != 'oldest'))
    all_requests = _with_alias(q.execute().data)

    # Attach an auto-delete countdown to each request
    now = datetime.now(timezone.utc)
    for r in all_requests:
        created = _xerox_created_at(r)
        if created:
            days_left = XEROX_RETENTION_DAYS - (now - created).days
            r['days_left'] = max(days_left, 0)
        else:
            r['days_left'] = None

    # Group requests by their date (day portion only) for date-wise display
    grouped = []
    current_day = None
    current_group = None
    for r in all_requests:
        day_part = r.get('date', '').split(',')[0].strip() if r.get('date') else 'Unknown Date'
        if day_part != current_day:
            current_day = day_part
            current_group = {"day": day_part, "requests": []}
            grouped.append(current_group)
        current_group["requests"].append(r)

    return render_template('admin_xerox.html', grouped=grouped, counts=get_admin_counts(),
                            search_q=search_q, sort_order=sort_order,
                            retention_days=XEROX_RETENTION_DAYS)


@app.route('/admin/xerox/update/<req_id>', methods=['POST'])
@login_required
def admin_xerox_update(req_id):
    new_status = request.form.get('status')
    supabase.table(TABLE_XEROX).update({"status": new_status}).eq("id", int(req_id)).execute()

    if new_status == "Accepted":
        flash("Document accepted — it is now ready to open & print. ✅", "success")
    else:
        flash("Document request denied.", "danger")
    return redirect(url_for('admin_xerox'))


@app.route('/admin/xerox/file/<req_id>')
@login_required
def admin_xerox_file(req_id):
    resp = supabase.table(TABLE_XEROX).select("*").eq("id", int(req_id)).execute()
    doc = _one_or_none(resp.data)
    if not doc:
        abort(404)
    try:
        file_bytes = supabase.storage.from_(STORAGE_BUCKET).download(
            f"{STORAGE_XEROX_PREFIX}/{doc['stored_filename']}"
        )
    except Exception:
        abort(404)
    mimetype = mimetypes.guess_type(doc['original_filename'])[0] or 'application/octet-stream'
    return Response(
        file_bytes,
        mimetype=mimetype,
        headers={"Content-Disposition": f'inline; filename="{doc["original_filename"]}"'}
    )


@app.route('/admin/xerox/delete/<req_id>', methods=['POST'])
@login_required
def admin_xerox_delete(req_id):
    """Manually delete a xerox request right now — removes the uploaded
    file from disk AND the row from the database."""
    resp = supabase.table(TABLE_XEROX).select("*").eq("id", int(req_id)).execute()
    row = _one_or_none(resp.data)
    if not row:
        flash("That document was already removed.", "danger")
        return redirect(url_for('admin_xerox'))

    _delete_xerox_row(row)
    flash("Document deleted permanently. 🗑️", "success")
    return redirect(url_for('admin_xerox'))


# ---------------------------------------------------------------------------
# ANNOUNCEMENTS (circular cards shown on the homepage)
# ---------------------------------------------------------------------------
ANNOUNCEMENT_COLORS = {'amber', 'green', 'blue', 'red', 'purple'}
ANNOUNCEMENT_ICONS = [
    'bi-megaphone-fill', 'bi-gift-fill', 'bi-percent', 'bi-stars',
    'bi-calendar-event-fill', 'bi-bell-fill', 'bi-tag-fill', 'bi-emoji-smile-fill'
]


@app.route('/admin/announcements')
@login_required
def admin_announcements():
    resp = supabase.table(TABLE_ANNOUNCEMENTS).select("*").order("id", desc=True).execute()
    all_announcements = _with_alias(resp.data)
    return render_template('admin_announcements.html', announcements=all_announcements,
                            counts=get_admin_counts(), icon_choices=ANNOUNCEMENT_ICONS)


@app.route('/admin/announcements/add', methods=['POST'])
@login_required
def admin_announcements_add():
    title = request.form.get('title', '').strip()
    message = request.form.get('message', '').strip()
    icon = request.form.get('icon', '').strip() or 'bi-megaphone-fill'
    color = request.form.get('color', '').strip()
    if color not in ANNOUNCEMENT_COLORS:
        color = 'amber'

    if not title:
        flash("Announcement title is required.", "danger")
        return redirect(url_for('admin_announcements'))

    supabase.table(TABLE_ANNOUNCEMENTS).insert({
        "title": title,
        "message": message,
        "icon": icon,
        "color": color,
        "date": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }).execute()
    invalidate_cache("home_announcements")
    flash("Announcement posted! 📢", "success")
    return redirect(url_for('admin_announcements'))


@app.route('/admin/announcements/delete/<announcement_id>', methods=['POST'])
@login_required
def admin_announcements_delete(announcement_id):
    supabase.table(TABLE_ANNOUNCEMENTS).delete().eq("id", int(announcement_id)).execute()
    invalidate_cache("home_announcements")
    flash("Announcement removed.", "success")
    return redirect(url_for('admin_announcements'))


# ---------------------------------------------------------------------------
# REVIEWS (customer feedback -> approved ones shown as "Our Happy Customers")
# ---------------------------------------------------------------------------
@app.route('/review', methods=['GET', 'POST'])
def review():
    if request.method == 'POST':
        name = request.form.get('name')
        whatsapp_number = request.form.get('whatsapp_number')
        review_text = request.form.get('review_text')
        try:
            rating = int(request.form.get('rating', 5))
        except (TypeError, ValueError):
            rating = 5
        rating = min(5, max(1, rating))

        clean_number = ''.join(filter(str.isdigit, whatsapp_number))

        supabase.table(TABLE_REVIEWS).insert({
            "name": name,
            "whatsapp_number": clean_number,
            "review_text": review_text,
            "rating": rating,
            "status": "Pending",
            "date": datetime.now().strftime("%d %b %Y, %I:%M %p")
        }).execute()
        flash("Thank you for your feedback! Your review has been submitted. 🌟", "success")
        return redirect(url_for('review'))

    return render_template('review.html', whatsapp_number=SHOP_WHATSAPP_NUMBER)


@app.route('/admin/reviews')
@login_required
def admin_reviews():
    search_q = request.args.get('q', '').strip()

    q = supabase.table(TABLE_REVIEWS).select("*").order("id", desc=True)
    if search_q:
        q = q.ilike("name", f"%{search_q}%")
    all_reviews = _with_alias(q.execute().data)

    stats = {
        "total": count_rows(TABLE_REVIEWS),
        "pending": count_rows(TABLE_REVIEWS, status="Pending"),
        "approved": count_rows(TABLE_REVIEWS, status="Approved"),
        "rejected": count_rows(TABLE_REVIEWS, status="Rejected"),
    }

    return render_template('admin_reviews.html', reviews=all_reviews, stats=stats,
                            counts=get_admin_counts(), search_q=search_q)


@app.route('/admin/reviews/update/<review_id>', methods=['POST'])
@login_required
def admin_reviews_update(review_id):
    new_status = request.form.get('status')
    supabase.table(TABLE_REVIEWS).update({"status": new_status}).eq("id", int(review_id)).execute()
    invalidate_cache("home_reviews")

    if new_status == "Approved":
        flash("Review approved and posted to the homepage. ✅", "success")
    else:
        flash("Review status updated.", "success")
    return redirect(url_for('admin_reviews'))


@app.route('/admin/reviews/delete/<review_id>', methods=['POST'])
@login_required
def admin_reviews_delete(review_id):
    supabase.table(TABLE_REVIEWS).delete().eq("id", int(review_id)).execute()
    invalidate_cache("home_reviews")
    flash("Review deleted permanently. 🗑️", "success")
    return redirect(url_for('admin_reviews'))


# ---------------------------------------------------------------------------
# STOCK MANAGEMENT + SHOPPING (cart-to-request, no online payment)
# ---------------------------------------------------------------------------
@app.route('/shop')
def shop():
    def _fetch():
        resp = supabase.table(TABLE_PRODUCTS).select("*").order("id", desc=True).execute()
        return _with_alias(resp.data)

    all_products = cached("shop_products", 15, _fetch)
    categories = sorted({p.get('category') for p in all_products if p.get('category')})

    search_q = request.args.get('q', '').strip()
    selected_category = request.args.get('category', '').strip()

    products = all_products
    if selected_category:
        products = [p for p in products if p.get('category') == selected_category]
    if search_q:
        needle = search_q.lower()
        products = [
            p for p in products
            if needle in (p.get('name') or '').lower() or needle in (p.get('category') or '').lower()
        ]

    cart = session.get('cart', {})
    cart_count = sum(cart.values()) if cart else 0
    return render_template('shop.html', products=products, cart=cart, cart_count=cart_count,
                            whatsapp_number=SHOP_WHATSAPP_NUMBER, categories=categories,
                            search_q=search_q, selected_category=selected_category)


@app.route('/cart/add/<product_id>', methods=['POST'])
def cart_add(product_id):
    resp = supabase.table(TABLE_PRODUCTS).select("*").eq("id", int(product_id)).execute()
    product = _one_or_none(resp.data)
    if not product:
        flash("This product is no longer available.", "danger")
        return redirect(url_for('shop'))

    try:
        qty = max(1, int(request.form.get('qty', 1)))
    except (TypeError, ValueError):
        qty = 1

    cart = session.get('cart', {})
    cart[product_id] = cart.get(product_id, 0) + qty
    session['cart'] = cart
    session.modified = True
    flash(f"Added '{product['name']}' to your cart.", "success")
    return redirect(url_for('shop'))


@app.route('/cart/remove/<product_id>', methods=['POST'])
def cart_remove(product_id):
    cart = session.get('cart', {})
    if product_id in cart:
        del cart[product_id]
        session['cart'] = cart
        session.modified = True
    return redirect(url_for('cart_view'))


@app.route('/cart/update/<product_id>/<action>', methods=['POST'])
def cart_update(product_id, action):
    """Plus/minus quantity stepper on the cart page. action is 'increase'
    or 'decrease' - decreasing to 0 removes the item entirely."""
    cart = session.get('cart', {})
    if product_id in cart:
        if action == 'increase':
            cart[product_id] = cart[product_id] + 1
        elif action == 'decrease':
            cart[product_id] = cart[product_id] - 1
            if cart[product_id] <= 0:
                del cart[product_id]
        session['cart'] = cart
        session.modified = True
    return redirect(url_for('cart_view'))


def _fetch_products_by_ids(pids):
    """Fetch every product in `pids` in a single DB round-trip instead of
    one query per item (avoids N+1 queries when carts have multiple items
    or many users are checking out at once)."""
    ids = [int(p) for p in pids]
    if not ids:
        return {}
    resp = supabase.table(TABLE_PRODUCTS).select("*").in_("id", ids).execute()
    return {str(p['id']): p for p in (resp.data or [])}


@app.route('/cart')
def cart_view():
    cart = session.get('cart', {})
    products_by_id = _fetch_products_by_ids(cart.keys())
    items = []
    total = 0
    for pid, qty in cart.items():
        product = products_by_id.get(str(pid))
        if product:
            line_total = product['price'] * qty
            total += line_total
            items.append({
                "id": pid,
                "name": product['name'],
                "price": product['price'],
                "photo": product.get('photo'),
                "qty": qty,
                "line_total": line_total
            })
    return render_template('cart.html', items=items, total=total)


@app.route('/cart/submit', methods=['POST'])
def cart_submit():
    cart = session.get('cart', {})
    if not cart:
        flash("Your cart is empty.", "danger")
        return redirect(url_for('shop'))

    name = request.form.get('name')
    whatsapp_number = request.form.get('whatsapp_number')
    comment = (request.form.get('comment') or '').strip()
    clean_number = ''.join(filter(str.isdigit, whatsapp_number))

    items = []
    total = 0
    products_by_id = _fetch_products_by_ids(cart.keys())
    for pid, qty in cart.items():
        product = products_by_id.get(str(pid))
        if product:
            line_total = product['price'] * qty
            total += line_total
            items.append({
                "product_id": pid,
                "name": product['name'],
                "price": product['price'],
                "qty": qty,
                "line_total": line_total
            })

    supabase.table(TABLE_ORDERS).insert({
        "name": name,
        "whatsapp_number": clean_number,
        "comment": comment,
        "line_items": items,
        "total": total,
        "status": "Pending",
        "date": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }).execute()

    session['cart'] = {}
    session.modified = True
    flash("Your order request has been submitted! We'll confirm it on WhatsApp shortly. 🛍️", "success")
    return redirect(url_for('shop'))


# ---- Admin: Stock Management ----
@app.route('/admin/stock')
@login_required
def admin_stock():
    resp = supabase.table(TABLE_PRODUCTS).select("*").order("id", desc=True).execute()
    all_products = _with_alias(resp.data)
    categories = sorted({p.get('category') for p in all_products if p.get('category')})
    return render_template('admin_stock.html', products=all_products, counts=get_admin_counts(),
                            categories=categories)


@app.route('/admin/stock/add', methods=['POST'])
@login_required
def admin_stock_add():
    name = request.form.get('name')
    price = request.form.get('price')
    photo_file = request.files.get('photo')

    # Category: admin either picks an existing category from the dropdown,
    # or types a brand new one in the "new category" box - the new one wins
    # whenever it's filled in.
    new_category = (request.form.get('new_category') or '').strip()
    selected_category = (request.form.get('category') or '').strip()
    category = new_category if new_category else selected_category
    category = category or 'General'

    photo_url = None
    if photo_file and photo_file.filename != '':
        if allowed_file(photo_file.filename, IMAGE_ALLOWED_EXT):
            ext = photo_file.filename.rsplit('.', 1)[1].lower()
            photo_name = f"{uuid.uuid4().hex}.{ext}"
            storage_path = f"{STORAGE_PRODUCTS_PREFIX}/{photo_name}"
            file_bytes, content_type = _resize_image_for_upload(photo_file, ext)
            supabase.storage.from_(STORAGE_BUCKET).upload(
                storage_path, file_bytes, {"content-type": content_type}
            )
            photo_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
        else:
            flash("Product photo must be jpg, jpeg, png or webp.", "danger")
            return redirect(url_for('admin_stock'))

    try:
        price_val = float(price)
    except (TypeError, ValueError):
        price_val = 0

    supabase.table(TABLE_PRODUCTS).insert({
        "name": name,
        "price": price_val,
        "photo": photo_url,
        "category": category,
        "date": datetime.now().strftime("%d %b %Y, %I:%M %p")
    }).execute()
    invalidate_cache("shop_products")
    flash("Product added to stock. ✅", "success")
    return redirect(url_for('admin_stock'))


@app.route('/admin/stock/delete/<product_id>', methods=['POST'])
@login_required
def admin_stock_delete(product_id):
    supabase.table(TABLE_PRODUCTS).delete().eq("id", int(product_id)).execute()
    invalidate_cache("shop_products")
    flash("Product removed from stock.", "success")
    return redirect(url_for('admin_stock'))


# ---- Admin: Orders (submitted carts) ----
@app.route('/admin/orders')
@login_required
def admin_orders():
    resp = supabase.table(TABLE_ORDERS).select("*").order("id", desc=True).execute()
    all_orders = _with_alias(resp.data)
    return render_template('admin_orders.html', orders=all_orders, counts=get_admin_counts())


@app.route('/admin/orders/done/<order_id>', methods=['POST'])
@login_required
def admin_orders_done(order_id):
    supabase.table(TABLE_ORDERS).update({"status": "Done"}).eq("id", int(order_id)).execute()
    flash("Order marked as done. ✅", "success")
    return redirect(url_for('admin_orders'))


@app.route('/admin/orders/remove/<order_id>', methods=['POST'])
@login_required
def admin_orders_remove(order_id):
    resp = supabase.table(TABLE_ORDERS).select("*").eq("id", int(order_id)).execute()
    order = _one_or_none(resp.data)
    if order:
        trash_row = _strip_row_id(order)
        trash_row['original_order_id'] = order['id']
        trash_row['deleted_date'] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        supabase.table(TABLE_TRASH).insert(trash_row).execute()
        supabase.table(TABLE_ORDERS).delete().eq("id", int(order_id)).execute()
        flash("Order moved to Trash.", "success")
    return redirect(url_for('admin_orders'))


# ---- Admin: Trash ----
@app.route('/admin/trash')
@login_required
def admin_trash():
    resp = supabase.table(TABLE_TRASH).select("*").order("id", desc=True).execute()
    trashed = _with_alias(resp.data)
    return render_template('admin_trash.html', trashed=trashed, counts=get_admin_counts())


@app.route('/admin/trash/restore/<order_id>', methods=['POST'])
@login_required
def admin_trash_restore(order_id):
    resp = supabase.table(TABLE_TRASH).select("*").eq("id", int(order_id)).execute()
    order = _one_or_none(resp.data)
    if order:
        restored_row = _strip_row_id(order)
        restored_row.pop('deleted_date', None)
        restored_row.pop('original_order_id', None)
        restored_row['status'] = 'Pending'
        supabase.table(TABLE_ORDERS).insert(restored_row).execute()
        supabase.table(TABLE_TRASH).delete().eq("id", int(order_id)).execute()
        flash("Order restored.", "success")
    return redirect(url_for('admin_trash'))


@app.route('/admin/trash/clear', methods=['POST'])
@login_required
def admin_trash_clear():
    supabase.table(TABLE_TRASH).delete().gt("id", 0).execute()
    flash("Trash bin cleared.", "success")
    return redirect(url_for('admin_trash'))


# ---------------------------------------------------------------------------
# QR CODE
# ---------------------------------------------------------------------------
@app.route('/generate_qr')
@login_required
def generate_qr():
    # Was hardcoded to http://127.0.0.1:5000, which only worked while running
    # locally. Using the current request's own domain means the generated QR
    # code correctly points at wherever the site is actually deployed
    # (Vercel URL, custom domain, or localhost during local development).
    website_url = request.url_root
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(website_url)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

    return render_template('qr_code.html', qr_image=qr_base64)


if __name__ == '__main__':
    app.run(debug=True)
