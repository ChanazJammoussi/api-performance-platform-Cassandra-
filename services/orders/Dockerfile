FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN opentelemetry-bootstrap -a install

COPY main.py .
COPY fault.py .

CMD ["opentelemetry-instrument", \
     "--service_name", "orders", \
     "--exporter_otlp_endpoint", "http://otel-collector:4317", \
     "--exporter_otlp_protocol", "grpc", \
     "--traces_exporter", "otlp", \
     "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
