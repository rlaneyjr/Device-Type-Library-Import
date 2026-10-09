FROM python:3.12-slim

ENV REPO_URL=https://github.com/netbox-community/devicetype-library.git
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .

# Install dependencies
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --upgrade pip && \
    pip3 install --no-cache-dir -r requirements.txt

# Copy over src code
COPY *.py ./

CMD ["python3", "-u", "nb-dt-import.py"]
