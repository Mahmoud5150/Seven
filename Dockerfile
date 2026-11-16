FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY seven ./seven
RUN useradd --create-home seven && chown -R seven /app
USER seven
# The same image runs the dashboard (default) and the worker (see docker-compose.yml).
CMD ["uvicorn", "seven.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
