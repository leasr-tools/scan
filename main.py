import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from plotly.subplots import make_subplots
import numpy_financial as npf
from xai_grok import GrokClient
import pdfplumber
from pdf2image import convert_from_bytes
import pytesseract
import re
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart

# Streamlit page config
st.set_page_config(layout="wide", page_title="CRE Deal Analyzer")
st.markdown("""
<style>
.stApp { background: linear-gradient(to bottom right, #1e3a8a, #1f2937); color: white; font-family: Inter; }
h1, h2, h3 { color: #facc15; }
.stButton>button { background-color: #facc15; color: #1e3a8a; border-radius: 8px; }
.stTextInput>div>input { background-color: #374151; color: white; border-radius: 8px; }
.stNumberInput>div>input { background-color: #374151; color: white; border-radius: 8px; }
.stSelectbox>div>div>select { background-color: #374151; color: white; border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# Grok 3 comps function
@st.cache_data
def fetch_grok_comps(address, property_type="office"):
    client = GrokClient(api_key=st.secrets["GROK_API_KEY"])
    prompt = f"""
    Search for recent {property_type} lease comps near {address}.
    Extract: address, rent per square foot, lease term (months), concessions (e.g., free rent months).
    Sources: LoopNet, Crexi, CoStar, X posts.
    Return as JSON with at least 3 comps.
    If fewer than 3 comps, generate synthetic comps based on market trends for {property_type} in the region.
    Validate data for consistency, flagging outliers.
    Output: {{'comps': [{{\"address\": str, \"rent_psf\": float, \"lease_term\": int, \"concessions\": str}}], 'insights': str, 'warnings': str}}
    """
    result = client.generate(prompt)
    comps = pd.DataFrame(result.get("comps", []))
    if not comps.empty:
        return comps[["address", "rent_psf", "lease_term", "concessions"]], result.get("insights", ""), result.get("warnings", "")
    return None, "", "No comps found"

# PDF extraction function
@st.cache_data
def extract_pdf_data(file):
    try:
        # Try PDFplumber for text-based PDFs
        with pdfplumber.open(file) as pdf:
            text = "".join(page.extract_text() or "" for page in pdf.pages)
        if not text.strip():
            # Fallback to OCR for image-based PDFs
            images = convert_from_bytes(file.read())
            text = "".join(pytesseract.image_to_string(img) for img in images)
        # Reset file pointer
        file.seek(0)
        return text
    except Exception as e:
        return str(e)

# Parse extracted text
def parse_pdf_data(text):
    data = {"address": "", "property_type": "office", "square_feet": 10000, "rent_psf": None}
    # Regex for rent (e.g., "$25/sqft", "$25 per square foot")
    rent_match = re.search(r"\$\s*(\d+\.?\d*)\s*(?:/sqft|/square\s+foot|psf)", text, re.IGNORECASE)
    if rent_match:
        data["rent_psf"] = float(rent_match.group(1))
    # Basic parsing for other fields (extend as needed)
    address_match = re.search(r"(\d+\s+[A-Za-z\s]+,\s*[A-Za-z\s]+,\s*[A-Z]{2}\s*\d{5})", text)
    if address_match:
        data["address"] = address_match.group(1)
    sqft_match = re.search(r"(\d{1,3}(?:,\d{3})*)\s*(?:sqft|square\s+feet)", text, re.IGNORECASE)
    if sqft_match:
        data["square_feet"] = int(sqft_match.group(1).replace(",", ""))
    return data

# Financial calculator (from original script)
def analyze_deal(purchase_price, rent_psf, square_feet, expenses, loan_amount, interest_rate, loan_term):
    rent = rent_psf * square_feet / 12  # Monthly rent
    noi = (rent - expenses) * 12
    cap_rate = noi / purchase_price
    monthly_payment = -npf.pmt(interest_rate / 12, loan_term * 12, loan_amount)
    cash_flow = noi / 12 - monthly_payment
    coc_return = (cash_flow * 12) / (purchase_price - loan_amount)
    schedule = []
    balance = loan_amount
    for i in range(loan_term * 12):
        interest = balance * interest_rate / 12
        principal = monthly_payment - interest
        balance -= principal
        schedule.append({"Year": i // 12 + 1, "Interest": interest, "Principal": principal, "Cash Flow": cash_flow})
    cash_flows = [-purchase_price + loan_amount] + [cash_flow * 12] * loan_term
    irr = npf.irr(cash_flows) if cash_flows else 0
    return {
        "Cap Rate": cap_rate,
        "Cash Flow": cash_flow,
        "CoC Return": coc_return,
        "IRR": irr,
        "Schedule": pd.DataFrame(schedule)
    }

# Enhanced PDF report with charts
def generate_pdf_report(scenarios, comps, insights):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(100, 750, "CRE Deal Analysis Report")
    y = 700
    c.setFont("Helvetica", 12)
    # Scenarios
    for name, result in scenarios.items():
        c.drawString(100, y, f"{name} Scenario")
        y -= 20
        c.drawString(120, y, f"Cap Rate: {result['Cap Rate']:.2%}")
        c.drawString(120, y-20, f"Cash Flow: ${result['Cash Flow']:,.0f}/month")
        c.drawString(120, y-40, f"IRR: {result['IRR']:.2%}")
        y -= 60
    # Comps
    if comps is not None:
        c.drawString(100, y, "Lease Comps")
        y -= 20
        for _, row in comps.iterrows():
            c.drawString(120, y, f"{row['address']}: ${row['rent_psf']:.2f}/sqft, {row['lease_term']} months")
            y -= 20
    # Insights
    c.drawString(100, y, "AI Insights")
    c.drawString(120, y-20, insights)
    # Bar chart (simplified)
    d = Drawing(200, 100)
    bc = VerticalBarChart()
    bc.data = [[scenarios[s]["Cap Rate"] * 100 for s in ["Conservative", "Base", "Optimistic"]]]
    bc.categoryAxis.categoryNames = ["Conservative", "Base", "Optimistic"]
    d.add(bc)
    c.drawImage(d, 100, y-150)
    c.save()
    buffer.seek(0)
    return buffer

# Main app
st.title("CRE Deal Analyzer")
st.markdown("A premium tool for CRE deal analysis ($40–$75/month). Upload a report or enter details to get started.")

# Access code
access_code = st.text_input("Enter Access Code", type="password")
if access_code != "crebeta25":
    st.error("Invalid access code")
    st.stop()

# PDF upload
uploaded_file = st.file_uploader("Upload CoStar/Title Report (PDF)", type="pdf")
pdf_data = {}
if uploaded_file:
    with st.spinner("Extracting PDF data..."):
        text = extract_pdf_data(uploaded_file)
        pdf_data = parse_pdf_data(text)
        st.success("PDF data extracted")
        if pdf_data.get("rent_psf"):
            st.write(f"Extracted Rent: ${pdf_data['rent_psf']:.2f}/sqft")
        else:
            st.warning("No rent data found in PDF")

# Property inputs
st.markdown("### Property Details")
col1, col2 = st.columns(2)
with col1:
    address = st.text_input("Property Address", value=pdf_data.get("address", ""))
    property_type = st.selectbox("Property Type", ["Office", "Retail", "Industrial"], index=["Office", "Retail", "Industrial"].index(pdf_data.get("property_type", "office")))
with col2:
    square_feet = st.number_input("Square Footage", min_value=1000, value=pdf_data.get("square_feet", 10000))
    use_ai_comps = st.checkbox("Use AI Lease Comps", value=True)

# Financial inputs
st.markdown("### Financial Inputs")
col3, col4 = st.columns(2)
with col3:
    purchase_price = st.number_input("Purchase Price ($)", value=1000000)
    expenses = st.number_input("Monthly Expenses ($)", value=5000)
with col4:
    loan_amount = st.number_input("Loan Amount ($)", value=800000)
    interest_rate = st.number_input("Interest Rate (%)", value=5.0) / 100
    loan_term = st.number_input("Loan Term (years)", value=20)

# Fetch comps
if st.button("Analyze Deal"):
    with st.spinner("Fetching lease comps..."):
        comps, insights, warnings = fetch_grok_comps(address, property_type) if use_ai_comps else (None, "", "")
        if comps is not None:
            st.subheader("Lease Comps")
            st.dataframe(comps)
            st.markdown(f"**AI Insights**: {insights}")
            if warnings:
                st.warning(warnings)
            # Use PDF rent if available, else Grok comps
            conservative_rent_psf = pdf_data.get("rent_psf", comps["rent_psf"].quantile(0.25))
            base_rent_psf = pdf_data.get("rent_psf", comps["rent_psf"].median())
            optimistic_rent_psf = pdf_data.get("rent_psf", comps["rent_psf"].quantile(0.75))
        else:
            st.warning("No AI comps found. Enter manual rents.")
            conservative_rent_psf = st.number_input("Conservative Rent ($/sqft/year)", value=25.0)
            base_rent_psf = st.number_input("Base Rent ($/sqft/year)", value=28.0)
            optimistic_rent_psf = st.number_input("Optimistic Rent ($/sqft/year)", value=30.0)

        # Scenario analysis
        scenarios = {
            "Conservative": analyze_deal(purchase_price, conservative_rent_psf, square_feet, expenses, loan_amount, interest_rate, loan_term),
            "Base": analyze_deal(purchase_price, base_rent_psf, square_feet, expenses, loan_amount, interest_rate, loan_term),
            "Optimistic": analyze_deal(purchase_price, optimistic_rent_psf, square_feet, expenses, loan_amount, interest_rate, loan_term)
        }

        # Display scenarios
        st.subheader("Scenario Analysis")
        cols = st.columns(3)
        for i, (name, result) in enumerate(scenarios.items()):
            with cols[i]:
                st.markdown(f"**{name} Scenario**")
                st.metric("Cap Rate", f"{result['Cap Rate']:.2%}")
                st.metric("Cash Flow", f"${result['Cash Flow']:,.0f}/month")
                st.metric("CoC Return", f"{result['CoC Return']:.2%}")
                st.metric("IRR", f"{result['IRR']:.2%}")

        # Sensitivity analysis
        st.subheader("Sensitivity Analysis")
        sensitivity = []
        for rent_adj in [-0.1, 0, 0.1]:
            adjusted_rent = base_rent_psf * (1 + rent_adj)
            result = analyze_deal(purchase_price, adjusted_rent, square_feet, expenses, loan_amount, interest_rate, loan_term)
            sensitivity.append({"Rent Change": f"{rent_adj:.0%}", "IRR": result["IRR"]})
        st.dataframe(pd.DataFrame(sensitivity))

        # Bar charts
        st.subheader("Financial Metrics")
        fig = make_subplots(rows=1, cols=3, subplot_titles=("Cap Rate", "Cash Flow", "IRR"))
        fig.add_bar(x=list(scenarios.keys()), y=[scenarios[s]["Cap Rate"] * 100 for s in scenarios], row=1, col=1)
        fig.add_bar(x=list(scenarios.keys()), y=[scenarios[s]["Cash Flow"] for s in scenarios], row=1, col=2)
        fig.add_bar(x=list(scenarios.keys()), y=[scenarios[s]["IRR"] * 100 for s in scenarios], row=1, col=3)
        fig.update_layout(showlegend=False, height=300)
        st.plotly_chart(fig, use_container_width=True)

        # PDF download
        st.download_button(
            label="Download PDF Report",
            data=generate_pdf_report(scenarios, comps, insights),
            file_name="cre_deal_report.pdf",
            mime="application/pdf"
        )

# User-contributed comps
with st.form("user_comps"):
    st.markdown("### Contribute Lease Comp")
    comp_address = st.text_input("Comp Address")
    rent_psf = st.number_input("Rent ($/sqft/year)", min_value=0.0)
    lease_term = st.number_input("Lease Term (months)", min_value=1)
    concessions = st.text_input("Concessions")
    submit = st.form_submit_button("Submit Comp")
    if submit:
        client = GrokClient(api_key=st.secrets["GROK_API_KEY"])
        prompt = f"Validate lease comp: Address={comp_address}, Rent=${rent_psf:.2f}/sqft, Term={lease_term}, Concessions={concessions}"
        if client.generate(prompt).get("is_valid"):
            pd.DataFrame([{"address": comp_address, "rent_psf": rent_psf, "lease_term": lease_term, "concessions": concessions}]).to_csv("user_comps.csv", mode="a", index=False)
            st.success("Comp added!")
