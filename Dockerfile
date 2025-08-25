# Playwright base image includes Chromium + dependencies
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# (Optional) create an unprivileged user
RUN useradd -m appuser
USER appuser

# Start the bot
CMD ["python", "main.py"]
