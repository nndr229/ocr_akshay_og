FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY main.py .

ENV PORT=8700
EXPOSE 8700

CMD ["python", "main.py"]
