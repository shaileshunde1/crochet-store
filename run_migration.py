from app import app, db
from sqlalchemy import text

with app.app_context():
    try:
        with db.engine.connect() as conn:
            # Add shipping_cost column
            print("Adding shipping_cost column to order table...")
            conn.execute(text("ALTER TABLE 'order' ADD COLUMN shipping_cost INTEGER"))
            
            # Set default values for existing records
            print("Setting default values for existing orders...")
            conn.execute(text("UPDATE 'order' SET shipping_cost = 0 WHERE shipping_cost IS NULL"))
            
            conn.commit()
            print("✓ Migration completed successfully!")
            print("✓ shipping_cost column added to order table")
            
    except Exception as e:
        print(f"✗ Migration failed: {e}")
        print("Note: If column already exists, you can ignore this error.")