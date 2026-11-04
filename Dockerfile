FROM python:3.13-slim
WORKDIR /app
COPY agent_sandbox_relay.py .
COPY relay/ relay/
RUN pip install --no-cache-dir fastapi==0.162.0 uvicorn==0.54.0 pydantic==2.14.2
RUN mkdir -p /data && chown 65534:65534 /data
USER 65534:65534
CMD ["uvicorn", "agent_sandbox_relay:app", "--host", "0.0.0.0", "--port", "8000"]
