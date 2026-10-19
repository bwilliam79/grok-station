FROM python:3.12-slim
RUN useradd -r -u 10001 relay
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
USER relay
EXPOSE 8790
CMD ["python", "-u", "relay.py"]
