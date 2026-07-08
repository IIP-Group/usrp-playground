FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --ws-max-size must be >= MAX_UPLOAD_MB (default 200 MB): uvicorn's own
# default of 16 MB would kill larger uploads with a hard 1009 close before
# main.py can reply with a clean "file_too_large" error.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "220000000"]
