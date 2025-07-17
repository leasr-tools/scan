from fastapi import FastAPI, Request
import requests
import uuid
import os
import pdfplumber

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "LeaseScan API is live."}

@app.post("/process-lease")
async def process_lease(request: Request):
    data = await request.json()
    file_url = data.get("file_url")
    email = data.get("email")

    if not file_url or not email:
        return {"status": "error", "message": "Missing file URL or email."}

    # Generate a unique filename
    unique_id = str(uuid.uuid4())[:8]
    filename = f"/tmp/lease_{unique_id}.pdf"  # Use /tmp for ephemeral storage
    report_filename = f"/tmp/report_{unique_id}.txt"

    try:
        # Download PDF from Tally URL
        r = requests.get(file_url)
        r.raise_for_status()

        with open(filename, "wb") as f:
            f.write(r.content)

        # Extract text with pdfplumber
        with pdfplumber.open(filename) as pdf:
            text = "".join(page.extract_text() or "" for page in pdf.pages)

        # Analyze with Claude API
        claude_response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.getenv("CLAUDE_API_KEY"),
                "Content-Type": "application/json",
                "anthropic-beta": "no-user-data-training"  # Prevent training
            },
            json={
                "model": "claude-3-sonnet-20240229",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": f"Analyze this lease: {text[:10000]}"}]
            }
        )
        claude_response.raise_for_status()
        claude_result = claude_response.json().get("content", [{}])[0].get("text", "")

        # Analyze with Grok 3 API
        grok_response = requests.post(
            os.getenv("GROK_API_URL"),
            headers={"Authorization": f"Bearer {os.getenv('GROK_API_KEY')}"},
            json={"query": f"Summarize key lease terms: {text[:10000]}"}
        )
        grok_response.raise_for_status()
        grok_result = grok_response.json().get("response", "")

        # Generate report
        report_content = f"Lease Analysis Report\n\nClaude Analysis:\n{claude_result}\n\nGrok Summary:\n{grok_result}"
        with open(report_filename, "w") as f:
            f.write(report_content)

        # Mock secure report URL (replace with S3 presigned URL in production)
        report_url = f"https://scan-qzy1.onrender.com/static/report_{unique_id}.txt"

        # Clean up files
        os.remove(filename)
        os.remove(report_filename)

        return {
            "status": "success",
            "message": "Lease processed successfully",
            "report_url": report_url,
            "email": email
        }

    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "message": f"Failed to download or process file: {str(e)}"
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"Unexpected error: {str(e)}"
        }
