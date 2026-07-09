FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN groupadd -r lerebel103 && useradd -r -g lerebel103 lerebel103

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

# Copy dependency files first for better Docker layer caching
COPY pyproject.toml uv.lock ./

# Install production dependencies only (no dev group, no local project install)
RUN uv sync --no-dev --frozen --no-install-project

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
CMD [".venv/bin/python", "-m", "app", "--config", "/etc/gw-evcharger-controller/config.yaml"]

# Labels for metadata
LABEL maintainer="lerebel103"
LABEL description="GW Charger Controller - EV charger integration for Home Assistant"
LABEL version="${VERSION}"
