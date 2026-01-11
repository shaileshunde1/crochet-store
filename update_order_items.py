from app import app, db

with app.app_context():
    with db.engine.connect() as conn:
        try:
            conn.execute(db.text('ALTER TABLE order_item ADD COLUMN color_variant_name VARCHAR(50)'))
            conn.commit()
            print("✓ Added color_variant_name column")
        except Exception as e:
            print(f"color_variant_name: {e}")
        
        try:
            conn.execute(db.text('ALTER TABLE order_item ADD COLUMN size_variant_name VARCHAR(50)'))
            conn.commit()
            print("✓ Added size_variant_name column")
        except Exception as e:
            print(f"size_variant_name: {e}")