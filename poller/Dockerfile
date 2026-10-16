FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/Kinet-s-r-o/netbox-dependencies" \
      org.opencontainers.image.description="ICMP poller for the netbox-dependencies NetBox plugin" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir icmplib==3.0.4
WORKDIR /app
COPY poller.py .
# Raw ICMP sockets: run with cap_add: NET_RAW (see examples/docker-compose.poller.yml).
CMD ["python", "-u", "poller.py"]
