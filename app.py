from flask import Flask, render_template, session, redirect, url_for, request, make_response, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from twilio.rest import Client
import csv
import os
from werkzeug.utils import secure_filename
from functools import wraps
from dotenv import load_dotenv
import razorpay
import hmac, hashlib
import traceback
import json
from flask_mail import Mail, Message

load_dotenv(override=True)
from utils.image_utils import save_product_image


PAYMENT_CREATED = "CREATED"
PAYMENT_PAID = "PAID"
PAYMENT_FAILED = "FAILED"

load_dotenv()

app = Flask(__name__)
app.secret_key = "mysecret"

# --- Email Configuration ---
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False') == 'True'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER')

mail = Mail(app)

# --- Upload configuration ---
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "products")



def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# razorpay config
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

def get_razorpay_client():
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Admin auth config
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# --- DATABASE SETUP ---
basedir = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(basedir, "store.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

from flask_migrate import Migrate
migrate = Migrate(app, db)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    order_index = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<Category {self.name}>"

# ---- Twilio SMS Config (local dev only) ----
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")           
client = Client(account_sid, auth_token)


# --- MODELS ---
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    description = db.Column(db.Text)
    image_url = db.Column(db.String(255))
    is_bestseller = db.Column(db.Boolean, default=False)
    category = db.Column(db.String(50))
    time_to_make_min = db.Column(db.Integer, nullable=True)  
    time_to_make_max = db.Column(db.Integer, nullable=True) 
    
    # NEW FIELDS
    is_new_launch = db.Column(db.Boolean, default=False)
    new_launch_date = db.Column(db.DateTime, nullable=True)
    sale_price = db.Column(db.Integer, nullable=True)  # If set, product is on sale

    images = db.relationship(
        "ProductImage",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

def cleanup_old_new_launches():
    """Automatically remove 'New Launch' badge from products older than 7 days"""
    from datetime import timedelta
    
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    
    old_launches = Product.query.filter(
        Product.is_new_launch == True,
        Product.new_launch_date != None,
        Product.new_launch_date < seven_days_ago
    ).all()
    
    for product in old_launches:
        product.is_new_launch = False
        product.new_launch_date = None
    
    if old_launches:
        db.session.commit()
        print(f"✓ Removed 'New Launch' from {len(old_launches)} products")
    
    return len(old_launches)

def send_order_confirmation_email(order):
    """Send order confirmation email to customer"""
    try:
        # Get order items
        items = OrderItem.query.filter_by(order_id=order.id).all()
        
        # Build items HTML
        items_html = ""
        wrap_total = 0
        
        for item in items:
            product = Product.query.get(item.product_id)
            
            # Get variant info
            variant_info = ""
            if item.color_variant_name:
                variant_info += f"<br><small style='color: #666;'><i>Color:</i> {item.color_variant_name}</small>"
            if item.size_variant_name:
                variant_info += f"<br><small style='color: #666;'><i>Size:</i> {item.size_variant_name}</small>"
            
            # Check for gift wrap
            gift_wrap = GiftWrap.query.filter_by(order_item_id=item.id).first()
            wrap_info = ""
            if gift_wrap:
                wrap_info = f"<br><small style='color: #8B6F47; background: #FFF8F0; padding: 2px 6px; border-radius: 3px;'><strong>🎁 Gift Wrap:</strong> {gift_wrap.wrap_type.title()} (+₹{gift_wrap.wrap_price})</small>"
                wrap_total += gift_wrap.wrap_price
            
            items_html += f"""
            <tr>
                <td style="padding: 10px; border-bottom: 1px solid #eee;">
                    <strong>{item.product_name}</strong>{variant_info}{wrap_info}
                </td>
                <td style="padding: 10px; border-bottom: 1px solid #eee; text-align: center;">{item.quantity}</td>
                <td style="padding: 10px; border-bottom: 1px solid #eee; text-align: right;">₹{item.unit_price}</td>
                <td style="padding: 10px; border-bottom: 1px solid #eee; text-align: right;">₹{item.unit_price * item.quantity}</td>
            </tr>
            """
        
        # Add wrap total row if applicable
        wrap_row = ""
        if wrap_total > 0:
            wrap_row = f"""
            <tr style="background: #FFF8F0;">
                <td colspan="3" style="padding: 10px; text-align: right;"><strong>🎁 Gift Wrap Total:</strong></td>
                <td style="padding: 10px; text-align: right;"><strong>₹{wrap_total}</strong></td>
            </tr>
            """
        shipping_row = ""
        if hasattr(order, 'shipping_cost'):
            if order.shipping_cost > 0:
                shipping_row = f"""
                <tr>
                    <td colspan="3" style="padding: 10px; text-align: right;"><strong>🚚 Shipping:</strong></td>
                    <td style="padding: 10px; text-align: right;"><strong>₹{order.shipping_cost}</strong></td>
                </tr>
                """
            else:
                shipping_row = f"""
                <tr>
                    <td colspan="3" style="padding: 10px; text-align: right;"><strong>🚚 Shipping:</strong></td>
                    <td style="padding: 10px; text-align: right;"><strong style="color: #10B981;">FREE</strong></td>
                </tr>
                """

        # Email HTML template
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #A67C52; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
                .content {{ background: #fff; padding: 30px; border: 1px solid #ddd; }}
                .order-details {{ background: #f9f9f9; padding: 20px; border-radius: 8px; margin: 20px 0; }}
                .footer {{ background: #f4f4f4; padding: 20px; text-align: center; border-radius: 0 0 8px 8px; font-size: 12px; color: #666; }}
                table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
                .total {{ font-size: 18px; font-weight: bold; color: #A67C52; }}
                .button {{ display: inline-block; padding: 12px 30px; background: #A67C52; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🎉 Order Confirmed!</h1>
                    <p>Thank you for your purchase at KCX Crochet Store</p>
                </div>
                
                <div class="content">
                    <p>Dear {order.customer_name},</p>
                    
                    <p>We're excited to let you know that your order has been successfully placed! Here are your order details:</p>
                    
                    <div class="order-details">
                        <h3 style="margin-top: 0; color: #A67C52;">Order #KCX{order.id}</h3>
                        <p><strong>Order Date:</strong> {order.created_at.strftime('%B %d, %Y at %I:%M %p')}</p>
                        <p><strong>Payment Status:</strong> <span style="color: #4CAF50;">{order.payment_status}</span></p>
                    </div>
                    
                    <h3>Order Items:</h3>
                    <table>
                        <thead>
                            <tr style="background: #f4f4f4;">
                                <th style="padding: 10px; text-align: left;">Product</th>
                                <th style="padding: 10px; text-align: center;">Qty</th>
                                <th style="padding: 10px; text-align: right;">Price</th>
                                <th style="padding: 10px; text-align: right;">Subtotal</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items_html}
                        </tbody>
                        <tfoot>
                            {wrap_row}
                            {shipping_row}
                            <tr>
                                <td colspan="3" style="padding: 15px; text-align: right; font-weight: bold;">Total:</td>
                                <td style="padding: 15px; text-align: right;" class="total">₹{order.total_amount}</td>
                            </tr>
                        </tfoot>
                    </table>
                    
                    <h3>Delivery Address:</h3>
                    <div class="order-details">
                        <p style="margin: 5px 0;"><strong>{order.customer_name}</strong></p>
                        <p style="margin: 5px 0;">{order.address}</p>
                        <p style="margin: 5px 0;">{order.city}, {order.pincode}</p>
                        <p style="margin: 5px 0;">📱 {order.phone}</p>
                        {f'<p style="margin: 5px 0;">📧 {order.email}</p>' if order.email else ''}
                    </div>
                    
                    <p style="margin-top: 30px;">Your order will be carefully handcrafted and shipped within 5-7 business days. You'll receive a tracking number once it's dispatched.</p>
                    
                    <p>If you have any questions about your order, feel free to reply to this email or contact us.</p>
                    
                    <p style="margin-top: 30px;">Thank you for supporting handmade! ❤️</p>
                </div>
                
                <div class="footer">
                    <p><strong>KCX Crochet Store</strong></p>
                    <p>Handmade with love in India 🇮🇳</p>
                    <p>Questions? Email us at {app.config['MAIL_DEFAULT_SENDER']}</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        # Send email
        msg = Message(
            subject=f'Order Confirmation - Order #KCX{order.id}',
            recipients=[order.email] if order.email else [],
            html=html_body
        )
        
        if order.email:
            mail.send(msg)
            print(f"✓ Order confirmation email sent to {order.email}")
            return True
        else:
            print("⚠ No email address provided for order")
            return False
            
    except Exception as e:
        print(f"✗ Failed to send email: {e}")
        traceback.print_exc()
        return False
    

def send_admin_order_notification(order):
    """Send order notification to admin"""
    try:
        items = OrderItem.query.filter_by(order_id=order.id).all()
        
        items_text = ""
        wrap_total = 0
        
        for item in items:
            variant_info = ""
            if item.color_variant_name:
                variant_info += f" | Color: {item.color_variant_name}"
            if item.size_variant_name:
                variant_info += f" | Size: {item.size_variant_name}"
            
            # Check for gift wrap
            gift_wrap = GiftWrap.query.filter_by(order_item_id=item.id).first()
            wrap_info = ""
            if gift_wrap:
                wrap_info = f" | 🎁 Gift Wrap: {gift_wrap.wrap_type.title()} (+₹{gift_wrap.wrap_price})"
                wrap_total += gift_wrap.wrap_price
            
            items_text += f"- {item.product_name}{variant_info}{wrap_info}\n  Qty: {item.quantity} x ₹{item.unit_price} = ₹{item.unit_price * item.quantity}\n\n"
        
        if wrap_total > 0:
            items_text += f"🎁 Gift Wrap Total: ₹{wrap_total}\n\n"
        
        if hasattr(order, 'shipping_cost'):
            if order.shipping_cost > 0:
                items_text += f"🚚 Shipping: ₹{order.shipping_cost}\n\n"
            else:
                items_text += f"🚚 Shipping: FREE\n\n"

        admin_email = os.getenv('ADMIN_EMAIL')
        if not admin_email:
            return False
        
        msg = Message(
            subject=f'🔔 New Order #KCX{order.id} - ₹{order.total_amount}',
            recipients=[admin_email],
            body=f"""
New order received!

Order ID: #KCX{order.id}
Customer: {order.customer_name}
Phone: {order.phone}
Email: {order.email or 'Not provided'}

Items:
{items_text}

Total: ₹{order.total_amount}
Payment Status: {order.payment_status}

Address:
{order.address}
{order.city}, {order.pincode}

Notes: {order.notes or 'None'}

---
View full details in admin panel
            """
        )
        
        mail.send(msg)
        print(f"✓ Admin notification sent")
        return True
        
    except Exception as e:
        print(f"✗ Failed to send admin email: {e}")
        return False

class ProductImage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image_url = db.Column(db.String(255), nullable=False)
    order_index = db.Column(db.Integer, default=0)  # For image ordering

    product_id = db.Column(
        db.Integer,
        db.ForeignKey("product.id", ondelete="CASCADE"),
        nullable=False
    )

    product = db.relationship(
        "Product",
        back_populates="images"
    )

class ProductVariant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"), nullable=False)
    variant_type = db.Column(db.String(20), nullable=False)  # 'color' or 'size'
    name = db.Column(db.String(50), nullable=False)  # 'Red', 'Large', etc.
    code = db.Column(db.String(20))  # Color hex code
    price_adjustment = db.Column(db.Integer, default=0)
    image_indices = db.Column(db.Text)  # JSON string of image indices
    
    product = db.relationship("Product", backref="variants")

class GiftWrap(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_item_id = db.Column(db.Integer, db.ForeignKey("order_item.id"), nullable=False)
    wrap_type = db.Column(db.String(50), nullable=False)  # 'jute', 'newspaper', etc.
    wrap_price = db.Column(db.Integer, nullable=False)
    
    order_item = db.relationship("OrderItem", backref="gift_wrap")


class ProductReview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id', ondelete='CASCADE'), nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    rating = db.Column(db.Integer, nullable=False)  # 1-5 stars
    review_text = db.Column(db.Text, nullable=False)
    image1_url = db.Column(db.String(255), nullable=True)
    image2_url = db.Column(db.String(255), nullable=True)
    is_approved = db.Column(db.Boolean, default=False)  # Admin approval
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    product = db.relationship('Product', backref='reviews')
    
    def __repr__(self):
        return f"<Review {self.id} for Product {self.product_id}>"

class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)  # e.g., "SAVE20"
    discount_type = db.Column(db.String(20), nullable=False)  # "percentage" or "fixed"
    discount_value = db.Column(db.Integer, nullable=False)  # 20 for 20% or 100 for ₹100
    min_order_value = db.Column(db.Integer, default=0)  # Minimum cart value to use coupon
    max_uses = db.Column(db.Integer, nullable=True)  # Total times coupon can be used
    max_uses_per_user = db.Column(db.Integer, default=1)  # Times per user
    times_used = db.Column(db.Integer, default=0)  # Track usage
    is_active = db.Column(db.Boolean, default=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def is_valid(self, cart_total, user_identifier=None):
        """Check if coupon is valid"""
        # Check if active
        if not self.is_active:
            return False, "This coupon is inactive"
        
        # Check expiry
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False, "This coupon has expired"
        
        # Check max uses
        if self.max_uses and self.times_used >= self.max_uses:
            return False, "This coupon has reached its usage limit"
        
        # Check minimum order value
        if cart_total < self.min_order_value:
            return False, f"Minimum order value of ₹{self.min_order_value} required"
        
        # TODO: Check per-user usage (implement if needed)
        
        return True, "Coupon is valid"
    
    def calculate_discount(self, cart_total):
        """Calculate discount amount"""
        if self.discount_type == "percentage":
            discount = int(cart_total * self.discount_value / 100)
        else:  # fixed
            discount = self.discount_value
        
        # Discount cannot exceed cart total
        return min(discount, cart_total)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    address = db.Column(db.Text, nullable=False)
    city = db.Column(db.String(80))
    pincode = db.Column(db.String(20))
    notes = db.Column(db.Text)
    total_amount = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(30), default="Pending")
    payment_status = db.Column(db.String(30), default="Unpaid")
    razorpay_order_id = db.Column(db.String(120), nullable=True)
    razorpay_payment_id = db.Column(db.String(120), nullable=True)
    razorpay_signature = db.Column(db.String(300), nullable=True)
    coupon_code = db.Column(db.String(50), nullable=True)
    coupon_discount = db.Column(db.Integer, default=0)
    subtotal = db.Column(db.Integer, nullable=False, default=0)  
    shipping_cost = db.Column(db.Integer, default=0)

    items = db.relationship("OrderItem", backref="order", lazy=True)

    def __repr__(self):
        return f"<Order #{self.id} {self.status} {self.payment_status}>"
    
class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product_name = db.Column(db.String(120), nullable=False)
    unit_price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    
    color_variant_name = db.Column(db.String(50), nullable=True)
    size_variant_name = db.Column(db.String(50), nullable=True)

    def __repr__(self):
        return f"<OrderItem {self.product_name} x{self.quantity}>"


# ------------ CART HELPERS ------------

def get_cart():
    """Return the cart dict from session, create if missing."""
    if "cart" not in session:
        session["cart"] = {}
    return session["cart"]

def build_cart():
    """Convert session cart to list of products with variant info, total and count."""
    cart = session.get("cart", {})
    items = []
    total = 0
    count = 0

    for cart_key, cart_data in cart.items():
        # Handle old cart format (just product_id: qty)
        if isinstance(cart_data, int):
            product_id = int(cart_key)
            qty = cart_data
            color_variant = None
            size_variant = None
        else:
            # New cart format with variants
            product_id = cart_data.get('product_id')
            qty = cart_data.get('quantity', 1)
            color_id = cart_data.get('color_id')
            size_id = cart_data.get('size_id')
            
            # Fetch variant details
            color_variant = ProductVariant.query.get(color_id) if color_id else None
            size_variant = ProductVariant.query.get(size_id) if size_id else None
        
        product = Product.query.get(int(product_id))
        if product:
            # Calculate price with variant adjustments
            effective_price = product.sale_price if product.sale_price else product.price
            
            if color_variant and color_variant.price_adjustment:
                effective_price += color_variant.price_adjustment
            if size_variant and size_variant.price_adjustment:
                effective_price += size_variant.price_adjustment
            
            items.append({
                "cart_key": cart_key,
                "product": product,
                "quantity": qty,
                "effective_price": effective_price,
                "color_variant": color_variant,
                "size_variant": size_variant
            })
            total += effective_price * qty
            count += qty

    return items, total, count

@app.context_processor
def inject_cart():
    items, total, count = build_cart()
    open_flag = session.pop("open_cart", False)

    return dict(
        cart_items_global=items,
        cart_total_global=total,
        cart_count_global=count,
        cart_open_global=open_flag,
        categories_global=[c.name for c in Category.query.order_by(Category.order_index).all()]
    )


# ------------ ROUTES ------------

@app.route("/")
def home():
    cleanup_old_new_launches()
    bestsellers = Product.query.filter_by(is_bestseller=True).all()
    
    # Get products by category for home page
    category_products = {}
    for category in Category.query.order_by(Category.order_index).all():
        products = Product.query.filter_by(category=category.name)\
            .order_by(Product.is_new_launch.desc(), Product.id.desc()).limit(4).all()
        if products:
            category_products[category.name] = products
    
    # Get recent approved reviews for homepage (last 8 reviews)
    recent_reviews = ProductReview.query.filter_by(is_approved=True)\
        .order_by(ProductReview.created_at.desc()).limit(8).all()
    
    return render_template("home.html", 
                         products=bestsellers, 
                         category_products=category_products,
                         recent_reviews=recent_reviews)

@app.route("/shop")
def shop():
    category = request.args.get('category')
    
    if category and Category.query.filter_by(name=category).first():
        products = Product.query.filter_by(category=category)\
            .order_by(Product.is_new_launch.desc(), Product.id.desc()).all()
    else:
        products = Product.query\
            .order_by(Product.is_new_launch.desc(), Product.id.desc()).all()
    
    return render_template("shop.html", products=products, selected_category=category)

@app.route("/product/<int:product_id>")
def product_detail(product_id):
    product = Product.query.get_or_404(product_id)
    
    # GET VARIANTS FROM DATABASE
    color_variants = ProductVariant.query.filter_by(
        product_id=product_id, 
        variant_type='color'
    ).all()
    
    size_variants = ProductVariant.query.filter_by(
        product_id=product_id, 
        variant_type='size'
    ).all()
    
    # Convert to JSON-friendly format
    import json
    colors_data = []
    for cv in color_variants:
        colors_data.append({
            'id': cv.id,
            'name': cv.name,
            'code': cv.code,
            'priceAdj': cv.price_adjustment,
            'images': json.loads(cv.image_indices) if cv.image_indices else []
        })
    
    sizes_data = []
    for sv in size_variants:
        sizes_data.append({
            'id': sv.id,
            'name': sv.name,
            'priceAdj': sv.price_adjustment,
            'images': json.loads(sv.image_indices) if sv.image_indices else []
        })
    
    # DEBUGGING: Print to console
    print(f"DEBUG - Product {product_id}:")
    print(f"  Images in DB: {len(product.images)}")
    print(f"  Color variants: {len(colors_data)}")
    print(f"  Size variants: {len(sizes_data)}")
    for cv in colors_data:
        print(f"    Color '{cv['name']}' -> images: {cv['images']}")
    for sv in sizes_data:
        print(f"    Size '{sv['name']}' -> images: {sv['images']}")
    
    # Get suggested products
    if product.category:
        suggested = Product.query.filter(
            Product.category == product.category,
            Product.id != product_id
        ).limit(4).all()
        
        if len(suggested) < 4:
            additional = Product.query.filter(
                Product.id != product_id
            ).order_by(db.func.random()).limit(4 - len(suggested)).all()
            suggested.extend(additional)
    else:
        suggested = Product.query.filter(
            Product.id != product_id
        ).order_by(db.func.random()).limit(4).all()
    
     # Get approved reviews for this product
    approved_reviews = ProductReview.query.filter_by(
        product_id=product_id,
        is_approved=True
    ).order_by(ProductReview.created_at.desc()).all()
    
    # Calculate average rating
    avg_rating = 0
    if approved_reviews:
        avg_rating = sum(r.rating for r in approved_reviews) / len(approved_reviews)

    return render_template(
        "product.html", 
        product=product, 
        suggested_products=suggested,
        color_variants_json=json.dumps(colors_data),
        size_variants_json=json.dumps(sizes_data),
        reviews=approved_reviews,  
        avg_rating=avg_rating,  
        review_count=len(approved_reviews)
    )

@app.route("/product/<int:product_id>/review", methods=["POST"])
def submit_review(product_id):
    product = Product.query.get_or_404(product_id)
    
    customer_name = request.form.get("name", "Anonymous").strip()
    rating = int(request.form.get("rating", 5))
    review_text = request.form.get("review", "").strip()
    
    if not review_text or len(review_text) < 10:
        flash("Review must be at least 10 characters long", "danger")
        return redirect(url_for('product_detail', product_id=product_id))
    
    # Create review
    review = ProductReview(
        product_id=product_id,
        customer_name=customer_name,
        rating=rating,
        review_text=review_text,
        is_approved=False  # Pending admin approval
    )
    
    # Handle image uploads (max 2)
    image_files = request.files.getlist("review_images")
    saved_count = 0
    
    for img_file in image_files[:2]:  # Max 2 images
        if img_file and img_file.filename and allowed_file(img_file.filename):
            image_url = save_product_image(img_file)  # Reuse existing function
            if saved_count == 0:
                review.image1_url = image_url
            else:
                review.image2_url = image_url
            saved_count += 1
    
    db.session.add(review)
    db.session.commit()
    
    flash("Thank you! Your review has been submitted and is pending approval.", "success")
    return redirect(url_for('product_detail', product_id=product_id))

@app.route("/create_order", methods=["POST"])
def create_order():
    data = request.get_json() or {}
    local_order_id = data.get("order_id")
    if not local_order_id:
        return jsonify({"error": "missing order_id"}), 400

    order = db.session.get(Order, local_order_id)
    if not order:
        return jsonify({"error": "order not found"}), 404

    try:
        amount_paisa = int(order.total_amount) * 100
    except Exception:
        amount_paisa = None

    if not isinstance(amount_paisa, int) or amount_paisa < 100:
        return jsonify({"error": "invalid_amount", "detail": f"amount_paisa={amount_paisa}"}), 400

    client = get_razorpay_client()

    try:
        razor_order = client.order.create({
            "amount": amount_paisa,
            "currency": "INR",
            "receipt": f"order_{order.id}",
            "payment_capture": 1
        })
    except Exception as e:
        print("ERROR creating razorpay order:", type(e), e)
        traceback.print_exc()
        return jsonify({"error": "razorpay_error", "detail": str(e)}), 500

    order.razorpay_order_id = razor_order.get("id")
    db.session.commit()

    return jsonify({
        "razorpay_order_id": razor_order.get("id"),
        "amount": amount_paisa,
        "currency": "INR",
        "key": RAZORPAY_KEY_ID
    })



@app.route("/verify_payment", methods=["POST"])
def verify_payment():
    payload = request.get_json() or {}
    r_order_id = payload.get("razorpay_order_id")
    r_payment_id = payload.get("razorpay_payment_id")
    r_signature = payload.get("razorpay_signature")
    local_order_id = payload.get("local_order_id")

    if not all([r_order_id, r_payment_id, r_signature, local_order_id]):
        return jsonify({"error": "missing fields"}), 400

    client = get_razorpay_client()
    params = {
        "razorpay_order_id": r_order_id,
        "razorpay_payment_id": r_payment_id,
        "razorpay_signature": r_signature
    }

    try:
        client.utility.verify_payment_signature(params)
    except razorpay.errors.SignatureVerificationError as e:
        order = db.session.get(Order, local_order_id)
        if order:
            order.payment_status = PAYMENT_FAILED
            db.session.commit()
        return jsonify({"status": "failure", "error": str(e)}), 400

    order = db.session.get(Order, local_order_id)

    if order and order.payment_status != PAYMENT_PAID:
        order.payment_status = PAYMENT_PAID
        order.razorpay_payment_id = r_payment_id
        order.razorpay_signature = r_signature
        db.session.commit()
        
        # Send confirmation email
        send_order_confirmation_email(order)
        send_admin_order_notification(order)

    return jsonify({"status": "success"})

@app.route("/razorpay_webhook", methods=["POST"])
def razorpay_webhook():
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    body = request.data
    signature = request.headers.get("X-Razorpay-Signature", "")

    if secret:
        computed = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed, signature):
            return "invalid signature", 400

    event = request.get_json()
    etype = event.get("event")

    if etype == "payment.captured":
        payment = event.get("payload", {}).get("payment", {}).get("entity", {})
        r_payment_id = payment.get("id")
        r_order_id = payment.get("order_id")

        if not r_payment_id or not r_order_id:
            return jsonify({"ok": True})

        local_order = Order.query.filter_by(razorpay_order_id=r_order_id).first()

        if not local_order:
            return jsonify({"ok": True})

        if local_order.payment_status == PAYMENT_PAID:
            return jsonify({"ok": True})

        local_order.payment_status = PAYMENT_PAID
        local_order.razorpay_payment_id = r_payment_id
        db.session.commit()

    return jsonify({"ok": True})


# ------------- Admin: Product Management -------------

def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_orders"))

    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("Admin login successful", "success")
            nxt = request.args.get("next") or url_for("admin_orders")
            return redirect(nxt)
        else:
            flash("Wrong password", "danger")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin/products")
@admin_required
def admin_products():
    products = Product.query.order_by(Product.id.desc()).all()
    return render_template("admin_products.html", products=products)

@app.route("/admin")
@admin_required
def admin_index():
    orders_count = Order.query.count()
    products_count = Product.query.count()
    
    # FIX: Correct today's date filtering
    from datetime import datetime, date
    today = date.today()
    
    # Count orders where created_at date equals today
    todays_count = Order.query.filter(
        db.func.date(Order.created_at) == today
    ).count()
    
    print(f"DEBUG: Today is {today}, found {todays_count} orders")
    
    return render_template("admin_index.html",
                           orders_count=orders_count,
                           products_count=products_count,
                           todays_count=todays_count)


@app.route("/admin/products/add", methods=["GET", "POST"])
@admin_required
def admin_product_add():
    if request.method == "POST":
        name = request.form.get("name")
        price = int(request.form.get("price") or 0)
        description = request.form.get("description")
        is_bestseller = request.form.get("is_bestseller") == "on"
        is_new_launch = request.form.get("is_new_launch") == "on"
        new_launch_date = datetime.utcnow() if is_new_launch else None
        category = request.form.get("category")
        
        sale_price_str = request.form.get("sale_price", "").strip()
        sale_price = int(sale_price_str) if sale_price_str else None
        time_min = request.form.get("time_min", "").strip()
        time_max = request.form.get("time_max", "").strip()

        p = Product(
            name=name,
            price=price,
            description=description,
            image_url=None,
            is_bestseller=is_bestseller,
            is_new_launch=is_new_launch,
            new_launch_date=new_launch_date,
            category=category,
            sale_price=sale_price,
            time_to_make_min=int(time_min) if time_min else None,  
            time_to_make_max=int(time_max) if time_max else None 
        )
        db.session.add(p)
        db.session.commit()

        # Handle multiple images
        files = request.files.getlist("images")
        for idx, file in enumerate(files):
            if file and file.filename:
                image_url = save_product_image(file)
                if idx == 0:
                    p.image_url = image_url
                db.session.add(
                    ProductImage(
                        product_id=p.id,
                        image_url=image_url,
                        order_index=idx
                    )
                )

        # Handle variants
        color_variants_json = request.form.get("color_variants", "[]")
        size_variants_json = request.form.get("size_variants", "[]")
        
        try:
            color_variants = json.loads(color_variants_json)
            for cv in color_variants:
                if cv.get('name'):
                    db.session.add(ProductVariant(
                        product_id=p.id,
                        variant_type='color',
                        name=cv.get('name'),
                        code=cv.get('code'),
                        price_adjustment=int(cv.get('price_adj', 0)),
                        image_indices=json.dumps(cv.get('images', []))
                    ))
        except:
            pass
        
        try:
            size_variants = json.loads(size_variants_json)
            for sv in size_variants:
                if sv.get('name'):
                    db.session.add(ProductVariant(
                        product_id=p.id,
                        variant_type='size',
                        name=sv.get('name'),
                        price_adjustment=int(sv.get('price_adj', 0)),
                        image_indices=json.dumps(sv.get('images', []))
                    ))
        except:
            pass

        db.session.commit()
        return redirect(url_for("admin_products"))

    # FOR GET REQUEST - adding new product (no existing variants)
    return render_template(
        "admin_product_form.html", 
        product=None, 
        categories=[c.name for c in Category.query.order_by(Category.order_index).all()],
        existing_color_variants=json.dumps([]),
        existing_size_variants=json.dumps([])
    )


@app.route("/admin/products/edit/<int:product_id>", methods=["GET", "POST"])
@admin_required
def admin_product_edit(product_id):
    product = Product.query.get_or_404(product_id)
    
    if request.method == "POST":
        product.name = request.form.get("name")
        product.price = int(request.form.get("price") or 0)
        product.description = request.form.get("description")
        product.is_bestseller = request.form.get("is_bestseller") == "on"
        product.is_new_launch = request.form.get("is_new_launch") == "on"
        is_new_launch_checked = request.form.get("is_new_launch") == "on"
        
        # If marking as new launch for first time, set the date
        if is_new_launch_checked and not product.is_new_launch:
            product.new_launch_date = datetime.utcnow()
        # If unchecking new launch, clear the date
        elif not is_new_launch_checked:
            product.new_launch_date = None
        
        product.is_new_launch = is_new_launch_checked
        product.category = request.form.get("category")
        
        sale_price_str = request.form.get("sale_price", "").strip()
        product.sale_price = int(sale_price_str) if sale_price_str else None
        time_min = request.form.get("time_min", "").strip()
        time_max = request.form.get("time_max", "").strip()
        product.time_to_make_min = int(time_min) if time_min else None
        product.time_to_make_max = int(time_max) if time_max else None

        # Handle image order
        image_order_json = request.form.get("image_order", "")
        if image_order_json:
            try:
                image_order = json.loads(image_order_json)
                for idx, img_id in enumerate(image_order):
                    img = ProductImage.query.get(int(img_id))
                    if img:
                        img.order_index = idx
                        if idx == 0:
                            product.image_url = img.image_url
            except:
                pass

        # Handle new images
        files = request.files.getlist("images")
        if files and files[0].filename:
            current_max_index = db.session.query(db.func.max(ProductImage.order_index)).filter_by(product_id=product.id).scalar() or -1
            for idx, file in enumerate(files):
                if file and file.filename and allowed_file(file.filename):
                    image_url = save_product_image(file)
                    if not product.image_url:
                        product.image_url = image_url
                    db.session.add(
                        ProductImage(
                            product_id=product.id,
                            image_url=image_url,
                            order_index=current_max_index + idx + 1
                        )
                    )

        # Update variants
        ProductVariant.query.filter_by(product_id=product.id).delete()
        
        color_variants_json = request.form.get("color_variants", "[]")
        size_variants_json = request.form.get("size_variants", "[]")
        
        try:
            color_variants = json.loads(color_variants_json)
            for cv in color_variants:
                if cv.get('name'):
                    db.session.add(ProductVariant(
                        product_id=product.id,
                        variant_type='color',
                        name=cv.get('name'),
                        code=cv.get('code'),
                        price_adjustment=int(cv.get('price_adj', 0)),
                        image_indices=json.dumps(cv.get('images', []))
                    ))
        except:
            pass
        
        try:
            size_variants = json.loads(size_variants_json)
            for sv in size_variants:
                if sv.get('name'):
                    db.session.add(ProductVariant(
                        product_id=product.id,
                        variant_type='size',
                        name=sv.get('name'),
                        price_adjustment=int(sv.get('price_adj', 0)),
                        image_indices=json.dumps(sv.get('images', []))
                    ))
        except:
            pass

        db.session.commit()
        return redirect(url_for("admin_products"))

    # FOR GET REQUEST - Load existing variants
    color_vars = ProductVariant.query.filter_by(product_id=product.id, variant_type='color').all()
    size_vars = ProductVariant.query.filter_by(product_id=product.id, variant_type='size').all()

    existing_colors = []
    for cv in color_vars:
        existing_colors.append({
            'id': str(cv.id),
            'name': cv.name,
            'code': cv.code or '#000000',
            'price_adj': cv.price_adjustment,
            'images': json.loads(cv.image_indices) if cv.image_indices else []
        })

    existing_sizes = []
    for sv in size_vars:
        existing_sizes.append({
            'id': str(sv.id),
            'name': sv.name,
            'price_adj': sv.price_adjustment,
            'images': json.loads(sv.image_indices) if sv.image_indices else []
        })

    return render_template(
        "admin_product_form.html", 
        product=product, 
        categories=[c.name for c in Category.query.order_by(Category.order_index).all()],
        existing_color_variants=json.dumps(existing_colors),
        existing_size_variants=json.dumps(existing_sizes)
    )


@app.route("/admin/products/delete/<int:product_id>", methods=["POST"])
@admin_required
def admin_product_delete(product_id):
    product = Product.query.get_or_404(product_id)

    if product.image_url:
        try:
            path = os.path.join("static", product.image_url)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    db.session.delete(product)
    db.session.commit()
    return redirect(url_for("admin_products"))

@app.route("/admin/orders")
@admin_required
def admin_orders():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template("admin_orders.html", orders=orders)


@app.route("/admin/orders/<int:order_id>")
@admin_required
def admin_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    items = OrderItem.query.filter_by(order_id=order.id).all()
    
    # Load gift wraps for each item
    for item in items:
        item.gift_wrap_list = GiftWrap.query.filter_by(order_item_id=item.id).all()
    
    return render_template("admin_order_detail.html", order=order, items=items)


@app.route("/admin/orders/<int:order_id>/set-status", methods=["POST"])
@admin_required
def admin_set_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form.get("status")
    if new_status:
        order.status = new_status
        db.session.commit()
    return redirect(url_for("admin_order_detail", order_id=order_id))

@app.route("/admin/products/delete-image/<int:image_id>", methods=["POST"])
@admin_required
def admin_delete_image(image_id):
    try:
        image = ProductImage.query.get_or_404(image_id)
        product = image.product
        
        try:
            image_path = os.path.join("static", image.image_url)
            if os.path.exists(image_path):
                os.remove(image_path)
        except Exception as e:
            print(f"Error deleting image file: {e}")
        
        if product.image_url == image.image_url:
            remaining_images = ProductImage.query.filter(
                ProductImage.product_id == product.id,
                ProductImage.id != image_id
            ).first()
            
            if remaining_images:
                product.image_url = remaining_images.image_url
            else:
                product.image_url = None
        
        db.session.delete(image)
        db.session.commit()
        
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error deleting image: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/admin/orders/export")
@admin_required
def admin_orders_export():
    orders = Order.query.order_by(Order.created_at.desc()).all()

    output = "Order ID,Created,Name,Phone,City,Total,Status,Payment Status\n"
    for o in orders:
        created = o.created_at.strftime("%Y-%m-%d %H:%M")
        line = f'{o.id},"{created}","{o.customer_name}","{o.phone}","{o.city or ""}",{o.total_amount},{o.status or ""},{o.payment_status or ""}\n'
        output += line

    response = make_response(output)
    response.headers["Content-Disposition"] = "attachment; filename=orders.csv"
    response.headers["Content-Type"] = "text/csv"
    return response


# ----- CART ACTIONS -----

@app.route("/add/<int:product_id>")
def add_to_cart(product_id):
    cart = get_cart()
    
    # Get variant selections from query params
    color_id = request.args.get('color')
    size_id = request.args.get('size')
    
    # Create unique cart key with variants
    cart_key = str(product_id)
    if color_id:
        cart_key += f"_c{color_id}"
    if size_id:
        cart_key += f"_s{size_id}"
    
    # Store as dict with variant info
    if cart_key not in cart:
        cart[cart_key] = {
            'product_id': product_id,
            'quantity': 1,
            'color_id': color_id,
            'size_id': size_id
        }
    else:
        cart[cart_key]['quantity'] += 1
    
    session["cart"] = cart
    session["open_cart"] = True
    return redirect(request.referrer or url_for("shop"))


@app.route("/cart/increase/<path:cart_key>")
def increase_quantity(cart_key):
    cart = get_cart()
    if cart_key in cart:
        if isinstance(cart[cart_key], int):
            cart[cart_key] += 1
        else:
            cart[cart_key]['quantity'] += 1
    session["cart"] = cart
    session["open_cart"] = True
    return redirect(request.referrer or url_for("cart"))


@app.route("/cart/decrease/<path:cart_key>")
def decrease_quantity(cart_key):
    cart = get_cart()
    if cart_key in cart:
        if isinstance(cart[cart_key], int):
            cart[cart_key] -= 1
            if cart[cart_key] <= 0:
                cart.pop(cart_key)
        else:
            cart[cart_key]['quantity'] -= 1
            if cart[cart_key]['quantity'] <= 0:
                cart.pop(cart_key)
    session["cart"] = cart
    session["open_cart"] = True
    return redirect(request.referrer or url_for("cart"))


@app.route("/cart/remove/<path:cart_key>")
def remove_from_cart(cart_key):
    cart = get_cart()
    cart.pop(cart_key, None)
    session["cart"] = cart
    session["open_cart"] = True
    return redirect(request.referrer or url_for("cart"))


@app.route("/cart")
def cart():
    items, total, count = build_cart()
    return render_template("cart.html", cart_items=items, total=total)


@app.route("/order-success/<int:order_id>")
def order_success(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template("order_success.html", order=order)

@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items, total, count = build_cart()

    if not items:
        return redirect(url_for("shop"))

    if request.method == "POST":
        customer_name = request.form.get("name")
        phone = request.form.get("phone")
        email = request.form.get("email")
        address = request.form.get("address")
        city = request.form.get("city")
        pincode = request.form.get("pincode")
        notes = request.form.get("notes")
    
        
        # Get gift wraps
        import json
        gift_wraps_json = request.form.get("gift_wraps", "{}")
        gift_wraps = {}
        try:
            gift_wraps = json.loads(gift_wraps_json)
        except:
            pass
        
        wrap_total = sum(wrap.get('price', 0) for wrap in gift_wraps.values())
        subtotal = total + wrap_total

        SHIPPING_COST = 80
        FREE_SHIPPING_THRESHOLD = 1000
        shipping_cost = 0 if subtotal >= FREE_SHIPPING_THRESHOLD else SHIPPING_COST
        
        # Validate coupon
        if coupon_code:
            coupon = Coupon.query.filter_by(code=coupon_code).first()
            if coupon:
                is_valid, message = coupon.is_valid(subtotal)
                if is_valid:
                    coupon_discount = coupon.calculate_discount(subtotal)
                    coupon.times_used += 1
        
        final_total = subtotal - coupon_discount

        order = Order(
            customer_name=customer_name,
            phone=phone,
            email=email,
            address=address,
            city=city,
            pincode=pincode,
            notes=notes,
            subtotal=subtotal,
            coupon_code=coupon_code if coupon_discount > 0 else None,
            coupon_discount=coupon_discount,
            shipping_cost=shipping_cost,
            total_amount=final_total,
        )

        db.session.add(order)
        db.session.flush()

        for item in items:
            effective_price = item["effective_price"]
            oi = OrderItem(
                order_id=order.id,
                product_id=item["product"].id,
                product_name=item["product"].name,
                unit_price=effective_price,
                quantity=item["quantity"],
                color_variant_name=item["color_variant"].name if item["color_variant"] else None,
                size_variant_name=item["size_variant"].name if item["size_variant"] else None
            )
            db.session.add(oi)
            db.session.flush()
            
            # Handle gift wraps
            product_id_str = str(item["product"].id)
            if product_id_str in gift_wraps:
                wrap_data = gift_wraps[product_id_str]
                db.session.add(GiftWrap(
                    order_item_id=oi.id,
                    wrap_type=wrap_data.get('type'),
                    wrap_price=wrap_data.get('price')
                ))

        db.session.commit()
        session["cart"] = {}

        return redirect(url_for("order_success", order_id=order.id))

    return render_template("checkout.html", cart_items=items, total=total, count=count)


@app.route("/checkout_ajax", methods=["POST"])
def checkout_ajax():
    items, total, count = build_cart()

    if not items:
        return jsonify({"error": "cart_empty"}), 400

    customer_name = request.form.get("name") or "Customer"
    phone = request.form.get("phone") or ""
    email = request.form.get("email") or ""
    address = request.form.get("address") or ""
    city = request.form.get("city") or ""
    pincode = request.form.get("pincode") or ""
    notes = request.form.get("notes") or ""
    
    # Get gift wrap data
    import json
    gift_wraps_json = request.form.get("gift_wraps", "{}")
    gift_wraps = {}
    try:
        gift_wraps = json.loads(gift_wraps_json)
        print(f"DEBUG: Received gift wraps: {gift_wraps}")
    except Exception as e:
        print(f"ERROR parsing gift wraps: {e}")
    
    # Handle coupon
    coupon_code = request.form.get("coupon_code", "").strip().upper()
    coupon_discount = 0

    # Calculate total with gift wraps
    wrap_total = sum(wrap.get('price', 0) for wrap in gift_wraps.values())
    subtotal = total + wrap_total

    # Calculate shipping
    SHIPPING_COST = 80
    FREE_SHIPPING_THRESHOLD = 1000

    shipping_cost = 0 if subtotal >= FREE_SHIPPING_THRESHOLD else SHIPPING_COST
    final_total = subtotal - coupon_discount + shipping_cost

    # Validate and apply coupon
    if coupon_code:
        coupon = Coupon.query.filter_by(code=coupon_code).first()
        if coupon:
            is_valid, message = coupon.is_valid(subtotal)
            if is_valid:
                coupon_discount = coupon.calculate_discount(subtotal)
                coupon.times_used += 1
                print(f"✓ Coupon '{coupon_code}' applied! Discount: ₹{coupon_discount}")
            else:
                print(f"✗ Coupon validation failed: {message}")
        else:
            print(f"✗ Coupon '{coupon_code}' not found")

    final_total = subtotal - coupon_discount

    print(f"DEBUG: Subtotal: ₹{subtotal}, Discount: ₹{coupon_discount}, Final: ₹{final_total}")

    order = Order(
        customer_name=customer_name,
        phone=phone,
        email=email,
        address=address,
        city=city,
        pincode=pincode,
        notes=notes,
        subtotal=subtotal,
        coupon_code=coupon_code if coupon_discount > 0 else None,
        coupon_discount=coupon_discount,
        shipping_cost=shipping_cost,
        total_amount=final_total,
        payment_status="Unpaid",
        status="Pending"
    )
    db.session.add(order)
    db.session.flush()
    
    print(f"DEBUG: Created order #{order.id}")

    # Process each cart item
    for it in items:
        prod = it["product"]
        qty = it["quantity"]
        effective_price = it["effective_price"]
        
        oi = OrderItem(
            order_id=order.id,
            product_id=prod.id,
            product_name=prod.name,
            unit_price=effective_price,
            quantity=qty,
            color_variant_name=it["color_variant"].name if it["color_variant"] else None,
            size_variant_name=it["size_variant"].name if it["size_variant"] else None
        )
        db.session.add(oi)
        db.session.flush()
        
        print(f"DEBUG: Added OrderItem {oi.id} for product {prod.name}")
        
        # Check if this product has gift wrap
        product_id_str = str(prod.id)
        if product_id_str in gift_wraps:
            wrap_data = gift_wraps[product_id_str]
            gift_wrap = GiftWrap(
                order_item_id=oi.id,
                wrap_type=wrap_data.get('type'),
                wrap_price=wrap_data.get('price')
            )
            db.session.add(gift_wrap)
            print(f"DEBUG: Added gift wrap '{wrap_data.get('type')}' (₹{wrap_data.get('price')}) to item {oi.id}")
        else:
            print(f"DEBUG: No gift wrap for product {prod.id}")

    db.session.commit()
    print(f"DEBUG: Order {order.id} committed successfully\n")

    return jsonify({"order_id": order.id, "total": final_total})

@app.route("/validate_coupon", methods=["POST"])
def validate_coupon():
    data = request.get_json()
    code = data.get("code", "").strip().upper()
    cart_total = int(data.get("cart_total", 0))
    
    if not code:
        return jsonify({"valid": False, "message": "Please enter a coupon code"})
    
    coupon = Coupon.query.filter_by(code=code).first()
    
    if not coupon:
        return jsonify({"valid": False, "message": "Invalid coupon code"})
    
    is_valid, message = coupon.is_valid(cart_total)
    
    if not is_valid:
        return jsonify({"valid": False, "message": message})
    
    discount = coupon.calculate_discount(cart_total)
    new_total = cart_total - discount

    # After calculating discount
    SHIPPING_COST = 80
    FREE_SHIPPING_THRESHOLD = 1000
    shipping = 0 if cart_total >= FREE_SHIPPING_THRESHOLD else SHIPPING_COST
    new_total = cart_total - discount + shipping

    
    return jsonify({
        "valid": True,
        "message": f"Coupon '{code}' applied successfully!",
        "discount": discount,
        "shipping": shipping,
        "new_total": new_total,
        "discount_type": coupon.discount_type,
        "discount_value": coupon.discount_value
    })
    
    # Check if coupon exists
    if code not in coupons:
        print(f"DEBUG: Invalid coupon code '{code}'")
        return jsonify({
            "valid": False, 
            "message": "Invalid coupon code"
        })
    
    coupon = coupons[code]
    
    # Check minimum order requirement
    if cart_total < coupon["min_order"]:
        print(f"DEBUG: Cart total ₹{cart_total} below minimum ₹{coupon['min_order']}")
        return jsonify({
            "valid": False, 
            "message": f"Minimum order of ₹{coupon['min_order']} required for this coupon"
        })
    
    # Calculate discount
    if coupon["type"] == "percentage":
        discount = int(cart_total * coupon["value"] / 100)
    else:
        discount = coupon["value"]
    
    # Cap discount at cart total
    discount = min(discount, cart_total)
    
    print(f"DEBUG: Coupon '{code}' applied! Discount: ₹{discount}")
    
    return jsonify({
        "valid": True,
        "discount": discount,
        "message": f"🎉 You saved ₹{discount}!"
    })

# ----- CATEGORY MANAGEMENT ROUTES -----

@app.route("/admin/categories")
@admin_required
def admin_categories():
    categories = Category.query.order_by(Category.order_index).all()
    return render_template("admin_categories.html", categories=categories)

@app.route("/admin/reviews")
@admin_required
def admin_reviews():
    pending_reviews = ProductReview.query.filter_by(is_approved=False).order_by(ProductReview.created_at.desc()).all()
    approved_reviews = ProductReview.query.filter_by(is_approved=True).order_by(ProductReview.created_at.desc()).all()
    return render_template("admin_reviews.html", pending=pending_reviews, approved=approved_reviews)
    
@app.route("/admin/coupons")
@admin_required
def admin_coupons():
    coupons = Coupon.query.order_by(Coupon.created_at.desc()).all()
    return render_template("admin_coupons.html", coupons=coupons)

@app.route("/admin/coupons/add", methods=["GET", "POST"])
@admin_required
def admin_coupon_add():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        discount_type = request.form.get("discount_type")
        discount_value = int(request.form.get("discount_value") or 0)
        min_order_value = int(request.form.get("min_order_value") or 0)
        max_uses = request.form.get("max_uses")
        max_uses = int(max_uses) if max_uses else None
        expires_at_str = request.form.get("expires_at")
        
        expires_at = None
        if expires_at_str:
            try:
                expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d")
            except:
                pass
        
        # Check if code already exists
        existing = Coupon.query.filter_by(code=code).first()
        if existing:
            flash(f"Coupon code '{code}' already exists!", "danger")
            return redirect(url_for("admin_coupon_add"))
        
        coupon = Coupon(
            code=code,
            discount_type=discount_type,
            discount_value=discount_value,
            min_order_value=min_order_value,
            max_uses=max_uses,
            expires_at=expires_at,
            is_active=True
        )
        
        db.session.add(coupon)
        db.session.commit()
        flash(f"Coupon '{code}' created successfully!", "success")
        return redirect(url_for("admin_coupons"))
    
    from datetime import datetime
    return render_template("admin_coupon_form.html", coupon=None, now=datetime.utcnow())

@app.route("/admin/coupons/edit/<int:coupon_id>", methods=["GET", "POST"])
@admin_required
def admin_coupon_edit(coupon_id):
    coupon = Coupon.query.get_or_404(coupon_id)
    
    if request.method == "POST":
        coupon.code = request.form.get("code", "").strip().upper()
        coupon.discount_type = request.form.get("discount_type")
        coupon.discount_value = int(request.form.get("discount_value") or 0)
        coupon.min_order_value = int(request.form.get("min_order_value") or 0)
        
        max_uses = request.form.get("max_uses")
        coupon.max_uses = int(max_uses) if max_uses else None
        
        expires_at_str = request.form.get("expires_at")
        if expires_at_str:
            try:
                coupon.expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d")
            except:
                coupon.expires_at = None
        else:
            coupon.expires_at = None
        
        db.session.commit()
        flash(f"Coupon '{coupon.code}' updated successfully!", "success")
        return redirect(url_for("admin_coupons"))
    
    from datetime import datetime
    return render_template("admin_coupon_form.html", coupon=coupon, now=datetime.utcnow())

@app.route("/admin/coupons/toggle/<int:coupon_id>", methods=["POST"])
@admin_required
def admin_coupon_toggle(coupon_id):
    coupon = Coupon.query.get_or_404(coupon_id)
    coupon.is_active = not coupon.is_active
    db.session.commit()
    
    status = "activated" if coupon.is_active else "deactivated"
    flash(f"Coupon '{coupon.code}' {status}!", "success")
    return redirect(url_for("admin_coupons"))

@app.route("/admin/coupons/delete/<int:coupon_id>", methods=["POST"])
@admin_required
def admin_coupon_delete(coupon_id):
    coupon = Coupon.query.get_or_404(coupon_id)
    code = coupon.code
    db.session.delete(coupon)
    db.session.commit()
    flash(f"Coupon '{code}' deleted!", "success")
    return redirect(url_for("admin_coupons"))


@app.route("/admin/reviews/<int:review_id>/approve", methods=["POST"])
@admin_required
def admin_approve_review(review_id):
    review = ProductReview.query.get_or_404(review_id)
    review.is_approved = True
    db.session.commit()
    flash(f"Review from {review.customer_name} approved!", "success")
    return redirect(url_for('admin_reviews'))

@app.route("/admin/reviews/<int:review_id>/reject", methods=["POST"])
@admin_required
def admin_reject_review(review_id):
    review = ProductReview.query.get_or_404(review_id)
    
    # Delete images if they exist
    for img_url in [review.image1_url, review.image2_url]:
        if img_url:
            try:
                img_path = os.path.join("static", img_url)
                if os.path.exists(img_path):
                    os.remove(img_path)
            except:
                pass
    
    db.session.delete(review)
    db.session.commit()
    flash(f"Review from {review.customer_name} deleted.", "info")
    return redirect(url_for('admin_reviews'))

@app.route("/admin/categories/add", methods=["POST"])
@admin_required
def admin_category_add():
    name = request.form.get("name", "").strip()
    if name:
        existing = Category.query.filter_by(name=name).first()
        if not existing:
            max_order = db.session.query(db.func.max(Category.order_index)).scalar() or -1
            category = Category(name=name, order_index=max_order + 1)
            db.session.add(category)
            db.session.commit()
            flash(f"Category '{name}' added successfully!", "success")
        else:
            flash(f"Category '{name}' already exists!", "warning")
    return redirect(url_for("admin_categories"))

@app.route("/admin/categories/delete/<int:category_id>", methods=["POST"])
@admin_required
def admin_category_delete(category_id):
    category = Category.query.get_or_404(category_id)
    # Check if any products use this category
    products_count = Product.query.filter_by(category=category.name).count()
    if products_count > 0:
        flash(f"Cannot delete '{category.name}' - {products_count} products are using it!", "danger")
    else:
        db.session.delete(category)
        db.session.commit()
        flash(f"Category '{category.name}' deleted!", "success")
    return redirect(url_for("admin_categories"))

@app.route("/admin/categories/reorder", methods=["POST"])
@admin_required
def admin_category_reorder():
    data = request.get_json()
    order = data.get("order", [])
    for idx, cat_id in enumerate(order):
        category = Category.query.get(int(cat_id))
        if category:
            category.order_index = idx
    db.session.commit()
    return jsonify({"success": True})


# ------------ MAIN ------------

with app.app_context():
    db.create_all()
    # Initialize default categories if none exist
    if Category.query.count() == 0:
        default_categories = [
            "Seasonal", "Desk Buddies", "Keyrings", 
            "Mini Bouquet", "Yarn", "Bookmarks", "Forever Flowers"
        ]
        for idx, cat_name in enumerate(default_categories):
            db.session.add(Category(name=cat_name, order_index=idx))
        db.session.commit()
        print("✓ Default categories initialized")
    
if __name__ == "__main__":
    app.run(debug=True)

