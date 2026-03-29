# Variables
IMAGE_NAME = gw-evcharger-controller
IMAGE_TAG = latest
CONTAINER_NAME = gw-evcharger-controller
DOCKER_USER = lerebel103

# Default target
.PHONY: help
help:
	@echo "Available targets:"
	@echo "  build       - Build Docker image"
	@echo "  build-multi - Build multi-architecture Docker images (amd64, arm64)"
	@echo "  push        - Push multi-architecture images to Docker registry"
	@echo "  up/start    - Start the application with docker-compose"
	@echo "  down/stop   - Stop the application with docker-compose"
	@echo "  logs        - View application logs"
	@echo "  test        - Run all tests (unit and property)"
	@echo "  lint        - Run linting checks"
	@echo "  format      - Format code with black and isort"
	@echo "  clean       - Clean up Docker images and containers"

# Build Docker image
.PHONY: build
build:
	@echo "Building Docker image..."
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .
	@echo "Build complete: $(IMAGE_NAME):$(IMAGE_TAG)"

# Build multi-architecture Docker images
.PHONY: build-multi
build-multi:
	@echo "Building multi-architecture Docker images..."
	@echo "Creating buildx builder if not exists..."
	docker buildx create --name multiarch --use --bootstrap 2>/dev/null || docker buildx use multiarch
	@echo "Building for linux/amd64 and linux/arm64..."
	docker buildx build \
		--platform linux/amd64,linux/arm64 \
		--tag $(DOCKER_USER)/$(IMAGE_NAME):$(IMAGE_TAG) \
		--tag $(DOCKER_USER)/$(IMAGE_NAME):$$(date +%Y%m%d) \
		--load \
		.
	@echo "Multi-architecture build complete"

# Push multi-architecture images to Docker registry
.PHONY: push
push:
	@echo "Pushing multi-architecture images to Docker registry..."
	@echo "Registry: $(DOCKER_USER)/$(IMAGE_NAME)"
	docker buildx create --name multiarch --use --bootstrap 2>/dev/null || docker buildx use multiarch
	docker buildx build \
		--platform linux/amd64,linux/arm64 \
		--tag $(DOCKER_USER)/$(IMAGE_NAME):$(IMAGE_TAG) \
		--tag $(DOCKER_USER)/$(IMAGE_NAME):$$(date +%Y%m%d) \
		--push \
		.
	@echo "Images pushed successfully to $(DOCKER_USER)/$(IMAGE_NAME)"

# Start application with docker-compose
.PHONY: up start
up start:
	@echo "Starting GW Charger Controller..."
	@if [ ! -f config.yaml ]; then \
		echo "Error: config.yaml not found. Please copy and customize config.yaml from config.example.yaml"; \
		exit 1; \
	fi
	docker-compose up -d
	@echo "Application started. Use 'make logs' to view output."

# Stop application with docker-compose
.PHONY: down stop
down stop:
	@echo "Stopping GW Charger Controller..."
	docker-compose down
	@echo "Application stopped."

# View application logs
.PHONY: logs
logs:
	@echo "Showing logs for GW Charger Controller (Ctrl+C to exit)..."
	docker-compose logs -f gw-evcharger-controller

# Run all tests (unit and property)
.PHONY: test
test:
	@echo "Running all tests..."
	python -m pytest tests/ -v
	@echo "All tests completed."

# Run linting checks
.PHONY: lint
lint:
	@echo "Running linting checks..."
	@echo "Running ruff check..."
	python -m ruff check app/ tests/
	@echo "Running ruff format --check..."
	python -m ruff format --check app/ tests/
	@echo "All linting checks passed."

# Format code with ruff
.PHONY: format
format:
	@echo "Formatting code..."
	@echo "Running ruff format..."
	python -m ruff format app/ tests/
	@echo "Running ruff check --fix..."
	python -m ruff check --fix app/ tests/
	@echo "Code formatting complete."

# Clean up Docker images and containers
.PHONY: clean
clean:
	@echo "Cleaning up Docker resources..."
	docker-compose down --rmi all --volumes --remove-orphans
	@echo "Cleanup complete."
