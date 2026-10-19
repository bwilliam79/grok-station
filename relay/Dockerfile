# python:3.13-slim, pinned by digest (multi-arch index). Bump: docker pull python:3.13-slim; docker inspect --format '{{index .RepoDigests 0}}'
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
RUN useradd -r -u 10001 relay && install -d -o relay /state
WORKDIR /app
COPY requirements.txt .
# requirements.txt is pip-compile --generate-hashes output from requirements.in
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY relay.py .
USER relay
EXPOSE 8790
CMD ["python", "-u", "relay.py"]
