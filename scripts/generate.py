import os
import sys
import time
import json
import smtplib
from email.mime.text import MIMEText
import requests
from dotenv import load_dotenv
import re
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

# ================= CONFIG =================
EXTERNAL_API_BASE = 'https://cloud-text-manager-server.vercel.app'
EXTERNAL_API_URL = f'{EXTERNAL_API_BASE}/api/all-files'
GENERATED_DIR = os.path.join(os.getcwd(), 'generated-content')

BATCH_SIZE = 1
CONCURRENCY_LIMIT = 1
REQUEST_DELAY = 1.5
MAX_RETRIES = 5

os.makedirs(GENERATED_DIR, exist_ok=True)

# ================= OPENROUTER =================
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('No OPENROUTER_API_KEY variable found.')

def generate_response(messages):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "z-ai/glm-4.5-air:free",
        "messages": messages
    }
    
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()

# ================= SLUG GENERATOR =================
def generate_safe_filename(original_filename):
    base_name = os.path.splitext(original_filename)[0]
    # To mimic JS replace logic: string to lower, remove non-word chars, trim, replace spaces/dashes with single dash
    slug = base_name.lower()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = slug.strip()
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    slug = slug.strip('-')
    return slug + '.mdx'

# ================= EMAIL =================
def send_email(subject, text):
    email_host = os.environ.get('EMAIL_HOST')
    if not email_host:
        return
        
    email_port = int(os.environ.get('EMAIL_PORT', 587))
    email_secure = os.environ.get('EMAIL_SECURE', '').lower() == 'true'
    email_user = os.environ.get('EMAIL_USER')
    email_pass = os.environ.get('EMAIL_PASS')
    email_from = os.environ.get('EMAIL_FROM', email_user)
    email_to = os.environ.get('EMAIL_TO', email_user)
    
    msg = MIMEText(text)
    msg['Subject'] = subject
    msg['From'] = email_from
    msg['To'] = email_to
    
    try:
        if email_secure or email_port == 465:
            server = smtplib.SMTP_SSL(email_host, email_port)
        else:
            server = smtplib.SMTP(email_host, email_port)
            server.starttls()
            
        server.login(email_user, email_pass)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Failed to send email: {e}")

# ================= UTILS =================
def sleep(seconds):
    time.sleep(seconds)

# ================= RETRY LOGIC =================
def generate_with_retry(prompt_text):
    attempt = 0
    
    while attempt < MAX_RETRIES:
        try:
            sleep(REQUEST_DELAY)
            
            messages = [
                {
                    "role": "user",
                    "content": prompt_text
                }
            ]
            response_data = generate_response(messages)
            
            choices = response_data.get('choices', [])
            if not choices:
                raise ValueError('Empty response')
                
            content = choices[0].get('message', {}).get('content')
            if not content:
                raise ValueError('Empty response content')
                
            return content
            
        except Exception as e:
            attempt += 1
            print(f"Retry {attempt}/{MAX_RETRIES}")
            
            # Simple rate limit handling
            if hasattr(e, 'response') and e.response is not None and getattr(e.response, 'status_code', None) == 429:
                print('Rate limit hit')
                
            sleep(1.0 * attempt)
            
            if attempt >= MAX_RETRIES:
                raise e

# ================= FILE PROCESS =================
def process_file(file_data):
    original_filename = file_data.get('originalFilename')
    print(f"Processing: {original_filename}")
    
    try:
        req = requests.get(file_data.get('secureUrl'))
        req.raise_for_status()
        
        try:
            prompt_text = req.json()
            if not isinstance(prompt_text, str):
                prompt_text = json.dumps(prompt_text)
        except ValueError:
            prompt_text = req.text
            
        generated_content = generate_with_retry(prompt_text)
        
        safe_filename = generate_safe_filename(original_filename)
        file_path = os.path.join(GENERATED_DIR, safe_filename)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(generated_content)
            
        put_url = f"{EXTERNAL_API_URL}/{file_data.get('_id')}"
        put_data = {
            "status": "AlreadyCopy",
            "completedTimestamp": int(time.time() * 1000)
        }
        requests.put(put_url, json=put_data)
        
        print(f"✅ Done: {safe_filename}")
        return {"success": True, "name": safe_filename}
        
    except Exception as e:
        print(f"❌ Failed: {original_filename} - {str(e)}")
        return {"success": False, "name": original_filename, "error": str(e)}

# ================= PARALLEL QUEUE =================
def run_queue(files):
    results = []
    # If CONCURRENCY_LIMIT is 1, regular loop is fine, but we mimic JS Promise.all style
    with ThreadPoolExecutor(max_workers=max(1, CONCURRENCY_LIMIT)) as executor:
        future_to_file = {executor.submit(process_file, f): f for f in files}
        for future in as_completed(future_to_file):
            results.append(future.result())
    return results

# ================= FOLDER STATS =================
def get_folder_stats(directory):
    if not os.path.exists(directory):
        return {"totalFiles": 0, "mdxFiles": 0}
        
    files = os.listdir(directory)
    mdx_files = sum(1 for f in files if f.endswith('.mdx'))
    return {"totalFiles": len(files), "mdxFiles": mdx_files}

# ================= SEND BATCH EMAIL =================
def send_batch_email(success, failed):
    tz = timezone(timedelta(hours=6)) # Dhaka is UTC+6
    now = datetime.now(tz)
    date_time_str = now.strftime('%m/%d/%Y, %I:%M:%S %p')
    
    folder_stats = get_folder_stats(GENERATED_DIR)
    
    subject = f"Content Generation Report lawn - {date_time_str} - {len(success)} Success, {len(failed)} Failed"
    
    success_list = '\n'.join([f"- {f['name']}" for f in success])
    failed_list = '\n'.join([f"- {f['name']} ({f.get('error')})" for f in failed])
    
    body = f'''Multi-Key Content Generation Report
----------------------------------
Date & Time (BDT): {date_time_str}
Total Processed: {len(success) + len(failed)}
Success: {len(success)}
Failed: {len(failed)}

Successful Files:
{success_list}

Failed Files:
{failed_list}

Folder Stats:
- Total files in folder: {folder_stats["totalFiles"]}
- Total MDX files: {folder_stats["mdxFiles"]}

Generated content has been saved to the repository.
'''
    send_email(subject, body)

# ================= MAIN =================
def main():
    print('🚀 Starting Processing...')
    
    try:
        response = requests.get(f"{EXTERNAL_API_BASE}/api/all-files?section=General")
        response.raise_for_status()
        
        all_files = response.json()
        pending_files = [f for f in all_files if f.get('status') == 'Pending'][:BATCH_SIZE]
        
        if not pending_files:
            print('No pending files.')
            return
            
        print(f"Found {len(pending_files)} files.")
        
        results = run_queue(pending_files)
        
        success = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]
        
        send_batch_email(success, failed)
        
        print('🎉 Batch Completed Successfully')
        
    except Exception as e:
        print(f'Fatal error: {str(e)}')
        send_email('Batch Failed', str(e))
        sys.exit(1)

if __name__ == "__main__":
    main()
