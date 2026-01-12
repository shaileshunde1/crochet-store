import sqlite3

# Connect to database
conn = sqlite3.connect('store.db')
cursor = conn.cursor()

try:
    # Add new columns
    cursor.execute('ALTER TABLE product ADD COLUMN time_to_make_min INTEGER')
    cursor.execute('ALTER TABLE product ADD COLUMN time_to_make_max INTEGER')
    conn.commit()
    print("✓ Database updated successfully!")
except sqlite3.OperationalError as e:
    if "duplicate column name" in str(e):
        print("✓ Columns already exist!")
    else:
        print(f"✗ Error: {e}")
finally:
    conn.close()