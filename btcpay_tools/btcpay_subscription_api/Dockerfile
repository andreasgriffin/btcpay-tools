FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY btcpay_tools/btcpay_subscription_api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --system --uid 10001 --no-create-home api
COPY --chown=10001:10001 btcpay_tools/btcpay_subscription_api /app/btcpay_tools/btcpay_subscription_api
RUN touch /app/btcpay_tools/__init__.py

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os; from urllib.request import urlopen; urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health', timeout=2)"
CMD ["python", "-m", "btcpay_tools.btcpay_subscription_api"]
