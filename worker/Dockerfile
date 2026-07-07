FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends libhdf5-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY usrp_testbed_library /tmp/usrp_testbed_library
RUN pip install --no-cache-dir /tmp/usrp_testbed_library && rm -rf /tmp/usrp_testbed_library

COPY worker/ .
CMD ["python", "worker.py"]
