FROM python:3.13-slim
RUN pip install --no-cache-dir icmplib==3.0.4
WORKDIR /app
COPY poller.py .
# Raw ICMP sockets: run with cap_add: NET_RAW (or as root).
CMD ["python", "-u", "poller.py"]
