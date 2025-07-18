from fastapi import FastAPI, Request
import requests
import uuid
import os
import pdfplumber
import json
import boto3
from botocore.exceptions import ClientError
import logging

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,  # Keep DEBUG for detailed logs
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = FastAPI()

# AWS S3 setup
s3_client = boto3.client(
    "s3",
    region_name=os.getenv("AWS_REGION", "us-east-1"),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
)
BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

@app.get("/")
def read_root():
    logger.info("Root endpoint accessed")
    return {"status": "ok", "message": "LeaseScan AI is live."}

@app.post("/process-lease")
async def process_lease(request: Request):
    logger.info("Starting lease processing")
    try:
        data = await request.json()
        file_url = data.get("file_url")
        email = data.get("email")
        logger.info(f"Received request with file_url: {file_url}, email: {email}")

        if not file_url or not email:
            logger.error("Missing file URL or email")
            return {"status": "error", "message": "Missing file URL or email."}

        # Extract email from Tally metadata
        email = email.split(",")[-2] if "," in email else email  # Assumes email is second-to-last
        logger.info(f"Parsed email: {email}")

        # Generate unique filenames
        unique_id = uuid.uuid4().hex
        filename = f"/tmp/lease_{unique_id}.pdf"
        report_filename = f"/tmp/report_{unique_id}.txt"
        s3_report_key = f"reports/{unique_id}/report.txt"
        logger.info(f"Generated unique_id: {unique_id}, s3_report_key: {s3_report_key}")

        # Download PDF from Tally URL
        logger.info(f"Downloading PDF from {file_url}")
        r = requests.get(file_url, timeout=30)
        r.raise_for_status()
        with open(filename, "wb") as f:
            f.write(r.content)
        logger.info(f"PDF downloaded to {filename}")

        # Extract text with pdfplumber
        logger.info("Extracting text with pdfplumber")
        with pdfplumber.open(filename) as pdf:
            text = "".join(page.extract_text() or "" for page in pdf.pages)
        logger.info(f"Extracted {len(text)} characters from PDF")

        # Claude prompt for comprehensive clause extraction
        claude_prompt = """
Act as an expert Commercial Real Estate lease analyst. Analyze the provided lease text (50-100 pages) to extract all critical clauses, ensuring no key details are missed. Extract:
- Offer and acceptance (parties, key terms)
- Rent (base, escalations, percentage rent)
- Lease term (start/end dates, renewals)
- Termination clauses (early termination, notice periods)
- Co-tenancy clauses (anchor tenant dependencies)
- CAM provisions (caps, escalations)
- Maintenance responsibilities
- Subleasing/assignment clauses
- Insurance/indemnification
- Default/remedy provisions
- Force majeure
- Other risk-related clauses (e.g., use restrictions)
For each clause, provide:
- Exact wording
- Page number(s)
- Section number (if applicable)
- Confidence score (1-100%)
- Description (1-2 sentences)
- Flag for manual review if ambiguous (<90% confidence)
Confirm absence of clauses (e.g., "No co-tenancy found"). Output a JSON object:
{
  "clauses": [
    {"type": "rent", "wording": "...", "page": "...", "section": "...", "confidence": 98, "description": "...", "manual_review": false},
    ...
  ],
  "missing_clauses": ["..."],
  "trust_score": 95
}
Do not store or train on the text. Process in-memory and discard after output.
"""
        # Claude API call with adjusted parameters
        logger.info(f"Calling Claude API with prompt length: {len(claude_prompt)} and text length: {len(text)}")
        claude_response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.getenv("CLAUDE_API_KEY"),
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01"  # Baseline version; check docs for newer version
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": f"{claude_prompt}\n{text[:25000]}"}]  # Reduced to 25k chars
            },
            timeout=120  # Increased to 120 seconds
        )
        logger.debug(f"Claude API response status: {claude_response.status_code}, headers: {claude_response.headers}")
        logger.debug(f"Claude API response body: {claude_response.text}")
        claude_response.raise_for_status()
        claude_result = claude_response.json().get("content", [{}])[0].get("text", "{}")
        if not claude_result or claude_result == "{}":
            raise ValueError("Claude API returned empty or invalid JSON response")
        claude_data = json.loads(claude_result)
        logger.info(f"Claude API returned {len(claude_data.get('clauses', []))} clauses, trust_score: {claude_data.get('trust_score', 'N/A')}")

        # Validate Claude output
        checklist = ["rent", "term", "termination", "co-tenancy", "CAM", "maintenance"]
        missing_clauses = [item for item in checklist if item not in [c["type"].lower() for c in claude_data.get("clauses", [])] + claude_data.get("missing_clauses", [])]
        if missing_clauses:
            logger.warning(f"Missing clauses: {', '.join(missing_clauses)}")
            # Optional: Re-run Claude for missing clauses
            pass

        # Grok prompt for risk analysis and investor summary
        grok_prompt = """
Act as a Commercial Real Estate risk analyst. Using the provided JSON of lease clauses, perform:
1. Risk analysis: Quantify NOI impact ($/year) for each clause (assume NOI=$120,000 unless specified).
2. Risk severity: High (>25% NOI), moderate (10-25%), low (<10%).
3. Manual review: Flag clauses with <90% confidence with page numbers.
4. Deal Impact Score: 1-10 (10=no risks, -1/low risk, -2/moderate, -3/high, -0.5/low-confidence).
5. Investor summary: 50-word LP summary (e.g., "Low-risk lease, stable cash flow").
Use DeepSearch for CRE context (e.g., retail co-tenancy risks). Output JSON:
{
  "risks": [
    {"type": "termination", "noi_impact": 48000, "severity": "high", "manual_review": false},
    ...
  ],
  "deal_impact_score": 8,
  "review_manually": [{"type": "CAM", "page": 25, "reason": "Confidence 82%"}],
  "investor_summary": "..."
}
Do not store or train on data. Process in-memory and discard.
"""
        # Grok API call
        logger.info("Calling Grok API")
        grok_response = requests.post(
            os.getenv("GROK_API_URL", "https://api.x.ai/v1/chat/completions"),
            headers={"Authorization": f"Bearer {os.getenv('GROK_API_KEY')}"},
            json={"query": f"{grok_prompt}\n{json.dumps(claude_data)}", "model": "grok-beta"},
            timeout=60
        )
        grok_response.raise_for_status()
        grok_data = grok_response.json().get("response", "{}")
        grok_result = json.loads(grok_data)
        logger.info(f"Grok API returned {len(grok_result.get('risks', []))} risks, deal_impact_score: {grok_result.get('deal_impact_score', 'N/A')}")

        # Generate report
        logger.info("Generating report")
        report_content = (
            f"# Lease Summary Report\n"
            f"**Property**: [Property Name]\n"
            f"**Uploaded**: {os.getenv('CURRENT_DATE', '2025-07-17 19:35:00 CDT')}\n"  # Updated to current time
            f"**Generated by**: LeaseScan AI\n"
            f"**Trust Score**: {claude_data.get('trust_score', 95)}%\n\n"
            f"## Key Details\n"
        )
        for clause in claude_data.get("clauses", []):
            report_content += f"- **{clause['type'].title()}**: {clause['description']} (Page {clause['page']}, {clause['confidence']}%)\n"
        if claude_data.get("missing_clauses"):
            report_content += f"- **Missing Clauses**: {', '.join(clause.title() for clause in claude_data['missing_clauses'])}\n"
        report_content += "\n## Risk Flags\n"
        if not grok_result.get("risks"):
            report_content += "- **No Significant Risks Found**: All clauses indicate low or no financial/operational risk.\n"
        for risk in grok_result.get("risks", []):
            report_content += f"- **{risk['type'].title()}** (Page {risk.get('page', 'N/A')}): ${risk['noi_impact']}/year, {risk['severity']} risk\n"
        if grok_result.get("review_manually"):
            report_content += "\n## Review Manually to Validate\n"
            for item in grok_result["review_manually"]:
                report_content += f"- **Page {item['page']}, {item['type'].title()}**: {item['reason']}\n"
        report_content += (
            f"\n## Investor Summary\n"
            f"{grok_result.get('investor_summary', 'Stable lease with minimal risks.')}\n"
            f"**Deal Impact Score**: {grok_result.get('deal_impact_score', 8)}/10 ({'Low' if grok_result.get('deal_impact_score', 8) >= 8 else 'Moderate'} Risk)\n"
        )

        # Save report locally for upload
        logger.info(f"Saving report to {report_filename}")
        with open(report_filename, "w") as f:
            f.write(report_content)

        # Upload report to S3
        logger.info(f"Uploading report to S3: {s3_report_key}")
        s3_client.upload_file(report_filename, BUCKET_NAME, s3_report_key)

        # Generate presigned URL for user delivery (24-hour expiry)
        logger.info("Generating S3 presigned URL")
        report_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": s3_report_key},
            ExpiresIn=86400
        )

        # Clean up local files
        logger.info(f"Cleaning up local files: {filename}, {report_filename}")
        os.remove(filename)
        os.remove(report_filename)

        logger.info(f"Lease processing completed successfully, report_url: {report_url}")
        return {
            "status": "success",
            "message": "Lease processing completed successfully",
            "report_url": report_url,
            "email": email
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}", exc_info=True)
        logger.error(f"Claude response details: status={claude_response.status_code if 'claude_response' in locals() else 'N/A'}, text={claude_response.text if 'claude_response' in locals() else 'N/A'}")
        return {
            "status": "error",
            "message": f"Failed to download or process file: {str(e)}"
        }
    except ClientError as e:
        logger.error(f"S3 error: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"S3 error: {str(e)}"
        }
    except ValueError as e:
        logger.error(f"Claude API returned invalid data: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Claude API returned invalid data: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Unexpected error: {str(e)}"
        }
