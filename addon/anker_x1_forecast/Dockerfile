# Our OWN glibc base — NOT HA's Alpine add-on base. python:3.12-slim is Debian/glibc cp312;
# on aarch64 it pulls scikit-learn's prebuilt manylinux wheel. That is the entire point of P0.
FROM python:3.12-slim@sha256:afdbd1e03f1f46c63fe1aad93f7c96c1c4ab5b2e76eddd1e89fe94e44e9c1418
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# P1 app modules + the vendored forecast_core package (server.py imports health/trainer,
# which import forecast_core). WORKDIR /app is on sys.path so flat imports resolve.
COPY server.py health.py trainer.py predictor.py run.sh ./
COPY forecast_core ./forecast_core
RUN chmod +x run.sh && useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser
CMD ["./run.sh"]
