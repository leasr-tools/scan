from fastapi import FastAPI, Request
import requests
import uuid
import os

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "LeaseScan API is live."}

@app.post("/process-lease")
async def process_lease(request: Request):
    data = await request.json()
    file_url = data.get("file_url")

    if not file_url:
        return {"status": "error", "message": "No file URL received."}

    # Generate a unique filename
    unique_id = str(uuid.uuid4())[:8]
    filename = f"/tmp/lease_{unique_id}.pdf"  # Use /tmp for ephemeral storage

    try:
        r = requests.get(file_url)
        r.raise_for_status()

        with open(filename, "wb") as f:
            f.write(r.content)

        # TODO: Add your lease processing code here
        print(f"[INFO] File {filename} saved and ready for processing.")

        # Simulate processing
        result = f"{filename} downloaded and processed successfully"

        # Clean up file after processing
        os.remove(filename)

        return {
            "status": "success",
            "message": result
        }

    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "message": f"Failed to download file: {str(e)}"
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"Unexpected error: {str(e)}"
        }
