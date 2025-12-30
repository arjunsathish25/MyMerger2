import sqlite3

db_path = r'instance\crm_app.db'  # Update this path if your database is elsewhere

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

columns = [
    ("internet_message_id", "TEXT"),
    ("conversation_id", "TEXT"),
    ("reply_received", "BOOLEAN"),
    ("reply_content", "TEXT"),
    ("reply_received_at", "DATETIME"),
    ("error_details", "TEXT"),
]

for column_name, column_type in columns:
    try:
        cursor.execute(f"ALTER TABLE contact_status ADD COLUMN {column_name} {column_type};")
        print(f"Column '{column_name}' added successfully.")
    except sqlite3.OperationalError as e:
        print(f"Error adding '{column_name}': {e}")

conn.commit()
conn.close()