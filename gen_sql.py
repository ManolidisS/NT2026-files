import json
import sqlite3
import time

uncompressed_file_path = "/experiment/etymology/raw-wiktextract-data.jsonl"
db_path = "/experiment/etymology/english_wiktionary.db"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Speed enhancements for fast database insertion
cursor.execute("PRAGMA synchronous = OFF")
cursor.execute("PRAGMA journal_mode = MEMORY")

# Added etymology column
cursor.execute("""
    CREATE TABLE IF NOT EXISTS dictionary (
        word TEXT,
        pos TEXT,
        etymology TEXT,
        data TEXT
    )
""")

print("Extracting English entries including etymology text...")
start_time = time.time()

batch = []
count = 0

with open(uncompressed_file_path, "r", encoding="utf-8") as f:
    for line in f:
        try:
            obj = json.loads(line)
            
            if obj.get("lang_code") == "en" and "word" in obj:
                word = obj["word"]
                pos = obj.get("pos", "")
                
                # Extract the plaintext etymology block
                etymology = obj.get("etymology_text", "Etymology not available.")
                
                batch.append((word, pos, etymology, line))
                count += 1
            
            if len(batch) >= 50000:
                cursor.executemany("INSERT INTO dictionary VALUES (?, ?, ?, ?)", batch)
                batch = []
                print(f"Indexed {count:,} entries...")
                
        except json.JSONDecodeError:
            continue

if batch:
    cursor.executemany("INSERT INTO dictionary VALUES (?, ?, ?, ?)", batch)

print("Rebuilding optimization index...")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_word ON dictionary (word)")
conn.commit()
conn.close()

print(f"Done! Extracted {count:,} entries with etymologies in {((time.time() - start_time) / 60):.2f} minutes.")
