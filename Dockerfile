FROM python:3.12-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    libxshmfence1 \
    libx11-xcb1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium

COPY . .

# Create .streamlit directory for secrets (Railway will inject env vars)
RUN mkdir -p .streamlit
# Railway ignores EXPOSE, but it's good for documentation
EXPOSE 8501

# Using a shell form here helps with variable expansion
CMD streamlit run amazon_product_comparision.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true