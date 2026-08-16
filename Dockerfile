# Use the official slim Python 3.12 image as the base.
FROM python:3.12-slim

# Set the working directory inside the container.
WORKDIR /app

# Copy requirements first so Docker can cache this layer.
# If requirements.txt hasn't changed, Docker skips pip install on rebuild.
COPY requirements.txt .

# Install dependencies.
# --no-cache-dir keeps the image smaller.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code.
COPY . .

# Expose port 10000 (Render default).
EXPOSE 10000

# Start the FastAPI application with uvicorn.
# --host 0.0.0.0 makes it reachable from outside the container.
# --port 10000 matches Render's default port.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]
