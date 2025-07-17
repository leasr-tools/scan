from fastapi import FastAPI, Request
import requests

app = FastAPI()


@app.get("/")
def root():
    return {"message": "LeaseScan API is live"}


@app.post("/process-lease")
async def process_lease(request: Request):
    try:
        data = await request.json()
        file_url = data.get("file_url")

        if not file_url:
            return {
                "status": "error",
                "message": "Missing 'file_url' in request body."
            }

        # Download the PDF from the provided URL
        filename = "uploaded_lease.pdf"
        try:
            response = requests.get(file_url)
            response.raise_for_status()  # raises HTTPError for bad responses
            with open(filename, "wb") as f:
                f.write(response.content)
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to download file: {str(e)}"
            }

        # Add your processing code here
        print(f"File saved as {filename}")

        return {
            "status": "success",
            "message": f"{filename} downloaded successfully"
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"Unexpected error: {str(e)}"
        }
