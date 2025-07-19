from fastapi import FastAPI, Request
import requests
import uuid
import os
import pdfplumber
from pdf2image import convert_from_path
import pytesseract
import json
import boto3
from botocore.exceptions import ClientError
import logging
import re  # Added for regex processing
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

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
BUCKET_NAME = os.getenv("AWS_S3_BUCKET")
if not BUCKET_NAME:
    raise ValueError("AWS_S3_BUCKET environment variable is not set")

def get_grok_risk_prompt():
    return """
Act as a Commercial Real Estate risk analyst. Using the provided JSON of lease clauses, perform a comprehensive risk analysis:

0. DeepSearch for Context
- Infer missing risks from clauses and identify property type (retail, industrial, office) to adjust risk detection:
  * Retail: co-tenancy, percentage rent.
  * Industrial: environmental liability, maintenance.
  * Office: CAM caps, occupancy risks.
- Note unusual terms (e.g., missing escalations) in risk categories.
- Validate rent schedule from clauses; flag gaps or inconsistencies for manual review.

1. Risk Identification & Quantification
- Evaluate each clause (e.g., termination, rent, CAM).
- Use provided NOI or annual rent as proxy if NOI is missing; if rent data is incomplete (e.g., gaps), estimate based on prior year and flag for review.
- Estimate NOI impact ($/year) with rationale when data allows. For each calculated NOI impact, include a 'calculation' field with a clear breakdown (e.g., '$840,000/year = $70,000/month × 12 months' or '$300,000 = early termination penalty').
- Identify positive aspects (e.g., strong tenant credit, favorable rent escalations) and include in a 'positive_highlights' field in the JSON output.

2. Risk Severity
- High (>25% NOI), moderate (10-25%), low (<10%); use qualitative severity if NOI is unknown.

3. Confidence & Manual Review
- Flag <90% confidence, missing data, or ambiguity with page and reason.

4. Deal Impact Score
- Start at 10, deduct -2 (high), -1 (moderate), -0.5 (low), -0.5 (manual review), cap at 1.
- Provide score_explanation and total_risk_impact by category (termination, financial, operational, environmental).

5. Financial & Lease Structure
- Highlight rent gaps, CAM absence, lease_type (NNN/gross), and expense responsibilities.

6. Property & Tenant Context
- Assess tenant credit (if data), deposit, guarantees based on JSON.

7. Environmental & Legal
- Flag missing hazardous material or zoning clauses with qualitative notes.

8. Insurance
- Assess coverage adequacy; flag missing business interruption insurance.

9. Risk Traceability
- Include reason for each risk severity, avoiding duplicates.

10. Investor Summary
- 75-word summary of strengths, risks, and cash flow outlook.

11. JSON Output
{
  "lease_type": "NNN",
  "risks": [{"type": "termination_early", "noi_impact": 120000, "calculation": "$120,000 = early termination penalty", "severity": "high", "reason": "...", "manual_review": false, "page": 1}],
  "risk_categories": {"termination": 120000, ...},
  "total_risk_impact": 165000,
  "deal_impact_score": 6.5,
  "score_explanation": "...",
  "review_manually": [{"type": "rent_base", "page": 2, "reason": "..."}],
  "financial_flags": [{"type": "missing_escalation", "impact": "..."}],
  "security_measures": {"deposit": 20000, "guarantee": "Unknown", "tenant_credit": "Unknown"},
  "time_risks": [{"type": "lease_expiration", "date": "2025-05-31", "impact": "..."}],
  "market_comparison": {"rent_escalation": "Unknown", "cam_caps": "Unknown"},
  "positive_highlights": ["Strong rent escalation to $70,000/month", "Tenant maintains $1M liability insurance"],
  "investor_summary": "..."
}
"""

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
        pdf_filename = f"/tmp/report_{unique_id}.pdf"
        s3_pdf_key = f"reports/{unique_id}/report.pdf"
        logger.info(f"Generated unique_id: {unique_id}, s3_pdf_key: {s3_pdf_key}")

        # Download PDF from Tally URL
        logger.info(f"Downloading PDF from {file_url}")
        r = requests.get(file_url, timeout=30)
        r.raise_for_status()
        with open(filename, "wb") as f:
            f.write(r.content)
        logger.info(f"PDF downloaded to {filename}")

        # Extract text with pdfplumber and fallback to Tesseract
        def extract_text_from_pdf(filename):
            try:
                with pdfplumber.open(filename) as pdf:
                    text = "".join(page.extract_text() or "" for page in pdf.pages)
                if text and text.strip():
                    logger.info("Text extracted successfully with pdfplumber")
                    return text
            except Exception as e:
                logger.warning(f"pdfplumber failed: {str(e)}")
            
            # Fallback to Tesseract OCR
            logger.info("Falling back to Tesseract OCR")
            images = convert_from_path(filename, dpi=300)  # Higher DPI for better accuracy
            text = "\n".join(pytesseract.image_to_string(image) for image in images)
            logger.info(f"Extracted {len(text)} characters with Tesseract")
            return text

        text = extract_text_from_pdf(filename)
        logger.info(f"Total extracted {len(text)} characters from PDF")

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
        # Claude API call with increased max_tokens
        logger.info(f"Calling Claude API with prompt length: {len(claude_prompt)} and text length: {len(text)}")
        claude_response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.getenv("CLAUDE_API_KEY"),
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "max-tokens-3-5-sonnet-2024-07-15"  # Enable 8192 token output
            },
            json={
                "model": "claude-3-5-sonnet-20240620",
                "max_tokens": 8192,  # Increased output limit
                "messages": [{"role": "user", "content": f"{claude_prompt}\n{text[:200000]}"}]  # 200k token context
            },
            timeout=120
        )
        logger.debug(f"Claude API response status: {claude_response.status_code}, headers: {claude_response.headers}")
        logger.debug(f"Claude API response body: {claude_response.text}")
        claude_response.raise_for_status()
        claude_result = claude_response.json().get("content", [{}])[0].get("text", "{}")
        # Extract JSON from the text block
        json_match = re.search(r'\{.*\}', claude_result, re.DOTALL)
        if json_match:
            claude_data = json.loads(json_match.group(0))
        else:
            raise ValueError("No valid JSON found in Claude response")
        logger.info(f"Claude API returned {len(claude_data.get('clauses', []))} clauses, trust_score: {claude_data.get('trust_score', 'N/A')}")

        # Log rent schedule for verification
        rent_clauses = [c for c in claude_data.get("clauses", []) if c.get("type").lower() == "rent"]
        logger.info(f"Extracted rent clauses: {rent_clauses}")

        # Validate Claude output
        checklist = ["rent", "term", "termination", "co-tenancy", "CAM", "maintenance"]
        missing_clauses = [item for item in checklist if item not in [c["type"].lower() for c in claude_data.get("clauses", [])] + claude_data.get("missing_clauses", [])]
        if missing_clauses:
            logger.warning(f"Missing clauses: {', '.join(missing_clauses)}")

        # Grok API call with increased max_tokens
        logger.info("Calling Grok API")
        grok_payload = {
            "model": "grok-3",
            "messages": [
                {"role": "system", "content": get_grok_risk_prompt()},
                {"role": "user", "content": json.dumps(claude_data)}
            ],
            "max_tokens": 8192  # Increased output limit
        }
        logger.debug(f"Grok request payload: {json.dumps(grok_payload)}")
        grok_response = requests.post(
            os.getenv("GROK_API_URL", "https://api.x.ai/v1/messages"),
            headers={"Authorization": f"Bearer {os.getenv('GROK_API_KEY')}"},
            json=grok_payload,
            timeout=120
        )
        logger.debug(f"Grok response status: {grok_response.status_code}, text: {grok_response.text}")
        grok_response.raise_for_status()
        grok_json = grok_response.json()
        logger.debug(f"Grok full response: {grok_json}")
        grok_content = grok_json.get("choices", [{}])[0].get("message", {}).get("content", "")
        # Extract JSON directly from content
        grok_data = grok_content  # No regex needed, use raw content
        if not grok_data or not grok_data.strip():
            raise ValueError("No valid JSON block found in Grok response")
        grok_result = json.loads(grok_data)
        logger.info(f"Grok API returned {len(grok_result.get('risks', []))} risks, deal_impact_score: {grok_result.get('deal_impact_score', 'N/A')}")

        # Generate PDF report
        logger.info("Generating PDF report")
        pdf = SimpleDocTemplate(pdf_filename, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        story.append(Paragraph(f"<b>Lease Summary Report</b>", styles['Heading1']))
        story.append(Paragraph(f"<b>Property</b>: [Property Name]", styles['Normal']))
        story.append(Paragraph(f"<b>Uploaded</b>: {os.getenv('CURRENT_DATE', '2025-07-19 15:52:00 CDT')}", styles['Normal']))  # Updated to current time
        story.append(Paragraph(f"<b>Generated by</b>: LeaseScan AI", styles['Normal']))
        story.append(Paragraph(f"<b>Trust Score</b>: {claude_data.get('trust_score', 95)}%", styles['Normal']))
        story.append(Spacer(1, 12))
        story.append(Paragraph("<b>Key Details</b>", styles['Heading2']))
        for clause in claude_data.get("clauses", []):
            story.append(Paragraph(f"- <b>{clause['type'].title()}</b>: {clause['description']} (Page {clause['page']}, {clause['confidence']}%", styles['Normal']))
        if claude_data.get("missing_clauses"):
            story.append(Paragraph(f"- <b>Missing Clauses</b>: {', '.join(clause.title() for clause in claude_data['missing_clauses'])}", styles['Normal']))
        story.append(Spacer(1, 12))
        story.append(Paragraph("<b>Risk Flags</b>", styles['Heading2']))
        # Categorize risks by severity
        high_risks = [r for r in grok_result.get("risks", []) if r.get("severity", "").lower() == "high"]
        moderate_risks = [r for r in grok_result.get("risks", []) if r.get("severity", "").lower() == "moderate"]
        low_risks = [r for r in grok_result.get("risks", []) if r.get("severity", "").lower() == "low"]
        if high_risks:
            story.append(Paragraph("<b>High Risk</b>", styles['Heading3']))
            for risk in high_risks:
                readable_type = risk['type'].replace("_", " ").title()
                story.append(Paragraph(f"- <b>{readable_type}</b> (Page {risk.get('page', 'N/A')}): ${risk['noi_impact']}/year ({risk.get('calculation', 'N/A')})", styles['Normal']))
        if moderate_risks:
            story.append(Paragraph("<b>Moderate Risk</b>", styles['Heading3']))
            for risk in moderate_risks:
                readable_type = risk['type'].replace("_", " ").title()
                story.append(Paragraph(f"- <b>{readable_type}</b> (Page {risk.get('page', 'N/A')}): ${risk['noi_impact']}/year ({risk.get('calculation', 'N/A')})", styles['Normal']))
        if low_risks:
            story.append(Paragraph("<b>Low Risk</b>", styles['Heading3']))
            for risk in low_risks:
                readable_type = risk['type'].replace("_", " ").title()
                story.append(Paragraph(f"- <b>{readable_type}</b> (Page {risk.get('page', 'N/A')}): ${risk['noi_impact']}/year ({risk.get('calculation', 'N/A')})", styles['Normal']))
        if not (high_risks or moderate_risks or low_risks):
            story.append(Paragraph("- <b>No Significant Risks Found</b>: All clauses indicate low or no financial/operational risk.", styles['Normal']))
        if grok_result.get("review_manually"):
            story.append(Spacer(1, 12))
            story.append(Paragraph("<b>Review Manually to Validate</b>", styles['Heading2']))
            for item in grok_result["review_manually"]:
                readable_type = item['type'].replace("_", " ").title()
                story.append(Paragraph(f"- <b>Page {item['page']}, {readable_type}</b>: {item['reason']}", styles['Normal']))
        story.append(Spacer(1, 12))
        # Add Positive Highlights section
        if grok_result.get("positive_highlights"):
            story.append(Paragraph("<b>Positive Highlights</b>", styles['Heading2']))
            for highlight in grok_result.get("positive_highlights", []):
                story.append(Paragraph(f"- {highlight}", styles['Normal']))
        story.append(Spacer(1, 12))
        story.append(Paragraph("<b>Investor Summary</b>", styles['Heading2']))
        story.append(Paragraph(grok_result.get('investor_summary', 'Stable lease with minimal risks.'), styles['Normal']))
        story.append(Paragraph(f"<b>Deal Impact Score</b>: {grok_result.get('deal_impact_score', 8)}/10 ({'Low' if grok_result.get('deal_impact_score', 8) >= 8 else 'Moderate'} Risk)", styles['Normal']))

        pdf.build(story)
        logger.info(f"PDF generated at {pdf_filename}")

        # Upload PDF to S3
        logger.info(f"Uploading PDF to S3: {s3_pdf_key}")
        s3_client.upload_file(pdf_filename, BUCKET_NAME, s3_pdf_key, ExtraArgs={'ContentType': 'application/pdf'})

        # Generate presigned URL for user delivery (24-hour expiry)
        logger.info("Generating S3 presigned URL")
        report_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": s3_pdf_key},
            ExpiresIn=86400
        )

        # Clean up local files
        logger.info(f"Cleaning up local files: {filename}, {pdf_filename}")
        os.remove(filename)
        os.remove(pdf_filename)

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
        logger.error(f"Invalid data error: {str(e)}", exc_info=True)
        logger.error(f"Grok response details: status={grok_response.status_code if 'grok_response' in locals() else 'N/A'}, text={grok_response.text if 'grok_response' in locals() else 'N/A'}")
        return {
            "status": "error",
            "message": f"Invalid data: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Unexpected error: {str(e)}"
        }
