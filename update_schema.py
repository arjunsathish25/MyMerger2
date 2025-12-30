import sqlite3
from pathlib import Path

def update_database_schema():
    """
    Adds missing columns to the contact_status table in a non-destructive way.
    This script is safe to run multiple times.
    """
    db_path = Path('instance') / 'crm_app.db'

    if not db_path.exists():
        print(f"Database file not found at '{db_path}'. No update needed. The app will create it on startup.")
        return

    print(f"Connecting to database at '{db_path}'...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get existing columns to avoid trying to add duplicates
    cursor.execute("PRAGMA table_info(contact_status)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    print(f"Found existing columns: {existing_columns}")

    # Define the columns needed for the sequence feature, including constraints.
    # The constraints are important for adding NOT NULL columns to an existing table.
    columns_to_ensure = [
        ("sequence_step", "INTEGER NOT NULL DEFAULT 0"),
        ("next_follow_up_at", "DATETIME"),
        ("sequence_status", "TEXT NOT NULL DEFAULT 'inactive'"),
    ]

    # Loop through the columns and add them if they don't exist
    for column_name, column_def in columns_to_ensure:
        if column_name not in existing_columns:
            try:
                print(f"Attempting to add column '{column_name}'...")
                cursor.execute(f"ALTER TABLE contact_status ADD COLUMN {column_name} {column_def};")
                print(f"SUCCESS: Column '{column_name}' added to the 'contact_status' table.")
            except sqlite3.OperationalError as e:
                print(f"ERROR adding column '{column_name}': {e}")
        else:
            print(f"INFO: Column '{column_name}' already exists. No action needed.")

    conn.commit()
    conn.close()
    print("\nDatabase schema update process finished.")

if __name__ == "__main__":
    update_database_schema()
