"""
Amazon Product Extractor — LangGraph version with Streamlit UI.
Usage: streamlit run step2.py
"""

import re
import os
import asyncio
import random
import json
import time
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field
from typing import List, Optional, TypedDict
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
import nest_asyncio
import streamlit as st

nest_asyncio.apply()
os.environ["GROQ_API_KEY"] = st.secrets["GROQ_API_KEY"]
os.environ["LANGSMITH_API_KEY"] = st.secrets["LANGSMITH_API_KEY"]
os.environ["LANGSMITH_TRACING"] = st.secrets["LANGSMITH_TRACING"]
os.environ["LANGSMITH_PROJECT"] = st.secrets["LANGSMITH_PROJECT"]

# ── LLM ──
llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0.3)
selection_llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0)

# ── Headers ──
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ── Pydantic models ──
class SpecEntry(BaseModel):
    key: str = Field(description="Spec name as shown on the page")
    value: str = Field(description="Spec value as shown on the page")


class ProductSpecs(BaseModel):
    name: str = Field(description="Full product name/title")
    brand: str = Field(description="Brand name. 'N/A' if not found")
    price: str = Field(description="Selling price in INR e.g. '₹29,999'. 'N/A' if not found")
    original_price: str = Field(description="MRP before discount. 'N/A' if not found")
    rating: str = Field(description="Rating e.g. '4.3 out of 5'. 'N/A' if not found")
    num_reviews: str = Field(description="Number of reviews. 'N/A' if not found")
    specs: List[SpecEntry] = Field(description="ALL technical specs as key-value pairs")
    highlights: List[str] = Field(description="Top 3-5 selling points / bullet features")
    category: str = Field(default="general", description="Product category e.g. 'phone', 'book', 'washing machine'")


# ── LangGraph State ──
class ProductState(TypedDict):
    query: str
    product_url: Optional[str]
    raw_html: Optional[str]
    raw_text: Optional[str]
    image_url: Optional[str]
    product_data: Optional[dict]
    error: Optional[str]


# ── Helper functions ──
async def fetch_page(url: str) -> str:
    for attempt in range(3):
        try:
            headers = {**HEADERS, "User-Agent": random.choice(USER_AGENTS)}
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(url, headers=headers, timeout=15.0)
                if r.status_code == 200:
                    return r.text
                await asyncio.sleep(1 + attempt)
        except Exception:
            await asyncio.sleep(1)
    return ""


def extract_product_image(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img", id="landingImage")
    if img:
        return img.get("data-old-hires") or img.get("src", "")
    img = soup.find("img", id="imgBlkFront")
    if img:
        return img.get("src", "")
    match = re.search(r'"hiRes"\s*:\s*"(https://[^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'"large"\s*:\s*"(https://[^"]+)"', html)
    if match:
        return match.group(1)
    return ""


def html_to_text(html: str, max_chars: int = 10000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "iframe", "svg", "link", "meta"]):
        tag.decompose()
    lines, prev = [], None
    for line in soup.get_text(separator="\n").split("\n"):
        line = line.strip()
        if line and len(line) > 1 and line != prev:
            lines.append(line)
            prev = line
    return "\n".join(lines)[:max_chars]


# ── LangGraph Nodes ──

def search_product(state: ProductState) -> ProductState:
    """Node 1: Search Amazon for the product and get the URL using LLM to pick the best match."""
    query = state["query"]
    url = f"https://www.amazon.in/s?k={query.replace(' ', '+')}"
    html = asyncio.run(fetch_page(url))
    if not html:
        state["error"] = "Could not search Amazon."
        return state

    soup = BeautifulSoup(html, "html.parser")

    # Collect candidate products with titles and ASINs
    candidates = []
    for item in soup.find_all("div", attrs={"data-asin": True}):
        asin = item.get("data-asin", "").strip()
        if not asin or len(asin) != 10:
            continue
        # Skip sponsored items (expanded patterns)
        sponsored = item.find(string=re.compile(r"Sponsored|sponsored|Ad$"))
        if sponsored:
            continue
        if item.find("span", class_=re.compile(r"puis-label-popover-default|s-label-popover-default|puis-sponsored-label")):
            continue
        if item.find("span", attrs={"data-component-type": "s-sponsored-label-info-icon"}):
            continue
        title_el = item.find("h2") or item.find("span", class_=re.compile(r"a-text-normal|a-size-medium"))
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        # Extract price for context
        price_el = item.find("span", class_="a-price-whole")
        price = price_el.get_text(strip=True) if price_el else "N/A"

        candidates.append({"asin": asin, "title": title, "price": price})
        if len(candidates) >= 10:
            break

    if not candidates:
        asins = re.findall(r"/dp/([A-Z0-9]{10})", html)
        if not asins:
            state["error"] = "Could not find product on Amazon."
            return state
        state["product_url"] = f"https://www.amazon.in/dp/{asins[0]}"
        return state

    # Ask LLM to pick the best match with improved prompt
    candidate_list = "\n".join(f"{i+1}. {c['title']} — ₹{c['price']}" for i, c in enumerate(candidates))
    messages = [
        SystemMessage(content="""You are a product search assistant. Given a user's search query and a list of Amazon search results, return ONLY the number of the result that best matches the EXACT product the user wants.

RULES:
- Match the brand, model name, and variant (storage/RAM/color) as closely as possible.
- NEVER pick accessories like cases, covers, chargers, or screen protectors unless the query asks for one.
- NEVER pick combo packs unless the query asks for one.
- Prefer the product sold by the brand or a reputable seller.
- If the query mentions a specific storage/RAM variant (e.g. 8GB/256GB), pick that exact variant.
- Reply with ONLY the number. Nothing else."""),
        HumanMessage(content=f"Query: {query}\n\nResults:\n{candidate_list}"),
    ]
    try:
        response = selection_llm.invoke(messages)
        choice = int(re.search(r'\d+', response.content).group()) - 1
        if 0 <= choice < len(candidates):
            state["product_url"] = f"https://www.amazon.in/dp/{candidates[choice]['asin']}"
            return state
    except Exception:
        pass

    # Fallback: return first candidate
    state["product_url"] = f"https://www.amazon.in/dp/{candidates[0]['asin']}"
    return state


def fetch_product_page(state: ProductState) -> ProductState:
    """Node 2: Fetch the product detail page HTML."""
    if state.get("error"):
        return state
    raw_html = asyncio.run(fetch_page(state["product_url"]))
    if not raw_html:
        state["error"] = "Failed to fetch product page."
        return state
    state["raw_html"] = raw_html
    state["image_url"] = extract_product_image(raw_html)
    state["raw_text"] = html_to_text(raw_html)
    return state


def extract_details(state: ProductState) -> ProductState:
    """Node 3: Use LLM to extract structured product details."""
    if state.get("error"):
        return state

    structured_llm = llm.with_structured_output(ProductSpecs)
    messages = [
        SystemMessage(content=f"""You are a product information extraction expert.
Below is text from an Amazon.in product detail page.

PAGE TEXT:
{state['raw_text']}

RULES:
1. Extract name, brand, price, original_price, rating, num_reviews.
2. For "specs": extract EVERY specification from Technical Details / Product Information tables as key-value pairs.
3. For "highlights": pick 3-5 standout feature bullets.
4. Only extract what is on the page. Do NOT invent data.
5. Use 'N/A' for fields truly not found."""),
        HumanMessage(content=f"Extract all product details for: {state['query']}"),
    ]

    try:
        for attempt in range(4):
            try:
                result = structured_llm.invoke(messages)
                state["product_data"] = result.model_dump()
                return state
            except Exception as e:
                err_str = str(e).lower()
                retryable = "429" in str(e) or "rate" in err_str or "connection" in err_str or "timeout" in err_str
                if attempt < 3 and retryable:
                    time.sleep(5 + attempt * 5)
                    continue
                raise e
    except Exception as e:
        # Try to parse from failed_generation
        err_str = str(e)
        try:
            raw_json = None
            # Method 1: Look for JSON after <function=ProductSpecs>
            # Use non-greedy match to find the JSON object right after the function tag
            fg_match = re.search(r"'failed_generation':\s*'<function=\w+>\s*(.+?)'\}\}", err_str, re.DOTALL)
            if fg_match:
                json_str = fg_match.group(1).strip()
                # The JSON might be cut off, try to find complete JSON
                # Find the outermost balanced braces
                brace_count = 0
                end_idx = -1
                for i, ch in enumerate(json_str):
                    if ch == '{':
                        brace_count += 1
                    elif ch == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i
                            break
                if end_idx > 0:
                    raw_json = json.loads(json_str[:end_idx + 1])

            # Method 2: Try regex for function tag pattern without greedy
            if not raw_json:
                fn_match = re.search(r'<function=\w+>\s*(\{"name".*?"specs".*?\].*?\})', err_str, re.DOTALL)
                if fn_match:
                    try:
                        raw_json = json.loads(fn_match.group(1))
                    except json.JSONDecodeError:
                        pass

            if raw_json:
                allowed = {f.alias or name for name, f in ProductSpecs.model_fields.items()}
                cleaned = {k: v for k, v in raw_json.items() if k in allowed}
                state["product_data"] = ProductSpecs(**cleaned).model_dump()
                return state
        except Exception:
            pass
        state["error"] = f"LLM extraction failed: {e}"
    return state


# ── Comparison function ──
def compare_products(products: list[dict]) -> str:
    """Compare multiple products using LLM and return markdown response."""
    product_summaries = []
    for i, data in enumerate(products, 1):
        specs_str = "\n".join(f"  - {s['key']}: {s['value']}" for s in data.get("specs", []))
        highlights_str = "\n".join(f"  - {h}" for h in data.get("highlights", []))
        product_summaries.append(f"""
PRODUCT {i}: {data['name']}
  Brand: {data['brand']}
  Price: {data['price']} (MRP: {data['original_price']})
  Rating: {data['rating']} ({data['num_reviews']} reviews)
  Highlights:
{highlights_str}
  Specifications:
{specs_str}
""")

    comparison_prompt = "\n".join(product_summaries)
    messages = [
        SystemMessage(content="""You are a product comparison expert helping Indian consumers make smart buying decisions.

Given the structured details of multiple products, provide:

1. **Quick Comparison Table** — Compare key specs side by side (use markdown table).
2. **Pros & Cons** — For each product, list 3-4 pros and 2-3 cons.
3. **Value for Money** — Which product offers the best value considering price vs features?
4. **Final Recommendation** — Clearly recommend ONE product as the best buy with reasoning.

Be specific, use actual numbers from the specs. Be honest about trade-offs.
Format your response in clean markdown."""),
        HumanMessage(content=f"Compare these products and recommend the best one to buy:\n{comparison_prompt}"),
    ]

    for attempt in range(4):
        try:
            response = llm.invoke(messages)
            return response.content
        except Exception as e:
            err_str = str(e).lower()
            retryable = "429" in str(e) or "rate" in err_str or "connection" in err_str or "timeout" in err_str
            if attempt < 3 and retryable:
                time.sleep(3 + attempt * 4)
                continue
            raise e


# ── Route: skip remaining nodes if error ──
def should_continue(state: ProductState) -> str:
    if state.get("error"):
        return "end"
    return "continue"


# ── Build the LangGraph ──
def build_extraction_graph():
    graph = StateGraph(ProductState)

    graph.add_node("search_product", search_product)
    graph.add_node("fetch_product_page", fetch_product_page)
    graph.add_node("extract_details", extract_details)

    graph.add_edge(START, "search_product")
    graph.add_conditional_edges("search_product", should_continue, {"continue": "fetch_product_page", "end": END})
    graph.add_conditional_edges("fetch_product_page", should_continue, {"continue": "extract_details", "end": END})
    graph.add_edge("extract_details", END)

    return graph.compile()


extraction_graph = build_extraction_graph()


# ── Streamlit UI ──
st.set_page_config(page_title="ShopSmart AI", page_icon="🛒", layout="wide")
st.title("🛒 ShopSmart AI — Product Comparison Tool")
st.caption("Enter product names → We fetch details from Amazon → AI analyzes & recommends the best buy for you")

num_products = st.slider("How many products to check?", min_value=1, max_value=5, value=1)

queries = []
cols = st.columns(num_products)
for i in range(num_products):
    with cols[i]:
        q = st.text_input(f"Product {i+1}", placeholder="e.g. Samsung Galaxy S25", key=f"product_{i}")
        queries.append(q.strip())

if st.button("🔍 Extract Details", type="primary"):
    filled = [q for q in queries if q]
    if not filled:
        st.warning("Please enter at least one product name.")
    else:
        results = []
        progress = st.progress(0, text="Starting...")
        for idx, query in enumerate(filled):
            progress.progress(idx / len(filled), text=f"Extracting: {query}...")
            # Add delay between products to avoid Groq rate limits
            if idx > 0:
                time.sleep(3)
            # Invoke the LangGraph
            result = extraction_graph.invoke({
                "query": query,
                "product_url": None,
                "raw_html": None,
                "raw_text": None,
                "image_url": None,
                "product_data": None,
                "error": None,
            })
            results.append(result)
        progress.progress(1.0, text="Done!")

        # Separate successful and failed
        successful = [r for r in results if r.get("product_data")]
        failed = [r for r in results if r.get("error")]

        for r in failed:
            error_msg = r.get("error", "")
            if "connection" in error_msg.lower() or "timeout" in error_msg.lower():
                st.error(f"❌ **{r['query']}**: Service temporarily unavailable. Please try again in a few seconds.")
            elif "429" in error_msg or "rate" in error_msg.lower():
                st.error(f"❌ **{r['query']}**: Too many requests. Please wait a moment and try again.")
            elif "could not find" in error_msg.lower() or "could not search" in error_msg.lower():
                st.error(f"❌ **{r['query']}**: Product not found on Amazon. Try a different search term.")
            elif "failed to fetch" in error_msg.lower():
                st.error(f"❌ **{r['query']}**: Unable to load product page. Please try again.")
            elif "extraction failed" in error_msg.lower():
                st.warning(f"⚠️ **{r['query']}**: Could not extract product details. The page format may not be supported. Please try again.")
            else:
                st.error(f"❌ **{r['query']}**: Something went wrong. Please try again.")

        if successful:
            cols = st.columns(len(successful))
            for col, r in zip(cols, successful):
                data = r["product_data"]
                image_url = r.get("image_url", "")
                product_url = r.get("product_url", "")
                with col:
                    if image_url:
                        st.markdown(
                            f'<div style="width:250px;height:250px;display:flex;align-items:center;justify-content:center;overflow:hidden;margin:auto">'
                            f'<img src="{image_url}" style="max-width:100%;max-height:100%;object-fit:contain" />'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    st.markdown(f"##### [{data['name'][:50]}]({product_url})")
                    st.metric("Price", data["price"])
                    st.caption(f"MRP: {data['original_price']}")
                    st.metric("Rating", data["rating"])
                    st.caption(f"{data['num_reviews']} reviews")
                    st.markdown(f"**Brand:** {data['brand']}")

                    if data["highlights"]:
                        st.markdown("**⭐ Highlights**")
                        for h in data["highlights"]:
                            st.markdown(f"- {h}")

                    if data["specs"]:
                        with st.expander(f"📋 Specs ({len(data['specs'])})"):
                            for s in data["specs"]:
                                st.markdown(f"**{s['key']}:** {s['value']}")

                    with st.expander("📋 JSON"):
                        st.json(data)

        # ── Comparison via LLM ──
        if len(successful) >= 2:
            st.divider()
            st.header("🏆 AI Comparison & Recommendation")

            with st.spinner("Comparing products with LLM..."):
                try:
                    product_data_list = [r["product_data"] for r in successful]
                    comparison_md = compare_products(product_data_list)
                    st.markdown(comparison_md)
                except Exception as e:
                    st.error(f"❌ Comparison failed: {e}")