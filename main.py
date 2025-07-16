from fastapi import FastAPI, Request
import requests
import os

app = FastAPI()

@app.post("/process-lease")
async def process_lease(request: Request):
    data = await request.json()
    file_url = data.get("file_url")

    if not file_url:
        return {"status": "error", "message": "No file URL received."}

    # Download the lease PDF
    filename = "uploaded_lease.pdf"
    r = requests.get(file_url)
    with open(filename, "wb") as f:
        f.write(r.content)

    print(f"File saved as {filename}")
    # Add your AI-processing code here later
    return {"status": "success", "message": f"{filename} downloaded"}