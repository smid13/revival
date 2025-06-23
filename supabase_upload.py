import requests
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
BUCKET_NAME = "qr"

def upload_qr_to_supabase(file_path, filename):
    upload_url = f"{SUPABASE_URL}/storage/v1/{filename}"
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
        "Content-Type": "image/png"
    }

    with open(file_path, "rb") as f:
        res = requests.put(upload_url, headers=headers, data=f.read())

    if res.status_code in [200, 201]:
        # Veřejná URL k souboru
        return f"{SUPABASE_URL}/storage/v1/{filename}"
    else:
        print(f"Chyba při nahrávání: {res.status_code} - {res.text}")
        return None
