import sqlite3

# Connect to your database
conn = sqlite3.connect('store.db')
cursor = conn.cursor()

try:
    # Add the missing columns
    cursor.execute('ALTER TABLE "order" ADD COLUMN coupon_code VARCHAR(50)')
    print("✓ Added coupon_code column")
except:
    print("⚠ coupon_code already exists")

try:
    cursor.execute('ALTER TABLE "order" ADD COLUMN coupon_discount INTEGER DEFAULT 0')
    print("✓ Added coupon_discount column")
except:
    print("⚠ coupon_discount already exists")

try:
    cursor.execute('ALTER TABLE "order" ADD COLUMN subtotal INTEGER DEFAULT 0')
    print("✓ Added subtotal column")
except:
    print("⚠ subtotal already exists")

# Update existing orders to have subtotal = total_amount
cursor.execute('UPDATE "order" SET subtotal = total_amount WHERE subtotal = 0')
print("✓ Updated existing orders")

conn.commit()
conn.close()
print("\n✅ Database fixed! You can now run your app.")