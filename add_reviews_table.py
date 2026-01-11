from app import app, db

with app.app_context():
    with db.engine.connect() as conn:
        try:
            conn.execute(db.text('''
                CREATE TABLE IF NOT EXISTS product_review (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    customer_name VARCHAR(100) NOT NULL,
                    rating INTEGER NOT NULL,
                    review_text TEXT NOT NULL,
                    image1_url VARCHAR(255),
                    image2_url VARCHAR(255),
                    is_approved BOOLEAN DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES product(id) ON DELETE CASCADE
                )
            '''))
            conn.commit()
            print("✓ product_review table created successfully!")
        except Exception as e:
            print(f"Table creation: {e}")