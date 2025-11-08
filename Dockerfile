FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .
ENV PORT=9066 SCRAPE_INTERVAL=10
EXPOSE 9066

CMD ["python", "-u", "exporter.py"]
