import requests
import os
from urllib.parse import quote  # Pro kódování názvů souborů v URL

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://jclouatxxqsagdhchryc.supabase.co")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
BUCKET_NAME = "qr"

def upload_qr_to_supabase(file_path, filename):
    # Zakóduj název souboru pro URL (řeší mezery/diakritiku)
    encoded_filename = quote(filename)
    
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{encoded_filename}"
    
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
        "Content-Type": "image/png"
    }

    try:
        with open(file_path, "rb") as f:
            response = requests.post(  # Použij POST místo PUT
                upload_url,
                headers=headers,
                data=f.read()
            )

        if response.status_code in [200, 201]:
            return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{encoded_filename}"
        else:
            print(f"Chyba při nahrávání: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        print(f"Chyba při komunikaci se Supabase: {str(e)}")
        return None
