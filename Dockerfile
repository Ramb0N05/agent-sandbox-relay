FROM python:3.13-slim
WORKDIR /app
COPY agent_sandboy_relay.py .
RUN pip install --no-cache-dir fastapi uvicorn
USER 65534:65534
CMD ["uvicorn", "agent_sandboy_relay:app", "--host", "0.0.0.0", "--port", "8000"]
