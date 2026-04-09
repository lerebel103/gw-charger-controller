FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN groupadd -r lerebel103 && useradd -r -g lerebel103 lerebel103

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY app/ ./app/

# Inject version from build arg (set by Makefile from git tag)
ARG VERSION=dev
RUN echo '"""Version of the EV charger integration."""\n\n__version__ = "'${VERSION}'"\n' > app/version.py

# Create config directory and set permissions
RUN mkdir -p /etc/gw-evcharger-controller && \
    chown -R lerebel103:lerebel103 /app /etc/gw-evcharger-controller

# Switch to non-root user
USER lerebel103

# Set Python path to include app directory
ENV PYTHONPATH=/app

# Default command - run with config from mounted volume
CMD ["python", "-m", "app", "--config", "/etc/gw-evcharger-controller/config.yaml"]

# Expose no ports (this is an MQTT client, not a server)
# Health check could be added later if needed

# Labels for metadata
LABEL maintainer="lerebel103"
LABEL description="GW Charger Controller - EV charger integration for Home Assistant"
LABEL version="${VERSION}"
