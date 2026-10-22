"""
Application routes for MiniShop.

MiniShop application routes. Security assessment fixes are applied to the
command diagnostics, order ownership checks, and image upload handling.
"""

import os
from functools import wraps
from io import BytesIO

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    current_app,
    send_from_directory,
    abort,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from app.models import get_db

from PIL import Image, UnidentifiedImageError

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED_IMAGE_TYPES = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}


def validate_uploaded_image(file, filename):
    """Accept only supported images whose contents match their format."""
    extension = os.path.splitext(filename)[1].lower()
    expected_format = ALLOWED_IMAGE_TYPES.get(extension)

    if expected_format is None:
        return False

    try:
        contents = file.read()
        file.seek(0)

        with Image.open(BytesIO(contents)) as image:
            if image.format != expected_format:
                return False
            image.verify()

        file.seek(0)
        return True

    except (UnidentifiedImageError, OSError, ValueError):
        file.seek(0)
        return False

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("main.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def seller_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("main.login", next=request.path))
        if session.get("role") != "seller":
            flash("Access denied. Seller account required.", "danger")
            return redirect(url_for("main.index"))
        return f(*args, **kwargs)
    return decorated


def customer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("main.login", next=request.path))
        if session.get("role") != "customer":
            flash("This feature is for customers only.", "warning")
            return redirect(url_for("main.index"))
        return f(*args, **kwargs)
    return decorated


def get_cart():
    """Return the shopping cart stored in the session."""
    return session.get("cart", {})


def cart_count():
    cart = get_cart()
    return sum(cart.values())


def cart_items_detail():
    """Return list of (product, quantity, subtotal) for current cart."""
    cart = get_cart()
    if not cart:
        return []
    db = get_db()
    items = []
    for pid_str, qty in cart.items():
        product = db.execute(
            "SELECT * FROM products WHERE id = ?", (int(pid_str),)
        ).fetchone()
        if product:
            items.append(
                {
                    "product": product,
                    "quantity": qty,
                    "subtotal": round(product["price"] * qty, 2),
                }
            )
    return items


# Make cart_count available in all templates
@bp.app_context_processor
def inject_cart():
    return {"cart_count": cart_count()}


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    db = get_db()
    products = db.execute(
        "SELECT id, name, description, price, image_filename, stock FROM products ORDER BY id"
    ).fetchall()
    return render_template("index.html", products=products)


@bp.route("/product/<int:product_id>")
def product_detail(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        abort(404)
    return render_template("product_detail.html", product=product)


# ---------------------------------------------------------------------------
# Authentication & Registration
# ---------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash(f"Welcome back, {user['username']}!", "success")
            next_url = request.args.get("next") or url_for("main.index")
            return redirect(next_url)
        else:
            flash("Invalid username or password.", "danger")
    return render_template("login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Allow new customers to create an account."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if not username or not password:
            flash("Username and password are required.", "danger")
        elif len(username) < 3:
            flash("Username must be at least 3 characters.", "danger")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
        elif password != password2:
            flash("Passwords do not match.", "danger")
        else:
            db = get_db()
            existing = db.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                flash("That username is already taken.", "danger")
            else:
                db.execute(
                    "INSERT INTO users (username, password_hash, role, profile_image) VALUES (?, ?, 'customer', NULL)",
                    (username, generate_password_hash(password)),
                )
                db.commit()
                flash("Account created! You can now log in.", "success")
                return redirect(url_for("main.login"))
    return render_template("register.html")


@bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------------------
# Shopping Cart & Checkout (real purchase)
# ---------------------------------------------------------------------------

@bp.route("/cart")
def cart_view():
    items = cart_items_detail()
    total = round(sum(i["subtotal"] for i in items), 2)
    return render_template("cart.html", items=items, total=total)


@bp.route("/cart/add/<int:product_id>", methods=["POST"])
@login_required
def cart_add(product_id):
    db = get_db()
    product = db.execute(
        "SELECT id, name, stock FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        flash("Product not found.", "danger")
        return redirect(url_for("main.index"))

    qty = request.form.get("quantity", 1, type=int)
    if qty < 1:
        qty = 1
    if product["stock"] < 1:
        flash(f"{product['name']} is out of stock.", "warning")
        return redirect(url_for("main.product_detail", product_id=product_id))

    cart = get_cart()
    key = str(product_id)
    cart[key] = cart.get(key, 0) + qty
    # Cap at available stock
    if cart[key] > product["stock"]:
        cart[key] = product["stock"]
        flash(f"Only {product['stock']} units available. Cart updated.", "warning")
    else:
        flash(f"Added {product['name']} to cart.", "success")
    session["cart"] = cart
    return redirect(request.referrer or url_for("main.cart_view"))


@bp.route("/cart/update", methods=["POST"])
@login_required
def cart_update():
    cart = get_cart()
    db = get_db()
    for key, value in request.form.items():
        if key.startswith("qty_"):
            pid = key[4:]
            try:
                qty = int(value)
            except ValueError:
                continue
            if qty <= 0:
                cart.pop(pid, None)
            else:
                product = db.execute(
                    "SELECT stock FROM products WHERE id = ?", (int(pid),)
                ).fetchone()
                if product:
                    cart[pid] = min(qty, product["stock"])
    session["cart"] = cart
    flash("Cart updated.", "success")
    return redirect(url_for("main.cart_view"))


@bp.route("/cart/remove/<int:product_id>", methods=["POST"])
@login_required
def cart_remove(product_id):
    cart = get_cart()
    cart.pop(str(product_id), None)
    session["cart"] = cart
    flash("Item removed from cart.", "info")
    return redirect(url_for("main.cart_view"))


@bp.route("/checkout", methods=["GET", "POST"])
@login_required
@customer_required
def checkout():
    items = cart_items_detail()
    if not items:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("main.cart_view"))

    total = round(sum(i["subtotal"] for i in items), 2)

    if request.method == "POST":
        db = get_db()
        # Re-check stock and create order
        try:
            for item in items:
                p = db.execute(
                    "SELECT stock FROM products WHERE id = ?",
                    (item["product"]["id"],),
                ).fetchone()
                if p is None or p["stock"] < item["quantity"]:
                    flash(
                        f"Not enough stock for {item['product']['name']}.",
                        "danger",
                    )
                    return redirect(url_for("main.cart_view"))

            cur = db.execute(
                "INSERT INTO orders (user_id, status, total) VALUES (?, 'Processing', ?)",
                (session["user_id"], total),
            )
            order_id = cur.lastrowid

            for item in items:
                db.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?)",
                    (
                        order_id,
                        item["product"]["id"],
                        item["quantity"],
                        item["product"]["price"],
                    ),
                )
                db.execute(
                    "UPDATE products SET stock = stock - ? WHERE id = ?",
                    (item["quantity"], item["product"]["id"]),
                )

            db.commit()
            session["cart"] = {}
            flash(f"Order #{order_id} placed successfully!", "success")
            return redirect(url_for("main.order_detail", order_id=order_id))
        except Exception as exc:
            db.rollback()
            flash(f"Checkout failed: {exc}", "danger")
            return redirect(url_for("main.cart_view"))

    return render_template("checkout.html", items=items, total=total)


# ---------------------------------------------------------------------------
# Customer Profile (vulnerable profile-picture upload)
# ---------------------------------------------------------------------------

@bp.route("/profile")
@login_required
def profile():
    db = get_db()
    user = db.execute(
        "SELECT id, username, role, profile_image FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    return render_template("profile.html", user=user)


@bp.route("/profile/upload", methods=["POST"])
@login_required
def profile_upload():
    """Upload a validated profile image for the logged-in user."""
    if "file" not in request.files:
        flash("No file part in the request.", "danger")
        return redirect(url_for("main.profile"))

    file = request.files["file"]
    if file.filename == "":
        flash("No file selected.", "warning")
        return redirect(url_for("main.profile"))

    filename = secure_filename(file.filename)
    if not filename:
        flash("Invalid filename.", "danger")
        return redirect(url_for("main.profile"))

    if not validate_uploaded_image(file, filename):
        flash(
            "Invalid image file. Use a valid JPEG, PNG, or WebP image.",
            "danger",
        )
        return redirect(url_for("main.profile"))

    # Prefix with user id to avoid collisions between users.
    stored_name = f"profile_{session['user_id']}_{filename}"
    save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)

    try:
        file.save(save_path)
        db = get_db()
        db.execute(
            "UPDATE users SET profile_image = ? WHERE id = ?",
            (stored_name, session["user_id"]),
        )
        db.commit()
        flash(f"Profile picture '{filename}' uploaded successfully.", "success")
    except Exception as exc:
        flash(f"Upload failed: {exc}", "danger")

    return redirect(url_for("main.profile"))


# ---------------------------------------------------------------------------
# Customer Orders
# ---------------------------------------------------------------------------

@bp.route("/orders")
@login_required
def orders():
    """List orders belonging to the currently logged-in customer."""
    db = get_db()
    user_orders = db.execute(
        """
        SELECT o.id, o.status, o.total, o.created_at
        FROM orders o
        WHERE o.user_id = ?
        ORDER BY o.created_at DESC
        """,
        (session["user_id"],),
    ).fetchall()
    return render_template("orders.html", orders=user_orders)


@bp.route("/orders/<int:order_id>")
@login_required

def order_detail(order_id):
    """Display an order only to its authenticated owner."""
    db = get_db()

    order = db.execute(
        """
        SELECT o.*, u.username
        FROM orders o
        JOIN users u ON o.user_id = u.id
        WHERE o.id = ? AND o.user_id = ?
        """,
        (order_id, session["user_id"]),
    ).fetchone()

    if order is None:
        abort(404)

    items = db.execute(
        """
        SELECT oi.quantity, oi.unit_price, p.name, p.id AS product_id
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
        """,
        (order_id,),
    ).fetchall()

    return render_template("order_detail.html", order=order, items=items)

# ---------------------------------------------------------------------------
# Seller Dashboard & Upload
# ---------------------------------------------------------------------------

@bp.route("/seller")
@seller_required
def seller_dashboard():
    db = get_db()
    products = db.execute(
        "SELECT id, name, price, stock, image_filename FROM products ORDER BY id"
    ).fetchall()
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    uploaded = []
    if os.path.isdir(upload_dir):
        uploaded = sorted(
            f for f in os.listdir(upload_dir) if not f.startswith(".")
        )
    return render_template(
        "seller_dashboard.html", products=products, uploaded_files=uploaded
    )


@bp.route("/seller/upload", methods=["GET", "POST"])
@seller_required
def seller_upload():
    """Allow sellers to upload validated product images."""
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part in the request.", "danger")
            return redirect(request.url)

        file = request.files["file"]
        if file.filename == "":
            flash("No file selected.", "warning")
            return redirect(request.url)

        filename = secure_filename(file.filename)
        if not filename:
            flash("Invalid filename.", "danger")
            return redirect(request.url)

        if not validate_uploaded_image(file, filename):
            flash(
                "Invalid image file. Use a valid JPEG, PNG, or WebP image.",
                "danger",
            )
            return redirect(request.url)

        save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
        try:
            file.save(save_path)
            product_id = request.form.get("product_id", type=int)
            if product_id:
                db = get_db()
                db.execute(
                    "UPDATE products SET image_filename = ? WHERE id = ?",
                    (filename, product_id),
                )
                db.commit()
                flash(
                    f"File '{filename}' uploaded and assigned to product #{product_id}.",
                    "success",
                )
            else:
                flash(f"File '{filename}' uploaded successfully.", "success")
        except Exception as exc:
            flash(f"Upload failed: {exc}", "danger")

        return redirect(url_for("main.seller_dashboard"))

    db = get_db()
    products = db.execute("SELECT id, name FROM products ORDER BY id").fetchall()
    return render_template("seller_upload.html", products=products)


@bp.route("/uploads/<path:filename>")
@login_required
def download_upload(filename):
    """Serve uploaded files as downloads (not executed)."""
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"],
        filename,
        as_attachment=True,
    )


@bp.route("/media/<path:filename>")
def serve_media(filename):
    """Serve trusted built-in images or validated uploaded images inline."""
    # Reject nested paths so callers cannot request arbitrary filesystem paths.
    if os.path.basename(filename) != filename:
        abort(404)

    static_img = os.path.join(current_app.root_path, "static", "img")
    static_path = os.path.join(static_img, filename)
    extension = os.path.splitext(filename)[1].lower()

    # Preserve trusted, bundled image placeholders, including SVG files.
    if extension in set(ALLOWED_IMAGE_TYPES) | {".svg"} and os.path.isfile(static_path):
        return send_from_directory(static_img, filename)

    if extension not in ALLOWED_IMAGE_TYPES:
        abort(404)

    upload_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
    try:
        with open(upload_path, "rb") as uploaded_file:
            if not validate_uploaded_image(uploaded_file, filename):
                abort(404)
    except OSError:
        abort(404)

    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"],
        filename,
        mimetype=mime_types[extension],
        as_attachment=False,
    )


# ---------------------------------------------------------------------------
# Product Diagnostics (Command Injection demo)
# ---------------------------------------------------------------------------

@bp.route("/diagnostics", methods=["GET", "POST"])
@login_required
def diagnostics():
    output = None
    product_code = ""

    if request.method == "POST":
        product_code = request.form.get("product_code", "").strip()

        if product_code:
            output = (
                "Diagnostic check for product:\n"
                + product_code
            )
        else:
            flash("Please enter a product code.", "warning")

    return render_template(
        "diagnostics.html",
        output=output,
        product_code=product_code,
    )

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@bp.app_errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found"), 404


@bp.app_errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Internal server error"), 500
